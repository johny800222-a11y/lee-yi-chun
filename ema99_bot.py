#!/usr/bin/env python3
"""
EMA99 + BB Squeeze V3 — 自動交易 Bot
─────────────────────────────────────────────────────────────
功能：
  ‧ 每小時掃描 Top100 USDT 永續合約
  ‧ 1H 趨勢過濾 + 15min 二次突破進場（日內交易模式）
  ‧ 止損 / 保本 / 分批止盈 / 移動止損 自動管理
  ‧ Telegram 即時通知（進場、出場、每小時狀態）
  ‧ PAPER_MODE=True 時完全虛擬，不送出真實訂單

環境變數（建議放在 .env 或直接設 export）：
  BINANCE_API_KEY
  BINANCE_API_SECRET
  TELEGRAM_BOT_TOKEN
  TELEGRAM_CHAT_ID
─────────────────────────────────────────────────────────────
"""
from __future__ import annotations
import json
import logging
import os
import time
from pathlib import Path

# 自動載入 .env 設定檔
_env_file = Path(__file__).parent / ".env"
if _env_file.exists():
    for _line in _env_file.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone, timedelta
from typing import Optional

import ccxt
import numpy as np
import pandas as pd
import requests

# ═══════════════════════════════════════════════════════════════
# CONFIG — 安全開關 + 憑證
# ═══════════════════════════════════════════════════════════════
PAPER_MODE = True     # ← 改 False 才送出真實訂單

BINANCE_KEY    = os.getenv("BINANCE_API_KEY",    "OY39AdYPIxm9FZdROrjDoRDVKbimtrc11A2Uy2j7YJOHIdDVIIAQiiMsd3r3g3Xw")
BINANCE_SECRET = os.getenv("BINANCE_API_SECRET", "3OvHOvzByCKDK0PavpGC5eGx7PPDZ5WLcKz0eg7HO7e1Drelma399JODcZtTEV0E")
TG_TOKEN       = os.getenv("TELEGRAM_BOT_TOKEN", "8005879844:AAG8DJoaphzsweVmdvMB6SNphJdRy0osQGo")
TG_CHAT_ID     = os.getenv("TELEGRAM_CHAT_ID",   "1768177615")

# ── 掃描間隔 ─────────────────────────────────────────────────────
SCAN_INTERVAL_SEC  = 1 * 60    # 每 1 分鐘掃一次
STATUS_INTERVAL_HR = 1         # 每幾小時發一次狀態報告

# ── 策略參數 ────────────────────────────────────────────────────
INITIAL_CAPITAL = 10_000
MAX_POS_PCT     = 0.10
MAX_OPEN_POS    = 10
TOP_N           = 100

EMA_N           = 99
BB_N, BB_STD    = 20, 2.0
ATR_N           = 14
WASHOUT_BARS    = 2
SLOPE_LOOKBACK  = 3
MIN_TP_DIST     = 0.008  # BB 上軌距進場至少 0.8%（日內放寬）
COOLDOWN_HOURS  = 1      # 出場後同一幣冷卻 1 小時才可再進場（日內）
BREAKEVEN_PCT   = 0.02
TRAIL_ATR_MULT  = 1.2
TAKER_FEE       = 0.0005

# ── 強化參數（2026-05 月度檢討新增）──────────────────────────
VOL_MA_N        = 20     # 成交量均線長度
VOL_MULT        = 1.1    # 突破 K 棒成交量需 > VOL_MA_N 均量 × VOL_MULT（日內放寬）
MAX_DRAWDOWN    = 0.20   # 最大回撤熔斷：資金跌破 INITIAL_CAPITAL*(1-MAX_DRAWDOWN) 停止進場

# ── 檔案 ────────────────────────────────────────────────────────
STATE_FILE      = Path("ema99_bot_state.json")
LOG_FILE        = Path("ema99_bot.log")
JSONBIN_API_KEY = os.getenv("JSONBIN_API_KEY", "")
JSONBIN_BIN_ID  = os.getenv("JSONBIN_BIN_ID",  "")

# ── Logging ─────────────────────────────────────────────────────
# 只用 FileHandler，避免 StreamHandler + shell 重導向（>> log 2>&1）造成每行寫兩次
_log_fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
_file_handler   = logging.FileHandler(LOG_FILE, encoding="utf-8")
_file_handler.setFormatter(_log_fmt)
_stream_handler = logging.StreamHandler()
_stream_handler.setFormatter(_log_fmt)

log = logging.getLogger("ema99_bot")
log.setLevel(logging.INFO)
log.propagate = False          # 不往 root 傳，避免重複
if not log.handlers:           # 確保只加一次
    log.addHandler(_file_handler)
    log.addHandler(_stream_handler)


