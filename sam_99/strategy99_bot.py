#!/usr/bin/env python3
"""
99SMA 戰法 Bot — 獨立實驗策略
═══════════════════════════════════════════════════════════════
策略邏輯（Sam 2140 直播整理）：
  1. 15分鐘圖表，掛兩條指標：
     - 黃線：15分鐘 99SMA（進場判斷）
     - 紅線：5分鐘 99SMA（止損判斷）

  2. 進場（多單，二次突破）：
     - 第一次 15m K棒收盤 > 15m 99SMA → 標記，不進
     - 回落後第二次 15m K棒收盤 > 15m 99SMA
     - 且 第二次突破那根量 > 橫盤期（99SMA 下方）均量
     → 收盤價掛單進場

  3. A、B、C 點：
     - A = 第一次突破後的回落低點
     - B = 第二次突破收盤（進場點）
     - C = 進場後最低低點（動態更新，直到方向確立）

  4. 止損：
     - 現價觸及 5分鐘 99SMA → 立即市價出場

  5. 止盈（斐波那契擴展 A→B 錨定 C）：
     - TP  = C + 1.0 × (B - A)（等幅），全數出場（Sam 原版：賺一個等幅就跑）

  6. 槓桿：依風險動態計算，上限 5x
     - 每筆風險 = 本金 2%（最多 20 USDT）
     - lev = min(5, risk / (entry - sl_price) * entry / margin)

  7. 資金池：完全獨立，初始 1000 USDT，不與其他策略共用

═══════════════════════════════════════════════════════════════
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone, timedelta
TZ8 = timezone(timedelta(hours=8))
from pathlib import Path
from typing import Optional

import threading

import ccxt
import numpy as np
import pandas as pd
import requests

try:
    from chart_screenshot import capture_entry_chart as _capture_chart
except ImportError:
    def _capture_chart(*a, **kw): pass

# ── 載入 .env ──────────────────────────────────────────────────
_env = Path(__file__).parent / ".env"
if _env.exists():
    for _l in _env.read_text().splitlines():
        _l = _l.strip()
        if _l and not _l.startswith("#") and "=" in _l:
            _k, _v = _l.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

# ═══════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════
PAPER_MODE = True   # ← 改 False 才送出真實訂單

BINANCE_KEY    = os.getenv("BINANCE_API_KEY",    "")
BINANCE_SECRET = os.getenv("BINANCE_API_SECRET", "")
TG_TOKEN       = os.getenv("TELEGRAM_BOT_TOKEN", "8779609140:AAHGfIR0hOL_I12NATRuiKlftuTuUvqzeYk")
TG_CHAT_ID     = os.getenv("TELEGRAM_CHAT_ID",   "1768177615")

# ── 資金（獨立，不共用）────────────────────────────────────────
INITIAL_CAPITAL = 1_000.0   # USDT
RISK_PCT        = 0.02      # 每筆最多虧 2%（20 USDT）
MAX_LEVERAGE    = 5
MAX_OPEN_POS    = 5
MARGIN_PCT      = 0.15      # 每倉最多用 15% 本金

# ── 策略參數 ───────────────────────────────────────────────────
SMA_N          = 99
CONSOL_BARS    = 30    # 橫盤期抓最近幾根在 99SMA 下方的K棒算均量
MIN_VOL_RATIO  = 1.2   # 突破量 / 橫盤均量 至少 1.2x
MIN_ADX        = 20    # ADX 低於此值 = 橫盤，不進場
MIN_CONF       = 65    # 最低信心分（低於此值跳過）

# ── BTC 短線煞車 ───────────────────────────────────────────────
# BTC 1H 收盤 < 1H 99SMA → 視為短線偏空，多單槓桿減半
BTC_BRAKE_ENABLED  = True
BTC_BRAKE_LEV_MULT = 0.5   # 煞車時多單槓桿乘以此倍數
_btc_brake_active: bool = False   # 由主掃描迴圈在掃到 BTC 時更新，不另打 API
FIRST_BREAK_EXPIRY = 16 # 第一次突破後最多 16 根（4小時）內等第二次，否則重置
FIRST_BREAK_EXPIRY_SEC = FIRST_BREAK_EXPIRY * 15 * 60  # 換算成秒（15m × 16根 = 14400秒）
C_LOCK_BARS    = 3     # 進場後連續 N 根收盤上漲就鎖定 C 點

# ── 掃描設定 ───────────────────────────────────────────────────
SCAN_INTERVAL    = 60    # 秒（15m K棒約每 900s 收，60s 輪詢確認收盤）
MONITOR_INTERVAL = 5     # 持倉監控執行緒：每 5 秒查一次 SL/TP
TOP_N            = 100
STATE_FILE       = Path(__file__).parent / "strategy99_state.json"

# ── 執行緒共用鎖（保護 state dict 避免 race condition）──────────
_state_lock = threading.Lock()

# ═══════════════════════════════════════════════════════════════
# LOGGING
# ═══════════════════════════════════════════════════════════════
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [99bot] %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(Path(__file__).parent / "strategy99_bot.log",
                            encoding="utf-8"),
    ],
)
log = logging.getLogger("strategy99")


# ═══════════════════════════════════════════════════════════════
# TELEGRAM
# ═══════════════════════════════════════════════════════════════
_tg_offset = 0   # 追蹤已讀的 update_id

def tg(msg: str) -> None:
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT_ID, "text": msg, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception as e:
        log.warning(f"TG 失敗: {e}")


def tg_get_updates() -> list:
    global _tg_offset
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates",
            params={"offset": _tg_offset, "timeout": 1},
            timeout=5,
        )
        updates = r.json().get("result", [])
        if updates:
            _tg_offset = updates[-1]["update_id"] + 1
        return updates
    except Exception:
        return []


def handle_tg_commands(pub, state: dict) -> None:
    """處理 TG 指令（主循環調用，保留向後相容）"""
    _process_tg_updates(pub, state)


def _process_tg_updates(pub, state: dict) -> None:
    for upd in tg_get_updates():
        msg = upd.get("message", {})
        text = msg.get("text", "").strip()
        # 處理群組 bot 指令（/cmd@botname 格式）
        if "@" in text:
            text = text.split("@")[0]
        if text in ["/狀態", "/status", "/s"]:
            _send_status(pub, state)
        elif text in ["/99"]:
            _send_99_status(pub, state)
        elif text in ["/test", "/測試"]:
            _send_test_signals()


def start_tg_listener(pub, state: dict) -> None:
    """背景 thread：每 3 秒輪詢 TG 指令，即時回應"""
    def _loop():
        while True:
            try:
                _process_tg_updates(pub, state)
            except Exception as e:
                log.warning(f"TG listener 異常: {e}")
            time.sleep(3)
    t = threading.Thread(target=_loop, daemon=True)
    t.start()
    log.info("TG 指令監聽已啟動（每 3 秒輪詢）")


# ═══════════════════════════════════════════════════════════════
# STATE
# ═══════════════════════════════════════════════════════════════
DEFAULT_STATE: dict = {
    "capital"   : INITIAL_CAPITAL,
    "positions" : {},   # symbol → position dict
    "signals"   : {},   # symbol → signal tracking dict
    "total_trades": 0,
    "wins"      : 0,
    "losses"    : 0,
    "peak_equity": INITIAL_CAPITAL,
    "max_drawdown": 0.0,
    "started_at": datetime.now(TZ8).isoformat(),
    "trade_history": [],  # 最近 50 筆交易紀錄
}


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return dict(DEFAULT_STATE)


def save_state(s: dict) -> None:
    STATE_FILE.write_text(
        json.dumps(s, indent=2, default=str, ensure_ascii=False),
        encoding="utf-8",
    )


# ═══════════════════════════════════════════════════════════════
# EXCHANGE
# 公開端點（掃描/K線/價格）不帶 API Key，不消耗配額
# 認證端點（下單/槓桿）只在 PAPER_MODE=False 時才用
# ═══════════════════════════════════════════════════════════════
def get_public_exchange() -> ccxt.binanceusdm:
    """公開端點，無需 API Key，用於所有資料抓取"""
    return ccxt.binanceusdm({"enableRateLimit": True})


def get_auth_exchange() -> ccxt.binanceusdm:
    """認證端點，僅用於真實下單"""
    return ccxt.binanceusdm({
        "apiKey"         : BINANCE_KEY,
        "secret"         : BINANCE_SECRET,
        "enableRateLimit": True,
    })


def fetch_ohlcv(exch, symbol: str, tf: str, limit: int = 300) -> pd.DataFrame:
    try:
        raw = exch.fetch_ohlcv(symbol, tf, limit=limit)
        df = pd.DataFrame(raw, columns=["ts", "open", "high", "low", "close", "volume"])
        df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
        return df.set_index("ts").astype(float)
    except Exception as e:
        log.warning(f"fetch_ohlcv {symbol} {tf}: {e}")
        return pd.DataFrame()


_STABLE_KEYWORDS = ["USDC", "USDD", "TUSD", "BUSD", "DAI", "FDUSD",
                    "USDP", "GUSD", "FRAX", "LUSD", "SUSD", "EURC",
                    "EUR", "GBP", "PAXG", "XAUT"]  # 穩定幣 / 法幣錨定

# ── 市值排名快取（每 6 小時更新一次）────────────────────────────
_mcap_cache: list[str] = []   # 市值前 N 幣的 base symbol，例如 ["BTC","ETH",...]
_mcap_cache_ts: float  = 0.0
MCAP_CACHE_TTL = 6 * 3600    # 6 小時

def _fetch_coingecko_top(n: int = 300) -> list[str]:
    """從 CoinGecko 抓市值前 n 名的 symbol（大寫）；每頁最多 250，自動分頁"""
    try:
        symbols: list[str] = []
        per_page = 250
        for page in range(1, 3):   # 最多抓 2 頁（500 筆），夠用
            url = (f"https://api.coingecko.com/api/v3/coins/markets"
                   f"?vs_currency=usd&order=market_cap_desc"
                   f"&per_page={per_page}&page={page}&sparkline=false")
            resp = requests.get(url, timeout=15)
            resp.raise_for_status()
            batch = [c["symbol"].upper() for c in resp.json()]
            symbols.extend(batch)
            if len(symbols) >= n:
                break
        symbols = symbols[:n]
        log.info(f"CoinGecko 市值前 {n} 名已更新，共 {len(symbols)} 筆")
        return symbols
    except Exception as e:
        log.warning(f"CoinGecko 取市值排名失敗: {e}")
        return []

MCAP_GATE = 1000  # 市值門檻：CoinGecko 前 1000 名（回測後共63支合約幣）

# ── 回測勝率濾網（backtest_99ma_db.json）────────────────────────
# 有足夠回測數據（≥ MIN_BT_SIGNALS 筆）且勝率 < BT_WIN_RATE_FLOOR → 跳過
BT_DB_PATH        = Path(__file__).parent / "backtest_99ma_db.json"
BT_WIN_RATE_FLOOR = 35.0   # 累積勝率低於此值 → 跳過（假突破太多）
BT_MIN_SIGNALS    = 15     # 至少這麼多歷史訊號才套用過濾（樣本太少不過濾）
_bt_db: dict      = {}
_bt_db_ts: float  = 0.0
BT_DB_TTL         = 3600   # 每小時重新載入一次（配合每週更新）

def _load_bt_db() -> dict:
    global _bt_db, _bt_db_ts
    if time.time() - _bt_db_ts < BT_DB_TTL and _bt_db:
        return _bt_db
    try:
        if BT_DB_PATH.exists():
            with open(BT_DB_PATH) as f:
                _bt_db = json.load(f)
            _bt_db_ts = time.time()
            log.info(f"回測DB載入：{len(_bt_db)} 幣")
    except Exception as e:
        log.warning(f"回測DB載入失敗: {e}")
    return _bt_db

def bt_win_rate_ok(symbol: str, side: str) -> tuple[bool, str]:
    """
    回傳 (True, "")           → 通過，全倉
    回傳 (True, "REDUCE_SIZE") → 通過，但縮倉40%（樣本不足）
    回傳 (False, 原因)         → 跳過
    symbol = "BTCUSDT" 或 "BTC"
    side   = "long" / "short"
    """
    db  = _load_bt_db()
    sym = symbol.replace("/USDT:USDT","").replace("/USDT","").replace("USDT","").upper()
    d   = db.get(sym)
    if not d or d.get("signals", 0) < BT_MIN_SIGNALS:
        return True, "REDUCE_SIZE"   # 樣本不足 → 縮倉40%，不是全倉放行

    # 多空分開看
    if side == "long":
        total = d.get("long_sigs", 0)
        wins  = d.get("long_wins", 0)
    else:
        total = d.get("short_sigs", 0)
        wins  = d.get("short_wins", 0)

    if total < 8:
        return True, "REDUCE_SIZE"   # 該方向樣本不足 → 縮倉40%

    rate = wins / total * 100
    if rate < BT_WIN_RATE_FLOOR:
        return False, f"回測{side}勝率{rate:.0f}%<{BT_WIN_RATE_FLOOR}%（{wins}/{total}），假突破多跳過"
    return True, ""

def get_top_symbols(exch, n: int = TOP_N) -> list[str]:
    global _mcap_cache, _mcap_cache_ts

    # ── 更新市值快取 ──────────────────────────────────────────
    if time.time() - _mcap_cache_ts > MCAP_CACHE_TTL or not _mcap_cache:
        fresh = _fetch_coingecko_top(MCAP_GATE)
        if fresh:
            _mcap_cache    = fresh
            _mcap_cache_ts = time.time()

    try:
        markets = exch.load_markets()
        # Step 1：Binance 所有 USDT 永續合約（排除穩定幣）
        all_perps = {s for s, m in markets.items()
                     if m.get("type") == "swap" and m.get("quote") == "USDT"
                     and m.get("active") and "/USDT:USDT" in s
                     and not any(k in s.upper() for k in _STABLE_KEYWORDS)}

        # Step 2：市值門檻過濾（只留 CoinGecko 前 MCAP_GATE 名）
        if _mcap_cache:
            mcap_set = set(_mcap_cache)   # base symbols，e.g. {"BTC","ETH",...}
            mcap_filtered = [s for s in all_perps
                             if s.replace("/USDT:USDT", "") in mcap_set]
        else:
            # CoinGecko 失敗時跳過市值過濾，直接用全部
            log.warning("市值排名不可用，跳過市值門檻過濾")
            mcap_filtered = list(all_perps)

        # Step 3：抓 24h 交易量，按量排序取前 n
        tickers = exch.fetch_tickers(mcap_filtered[:500])
        ranked = sorted(
            [(s, t.get("quoteVolume", 0) or 0) for s, t in tickers.items()],
            key=lambda x: -x[1]
        )
        result = [s for s, _ in ranked[:n]]
        log.info(f"掃描清單：市值前{MCAP_GATE}名中，交易量前 {len(result)} 名")
        return result

    except Exception as e:
        log.warning(f"get_top_symbols: {e}")
        return []


def get_current_price(exch, symbol: str) -> Optional[float]:
    try:
        t = exch.fetch_ticker(symbol)
        return float(t["last"])
    except Exception:
        return None


def is_btc_brake() -> bool:
    """BTC 1H < 1H SMA99 → 多單煞車。狀態由主掃描迴圈更新，零額外 API 呼叫。"""
    return BTC_BRAKE_ENABLED and _btc_brake_active


def update_btc_brake(exch) -> None:
    """掃描到 BTC 時呼叫，拉 1H K棒判斷 1H SMA99。"""
    global _btc_brake_active
    if not BTC_BRAKE_ENABLED:
        return
    try:
        df1h = fetch_ohlcv(exch, "BTC/USDT:USDT", "1h", limit=110)
        if df1h.empty or len(df1h) < 99:
            return
        closes = df1h["close"].values
        sma99  = float(np.mean(closes[-99:]))
        brake  = float(closes[-1]) < sma99
        if brake != _btc_brake_active:
            _btc_brake_active = brake
            status = "啟動 🔴" if brake else "解除 🟢"
            log.info(f"[BTC煞車] {status}  BTC 1H close={closes[-1]:.1f}  SMA99={sma99:.1f}")
    except Exception as e:
        log.warning(f"update_btc_brake: {e}")


def set_leverage(auth_exch, symbol: str, lev: int) -> None:
    if PAPER_MODE:
        return
    try:
        auth_exch.set_leverage(lev, symbol)
    except Exception as e:
        log.warning(f"set_leverage {symbol} {lev}x: {e}")


# ═══════════════════════════════════════════════════════════════
# INDICATORS
# ═══════════════════════════════════════════════════════════════
def calc_sma(df: pd.DataFrame, n: int = SMA_N) -> pd.DataFrame:
    df = df.copy()
    df["sma"] = df["close"].rolling(n).mean()
    # ADX（14）
    high, low, close = df["high"], df["low"], df["close"]
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low  - close.shift()).abs(),
    ], axis=1).max(axis=1)
    atr14 = tr.ewm(span=14, adjust=False).mean()
    up   = high - high.shift()
    down = low.shift() - low
    pdm  = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=df.index)
    ndm  = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=df.index)
    pdi  = 100 * pdm.ewm(span=14, adjust=False).mean() / atr14.replace(0, np.nan)
    ndi  = 100 * ndm.ewm(span=14, adjust=False).mean() / atr14.replace(0, np.nan)
    dx   = (100 * (pdi - ndi).abs() / (pdi + ndi).replace(0, np.nan)).fillna(0)
    df["adx"] = dx.ewm(span=14, adjust=False).mean()
    return df


def get_5m_sma99_price(exch, symbol: str) -> Optional[float]:
    """取得 5分鐘 99SMA 當前值（止損用）"""
    df5 = fetch_ohlcv(exch, symbol, "5m", limit=110)
    if df5.empty or len(df5) < SMA_N:
        return None
    return float(df5["close"].rolling(SMA_N).mean().iloc[-1])


# ═══════════════════════════════════════════════════════════════
# SIGNAL DETECTION — 二次突破 + 量能確認
# ═══════════════════════════════════════════════════════════════
def detect_signal(exch, symbol: str, sig_state: dict) -> Optional[dict]:
    """
    回傳 None = 無訊號
    回傳 dict = 進場訊號，包含 entry, a_point, sl_price, lev 等
    """
    df = fetch_ohlcv(exch, symbol, "15m", limit=200)
    if df.empty or len(df) < SMA_N + 10:
        return None

    df = calc_sma(df)
    df = df.dropna()
    if len(df) < 5:
        return None

    # ── 個幣 1H SMA99 方向過濾 ──────────────────────────────────
    df1h = fetch_ohlcv(exch, symbol, "1h", limit=110)
    if df1h.empty or len(df1h) < SMA_N:
        log.debug(f"{symbol} 1H K棒不足，跳過")
        return None
    sma99_1h = float(df1h["close"].rolling(SMA_N).mean().iloc[-1])
    close_1h = float(df1h["close"].iloc[-1])
    coin_1h_bull = close_1h > sma99_1h   # 個幣 1H 偏多
    coin_1h_bear = close_1h < sma99_1h   # 個幣 1H 偏空

    # ── 15m RSI（14）過濾：避免追高殺低 ─────────────────────────
    delta  = df["close"].diff()
    gain   = delta.clip(lower=0).rolling(14).mean()
    loss   = (-delta.clip(upper=0)).rolling(14).mean()
    rs     = gain / loss.replace(0, float("nan"))
    rsi_15m = float(100 - (100 / (1 + rs)).iloc[-2])  # 用倒數第二根（已收盤）


    now_bar  = df.iloc[-1]   # 最新已收盤K棒（-1 是最新，但要用倒數第二確認收盤）
    prev_bar = df.iloc[-2]   # 確認收盤用最新完整K棒

    # ── ADX 過濾：橫盤市場不進場 ──────────────────────────────
    adx_val = float(df["adx"].iloc[-2]) if "adx" in df.columns else 0.0
    if adx_val < MIN_ADX:
        log.debug(f"{symbol} ADX={adx_val:.1f} < {MIN_ADX}，橫盤跳過")
        return None

    # ── SMA99 斜率方向（用最近 5 根 SMA 值判斷斜率）──────────────
    sma_vals = df["sma"].dropna().values
    if len(sma_vals) >= 5:
        sma_slope = float(sma_vals[-2] - sma_vals[-6])  # 最近 5 根的變化量
    else:
        sma_slope = 0.0
    sma_up   = sma_slope > 0    # SMA99 斜率向上 → 偏多
    sma_down = sma_slope < 0    # SMA99 斜率向下 → 偏空

    # ── 取橫盤均量（多單用SMA下方；空單用SMA上方）──────────────
    def _consol_vol(above: bool) -> float:
        """above=True → 取SMA上方K棒均量（空單基準）；False → 下方（多單基準）"""
        vols = []
        for i in range(2, min(CONSOL_BARS + 2, len(df))):
            bar = df.iloc[-i]
            cond = bar["close"] > bar["sma"] if above else bar["close"] < bar["sma"]
            if cond:
                vols.append(bar["volume"])
            else:
                break
        if len(vols) < 3:
            for i in range(2, len(df)):
                bar = df.iloc[-i]
                cond = bar["close"] > bar["sma"] if above else bar["close"] < bar["sma"]
                if cond:
                    vols.append(bar["volume"])
                if len(vols) >= CONSOL_BARS:
                    break
        return float(np.mean(vols)) if vols else 0.0

    avg_consol_vol   = _consol_vol(above=False)  # 多單：SMA 下方均量
    avg_consol_vol_s = _consol_vol(above=True)   # 空單：SMA 上方均量

    # ── 狀態機 ────────────────────────────────────────────────
    stage   = sig_state.get("stage",   "watching")    # 多單狀態
    stage_s = sig_state.get("stage_s", "watching_s")  # 空單狀態
    confirmed = prev_bar  # 確認K棒（已收盤完整的那根）

    # ══════════════════════════════════════════════════════════
    # ── 多單狀態機（跌破→回測→再突破做多）──────────────────────
    # ══════════════════════════════════════════════════════════

    now_ts = datetime.now(timezone.utc).timestamp()

    # -- 多: 等待第一次突破（需 SMA99 斜率向上）--
    if stage == "watching":
        if (confirmed["close"] > confirmed["sma"] and
                df.iloc[-3]["close"] <= df.iloc[-3]["sma"] and
                sma_up):
            sig_state["stage"]             = "first_broke"
            sig_state["first_break_ts"]    = now_ts          # ✅ 用時間戳，不用 bar index
            sig_state["first_break_price"] = float(confirmed["close"])
            sig_state["a_point"]           = None
            log.info(f"{symbol} [多] 第一次突破 99SMA @ {confirmed['close']:.4f}")

    elif stage == "first_broke":
        elapsed = now_ts - sig_state.get("first_break_ts", 0)
        if elapsed > FIRST_BREAK_EXPIRY_SEC:
            sig_state["stage"] = "watching"
            log.info(f"{symbol} [多] 第一次突破逾期（{elapsed/60:.0f}分），重置")
        elif confirmed["close"] < confirmed["sma"]:
            if sig_state.get("a_point") is None:
                sig_state["a_point"] = float(confirmed["low"])
            else:
                sig_state["a_point"] = min(sig_state["a_point"], float(confirmed["low"]))
            sig_state["stage"] = "pulled_back"

    elif stage == "pulled_back":
        elapsed = now_ts - sig_state.get("first_break_ts", 0)
        if elapsed > FIRST_BREAK_EXPIRY_SEC:
            sig_state["stage"] = "watching"
            log.info(f"{symbol} [多] 等待逾期（{elapsed/60:.0f}分），重置")
        else:
            if confirmed["close"] < confirmed["sma"]:
                a = sig_state.get("a_point")
                sig_state["a_point"] = min(a, float(confirmed["low"])) if a else float(confirmed["low"])

            second_break = (confirmed["close"] > confirmed["sma"] and
                            df.iloc[-3]["close"] <= df.iloc[-3]["sma"] and
                            sma_up)   # 第二次突破同樣要求斜率向上
            vol_ok = (avg_consol_vol > 0 and
                      confirmed["volume"] >= avg_consol_vol * MIN_VOL_RATIO)

            if second_break and vol_ok and sig_state.get("a_point"):
                # ── 新增過濾 1：個幣 1H 需在 SMA99 之上（多頭結構）──
                if not coin_1h_bull:
                    log.info(f"{symbol} [多] 個幣 1H close={close_1h:.5g} < SMA99={sma99_1h:.5g}，非多頭結構，跳過")
                    sig_state["stage"] = "watching"
                    return None
                # ── 新增過濾 2：15m RSI 不超買（< 70）──
                if rsi_15m >= 70:
                    log.info(f"{symbol} [多] 15m RSI={rsi_15m:.1f} 超買，不追高，跳過")
                    sig_state["stage"] = "watching"
                    return None
                # ── 新增過濾 3：回測勝率濾網 ──────────────────────
                bt_ok, bt_reason = bt_win_rate_ok(symbol, "long")
                if not bt_ok:
                    log.info(f"{symbol} [多] {bt_reason}")
                    sig_state["stage"] = "watching"
                    return None
                if bt_reason == "REDUCE_SIZE":
                    log.info(f"{symbol} [多] 回測樣本不足，縮倉40%進場")
                entry   = float(confirmed["close"])
                a_point = sig_state["a_point"]
                sl_price = get_5m_sma99_price(exch, symbol)
                if sl_price is None or sl_price >= entry:
                    log.info(f"{symbol} [多] 無法取得 5m SMA99 或 SL >= entry，跳過")
                    sig_state["stage"] = "watching"
                elif (entry - sl_price) / entry < 0.008:
                    log.info(f"{symbol} [多] 5m SMA99 距進場價太近（{(entry-sl_price)/entry*100:.2f}%<0.8%），SL無緩衝，跳過")
                    sig_state["stage"] = "watching"
                else:
                    sl_dist  = entry - sl_price
                    risk_usd = INITIAL_CAPITAL * RISK_PCT
                    lev      = min(MAX_LEVERAGE, max(1, int(risk_usd / sl_dist * entry / (INITIAL_CAPITAL * MARGIN_PCT))))
                    log.info(f"{symbol} ✅ [多] 二次突破 entry={entry:.4f} A={a_point:.4f} SL={sl_price:.4f} lev={lev}x  1H={'多' if coin_1h_bull else '空'} RSI={rsi_15m:.1f}")
                    sig_state["stage"] = "watching"
                    return {
                        "symbol"   : symbol,
                        "side"     : "long",
                        "entry"    : entry,
                        "a_point"  : a_point,
                        "b_point"  : entry,
                        "sl_price" : sl_price,
                        "leverage" : lev,
                        "vol_ratio": round(confirmed["volume"] / avg_consol_vol, 2),
                        "adx"      : round(adx_val, 1),
                    }
            elif second_break and not vol_ok:
                log.info(f"{symbol} [多] 第二次突破但量能不足（{confirmed['volume']/avg_consol_vol:.2f}x），忽略")
                sig_state["stage"] = "watching"

    # ══════════════════════════════════════════════════════════
    # ── 空單狀態機（突破→回測→再跌破做空）──────────────────────
    # ══════════════════════════════════════════════════════════

    # -- 空: 等待第一次跌破（需 SMA99 斜率向下）--
    if stage_s == "watching_s":
        if (confirmed["close"] < confirmed["sma"] and
                df.iloc[-3]["close"] >= df.iloc[-3]["sma"] and
                sma_down):
            sig_state["stage_s"]             = "first_broke_s"
            sig_state["first_break_ts_s"]    = now_ts          # ✅ 用時間戳，不用 bar index
            sig_state["first_break_price_s"] = float(confirmed["close"])
            sig_state["a_point_s"]           = None  # 尚未反彈（A點=最高點）
            log.info(f"{symbol} [空] 第一次跌破 99SMA @ {confirmed['close']:.4f}")

    elif stage_s == "first_broke_s":
        elapsed_s = now_ts - sig_state.get("first_break_ts_s", 0)
        if elapsed_s > FIRST_BREAK_EXPIRY_SEC:
            sig_state["stage_s"] = "watching_s"
            log.info(f"{symbol} [空] 第一次跌破逾期（{elapsed_s/60:.0f}分），重置")
        elif confirmed["close"] > confirmed["sma"]:
            # 反彈回到 SMA 上方，記錄 A 點（最高點）
            if sig_state.get("a_point_s") is None:
                sig_state["a_point_s"] = float(confirmed["high"])
            else:
                sig_state["a_point_s"] = max(sig_state["a_point_s"], float(confirmed["high"]))
            sig_state["stage_s"] = "pulled_back_s"

    elif stage_s == "pulled_back_s":
        elapsed_s = now_ts - sig_state.get("first_break_ts_s", 0)
        if elapsed_s > FIRST_BREAK_EXPIRY_SEC:
            sig_state["stage_s"] = "watching_s"
            log.info(f"{symbol} [空] 等待逾期（{elapsed_s/60:.0f}分），重置")
        else:
            if confirmed["close"] > confirmed["sma"]:
                a = sig_state.get("a_point_s")
                sig_state["a_point_s"] = max(a, float(confirmed["high"])) if a else float(confirmed["high"])

            # 第二次跌破條件
            second_break_s = (confirmed["close"] < confirmed["sma"] and
                              df.iloc[-3]["close"] >= df.iloc[-3]["sma"] and
                              sma_down)   # 第二次跌破同樣要求斜率向下
            vol_ok_s = (avg_consol_vol_s > 0 and
                        confirmed["volume"] >= avg_consol_vol_s * MIN_VOL_RATIO)  # 空單用SMA上方均量

            if second_break_s and vol_ok_s and sig_state.get("a_point_s"):
                # ── 新增過濾 1：個幣 1H 需在 SMA99 之下（空頭結構）──
                if not coin_1h_bear:
                    log.info(f"{symbol} [空] 個幣 1H close={close_1h:.5g} > SMA99={sma99_1h:.5g}，非空頭結構，跳過")
                    sig_state["stage_s"] = "watching_s"
                    return None
                # ── 新增過濾 2：15m RSI 不超賣（> 30）──
                if rsi_15m <= 30:
                    log.info(f"{symbol} [空] 15m RSI={rsi_15m:.1f} 超賣，不追殺，跳過")
                    sig_state["stage_s"] = "watching_s"
                    return None
                # ── 新增過濾 3：回測勝率濾網 ──────────────────────
                bt_ok, bt_reason = bt_win_rate_ok(symbol, "short")
                if not bt_ok:
                    log.info(f"{symbol} [空] {bt_reason}")
                    sig_state["stage_s"] = "watching_s"
                    return None
                if bt_reason == "REDUCE_SIZE":
                    log.info(f"{symbol} [空] 回測樣本不足，縮倉40%進場")
                entry   = float(confirmed["close"])
                a_point = sig_state["a_point_s"]
                sl_price = get_5m_sma99_price(exch, symbol)
                if sl_price is None or sl_price <= entry:
                    log.info(f"{symbol} [空] 無法取得 5m SMA99 或 SL <= entry，跳過")
                    sig_state["stage_s"] = "watching_s"
                elif (sl_price - entry) / entry < 0.008:
                    log.info(f"{symbol} [空] 5m SMA99 距進場價太近（{(sl_price-entry)/entry*100:.2f}%<0.8%），SL無緩衝，跳過")
                    sig_state["stage_s"] = "watching_s"
                else:
                    sl_dist  = sl_price - entry
                    risk_usd = INITIAL_CAPITAL * RISK_PCT
                    lev      = min(MAX_LEVERAGE, max(1, int(risk_usd / sl_dist * entry / (INITIAL_CAPITAL * MARGIN_PCT))))
                    log.info(f"{symbol} ✅ [空] 二次跌破 entry={entry:.4f} A={a_point:.4f} SL={sl_price:.4f} lev={lev}x  1H={'空' if coin_1h_bear else '多'} RSI={rsi_15m:.1f}")
                    sig_state["stage_s"] = "watching_s"
                    return {
                        "symbol"   : symbol,
                        "side"     : "short",
                        "entry"    : entry,
                        "a_point"  : a_point,
                        "b_point"  : entry,
                        "sl_price" : sl_price,
                        "leverage" : lev,
                        "vol_ratio": round(confirmed["volume"] / avg_consol_vol_s, 2),
                        "adx"      : round(adx_val, 1),
                    }
            elif second_break_s and not vol_ok_s:
                log.info(f"{symbol} [空] 第二次跌破但量能不足（{confirmed['volume']/avg_consol_vol_s:.2f}x），忽略")
                sig_state["stage_s"] = "watching_s"

    return None


# ═══════════════════════════════════════════════════════════════
# POSITION MANAGEMENT
# ═══════════════════════════════════════════════════════════════
def _confidence_bar(score: int) -> str:
    filled = round(score / 10)
    return "█" * filled + "░" * (10 - filled)


def _calc_confidence(signal: dict) -> int:
    """0~100 信心分數"""
    score = 50
    # 量能（最重要，+0~25）
    vr = signal.get("vol_ratio", 1.0)
    score += min(25, int((vr - MIN_VOL_RATIO) * 20))
    # 結構大小（A→B 距離 / entry，大 = 更明確）
    ab_pct = (signal["b_point"] - signal["a_point"]) / signal["entry"] * 100
    if ab_pct > 3:
        score += 15
    elif ab_pct > 1.5:
        score += 8
    # 突破乾淨（sl距離適中）
    sl_pct = (signal["entry"] - signal["sl_price"]) / signal["entry"] * 100
    if 1.0 <= sl_pct <= 3.5:
        score += 10
    return min(100, max(0, score))


def open_position(pub, auth, state: dict, signal: dict) -> None:
    capital   = state["capital"]
    positions = state["positions"]

    if len(positions) >= MAX_OPEN_POS:
        log.info(f"已達最大持倉 {MAX_OPEN_POS}，跳過 {signal['symbol']}")
        return

    # ── 信心分過濾 ─────────────────────────────────────────────
    conf_check = _calc_confidence(signal)
    if conf_check < MIN_CONF:
        log.info(f"{signal['symbol']} 信心分 {conf_check} < {MIN_CONF}，跳過")
        return

    sym    = signal["symbol"]
    entry  = signal["entry"]
    lev    = signal["leverage"]
    side   = signal.get("side", "long")

    # ── BTC 煞車：BTC 1H < SMA99，多單槓桿減半 ──────────────────
    if side == "long" and is_btc_brake():
        orig_lev = lev
        lev = max(1, int(lev * BTC_BRAKE_LEV_MULT))
        log.info(f"[BTC煞車] {sym} BTC 1H < SMA99，多單槓桿 {orig_lev}x → {lev}x")

    # ── 回測DB未知幣：倉位縮小（繼續累積數據但限制風險）────────────
    # DB無資料 或 訊號數 < BT_MIN_SIGNALS → 視為未驗證幣，倉位縮至 40%
    BT_UNKNOWN_MARGIN_MULT = 0.4   # 未知幣倉位乘數
    bt_ok, _ = bt_win_rate_ok(sym, side)
    sym_base = sym.replace("/USDT:USDT","").replace("/USDT","").replace("USDT","").upper()
    db_entry = _load_bt_db().get(sym_base)
    is_unknown = (not db_entry) or (db_entry.get("signals", 0) < BT_MIN_SIGNALS)
    margin_mult = BT_UNKNOWN_MARGIN_MULT if is_unknown else 1.0
    if is_unknown:
        log.info(f"{sym} 回測數據不足，倉位縮至 {int(BT_UNKNOWN_MARGIN_MULT*100)}%")

    margin = capital * MARGIN_PCT * margin_mult
    size   = (margin * lev) / entry

    a    = signal["a_point"]
    b    = signal["b_point"]
    sl   = signal["sl_price"]
    ab   = abs(b - a)  # 用絕對值，多空都適用

    # 暫用 C = entry 先估止盈（C 確立後會重算）
    c = entry
    if side == "long":
        tp1 = round(c + 1.0 * ab, 6)   # 等幅全出（Sam 原版）
    else:
        tp1 = round(c - 1.0 * ab, 6)   # 等幅全出（Sam 原版）

    pos = {
        "symbol"    : sym,
        "entry"     : entry,
        "size"      : size,
        "margin"    : margin,
        "orig_margin": margin,
        "leverage"  : lev,
        "sl_price"  : sl,
        "a_point"   : a,
        "b_point"   : b,
        "c_point"   : c,
        "c_locked"  : False,
        "c_up_bars" : 0,       # 多單：連續未創新低計數；空單：連續未創新高計數
        "c_up_since": None,    # 開始計數的時間戳（用秒，不用次數）
        "tp1_price" : tp1,
        "tp1_hit"   : False,
        "sl_moved"  : False,
        "opened_at" : datetime.now(TZ8).isoformat(),
        "side"      : side,
        # ── 複盤用 ────────────────────────────────────
        "entry_vol_ratio" : signal.get("vol_ratio", 0),
        "entry_adx"       : signal.get("adx", 0),
        "entry_conf"      : _calc_confidence(signal),
        "btc_brake_on"    : is_btc_brake(),
    }

    positions[sym] = pos
    save_state(state)

    if not PAPER_MODE:
        set_leverage(auth, sym, lev)

    # ── TG 通知（Sam 格式）────────────────────────────────
    conf     = _calc_confidence(signal)
    bar      = _confidence_bar(conf)
    if side == "long":
        sl_pct  = (entry - sl) / entry * 100
        risk_usd = size * (entry - sl)
        dir_emoji = "🟢"
        dir_text  = "做多"
    else:
        sl_pct  = (sl - entry) / entry * 100
        risk_usd = size * (sl - entry)
        dir_emoji = "🔴"
        dir_text  = "做空"
    tp1_pct  = abs(tp1 - entry) / entry * 100
    coin     = sym.replace("/USDT:USDT", "").lower()

    adx_val = signal.get("adx", 0.0)
    msg = (f"{dir_emoji} <b>{coin}{dir_text}  {lev}x槓桿</b>\n"
           f"  信心 [{bar}] {conf}/100  ADX={adx_val:.1f}\n"
           f"  進場：{entry:.5g}\n"
           f"  止損：{sl:.5g}（{sl_pct:.1f}%）\n"
           f"  TP（等幅全出）：{tp1:.5g}（+{tp1_pct:.1f}%）\n"
           f"  倉位：{margin*lev:.1f}U  風險：{risk_usd:.2f}U"
           + ("\n  ⚠️ 回測數據不足，縮倉40%" if is_unknown else "")
           + ("\n  📋 Paper Mode" if PAPER_MODE else ""))
    tg(msg)
    log.info(f"開倉 {sym} @ {entry:.5g}  lev={lev}x  conf={conf}")
    # 自動截圖進場當下 K 線圖（含 SMA99 + Entry/SL/TP 標線）
    _capture_chart(
        symbol=sym, source="99", interval="15",
        note=f"{dir_text} {lev}x @ {entry:.5g}",
        entry=entry, sl=sl, tp1=tp1, tp2=tp2, tp3=tp3, side=side,
    )


def _update_tp_levels(pos: dict) -> None:
    """根據當前 C 點重算 TP 和移動止損（多空均適用）"""
    a    = pos["a_point"]
    b    = pos["b_point"]
    c    = pos["c_point"] or b
    ab   = abs(b - a)
    side = pos.get("side", "long")
    if side == "long":
        pos["tp1_price"] = round(c + 0.786 * ab, 6)
        pos["tp2_price"] = round(c + 1.0   * ab, 6)
        pos["tp3_price"] = round(c + 1.272 * ab, 6)
        pos["sl_05"]     = round(c + 0.5   * ab, 6)
    else:  # short
        pos["tp1_price"] = round(c - 0.786 * ab, 6)
        pos["tp2_price"] = round(c - 1.0   * ab, 6)
        pos["tp3_price"] = round(c - 1.272 * ab, 6)
        pos["sl_05"]     = round(c - 0.5   * ab, 6)


def manage_position(pub, auth, state: dict, sym: str) -> None:
    """每次掃描都呼叫，管理持倉中的止損/止盈"""
    positions = state["positions"]
    pos = positions.get(sym)
    if not pos:
        return

    price = get_current_price(pub, sym)
    if price is None:
        return

    # ── 更新 5分鐘 99SMA 止損線 ───────────────────────────────
    sl_5m = get_5m_sma99_price(pub, sym)
    if sl_5m and sl_5m > pos["sl_price"] and not pos["sl_moved"]:
        # SL 只能往有利方向移（多單只往上移）
        # 但 5m SMA99 有時會往上走，更新它
        pass  # 初始止損保持 5m SMA99 觸及即出

    side = pos.get("side", "long")

    # ── 動態更新 C 點 ─────────────────────────────────────
    # 多單：C = 進場後最低點；空單：C = 進場後最高點
    # 鎖定條件：連續 C_LOCK_BARS 根 15m K棒（= C_LOCK_BARS × 900 秒）沒創新低/新高
    _c_lock_sec = C_LOCK_BARS * 15 * 60   # 3根 × 15分 = 2700秒
    now_ts_c    = datetime.now(timezone.utc).timestamp()
    if not pos["c_locked"]:
        if side == "long":
            if pos["c_point"] is None or price < pos["c_point"]:
                pos["c_point"]    = price
                pos["c_up_since"] = None   # 創新低，重置計時
                _update_tp_levels(pos)
            else:
                if pos.get("c_up_since") is None:
                    pos["c_up_since"] = now_ts_c   # 開始計時
                elif now_ts_c - pos["c_up_since"] >= _c_lock_sec:
                    pos["c_locked"] = True
                    _update_tp_levels(pos)
                    log.info(f"{sym} [多] C點鎖定 @ {pos['c_point']:.4f}  TP1={pos['tp1_price']:.4f}")
        else:  # short
            if pos["c_point"] is None or price > pos["c_point"]:
                pos["c_point"]    = price
                pos["c_up_since"] = None   # 創新高，重置計時
                _update_tp_levels(pos)
            else:
                if pos.get("c_up_since") is None:
                    pos["c_up_since"] = now_ts_c
                elif now_ts_c - pos["c_up_since"] >= _c_lock_sec:
                    pos["c_locked"] = True
                    _update_tp_levels(pos)
                    log.info(f"{sym} [空] C點鎖定 @ {pos['c_point']:.4f}  TP1={pos['tp1_price']:.4f}")

    # ── 止損檢查（5分鐘 99SMA 觸及即出）─────────────────────
    sl_trigger = sl_5m if sl_5m else pos["sl_price"]
    if not pos["sl_moved"]:
        if side == "long" and price <= sl_trigger:
            _close_position(pub, state, sym, price, reason=f"SL觸及 5m99SMA ({sl_trigger:.4f})")
            _try_reverse(pub, auth, state, sym, price)
            return
        elif side == "short" and price >= sl_trigger:
            _close_position(pub, state, sym, price, reason=f"SL觸及 5m99SMA ({sl_trigger:.4f})")
            _try_reverse(pub, auth, state, sym, price)
            return
    else:
        if side == "long" and price <= pos.get("sl_05", 0):
            _close_position(pub, state, sym, price, reason=f"移動止損觸及 ({pos['sl_05']:.4f})")
            _try_reverse(pub, auth, state, sym, price)
            return
        elif side == "short" and price >= pos.get("sl_05", float("inf")):
            _close_position(pub, state, sym, price, reason=f"移動止損觸及 ({pos['sl_05']:.4f})")
            _try_reverse(pub, auth, state, sym, price)
            return

    # ── TP：等幅（1.0×AB），全數出場（Sam 原版）────────────────
    tp1_hit_cond = (price >= pos["tp1_price"]) if side == "long" else (price <= pos["tp1_price"])
    if pos["c_locked"] and not pos["tp1_hit"] and tp1_hit_cond:
        _close_position(pub, state, sym, price, reason=f"TP等幅全出 ({pos['tp1_price']:.5g})")


def _try_reverse(pub, auth, state: dict, sym: str, price: float) -> None:
    """止損出場後立刻重新掃描，若反向結構已就緒則反手開單。"""
    sig_state = state.get("signals", {}).get(sym)
    if not sig_state:
        return
    signal = detect_signal(pub, sym, sig_state)
    if signal:
        log.info(f"[反手] {sym} 止損後偵測到反向訊號，方向={signal['side']}")
        open_position(pub, auth, state, signal)


def _close_position(pub, state: dict, sym: str, price: float, reason: str) -> bool:
    pos = state["positions"].get(sym)
    if not pos:
        return False

    side      = pos.get("side", "long")
    close_pnl = pos["size"] * (price - pos["entry"]) if side == "long" else pos["size"] * (pos["entry"] - price)
    total_pnl_early = close_pnl + pos.get("realized_pnl", 0)
    is_win    = total_pnl_early > 0

    state["capital"] += close_pnl
    state["total_trades"] += 1
    if is_win:
        state["wins"]   += 1
    else:
        state["losses"] += 1

    # 更新最大回撤（用總權益：capital + 所有持倉未實現盈虧）
    upnl = 0.0
    for s, p in state["positions"].items():
        if s == sym:
            continue
        px = get_current_price(pub, s) or p["entry"]
        if p.get("side", "long") == "long":
            upnl += p["size"] * (px - p["entry"])
        else:
            upnl += p["size"] * (p["entry"] - px)
    equity = state["capital"] + upnl
    if equity > state["peak_equity"]:
        state["peak_equity"] = equity
    dd = (state["peak_equity"] - equity) / state["peak_equity"]
    if dd > state["max_drawdown"]:
        state["max_drawdown"] = dd

    del state["positions"][sym]

    # ── 儲存交易紀錄（最近50筆）─────────────────────────────
    orig_margin = pos.get("orig_margin", pos.get("margin", 1))
    total_pnl   = round(close_pnl + pos.get("realized_pnl", 0), 2)   # 含分段已實現
    pnl_pct     = total_pnl / orig_margin * 100 if orig_margin else 0
    trade_record = {
        "symbol"    : sym.replace("/USDT:USDT", ""),
        "side"      : pos.get("side", "long"),
        "entry"     : round(pos["entry"], 6),
        "exit"      : round(price, 6),
        "pnl"       : total_pnl,
        "pnl_pct"   : round(pnl_pct, 1),
        "reason"    : reason,
        "opened_at" : pos.get("opened_at", "")[:16],
        "closed_at" : datetime.now(TZ8).strftime("%Y-%m-%d %H:%M"),
        "win"       : total_pnl > 0,
    }
    history = state.setdefault("trade_history", [])
    history.append(trade_record)
    if len(history) > 50:
        state["trade_history"] = history[-50:]

    save_state(state)

    win_rate = state["wins"] / state["total_trades"] * 100 if state["total_trades"] else 0
    coin     = sym.replace("/USDT:USDT", "").lower()
    tg(f"{'🟢' if is_win else '🔴'} <b>{coin} 出場</b>\n"
       f"  {reason}\n"
       f"  進場：{pos['entry']:.5g} → 出場：{price:.5g}\n"
       f"  {'盈利' if is_win else '虧損'}：{total_pnl:+.2f} U（{pnl_pct:+.1f}%）\n"
       f"  資金：{state['capital']:.2f} U\n"
       f"  勝率：{win_rate:.1f}% ({state['wins']}/{state['total_trades']})")

    # ── 失敗單追蹤（新規則生效後止損出場 → 通知做損前結構分析）──────────
    if not is_win and "SL" in reason:
        entry_time = pos.get("opened_at", "")[:16]
        tg(f"🔍 <b>【損前結構分析請求】</b>\n"
           f"  symbol: <b>{coin.upper()}</b>\n"
           f"  entry_time: <b>{entry_time}</b>\n"
           f"  side: {pos.get('side','long')}  entry: {pos['entry']:.5g}\n"
           f"  ⚠️ 請查看 15m 趨勢 vs 1H 結構是否背離")
    log.info(f"平倉 {sym} @ {price:.4f}  PnL={total_pnl:+.2f}  reason={reason}")
    return True


def _send_test_signals() -> None:
    """模擬進場 + 出場通知，供格式確認用"""
    # 模擬進場
    tg(
        "🟢 <b>link做多</b>  2x槓桿\n"
        "  信心 [███████░░░] 78/100\n"
        "  進場：8.994\n"
        "  止損：8.805（2.1%）\n"
        "  TP1：9.273（出場33%，移保本）\n"
        "  TP2：9.482（出場剩50%，移TP1）\n"
        "  TP3：9.761（全出）\n"
        "  倉位：150.0U  風險：9.7U\n"
        "  <i>📋 Paper Mode</i>"
    )
    time.sleep(1)
    # 模擬 TP1 出場
    tg(
        "🏁 <b>link 止盈 TP1</b>\n"
        "  出場 33%  @9.275\n"
        "  盈虧：+4.24U\n"
        "  止損移至保本（8.994）\n"
        "  剩餘倉位繼續持有"
    )
    time.sleep(1)
    # 模擬全出
    tg(
        "✅ <b>link 平倉完成</b>  做多\n"
        "  進場：8.994  出場：9.761\n"
        "  盈虧：<b>+18.3U</b>（+8.5%）\n"
        "  持倉：2h 34m\n"
        "  ──────────────\n"
        "  累計：3勝0敗  勝率：100%"
    )


def _send_99_status(pub, state: dict) -> None:
    """處理 /99 指令 — 僅顯示本金、未實現盈虧、持倉"""
    capital   = state["capital"]
    positions = state["positions"]

    upnl = 0.0
    pos_lines = []
    for sym, p in positions.items():
        px = get_current_price(pub, sym)
        if px:
            side = p.get("side", "long")
            if side == "long":
                u = p["size"] * (px - p["entry"])
            else:
                u = p["size"] * (p["entry"] - px)
            upnl += u
            coin = sym.replace("/USDT:USDT", "").lower()
            side_txt = "多" if side == "long" else "空"
            pos_lines.append(
                f"  • {coin} {side_txt} {p['leverage']}x  進:{p['entry']:.5g}  現:{px:.5g}  浮動:{u:+.2f}U"
            )

    equity = capital + upnl
    lines = [
        f"📊 <b>99戰法持倉</b>",
        f"━━━━━━━━━━━━━━",
        f"本金：{INITIAL_CAPITAL:.0f} U",
        f"可用：{capital:.2f} U",
        f"未實現盈虧：{upnl:+.2f} U",
        f"淨值：{equity:.2f} U",
        f"━━━━━━━━━━━━━━",
        f"持倉：{len(positions)} 個",
    ]
    if pos_lines:
        lines += pos_lines
    else:
        lines.append("  （目前無持倉）")
    lines.append(f"{'📋 Paper Mode' if PAPER_MODE else '🔴 Live Mode'}")
    tg("\n".join(lines))


def _send_status(pub, state: dict) -> None:
    capital   = state["capital"]
    positions = state["positions"]
    total     = state["total_trades"]
    wins      = state["wins"]
    win_rate  = wins / total * 100 if total else 0
    dd        = state["max_drawdown"] * 100

    upnl = 0.0
    pos_lines = []
    for sym, p in positions.items():
        px = get_current_price(pub, sym)
        if px:
            s = p.get("side", "long")
            u = p["size"] * (px - p["entry"]) if s == "long" else p["size"] * (p["entry"] - px)
            upnl += u
            coin = sym.replace("/USDT:USDT", "").lower()
            tp_next = p.get("tp2_price" if p.get("tp1_hit") else "tp1_price", 0)
            pos_lines.append(
                f"  • {coin} {p['leverage']}x  浮動：{u:+.2f}U  "
                f"SL:{p['sl_price']:.5g}  下一TP:{tp_next:.5g}"
            )

    equity = capital + upnl
    started = state.get("started_at", "")[:10]
    lines = [
        f"📊 <b>99戰法 狀態</b>",
        f"━━━━━━━━━━━━━━",
        f"淨值：{equity:.2f} U（浮動 {upnl:+.2f}）",
        f"可用：{capital:.2f} U",
        f"持倉：{len(positions)} 個",
    ]
    if pos_lines:
        lines += pos_lines
    lines += [
        f"━━━━━━━━━━━━━━",
        f"交易：{total} 筆  勝率：{win_rate:.1f}%（{wins}勝/{state['losses']}敗）",
        f"最大回撤：{dd:.1f}%",
        f"初始：{INITIAL_CAPITAL:.0f} U  啟動：{started}",
        f"{'📋 Paper Mode' if PAPER_MODE else '🔴 Live Mode'}",
        f"",
        f"💡 輸入 /99 查看持倉詳情",
    ]
    tg("\n".join(lines))


# ═══════════════════════════════════════════════════════════════
# 持倉監控執行緒（每 MONITOR_INTERVAL 秒，只查有開倉的幣）
# ═══════════════════════════════════════════════════════════════

def _position_monitor_loop(pub, auth, state: dict) -> None:
    """
    獨立執行緒：每 MONITOR_INTERVAL 秒檢查一次所有持倉的 SL/TP。
    只打「有倉位的幣」的 API，快速且低頻，消除主掃描的輪詢滑價。
    所有 state 存取都透過 _state_lock 保護。
    """
    log.info("[Monitor] 持倉監控執行緒啟動")
    while True:
        try:
            time.sleep(MONITOR_INTERVAL)

            with _state_lock:
                syms = list(state["positions"].keys())

            if not syms:
                continue

            for sym in syms:
                try:
                    with _state_lock:
                        # 再確認還在（主執行緒可能已平倉）
                        if sym not in state["positions"]:
                            continue
                        pos = state["positions"][sym]

                    # ── 取現價 & 5m SMA（在 lock 外做 IO，避免長時間持鎖）──
                    price  = get_current_price(pub, sym)
                    sl_5m  = get_5m_sma99_price(pub, sym)

                    if price is None:
                        continue

                    with _state_lock:
                        # 二次確認倉位仍存在（IO 期間可能被主執行緒平掉）
                        if sym not in state["positions"]:
                            continue
                        manage_position_monitor(pub, auth, state, sym, price, sl_5m)

                except Exception as e:
                    log.warning(f"[Monitor] {sym} 監控異常: {e}")

        except Exception as e:
            log.error(f"[Monitor] 執行緒異常: {e}", exc_info=True)
            time.sleep(10)   # 異常後等 10s 再繼續，不死循環


def manage_position_monitor(pub, auth, state: dict, sym: str,
                             price: float, sl_5m: Optional[float]) -> None:
    """
    持倉監控執行緒專用的倉位管理函數（與主執行緒的 manage_position 相同邏輯）。
    呼叫時必須持有 _state_lock。
    """
    positions = state["positions"]
    pos = positions.get(sym)
    if not pos:
        return

    side = pos.get("side", "long")

    # ── 動態更新 C 點 ──────────────────────────────────────────
    _c_lock_sec = C_LOCK_BARS * 15 * 60
    now_ts_c    = datetime.now(timezone.utc).timestamp()
    if not pos["c_locked"]:
        if side == "long":
            if pos["c_point"] is None or price < pos["c_point"]:
                pos["c_point"]    = price
                pos["c_up_since"] = None
                _update_tp_levels(pos)
            else:
                if pos.get("c_up_since") is None:
                    pos["c_up_since"] = now_ts_c
                elif now_ts_c - pos["c_up_since"] >= _c_lock_sec:
                    pos["c_locked"] = True
                    _update_tp_levels(pos)
                    log.info(f"[Monitor] {sym} [多] C點鎖定 @ {pos['c_point']:.4f}  TP1={pos['tp1_price']:.4f}")
        else:
            if pos["c_point"] is None or price > pos["c_point"]:
                pos["c_point"]    = price
                pos["c_up_since"] = None
                _update_tp_levels(pos)
            else:
                if pos.get("c_up_since") is None:
                    pos["c_up_since"] = now_ts_c
                elif now_ts_c - pos["c_up_since"] >= _c_lock_sec:
                    pos["c_locked"] = True
                    _update_tp_levels(pos)
                    log.info(f"[Monitor] {sym} [空] C點鎖定 @ {pos['c_point']:.4f}  TP1={pos['tp1_price']:.4f}")

    # ── 止損檢查 ──────────────────────────────────────────────
    sl_trigger = sl_5m if sl_5m else pos["sl_price"]
    if not pos["sl_moved"]:
        if side == "long" and price <= sl_trigger:
            _close_position(pub, state, sym, price, reason=f"SL觸及 5m99SMA ({sl_trigger:.4f})")
            _try_reverse(pub, auth, state, sym, price)
            return
        elif side == "short" and price >= sl_trigger:
            _close_position(pub, state, sym, price, reason=f"SL觸及 5m99SMA ({sl_trigger:.4f})")
            _try_reverse(pub, auth, state, sym, price)
            return
    else:
        if side == "long" and price <= pos.get("sl_05", 0):
            _close_position(pub, state, sym, price, reason=f"移動止損觸及 ({pos['sl_05']:.4f})")
            _try_reverse(pub, auth, state, sym, price)
            return
        elif side == "short" and price >= pos.get("sl_05", float("inf")):
            _close_position(pub, state, sym, price, reason=f"移動止損觸及 ({pos['sl_05']:.4f})")
            _try_reverse(pub, auth, state, sym, price)
            return

    # ── TP：等幅（1.0×AB），全數出場（Sam 原版）────────────────
    tp1_hit_cond = (price >= pos["tp1_price"]) if side == "long" else (price <= pos["tp1_price"])
    if pos["c_locked"] and not pos["tp1_hit"] and tp1_hit_cond:
        _close_position(pub, state, sym, price, reason=f"TP等幅全出 ({pos['tp1_price']:.5g})")


# ═══════════════════════════════════════════════════════════════
# MAIN LOOP
# ═══════════════════════════════════════════════════════════════
_PID_FILE = Path(__file__).parent / "strategy99_bot.pid"

def _check_single_instance() -> None:
    """防止重複啟動：若 PID file 存在且進程仍在跑，直接退出。"""
    if _PID_FILE.exists():
        old_pid = int(_PID_FILE.read_text().strip())
        try:
            import os as _os
            _os.kill(old_pid, 0)          # 送 signal 0 只是測試進程是否存在
            log.error(f"已有 bot 在跑（PID={old_pid}），本次啟動中止。")
            raise SystemExit(1)
        except ProcessLookupError:
            pass                           # 舊 PID 已死，繼續啟動
    _PID_FILE.write_text(str(os.getpid()))

def main() -> None:
    _check_single_instance()
    log.info("═" * 50)
    log.info("99SMA 戰法 Bot 啟動")
    log.info(f"本金：{INITIAL_CAPITAL} USDT  |  最大槓桿：{MAX_LEVERAGE}x  |  Paper={PAPER_MODE}")
    log.info("═" * 50)

    tg(f"🚀 <b>99戰法 Bot 啟動</b>\n"
       f"本金：{INITIAL_CAPITAL} USDT\n"
       f"最大槓桿：{MAX_LEVERAGE}x\n"
       f"風險/筆：{RISK_PCT*100:.0f}%\n"
       f"{'📋 Paper Mode' if PAPER_MODE else '🔴 Live Mode'}")

    pub   = get_public_exchange()   # 掃描/K線/價格 — 不消耗 API 配額
    auth  = get_auth_exchange()     # 下單用（PAPER_MODE 時不實際呼叫）
    state = load_state()

    # 確保 signals 欄位存在
    if "signals" not in state:
        state["signals"] = {}

    # ── 啟動持倉監控執行緒（daemon=True，主程式結束時自動停止）──
    monitor_thread = threading.Thread(
        target=_position_monitor_loop,
        args=(pub, auth, state),
        daemon=True,
        name="PositionMonitor",
    )
    monitor_thread.start()

    # 啟動 TG 指令背景監聽（即時回應，不受主循環間隔影響）
    start_tg_listener(pub, state)

    while True:
        try:
            loop_start = time.time()

            # ── 1. 掃描新訊號（持倉管理已由監控執行緒負責）──
            with _state_lock:
                pos_count = len(state["positions"])

            update_btc_brake(pub)   # 每輪都更新 BTC 1H 煞車，不受持倉數限制

            if pos_count < MAX_OPEN_POS:
                symbols = get_top_symbols(pub, TOP_N)
                for sym in symbols:
                    with _state_lock:
                        already_open = sym in state["positions"]
                        if sym not in state["signals"]:
                            state["signals"][sym] = {"stage": "watching", "stage_s": "watching_s"}
                        sig_state = state["signals"][sym]

                    if already_open:
                        continue

                    signal = detect_signal(pub, sym, sig_state)
                    if signal:
                        with _state_lock:
                            open_position(pub, auth, state, signal)

                    time.sleep(0.3)   # rate limit 緩衝

            # ── 2. 處理 TG 指令（/狀態）─────────────────────
            handle_tg_commands(pub, state)

            # ── 3. 監控執行緒健康檢查 ──────────────────────
            if not monitor_thread.is_alive():
                log.error("[Monitor] 持倉監控執行緒意外停止，重新啟動")
                monitor_thread = threading.Thread(
                    target=_position_monitor_loop,
                    args=(pub, auth, state),
                    daemon=True,
                    name="PositionMonitor",
                )
                monitor_thread.start()

            # ── 4. 等下一輪 ───────────────────────────────────
            elapsed = time.time() - loop_start
            sleep_t = max(5, SCAN_INTERVAL - elapsed)
            log.info(f"本輪耗時 {elapsed:.1f}s，等待 {sleep_t:.0f}s")
            time.sleep(sleep_t)

        except KeyboardInterrupt:
            log.info("手動停止")
            tg("⏹ 99戰法 Bot 手動停止")
            break
        except Exception as e:
            log.error(f"主循環異常: {e}", exc_info=True)
            time.sleep(30)

    _PID_FILE.unlink(missing_ok=True)


if __name__ == "__main__":
    import os
    main()