# ═══════════════════════════════════════════════════════════════
# TELEGRAM NOTIFIER
# ═══════════════════════════════════════════════════════════════
class Telegram:
    _BASE = "https://api.telegram.org/bot{token}/{method}"

    def __init__(self, token: str, chat_id: str):
        self.token   = token
        self.chat_id = chat_id
        self._ok     = bool(token and token != "YOUR_TG_TOKEN")

    def send(self, text: str) -> bool:
        if not self._ok:
            return False
        url = self._BASE.format(token=self.token, method="sendMessage")
        try:
            r = requests.post(url, json={
                "chat_id"    : self.chat_id,
                "text"       : text,
                "parse_mode" : "HTML",
            }, timeout=10)
            return r.ok
        except Exception as e:
            log.warning(f"Telegram send failed: {e}")
            return False

    # ── 訊息模板 ──────────────────────────────────────────────
    def on_start(self, paper: bool) -> None:
        mode = "📄 虛擬盤" if paper else "🔴 實盤"
        self.send(
            f"🤖 <b>EMA99 Bot 啟動</b>\n"
            f"模式：{mode}\n"
            f"時間：{_now()}"
        )

    def on_signal(self, sym: str, entry_px: float, sl: float,
                  bb_u: float, lev: int, paper: bool) -> None:
        sl_pct  = (entry_px - sl) / entry_px * 100
        tp_pct  = (bb_u - entry_px) / entry_px * 100
        mode    = "[虛擬]" if paper else "[實盤]"
        self.send(
            f"🔍 <b>訊號偵測 {mode}</b>\n"
            f"幣對：<code>{sym}</code>\n"
            f"進場價：{entry_px:.4f}\n"
            f"止損：{sl:.4f}  <i>(-{sl_pct:.1f}%)</i>\n"
            f"目標(BB上軌)：{bb_u:.4f}  <i>(+{tp_pct:.1f}%)</i>\n"
            f"槓桿：{lev}×"
        )

    def on_entry(self, sym: str, entry_px: float, qty: float,
                 margin: float, sl: float, lev: int, paper: bool) -> None:
        mode = "[虛擬]" if paper else "[實盤]"
        self.send(
            f"✅ <b>進場 {mode}</b>\n"
            f"幣對：<code>{sym}</code>\n"
            f"進場價：{entry_px:.4f}\n"
            f"數量：{qty:.4f}\n"
            f"保證金：{margin:.2f} USDT\n"
            f"止損：{sl:.4f}\n"
            f"槓桿：{lev}×"
        )

    def on_partial_tp(self, sym: str, entry_px: float, tp_px: float,
                      pnl: float, paper: bool) -> None:
        pct  = (tp_px - entry_px) / entry_px * 100
        mode = "[虛擬]" if paper else "[實盤]"
        self.send(
            f"🟡 <b>分批止盈 50% {mode}</b>\n"
            f"幣對：<code>{sym}</code>\n"
            f"出場價：{tp_px:.4f}  <i>(+{pct:.1f}%)</i>\n"
            f"損益：{pnl:+.2f} USDT\n"
            f"剩餘 50% 移動止損已啟動"
        )

    def on_trail_stop(self, sym: str, entry_px: float, exit_px: float,
                      pnl: float, hold_h: float, paper: bool) -> None:
        pct  = (exit_px - entry_px) / entry_px * 100
        mode = "[虛擬]" if paper else "[實盤]"
        emoji = "🟢" if pnl > 0 else "🔴"
        self.send(
            f"{emoji} <b>移動止損出場 {mode}</b>\n"
            f"幣對：<code>{sym}</code>\n"
            f"出場價：{exit_px:.4f}  <i>({pct:+.1f}%)</i>\n"
            f"損益：{pnl:+.2f} USDT\n"
            f"持倉時間：{hold_h:.1f}h"
        )

    def on_stop_loss(self, sym: str, entry_px: float, exit_px: float,
                     pnl: float, hold_h: float, reason: str, paper: bool) -> None:
        pct  = (exit_px - entry_px) / entry_px * 100
        mode = "[虛擬]" if paper else "[實盤]"
        label = "保本止損" if reason == "breakeven" else "止損"
        self.send(
            f"🔴 <b>{label}出場 {mode}</b>\n"
            f"幣對：<code>{sym}</code>\n"
            f"出場價：{exit_px:.4f}  <i>({pct:+.1f}%)</i>\n"
            f"損益：{pnl:+.2f} USDT\n"
            f"持倉時間：{hold_h:.1f}h"
        )

    def on_hourly_status(self, capital: float, positions: dict,
                         open_pnl: float, paper: bool) -> None:
        mode  = "📄 虛擬盤" if paper else "🔴 實盤"
        total = capital + open_pnl
        ret   = (total - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100
        lines = [
            f"📊 <b>每小時狀態報告</b>  {mode}",
            f"時間：{_now()}",
            f"可用資金：{capital:.2f} USDT",
            f"未實現盈虧：{open_pnl:+.2f} USDT",
            f"淨值：{total:.2f}  <i>({ret:+.1f}%)</i>",
            f"持倉：{len(positions)}/{MAX_OPEN_POS}",
        ]
        if positions:
            lines.append("─ 持倉明細 ─")
            for sym, p in positions.items():
                side_icon = "🟢" if p.get("side", "long") == "long" else "🔴"
                if p.get("side", "long") == "long":
                    pnl_pct = (p.get("cur_px", p["entry_px"]) - p["entry_px"]) / p["entry_px"] * 100
                else:
                    pnl_pct = (p["entry_px"] - p.get("cur_px", p["entry_px"])) / p["entry_px"] * 100
                lines.append(f"  {side_icon}{sym.split('/')[0]}: {pnl_pct:+.1f}%  入{p['entry_px']:.4f}")
        self.send("\n".join(lines))

    def on_watchlist(self, items: list, paper: bool) -> None:
        if not items:
            return
        mode = "[虛擬]" if paper else "[實盤]"
        header = (
            f"👀 <b>觀察名單 {mode}</b>  {_now()}\n"
            f"共 {len(items)} 個幣對接近訊號\n"
        )
        # 每批 5 個避免超過 Telegram 字元限制
        for i in range(0, len(items), 5):
            chunk = items[i:i + 5]
            lines = [header] if i == 0 else ["👀 <b>（續）</b>\n"]
            for it in chunk:
                sym = it["sym"]
                name = sym.split("/")[0]
                if it["state"] == "A":
                    tag = f"📍 即將突破EMA99（差 {it['gap_ema']:.1f}%）"
                else:
                    tag = f"⚡ 已突破EMA，待過BB中軌（差 {it['gap_bbm']:.1f}%）"
                lines.append(
                    f"<code>{name}</code>  {tag}\n"
                    f"  現價 {it['cl']:.4f}  EMA {it['ema']:.4f}"
                    f"  BB中軌 {it['bb_m']:.4f}\n"
                    f"  洗盤 {it['consec']} 根K棒"
                    f"  目標 {it['bb_u']:.4f}"
                )
            self.send("\n\n".join(lines))

    def on_error(self, msg: str) -> None:
        self.send(f"⚠️ <b>Bot 錯誤</b>\n{msg}\n時間：{_now()}")


def _now() -> str:
    tz_taipei = timezone(timedelta(hours=8))
    return datetime.now(tz_taipei).strftime("%Y-%m-%d %H:%M UTC+8")


# ═══════════════════════════════════════════════════════════════
# STATE PERSISTENCE
# ═══════════════════════════════════════════════════════════════
DEFAULT_STATE = {
    "capital"        : INITIAL_CAPITAL,
    "positions"      : {},
    "trades"         : [],
    "last_run"       : None,
    "watchlist_sent" : [],   # 已推送過的觀察名單幣對（避免重複）
    "cooldown"       : {},   # sym -> 最後出場時間（冷卻用）
}


def _jsonbin_load() -> Optional[dict]:
    """從 jsonbin.io 讀取狀態（回傳 None 表示失敗）"""
    if not JSONBIN_API_KEY or not JSONBIN_BIN_ID:
        return None
    try:
        r = requests.get(
            f"https://api.jsonbin.io/v3/b/{JSONBIN_BIN_ID}/latest",
            headers={"X-Master-Key": JSONBIN_API_KEY},
            timeout=10,
        )
        if r.ok:
            return r.json().get("record")
    except Exception as e:
        log.warning(f"jsonbin load failed: {e}")
    return None


def _jsonbin_save(s: dict) -> bool:
    """把狀態寫到 jsonbin.io（回傳是否成功）"""
    if not JSONBIN_API_KEY or not JSONBIN_BIN_ID:
        return False
    try:
        r = requests.put(
            f"https://api.jsonbin.io/v3/b/{JSONBIN_BIN_ID}",
            headers={"X-Master-Key": JSONBIN_API_KEY, "Content-Type": "application/json"},
            json=s,
            timeout=10,
        )
        return r.ok
    except Exception as e:
        log.warning(f"jsonbin save failed: {e}")
        return False


def load_state() -> dict:
    # 優先從雲端讀取（Render 重啟後也不會掉資料）
    remote = _jsonbin_load()
    if remote is not None:
        log.info("狀態從 jsonbin.io 載入")
        return remote
    # fallback：本地檔案
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return dict(DEFAULT_STATE)


def save_state(s: dict) -> None:
    # 同時寫本地 + 雲端
    STATE_FILE.write_text(json.dumps(s, indent=2, default=str, ensure_ascii=False),
                          encoding="utf-8")
    _jsonbin_save(s)


# ═══════════════════════════════════════════════════════════════
# EXCHANGE
# ═══════════════════════════════════════════════════════════════
def get_exchange() -> ccxt.binanceusdm:
    return ccxt.binanceusdm({
        "apiKey"         : BINANCE_KEY,
        "secret"         : BINANCE_SECRET,
        "enableRateLimit": True,
    })


def fetch_ohlcv(exch, symbol: str, tf: str, limit: int = 300) -> pd.DataFrame:
    try:
        raw = exch.fetch_ohlcv(symbol, tf, limit=limit)
        if not raw:
            return pd.DataFrame()
        df = pd.DataFrame(raw, columns=["ts", "open", "high", "low", "close", "volume"])
        df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
        return df.set_index("ts").astype(float)
    except Exception as e:
        log.warning(f"fetch_ohlcv {symbol} {tf}: {e}")
        return pd.DataFrame()


def get_top_symbols(exch, n: int) -> list[str]:
    exch.load_markets()
    # 只拉 USDT 永續合約，減少請求量
    tickers = exch.fetch_tickers(params={"type": "future"})
    cands = [
        (sym, float(t.get("quoteVolume") or 0))
        for sym, t in tickers.items()
        if sym.endswith("/USDT:USDT") and float(t.get("quoteVolume") or 0) > 0
    ]
    cands.sort(key=lambda x: x[1], reverse=True)
    return [c[0] for c in cands[:n]]


def get_current_price(exch, symbol: str) -> float:
    try:
        ticker = exch.fetch_ticker(symbol)
        return float(ticker["last"])
    except Exception:
        return 0.0


def place_order(exch, symbol: str, side: str, qty: float, price: float,
                reduce_only: bool = False) -> Optional[dict]:
    if PAPER_MODE:
        log.info(f"[PAPER] {side.upper()} {qty:.6f} {symbol} @ ~{price:.4f}")
        return {"id": f"paper_{int(time.time())}", "price": price, "status": "closed"}
    try:
        params = {"reduceOnly": reduce_only}
        order  = exch.create_order(symbol, "MARKET", side, qty, params=params)
        log.info(f"[LIVE] {side.upper()} {qty:.6f} {symbol}  id={order['id']}")
        return order
    except Exception as e:
        log.error(f"place_order {symbol} {side}: {e}")
        return None


def set_leverage(exch, symbol: str, lev: int) -> None:
    if PAPER_MODE:
        return
    try:
        exch.set_leverage(lev, symbol)
    except Exception as e:
        log.warning(f"set_leverage {symbol} {lev}x: {e}")


# ═══════════════════════════════════════════════════════════════
# INDICATORS
# ═══════════════════════════════════════════════════════════════
def _ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def _bollinger(s: pd.Series, n: int = 20, sd: float = 2.0):
    mid = s.rolling(n).mean()
    dev = s.rolling(n).std()
    return mid + sd * dev, mid, mid - sd * dev


def _atr(hi: pd.Series, lo: pd.Series, cl: pd.Series, n: int = 14) -> pd.Series:
    tr = pd.concat([
        hi - lo,
        (hi - cl.shift()).abs(),
        (lo - cl.shift()).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(span=n, adjust=False).mean()


def add_4h_ind(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["ema"]   = _ema(df["close"], EMA_N)
    df["slope"] = df["ema"].diff(SLOPE_LOOKBACK)
    return df


def add_1h_ind(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["ema"]    = _ema(df["close"], EMA_N)
    bb_u, bb_m, bb_l = _bollinger(df["close"], BB_N, BB_STD)
    df["bb_u"]   = bb_u
    df["bb_m"]   = bb_m
    df["bb_l"]   = bb_l
    df["atr"]    = _atr(df["high"], df["low"], df["close"], ATR_N)
    df["vol_ma"] = df["volume"].rolling(VOL_MA_N).mean()   # 成交量均線
    return df


# ═══════════════════════════════════════════════════════════════
# SIGNAL
# ═══════════════════════════════════════════════════════════════
def _4h_ok(d4: pd.DataFrame) -> bool:
    """1H 多頭濾網：收盤 > EMA99 且斜率向上"""
    if len(d4) < SLOPE_LOOKBACK + 2:
        return False
    last = d4.iloc[-1]
    return bool(last["close"] > last["ema"] and last["slope"] > 0)


def _4h_ok_short(d4: pd.DataFrame) -> bool:
    """1H 空頭濾網：收盤 < EMA99 且斜率向下"""
    if len(d4) < SLOPE_LOOKBACK + 2:
        return False
    last = d4.iloc[-1]
    return bool(last["close"] < last["ema"] and last["slope"] < 0)


def _entry_signal(d1: pd.DataFrame) -> Optional[dict]:
    idx = len(d1) - 1
    if idx < EMA_N + BB_N + WASHOUT_BARS + 5:
        return None
    bar  = d1.iloc[idx]
    prev = d1.iloc[idx - 1]

    if bar["close"] <= bar["ema"]:
        return None
    if bar["close"] <= bar["bb_m"]:
        return None
    if prev["close"] > prev["ema"]:
        return None

    # 連續洗盤計數
    consec = 0
    for k in range(1, idx + 1):
        if d1.iloc[idx - k]["close"] <= d1.iloc[idx - k]["ema"]:
            consec += 1
        else:
            break
    if consec < WASHOUT_BARS:
        return None

    # ── 成交量確認：突破 K 棒量 > VOL_MA_N 均量 × VOL_MULT ─────
    vol_ma = bar.get("vol_ma", 0)
    if vol_ma > 0 and bar["volume"] < vol_ma * VOL_MULT:
        return None   # 量能不足，假突破過濾

    entry_px = bar["close"]
    sl = min(bar["ema"], bar["bb_l"])
    if sl >= entry_px:
        sl = entry_px * 0.97

    return {
        "entry_px": entry_px,
        "sl"      : sl,
        "bb_u"    : bar["bb_u"],
        "atr"     : bar["atr"],
    }


def _entry_signal_short(d1: pd.DataFrame) -> Optional[dict]:
    """
    空單進場訊號（_entry_signal 的鏡像）：
    ‧ 前根收盤 > EMA99（剛在上方）
    ‧ 當根收盤 < EMA99 且 < BB 中軌
    ‧ 前 WASHOUT_BARS 根收盤皆 > EMA99（高位洗盤後翻空）
    ‧ 量能確認
    """
    idx = len(d1) - 1
    if idx < EMA_N + BB_N + WASHOUT_BARS + 5:
        return None
    bar  = d1.iloc[idx]
    prev = d1.iloc[idx - 1]

    if bar["close"] >= bar["ema"]:
        return None
    if bar["close"] >= bar["bb_m"]:
        return None
    if prev["close"] < prev["ema"]:
        return None

    # 連續高位 K 棒計數
    consec = 0
    for k in range(1, idx + 1):
        if d1.iloc[idx - k]["close"] >= d1.iloc[idx - k]["ema"]:
            consec += 1
        else:
            break
    if consec < WASHOUT_BARS:
        return None

    # 量能確認
    vol_ma = bar.get("vol_ma", 0)
    if vol_ma > 0 and bar["volume"] < vol_ma * VOL_MULT:
        return None

    entry_px = bar["close"]
    sl = max(bar["ema"], bar["bb_u"])
    if sl <= entry_px:
        sl = entry_px * 1.03

    return {
        "entry_px": entry_px,
        "sl"      : sl,
        "bb_l"    : bar["bb_l"],   # 空單 TP 用 BB 下軌
        "atr"     : bar["atr"],
    }


def _watchlist_check(d1: pd.DataFrame) -> Optional[dict]:
    """
    觀察名單條件（滿足洗盤，但尚未完整觸發進場訊號）：
    A：價格在 EMA99 下方 ≤2%，洗盤根數 ≥ WASHOUT_BARS → 即將突破
    B：已突破 EMA99，但仍在 BB 中軌下方 → 突破中待確認
    """
    idx = len(d1) - 1
    if idx < EMA_N + BB_N + WASHOUT_BARS + 5:
        return None

    bar  = d1.iloc[idx]
    prev = d1.iloc[idx - 1]
    cl   = bar["close"]
    ema  = bar["ema"]
    bb_m = bar["bb_m"]
    bb_u = bar["bb_u"]

    # 連續洗盤計數
    consec = 0
    for k in range(1, idx + 1):
        if d1.iloc[idx - k]["close"] <= d1.iloc[idx - k]["ema"]:
            consec += 1
        else:
            break

    if consec < WASHOUT_BARS:
        return None

    # A：在 EMA99 下方但差距 ≤2%
    if cl < ema:
        gap = (ema - cl) / ema * 100
        if gap <= 2.0:
            return {
                "state"  : "A",
                "cl"     : cl,
                "ema"    : ema,
                "bb_m"   : bb_m,
                "bb_u"   : bb_u,
                "gap_ema": gap,
                "gap_bbm": (bb_m - cl) / bb_m * 100,
                "consec" : consec,
            }

    # B：已過 EMA99 但未過 BB 中軌（fresh crossover）
    if cl > ema and cl < bb_m and prev["close"] <= prev["ema"]:
        # 用 prev bars 重新算 washout（現在 idx-1 是剛過去的）
        consec_prev = 0
        for k in range(1, idx + 1):
            if d1.iloc[idx - k]["close"] <= d1.iloc[idx - k]["ema"]:
                consec_prev += 1
            else:
                break
        if consec_prev >= WASHOUT_BARS:
            return {
                "state"  : "B",
                "cl"     : cl,
                "ema"    : ema,
                "bb_m"   : bb_m,
                "bb_u"   : bb_u,
                "gap_ema": (cl - ema) / ema * 100,
                "gap_bbm": (bb_m - cl) / bb_m * 100,
                "consec" : consec_prev,
            }

    return None


# ═══════════════════════════════════════════════════════════════
# MAIN LOOP ITERATION
# ═══════════════════════════════════════════════════════════════
def run_once(exch, tg: Telegram, state: dict, send_status: bool = False) -> None:
    now     = datetime.now(timezone.utc)
    capital = state["capital"]
    positions = state["positions"]

    log.info("─" * 60)
    log.info(f"Run {now.strftime('%Y-%m-%d %H:%M UTC')}  PAPER={PAPER_MODE}  "
             f"cap={capital:.2f}  pos={len(positions)}")

    # ── 1. 取得 Top100 ────────────────────────────────────────
    try:
        top_syms = get_top_symbols(exch, TOP_N)
    except Exception as e:
        msg = str(e)
        log.error(f"get_top_symbols 失敗: {msg}")
        # IP 被封鎖時靜默等待，不發 Telegram 通知
        if "-1003" in msg or "418" in msg or "banned" in msg.lower():
            log.warning("Binance IP 封鎖中，靜默等待下次執行...")
            return
        tg.on_error(f"get_top_symbols 失敗: {msg}")
        return

    # ── 2. 更新持倉當前價（for status display）────────────────
    for sym, pos in list(positions.items()):
        cur = get_current_price(exch, sym)
        if cur > 0:
            pos["cur_px"] = cur

    # ── 3. 出場檢查 ───────────────────────────────────────────
    for sym in list(positions.keys()):
        pos = positions[sym]
        side_pos = pos.get("side", "long")   # 向下相容舊持倉（無 side 欄位視為多）
        d1  = fetch_ohlcv(exch, sym, "15m", limit=250)
        if d1.empty:
            continue
        d1 = add_1h_ind(d1)
        bar  = d1.iloc[-1]
        lo, hi, cl = bar["low"], bar["high"], bar["close"]
        atr_v = bar["atr"]
        pos["cur_px"] = cl

        entry_px = pos["entry_px"]
        hold_h   = (now - datetime.fromisoformat(pos["entry_ts"])).total_seconds() / 3600

        just_partial = False

        if side_pos == "long":
            # ── 多單出場 ──────────────────────────────────────
            eff_sl = max(pos["sl"], pos.get("trail_sl") or 0.0)

            # 止損
            if lo <= eff_sl:
                exit_px = eff_sl
                net = (exit_px - entry_px) / entry_px * pos["notional"]
                net -= pos["qty"] * exit_px * TAKER_FEE
                place_order(exch, sym, "sell", pos["qty"], exit_px, reduce_only=True)
                capital += pos["margin"] + net
                reason_label = "breakeven" if pos.get("breakeven_set") and exit_px >= entry_px else "stop"
                tg.on_stop_loss(sym, entry_px, exit_px, net, hold_h, reason_label, PAPER_MODE)
                state["trades"].append(_trade_record(sym, pos, exit_px, net, "stop_loss", now))
                log.info(f"LONG STOP {sym}  exit={exit_px:.4f}  pnl={net:+.2f}")
                state.setdefault("cooldown", {})[sym] = str(now)
                del positions[sym]
                continue

            # 分批止盈（BB 上軌）
            if not pos.get("partial") and hi >= pos["bb_u"]:
                just_partial = True
                tp_px    = pos["bb_u"]
                half_qty = pos["qty"] * 0.5
                h_not    = pos["notional"] * 0.5
                h_mar    = pos["margin"]   * 0.5
                net = (tp_px - entry_px) / entry_px * h_not
                net -= half_qty * tp_px * TAKER_FEE
                place_order(exch, sym, "sell", half_qty, tp_px, reduce_only=True)
                capital += h_mar + net
                tg.on_partial_tp(sym, entry_px, tp_px, net, PAPER_MODE)
                state["trades"].append(_trade_record(sym, pos, tp_px, net, "partial_tp", now))
                log.info(f"LONG PARTIAL_TP {sym}  exit={tp_px:.4f}  pnl={net:+.2f}")
                pos["qty"]      = half_qty
                pos["notional"] = h_not
                pos["margin"]   = h_mar
                pos["partial"]  = True
                pos["peak_close"] = cl
                pos["trail_sl"] = max(cl - TRAIL_ATR_MULT * atr_v, entry_px)

            # 更新 ratchet trailing SL（多單：peak 往上拉）
            if pos.get("partial") and pos.get("trail_sl") is not None:
                pk = pos.get("peak_close", cl)
                if cl > pk:
                    pos["peak_close"] = cl
                    pk = cl
                new_trail = max(pk - TRAIL_ATR_MULT * atr_v, entry_px)
                if new_trail > pos["trail_sl"]:
                    pos["trail_sl"] = new_trail

            # 移動止損出場
            if not just_partial and pos.get("trail_sl") and lo <= pos["trail_sl"]:
                exit_px = pos["trail_sl"]
                net = (exit_px - entry_px) / entry_px * pos["notional"]
                net -= pos["qty"] * exit_px * TAKER_FEE
                place_order(exch, sym, "sell", pos["qty"], exit_px, reduce_only=True)
                capital += pos["margin"] + net
                tg.on_trail_stop(sym, entry_px, exit_px, net, hold_h, PAPER_MODE)
                state["trades"].append(_trade_record(sym, pos, exit_px, net, "trail_stop", now))
                log.info(f"LONG TRAIL_STOP {sym}  exit={exit_px:.4f}  pnl={net:+.2f}")
                state.setdefault("cooldown", {})[sym] = str(now)
                del positions[sym]
                continue

            # 保本止損
            if not pos.get("breakeven_set") and cl >= entry_px * (1 + BREAKEVEN_PCT):
                pos["sl"] = max(pos["sl"], entry_px)
                pos["breakeven_set"] = True
                log.info(f"LONG BREAKEVEN SET {sym}  sl={pos['sl']:.4f}")

        else:  # side_pos == "short"
            # ── 空單出場 ──────────────────────────────────────
            # 空單止損在上方，有效止損 = min(初始止損, 移動止損)
            trail = pos.get("trail_sl")
            if trail is not None:
                eff_sl = min(pos["sl"], trail)
            else:
                eff_sl = pos["sl"]

            # 止損（收盤或最高價觸及）
            if hi >= eff_sl:
                exit_px = eff_sl
                net = (entry_px - exit_px) / entry_px * pos["notional"]
                net -= pos["qty"] * exit_px * TAKER_FEE
                place_order(exch, sym, "buy", pos["qty"], exit_px, reduce_only=True)
                capital += pos["margin"] + net
                reason_label = "breakeven" if pos.get("breakeven_set") and exit_px <= entry_px else "stop"
                tg.on_stop_loss(sym, entry_px, exit_px, net, hold_h, reason_label, PAPER_MODE)
                state["trades"].append(_trade_record(sym, pos, exit_px, net, "stop_loss", now))
                log.info(f"SHORT STOP {sym}  exit={exit_px:.4f}  pnl={net:+.2f}")
                state.setdefault("cooldown", {})[sym] = str(now)
                del positions[sym]
                continue

            # 分批止盈（BB 下軌）
            if not pos.get("partial") and lo <= pos["bb_l"]:
                just_partial = True
                tp_px    = pos["bb_l"]
                half_qty = pos["qty"] * 0.5
                h_not    = pos["notional"] * 0.5
                h_mar    = pos["margin"]   * 0.5
                net = (entry_px - tp_px) / entry_px * h_not
                net -= half_qty * tp_px * TAKER_FEE
                place_order(exch, sym, "buy", half_qty, tp_px, reduce_only=True)
                capital += h_mar + net
                tg.on_partial_tp(sym, entry_px, tp_px, net, PAPER_MODE)
                state["trades"].append(_trade_record(sym, pos, tp_px, net, "partial_tp", now))
                log.info(f"SHORT PARTIAL_TP {sym}  exit={tp_px:.4f}  pnl={net:+.2f}")
                pos["qty"]      = half_qty
                pos["notional"] = h_not
                pos["margin"]   = h_mar
                pos["partial"]  = True
                pos["trough_close"] = cl
                # 空單移動止損：入場價 ↓ 後跟隨（上移保護利潤）
                pos["trail_sl"] = min(cl + TRAIL_ATR_MULT * atr_v, entry_px)

            # 更新 ratchet trailing SL（空單：trough 往下拉）
            if pos.get("partial") and pos.get("trail_sl") is not None:
                tr = pos.get("trough_close", cl)
                if cl < tr:
                    pos["trough_close"] = cl
                    tr = cl
                new_trail = min(tr + TRAIL_ATR_MULT * atr_v, entry_px)
                if new_trail < pos["trail_sl"]:
                    pos["trail_sl"] = new_trail

            # 移動止損出場
            if not just_partial and pos.get("trail_sl") and hi >= pos["trail_sl"]:
                exit_px = pos["trail_sl"]
                net = (entry_px - exit_px) / entry_px * pos["notional"]
                net -= pos["qty"] * exit_px * TAKER_FEE
                place_order(exch, sym, "buy", pos["qty"], exit_px, reduce_only=True)
                capital += pos["margin"] + net
                tg.on_trail_stop(sym, entry_px, exit_px, net, hold_h, PAPER_MODE)
                state["trades"].append(_trade_record(sym, pos, exit_px, net, "trail_stop", now))
                log.info(f"SHORT TRAIL_STOP {sym}  exit={exit_px:.4f}  pnl={net:+.2f}")
                state.setdefault("cooldown", {})[sym] = str(now)
                del positions[sym]
                continue

            # 保本止損（空單：SL 移至成本，即進場價）
            if not pos.get("breakeven_set") and cl <= entry_px * (1 - BREAKEVEN_PCT):
                pos["sl"] = min(pos["sl"], entry_px)
                pos["breakeven_set"] = True
                log.info(f"SHORT BREAKEVEN SET {sym}  sl={pos['sl']:.4f}")

    state["capital"] = capital

    # ── 4. 進場掃描 + 觀察名單 ───────────────────────────────
    # 最大回撤熔斷：資金跌破 INITIAL_CAPITAL*(1-MAX_DRAWDOWN) 時停止新進場
    drawdown_floor = INITIAL_CAPITAL * (1 - MAX_DRAWDOWN)
    drawdown_tripped = capital < drawdown_floor
    if drawdown_tripped:
        log.warning(f"⚠️ 最大回撤熔斷觸發！capital={capital:.2f} < floor={drawdown_floor:.2f}，暫停進場")

    can_enter = len(positions) < MAX_OPEN_POS and capital >= 50 and not drawdown_tripped
    if not can_enter and not drawdown_tripped:
        log.info("持倉已滿，只掃描觀察名單")

    watchlist: list[dict] = []

    for sym in top_syms:
        if sym in positions:
            continue

        # 1H 指標（多空共用）
        d4 = fetch_ohlcv(exch, sym, "1h", limit=150)
        if d4.empty:
            continue
        d4 = add_4h_ind(d4)

        # 15min 資料（多空共用）
        d1 = fetch_ohlcv(exch, sym, "15m", limit=250)
        if d1.empty:
            continue
        d1 = add_1h_ind(d1)

        # 先偵測多空訊號（不依賴1H方向，讓訊號函式先出結果）
        sig_long  = _entry_signal(d1)        if _4h_ok(d4)       else None
        sig_short = _entry_signal_short(d1)  if _4h_ok_short(d4) else None

        # 優先多單（若同時觸發，取多）
        sig  = sig_long or sig_short
        direction = "long" if sig_long else ("short" if sig_short else None)

        if sig is not None and direction is not None and can_enter and len(positions) < MAX_OPEN_POS:
            entry_px = sig["entry_px"]

            # 冷卻期過濾
            cooldown_map = state.get("cooldown", {})
            if sym in cooldown_map:
                last_exit = datetime.fromisoformat(cooldown_map[sym])
                if (now - last_exit).total_seconds() < COOLDOWN_HOURS * 3600:
                    log.info(f"COOLDOWN {sym}  跳過（距上次出場 {(now-last_exit).total_seconds()/3600:.1f}h）")
                    continue
                else:
                    del cooldown_map[sym]

            # TP 距離過濾
            if direction == "long":
                tp_ref  = sig["bb_u"]
                tp_dist = (tp_ref - entry_px) / entry_px
            else:
                tp_ref  = sig["bb_l"]
                tp_dist = (entry_px - tp_ref) / entry_px
            if tp_dist < MIN_TP_DIST:
                log.info(f"TP_TOO_CLOSE {sym} {direction}  距離 {tp_dist*100:.1f}%，跳過")
                continue

            # ── 執行進場 ──────────────────────────────────
            atr_v   = sig["atr"]
            atr_pct = atr_v / entry_px if entry_px > 0 else 0.05
            lev = 3 if atr_pct < 0.025 else (2 if atr_pct < 0.04 else 1)

            margin   = capital * MAX_POS_PCT
            notional = margin * lev
            sl       = sig["sl"]

            if direction == "long":
                sl_dist = (entry_px - sl) / entry_px
            else:
                sl_dist = (sl - entry_px) / entry_px
            if sl_dist > 0:
                max_not = (capital * 0.03) / sl_dist
                if notional > max_not:
                    notional = max_not
                    margin   = notional / lev

            entry_fee = notional * TAKER_FEE
            if margin + entry_fee > capital:
                continue

            qty          = notional / entry_px
            order_side   = "buy" if direction == "long" else "sell"
            tp_field_key = "bb_u" if direction == "long" else "bb_l"

            tg.on_signal(sym, entry_px, sl, tp_ref, lev, PAPER_MODE)
            set_leverage(exch, sym, lev)
            order = place_order(exch, sym, order_side, qty, entry_px)
            if order is None:
                continue

            capital -= margin + entry_fee
            pos_record = {
                "side"         : direction,
                "entry_px"     : entry_px,
                "cur_px"       : entry_px,
                "qty"          : qty,
                "lev"          : lev,
                "margin"       : margin,
                "notional"     : notional,
                "sl"           : sl,
                "partial"      : False,
                "trail_sl"     : None,
                "breakeven_set": False,
                "entry_ts"     : str(now),
            }
            if direction == "long":
                pos_record["bb_u"]       = tp_ref
                pos_record["peak_close"] = entry_px
            else:
                pos_record["bb_l"]         = tp_ref
                pos_record["trough_close"] = entry_px

            positions[sym] = pos_record
            log.info(f"ENTRY {direction.upper()} {sym}  px={entry_px:.4f}  lev={lev}x  sl={sl:.4f}  tp={tp_ref:.4f}")
            tg.on_entry(sym, entry_px, qty, margin, sl, lev, PAPER_MODE)
            time.sleep(0.3)

        elif sig is None:
            # ── 觀察名單（多頭為主）──────────────────────────
            w = _watchlist_check(d1)
            if w:
                w["sym"] = sym
                watchlist.append(w)

    # 觀察名單排序：State B 優先，再按接近程度
    watchlist.sort(key=lambda x: (0 if x["state"] == "B" else 1, x.get("gap_ema", 0)))

    # 只推送「新進入」觀察名單的幣對（每幣只推一次，離開後重置）
    prev_sent    = set(state.get("watchlist_sent", []))
    current_syms = {w["sym"] for w in watchlist}
    new_items    = [w for w in watchlist if w["sym"] not in prev_sent]

    if new_items:
        log.info(f"觀察名單新增 {len(new_items)} 個: {[w['sym'].split('/')[0] for w in new_items]}")
        tg.on_watchlist(new_items, PAPER_MODE)
    else:
        log.info(f"觀察名單無新增（目前追蹤 {len(current_syms)} 個）")

    # 更新已推送清單：只保留仍在觀察名單的幣（離開後自動重置，下次重新進入會再推）
    state["watchlist_sent"] = list(current_syms)

    state["capital"]  = capital
    state["last_run"] = str(now)
    save_state(state)

    # ── 5. 狀態通知（僅在排程器認為到時間才送）────────────────
    open_pnl = 0.0
    for p in positions.values():
        cur = p.get("cur_px", p["entry_px"])
        if p.get("side", "long") == "long":
            open_pnl += (cur - p["entry_px"]) / p["entry_px"] * p["notional"]
        else:
            open_pnl += (p["entry_px"] - cur) / p["entry_px"] * p["notional"]
    if send_status:
        tg.on_hourly_status(capital, positions, open_pnl, PAPER_MODE)
    log.info(f"Done  capital={capital:.2f}  pos={len(positions)}  open_pnl={open_pnl:+.2f}")


def _trade_record(sym: str, pos: dict, exit_px: float,
                  net: float, reason: str, ts) -> dict:
    return {
        "sym"      : sym,
        "entry_px" : pos["entry_px"],
        "exit_px"  : exit_px,
        "pnl"      : net,
        "reason"   : reason,
        "entry_ts" : pos["entry_ts"],
        "exit_ts"  : str(ts),
    }


# ═══════════════════════════════════════════════════════════════
# TELEGRAM COMMAND LISTENER — 回覆 /持倉 /盈虧 /狀態
# ═══════════════════════════════════════════════════════════════
import threading

def _cmd_listener(tg: "Telegram") -> None:
    """背景執行緒：輪詢 Telegram 訊息，回覆指令"""
    log.info("cmd_listener thread started")
    url_get = f"https://api.telegram.org/bot{tg.token}/getUpdates"

    # 啟動時跳過所有舊的 pending updates，避免處理過期指令
    offset = 0
    try:
        r = requests.get(url_get, params={"offset": -1, "timeout": 0}, timeout=10)
        if r.ok:
            results = r.json().get("result", [])
            if results:
                offset = results[-1]["update_id"] + 1
                log.info(f"cmd_listener: 跳過 pending updates，初始 offset={offset}")
    except Exception as e:
        log.warning(f"cmd_listener init offset failed: {e}")

    while True:
        try:
            r = requests.get(url_get, params={"offset": offset, "timeout": 30}, timeout=40)
            if not r.ok:
                log.warning(f"getUpdates HTTP {r.status_code}")
                time.sleep(5)
                continue
            for upd in r.json().get("result", []):
                offset = upd["update_id"] + 1
                msg = upd.get("message", {})
                text = msg.get("text", "").strip()
                cid  = str(msg.get("chat", {}).get("id", ""))
                if cid != str(tg.chat_id):
                    continue

                state = load_state()
                positions = state.get("positions", {})
                capital   = state.get("capital", 0)

                # ── 讀取 NFES Bot 狀態（合併顯示）────────────────
                nfes_state     = {}
                nfes_positions = {}
                nfes_capital   = 0.0
                try:
                    nfes_path = Path(__file__).parent / "nfes_bot_state.json"
                    if nfes_path.exists():
                        nfes_state     = json.loads(nfes_path.read_text(encoding="utf-8"))
                        nfes_positions = nfes_state.get("positions", {})
                        nfes_capital   = nfes_state.get("capital", 0.0) or 0.0
                except Exception as _ne:
                    log.warning(f"讀取 nfes_bot_state.json 失敗: {_ne}")

                def _pos_upnl(p):
                    """計算持倉未實現盈虧（支援多/空）"""
                    cur  = p.get("cur_px", p["entry_px"])
                    side = p.get("side", "long")
                    if side == "long":
                        return (cur - p["entry_px"]) / p["entry_px"] * p["notional"]
                    else:
                        return (p["entry_px"] - cur) / p["entry_px"] * p["notional"]

                if text in ("/持倉", "/positions"):
                    total_pos = len(positions) + len(nfes_positions)
                    if total_pos == 0:
                        tg.send("目前無持倉")
                    else:
                        lines = ["📋 <b>目前持倉（全策略合計）</b>"]
                        if positions:
                            lines.append("\n🔷 <b>EMA99 Bot</b>")
                            for sym, p in positions.items():
                                cur  = p.get("cur_px", p["entry_px"])
                                side = p.get("side", "long")
                                upnl = _pos_upnl(p)
                                pct  = upnl / p["notional"] * 100
                                side_icon = "🟢" if side == "long" else "🔴"
                                lines.append(
                                    f"\n{side_icon}<code>{sym.split('/')[0]}</code>  {side.upper()}\n"
                                    f"  進場：{p['entry_px']:.4f}  現價：{cur:.4f}\n"
                                    f"  未實現：{upnl:+.2f} USDT ({pct:+.1f}%)\n"
                                    f"  槓桿：{p['lev']}x  止損：{p['sl']:.4f}"
                                )
                        if nfes_positions:
                            lines.append("\n🔶 <b>NFES Signal Bot</b>")
                            for sym, p in nfes_positions.items():
                                cur  = p.get("cur_px", p["entry_px"])
                                side = p.get("side", "long")
                                upnl = _pos_upnl(p)
                                pct  = upnl / p["notional"] * 100
                                side_icon = "🟢" if side == "long" else "🔴"
                                lines.append(
                                    f"\n{side_icon}<code>{sym.split('/')[0]}</code>  {side.upper()}\n"
                                    f"  進場：{p['entry_px']:.4f}  現價：{cur:.4f}\n"
                                    f"  未實現：{upnl:+.2f} USDT ({pct:+.1f}%)\n"
                                    f"  槓桿：{p['lev']}x  止損：{p['sl']:.4f}"
                                )
                        # 合計未實現
                        all_upnl = sum(_pos_upnl(p) for p in positions.values()) \
                                 + sum(_pos_upnl(p) for p in nfes_positions.values())
                        lines.append(f"\n─ 合計未實現：{all_upnl:+.2f} USDT  共 {total_pos} 倉 ─")
                        tg.send("\n".join(lines))

                elif text in ("/盈虧", "/pnl"):
                    # EMA99
                    trades_e   = state.get("trades", [])
                    realized_e = sum(t["pnl"] for t in trades_e)
                    open_e     = sum(_pos_upnl(p) for p in positions.values())
                    # NFES
                    trades_n   = nfes_state.get("trades", [])
                    realized_n = sum(t["pnl"] for t in trades_n)
                    open_n     = sum(_pos_upnl(p) for p in nfes_positions.values())
                    # 合計
                    realized_all = realized_e + realized_n
                    open_all     = open_e + open_n
                    capital_all  = capital + nfes_capital
                    total_all    = capital_all + open_all
                    ret_all      = (total_all - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100 if INITIAL_CAPITAL else 0
                    tg.send(
                        f"💰 <b>盈虧摘要（全策略合計）</b>\n"
                        f"已實現：{realized_all:+.2f} USDT\n"
                        f"未實現：{open_all:+.2f} USDT\n"
                        f"可用資金：{capital_all:.2f} USDT\n"
                        f"淨值：{total_all:.2f} USDT  <i>({ret_all:+.1f}%)</i>\n"
                        f"交易次數：{len(trades_e)+len(trades_n)}  "
                        f"（EMA99:{len(trades_e)} / NFES:{len(trades_n)}）\n"
                        f"─\n"
                        f"🔷 EMA99  已實現 {realized_e:+.2f}  未實現 {open_e:+.2f}  持倉 {len(positions)}\n"
                        f"🔶 NFES   已實現 {realized_n:+.2f}  未實現 {open_n:+.2f}  持倉 {len(nfes_positions)}"
                    )

                elif text in ("/狀態", "/status"):
                    open_e   = sum(_pos_upnl(p) for p in positions.values())
                    open_n   = sum(_pos_upnl(p) for p in nfes_positions.values())
                    open_all = open_e + open_n
                    cap_all  = capital + nfes_capital
                    total_all = cap_all + open_all
                    ret_all   = (total_all - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100 if INITIAL_CAPITAL else 0
                    mode      = "📄 虛擬盤" if PAPER_MODE else "🔴 實盤"
                    lines = [
                        f"📊 <b>狀態報告（全策略）</b>  {mode}",
                        f"時間：{_now()}",
                        f"可用資金：{cap_all:.2f} USDT",
                        f"未實現盈虧：{open_all:+.2f} USDT",
                        f"淨值：{total_all:.2f}  <i>({ret_all:+.1f}%)</i>",
                        f"持倉：{len(positions)+len(nfes_positions)} 倉",
                    ]
                    if positions:
                        lines.append("─ 🔷 EMA99 ─")
                        for sym, p in positions.items():
                            side_icon = "🟢" if p.get("side","long") == "long" else "🔴"
                            pct = _pos_upnl(p) / p["notional"] * 100
                            lines.append(f"  {side_icon}{sym.split('/')[0]}: {pct:+.1f}%  入{p['entry_px']:.4f}")
                    if nfes_positions:
                        lines.append("─ 🔶 NFES ─")
                        for sym, p in nfes_positions.items():
                            side_icon = "🟢" if p.get("side","long") == "long" else "🔴"
                            pct = _pos_upnl(p) / p["notional"] * 100
                            lines.append(f"  {side_icon}{sym.split('/')[0]}: {pct:+.1f}%  入{p['entry_px']:.4f}")
                    tg.send("\n".join(lines))

                elif text in ("/觀察", "/watchlist"):
                    tg.send("🔄 正在掃描觀察名單，請稍候...")
                    try:
                        _exch = get_exchange()
                        syms  = get_top_symbols(_exch, TOP_N)
                        wl: list[dict] = []
                        for s in syms:
                            if s in positions:
                                continue
                            d4t = fetch_ohlcv(_exch, s, "1h", limit=150)
                            if d4t.empty:
                                continue
                            d4t = add_4h_ind(d4t)
                            if not _4h_ok(d4t):
                                continue
                            d1t = fetch_ohlcv(_exch, s, "15m", limit=250)
                            if d1t.empty:
                                continue
                            d1t = add_1h_ind(d1t)
                            w   = _watchlist_check(d1t)
                            if w:
                                w["sym"] = s
                                wl.append(w)
                        wl.sort(key=lambda x: (0 if x["state"] == "B" else 1, x.get("gap_ema", 0)))
                        if wl:
                            tg.on_watchlist(wl, PAPER_MODE)
                        else:
                            tg.send("👀 目前無符合觀察條件的幣對")
                    except Exception as we:
                        tg.send(f"⚠️ 觀察名單掃描失敗: {we}")

                elif text and text.split()[0] in ("/歷史", "/history"):
                    parts  = text.split()
                    try:
                        n = int(parts[1]) if len(parts) > 1 else 10
                    except ValueError:
                        n = 10
                    n = max(1, min(n, 50))
                    # 合併兩個 bot 的交易記錄，加上來源標籤後按時間排序
                    trades_e = [dict(t, _bot="E") for t in state.get("trades", [])]
                    trades_n = [dict(t, _bot="N") for t in nfes_state.get("trades", [])]
                    all_trades = sorted(
                        trades_e + trades_n,
                        key=lambda t: str(t.get("exit_ts", "")),
                    )
                    if not all_trades:
                        tg.send("尚無歷史交易紀錄")
                    else:
                        recent   = all_trades[-n:][::-1]
                        realized = sum(t["pnl"] for t in all_trades)
                        wins     = sum(1 for t in all_trades if t["pnl"] > 0)
                        wr       = wins / len(all_trades) * 100
                        lines = [f"📜 <b>歷史訂單（最近 {len(recent)} / 共 {len(all_trades)} 筆）</b>"]
                        for t in recent:
                            sym  = t.get("sym", "?").split("/")[0]
                            ep   = t.get("entry_px", 0)
                            xp   = t.get("exit_px", 0)
                            pnl  = t.get("pnl", 0)
                            pct  = (xp - ep) / ep * 100 if ep else 0
                            rsn  = t.get("reason", "")
                            ts   = str(t.get("exit_ts", ""))[:16]
                            emo  = "✅" if pnl > 0 else "❌"
                            bot_tag = "🔷" if t.get("_bot") == "E" else "🔶"
                            lines.append(
                                f"\n{emo}{bot_tag}<code>{sym}</code>  {ts}\n"
                                f"  {ep:.4f} → {xp:.4f}  ({pct:+.1f}%)\n"
                                f"  PnL：{pnl:+.2f} USDT  [{rsn}]"
                            )
                        lines.append(
                            f"\n— — —\n"
                            f"總實現：{realized:+.2f} USDT\n"
                            f"勝率：{wr:.1f}% ({wins}/{len(all_trades)})  "
                            f"（🔷EMA99:{len(trades_e)} / 🔶NFES:{len(trades_n)}）"
                        )
                        tg.send("\n".join(lines))

                elif text.lower() in ("/help", "/說明"):
                    tg.send(
                        "📖 <b>可用指令</b>\n"
                        "/持倉 — 顯示目前所有持倉\n"
                        "/盈虧 — 顯示損益摘要\n"
                        "/狀態 — 完整狀態報告\n"
                        "/觀察 — 即時掃描觀察名單（即將突破）\n"
                        "/歷史 [N] — 顯示最近 N 筆歷史訂單（預設 10，最多 50）"
                    )

        except Exception as e:
            log.warning(f"cmd_listener error: {e}")
            time.sleep(5)


def _cmd_listener_supervised(tg: "Telegram") -> None:
    """守護 cmd_listener:崩潰自動重啟"""
    while True:
        try:
            _cmd_listener(tg)
        except Exception as e:
            log.exception(f"cmd_listener crashed, restarting in 10s: {e}")
            time.sleep(10)


# ═══════════════════════════════════════════════════════════════
# SCHEDULER — 每 SCAN_INTERVAL_SEC 秒掃一次，立即下單
# ═══════════════════════════════════════════════════════════════
def main() -> None:
    state           = load_state()
    tg              = Telegram(TG_TOKEN, TG_CHAT_ID)
    last_status_ts  = None   # 上次發狀態報告的時間

    # 啟動指令監聽執行緒（含崩潰重啟）
    t = threading.Thread(target=_cmd_listener_supervised, args=(tg,), daemon=True)
    t.start()
    log.info("cmd_listener supervisor thread started")

    tg.on_start(PAPER_MODE)
    log.info("=" * 60)
    log.info(f"  EMA99 Bot 啟動  PAPER={PAPER_MODE}")
    log.info(f"  掃描間隔：{SCAN_INTERVAL_SEC // 60} 分鐘")
    log.info(f"  capital={state['capital']:.2f}  pos={len(state['positions'])}")
    log.info("=" * 60)

    while True:
        now = datetime.now(timezone.utc)

        # 判斷是否要發狀態報告（第一次固定發，之後每 STATUS_INTERVAL_HR 小時一次）
        send_status = (
            last_status_ts is None or
            (now - last_status_ts).total_seconds() >= STATUS_INTERVAL_HR * 3600
        )

        try:
            # ── 狀態快取：不每分鐘打 JSONBin ────────────────────
            # 只有在 state 尚未載入（啟動/崩潰重啟）時才從雲端拉取；
            # 正常迴圈使用 run_once 回傳的 in-memory state 即可。
            run_once(get_exchange(), tg, state, send_status=send_status)
            if send_status:
                last_status_ts = now
        except Exception as e:
            log.exception(f"run_once error: {e}")
            tg.on_error(str(e))
            # 發生例外時重新從雲端載入，確保狀態一致
            state = load_state()

        log.info(f"等待 {SCAN_INTERVAL_SEC // 60} 分鐘後下次掃描...")
        time.sleep(SCAN_INTERVAL_SEC)


if __name__ == "__main__":
    main()
