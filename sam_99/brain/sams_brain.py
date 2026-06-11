"""
sam'sBrain v2 — 主力思維版
================================================
靈魂來源：原主 5 年 2140 YouTube 直播交易邏輯

核心原則（絕不繞過）：
  1. 主力思維優先 — 永遠問「主力在幹嘛？」
  2. 假突破確認 — 第一根突破可能是洗盤，等確認
  3. 寧願錯過不做錯 — 貪心就不適合做短線
  4. 每個幣看自己的歷史 — 不同幣行為不同
  5. 流動性判斷 — 市場去流動性集中的地方
  6. 人性不變 — 散戶貪婪/恐懼就是你的機會
  7. 多時間框架 — 日線方向、4H結構、1H進場
  8. 進場門檻 75 分以上才動手

- 虛擬本金：1,000 USDT
- 風險：1R = 本金 × 1%（10U）
- 槓桿：sam 自主判斷 1~5x
- 分段TP：TP1=2R出33%→移保本，TP2=3.5R出50%→移TP1，TP3=5.5R全出
- supervisord 管理，每 5 分鐘掃描
"""

import json
import time
import uuid
import asyncio
import logging
import sys
from datetime import datetime, timezone, timedelta
TZ8 = timezone(timedelta(hours=8))
from pathlib import Path

import httpx
import yaml

try:
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).parent.parent))
    from chart_screenshot import capture_entry_chart as _capture_chart
except Exception:
    def _capture_chart(*a, **kw): pass

# ─────────────────────────────────────────────
# 設定
# ─────────────────────────────────────────────
BASE_DIR        = Path(__file__).parent
STATE_FILE      = BASE_DIR / "sams_brain_state.json"
TRADE_LOG_FILE  = BASE_DIR / "sams_brain_trades.json"
REFLECTION_DIR  = BASE_DIR / "sams_brain_reflections"
PERSONA_FILE    = BASE_DIR / "trading_persona.yaml"
BRAIN_ASK_URL   = "http://localhost:8766/api/brain/ask"
SCAN_INTERVAL        = 300        # 5 分鐘
DAILY_API_CAP        = 60         # 每天最多 60 次 Claude 呼叫（硬性上限，超過當天靜音）
INITIAL_CAPITAL      = 1_000.0
RISK_PER_TRADE       = 0.01       # 1R = 1%
MIN_SCORE            = 75         # 原主靈魂：低於75分不動手
MAX_INTRADAY_PER_DAY = 3          # 日內單每日最多 3 筆，避免過度交易
INTRADAY_BAR_EXIT    = False      # 日內3根K棒強制離場（False = 關閉，改用其他出場策略）
ENABLE_INTRADAY      = False      # 日內單開關（False = 停用，有明確方向再手動開）
MIN_SCORE_INTRADAY   = 90         # 日內單專用門檻：需 90 分以上才算「非常明確方向」
INTRADAY_COOLDOWN    = 1800       # 同一幣 30 分鐘內不重複問 LLM（節省 token）
_intraday_last_scan: dict = {}    # {symbol: timestamp}  冷卻記錄（記憶體）
_LLM_FAIL_COUNT: int = 0          # 連續 LLM 失敗次數
_LLM_ALERTED:    bool = False     # 已發過告警，避免重複
_BRAIN_SERVER_PATH = Path(__file__).parent / "brain_server.py"
INTRADAY_SYMBOLS     = [          # 日內掃描幣種（流動性好的主流幣）
    "BTCUSDT","ETHUSDT","SOLUSDT","BNBUSDT","XRPUSDT",
    "ADAUSDT","DOGEUSDT","AVAXUSDT","DOTUSDT","LINKUSDT",
]

# TG
TG_TOKEN = "8779609140:AAHGfIR0hOL_I12NATRuiKlftuTuUvqzeYk"
TG_CHAT  = "1768177615"
TG_URL   = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"

async def _tg(msg: str):
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            await c.post(TG_URL, json={"chat_id": TG_CHAT, "text": msg, "parse_mode": "HTML"})
    except Exception as e:
        log.warning(f"[TG] 通知失敗: {e}")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [SAM_v2] %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(BASE_DIR / "sams_brain.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("sams_brain_v2")
REFLECTION_DIR.mkdir(exist_ok=True)
THINKING_FILE = BASE_DIR / "sams_thinking_log.json"

# ─────────────────────────────────────────────
# 知識庫接入（2140 原主知識）
# ─────────────────────────────────────────────
sys.path.insert(0, str(BASE_DIR))
try:
    from knowledge_bridge import retrieve, format_for_prompt
    KNOWLEDGE_AVAILABLE = True
    log.info("[KNOWLEDGE] 2140 知識庫已接入")
except Exception as e:
    KNOWLEDGE_AVAILABLE = False
    log.warning(f"[KNOWLEDGE] 知識庫載入失敗: {e}")

def _fmt_pattern(p: dict) -> str:
    """將形態識別結果格式化為可讀字串，傳給大腦"""
    if not p or not p.get("detected"):
        return "未識別"
    pt = p.get("pattern", "")
    if pt in ("bull_ABCD", "bear_ABCD"):
        direction = "多頭" if pt == "bull_ABCD" else "空頭"
        return (f"✅ {direction}AB=CD  A={p.get('A')} B={p.get('B')} C={p.get('C')}  "
                f"保守目標(0.786)={p.get('target_conservative')}  "
                f"標準目標(1.0)={p.get('target_standard')}  "
                f"{'⚠️ 已進入目標區' if p.get('in_target_zone') else ''}")
    if pt in ("symmetric", "ascending", "descending"):
        apex = "⚡ 接近頂點即將表態！" if p.get("near_apex") else f"壓縮{p.get('compression_pct')}%"
        return (f"✅ {pt}三角收斂  {apex}  "
                f"中軸={p.get('midline')}  "
                f"突破目標↑={p.get('breakout_target_up')}  ↓={p.get('breakout_target_down')}")
    if pt in ("HS_bottom", "HS_top"):
        label = "頭肩底" if pt == "HS_bottom" else "頭肩頂"
        broke = "✅ 已突破頸線" if p.get("breakout") else "⏳ 等待突破頸線"
        return (f"✅ {label}  頸線={p.get('neckline')}  目標={p.get('target')}  "
                f"止損={p.get('sl_level')}  {broke}")
    if pt in ("W_bottom", "M_top"):
        label = "W底（投間底）" if pt == "W_bottom" else "M頭（雙頂）"
        broke = "✅ 已突破頸線" if p.get("breakout") else "⏳ 等待突破頸線"
        return (f"✅ {label}  頸線={p.get('neckline')}  目標={p.get('target')}  "
                f"止損={p.get('sl_level')}  {broke}")
    return str(p)

def _get_knowledge(query: str, top_k: int = 4) -> str:
    if not KNOWLEDGE_AVAILABLE:
        return ""
    try:
        docs = retrieve(query, top_k=top_k)
        return format_for_prompt(docs)
    except Exception:
        return ""

# ─────────────────────────────────────────────
# 狀態管理
# ─────────────────────────────────────────────
def load_state() -> dict:
    default = {
        "name":        "sam'sBrain v2",
        "capital":     INITIAL_CAPITAL,
        "equity":      INITIAL_CAPITAL,
        "positions":   {},
        "watchlist":   {},   # 60~74分的幣放這裡等待更好進場機會
        "total_trades": 0,
        "wins":        0,
        "losses":      0,
        "max_drawdown": 0.0,
        "peak_equity": INITIAL_CAPITAL,
        "started_at":  datetime.now(TZ8).isoformat(),
        "last_scan":   None,
        "brain_version": "v2_soul_transplant",
        "loss_streak":  0,           # 連續虧損計數（連虧3筆觸發冷靜）
        "entry_pause_until": 0,      # 開新倉冷靜期截止時間戳
    }
    if STATE_FILE.exists():
        saved = json.loads(STATE_FILE.read_text())
        default.update(saved)
    return default

def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))

def load_trades() -> list:
    return json.loads(TRADE_LOG_FILE.read_text()) if TRADE_LOG_FILE.exists() else []

def save_trade(trade: dict):
    trades = load_trades()
    trades.append(trade)
    TRADE_LOG_FILE.write_text(json.dumps(trades, ensure_ascii=False, indent=2))

# ─────────────────────────────────────────────
# 市場快照 — 多時間框架 + 市場結構 + 流動性
# ─────────────────────────────────────────────
SAM_WATCHLIST: list[str] = []

# ── 訊號記憶：防止同一幣同一訊號重複打 API ─────────────────────────
# {symbol: {"reason": str, "ts": float, "approved": bool}}
_SIGNAL_MEMORY: dict[str, dict] = {}
SIGNAL_COOLDOWN_SEC = 1800  # 同理由30分鐘內不重複問（橫盤洗盤保護）

# 手動加入名單：市值排名在 300 外但 Sam 有興趣的幣（CoinGecko 代號與幣安不符的也在這）
MANUAL_WHITELIST: list[str] = [
    "PHAROSUSDT",   # Pharos 市值~$258M，CoinGecko 代號 PROS，排名 #309
]

async def _refresh_watchlist() -> list[str]:
    """
    市值前 300 加密貨幣（CoinGecko）× Binance 合約可交易。
    - 排除穩定幣、黃金代幣、非加密資產（美股ETF、貴金屬等）
    - 用市值排名，不用成交量（成交量前100容易被短期炒作幣洗盤）
    """
    # 非加密貨幣 / 穩定幣 / 黃金代幣黑名單（用 CoinGecko symbol，不含 USDT 後綴）
    EXCLUDE = {
        # 穩定幣
        "USDT","USDC","BUSD","DAI","FDUSD","TUSD","PYUSD","USDS","USDE",
        "SUSDE","USDB","USDP","FRAX","STABLE","USDD","CRVUSD","EURC","GHO",
        # 黃金/銀/商品代幣
        "XAUT","PAXG","XAG","XAU","NATGAS",
        # Wrapped/staked 衍生品（非原生幣）
        "WBTC","WETH","STETH","WSTETH","CBBTC","BTCB",
        # 其他特殊幣（Gas 代幣、槓桿 ETF 等）
        "GWEI","SOXL","TSLA","NVDA","AMD","INTC","QQQ",
        "MSOL","JITOSOL","BNSOL",  # staked SOL
    }

    fallback = ["BTCUSDT","ETHUSDT","SOLUSDT","BNBUSDT","XRPUSDT",
                "DOGEUSDT","AVAXUSDT","LINKUSDT","ADAUSDT","DOTUSDT"]

    async with httpx.AsyncClient(timeout=15) as c:
        # 1. 取 CoinGecko 市值前200（免費，無需 API Key）
        try:
            # 市值前300：分兩頁抓（CoinGecko 單頁上限250）
            cg_r1 = await c.get(
                "https://api.coingecko.com/api/v3/coins/markets",
                params={"vs_currency":"usd","order":"market_cap_desc",
                        "per_page":250,"page":1,"sparkline":"false"}
            )
            cg_r2 = await c.get(
                "https://api.coingecko.com/api/v3/coins/markets",
                params={"vs_currency":"usd","order":"market_cap_desc",
                        "per_page":250,"page":2,"sparkline":"false"}
            )
            cg_page2 = cg_r2.json() if cg_r2.status_code == 200 else []
            cg_r = type('R', (), {'json': lambda self: cg_r1.json() + cg_page2[:50]})()  # 250+50=300
            cg_coins = cg_r.json()
        except Exception as e:
            log.warning(f"[WATCHLIST] CoinGecko 失敗: {e}，使用備援名單")
            return fallback

        # 2. 取 Binance Futures 目前可交易的合約
        try:
            ei_r = await c.get("https://fapi.binance.com/fapi/v1/exchangeInfo", timeout=10)
            futures_base = {
                s["baseAsset"].upper()
                for s in ei_r.json()["symbols"]
                if s["quoteAsset"] == "USDT" and s["status"] == "TRADING"
            }
        except Exception as e:
            log.warning(f"[WATCHLIST] Binance exchangeInfo 失敗: {e}，使用備援名單")
            return fallback

    # 3. 交叉過濾
    result = []
    for coin in cg_coins:
        sym = coin["symbol"].upper()
        if sym in EXCLUDE:
            continue
        # 過濾非 ASCII 符號（排除中文名稱幣種等異常資料）
        if not sym.isascii() or not sym.isalnum():
            continue
        # 支援 1000x 合約（如 PEPE → 1000PEPE）
        for candidate in [sym, "1000" + sym]:
            if candidate in futures_base:
                result.append(candidate + "USDT")
                break

    log.info(f"[WATCHLIST] 市值前200純加密幣：共 {len(result)} 個，"
             f"前5：{result[:5]}")
    # 合併手動白名單（去重）
    for sym in MANUAL_WHITELIST:
        if sym not in result:
            result.append(sym)
    log.info(f"[WATCHLIST] 含手動白名單共 {len(result)} 個")
    return result if result else fallback

def _calc_ema(data: list, period: int) -> list:
    k = 2 / (period + 1)
    result = [data[0]]
    for v in data[1:]:
        result.append(v * k + result[-1] * (1 - k))
    return result

def _calc_rsi(closes: list, period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i-1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - 100 / (1 + rs), 2)


def _rsi_series(closes: list, period: int = 14) -> list:
    """逐根計算 RSI 序列（rsi_series[i] 對應 closes[i+period]）"""
    out = []
    for i in range(period, len(closes)):
        out.append(_calc_rsi(closes[:i + 1], period))
    return out


def _detect_continuous_divergence(closes: list, highs: list, lows: list,
                                  period: int = 14) -> dict:
    """
    Sam 真正的吸籌/出貨訊號 —— 連續背離（不是單張快照背離）。

    連續底背離（主力吸籌完成）：
      K 線低點越來越低（price_low 遞減），但 RSI 低點越墊越高（rsi_low 遞增）
      → 賣壓衰竭，主力在底部接貨。OM / ZEC / DASH 劇本核心。
    連續頂背離（主力出貨）：
      K 線高點越來越高，但 RSI 高點越來越低 → 動能衰竭。

    需至少 3 個擺動低/高點呈一致背離，強度遠高於單一時框快照背離。
    """
    out = {"bull": False, "bear": False, "bull_strength": 0, "bear_strength": 0}
    if len(closes) < 50:
        return out
    rsi_s = _rsi_series(closes, period)
    if len(rsi_s) < 30:
        return out
    off = period  # rsi_s[j] 對應 K 棒 index j+off

    def swings(vals, find_low=True, order=3):
        pts = []
        for i in range(order, len(vals) - order):
            window = vals[i - order:i + order + 1]
            if find_low and vals[i] == min(window):
                pts.append(i)
            elif (not find_low) and vals[i] == max(window):
                pts.append(i)
        return [i for i in pts if i - off >= 0]

    low_idx = swings(lows, find_low=True)
    if len(low_idx) >= 3:
        last3 = low_idx[-3:]
        p = [lows[i] for i in last3]
        r = [rsi_s[i - off] for i in last3]
        if p[0] > p[1] > p[2] and r[0] < r[1] < r[2]:
            out["bull"] = True
            out["bull_strength"] = round(r[2] - r[0], 1)

    high_idx = swings(highs, find_low=False)
    if len(high_idx) >= 3:
        last3 = high_idx[-3:]
        p = [highs[i] for i in last3]
        r = [rsi_s[i - off] for i in last3]
        if p[0] < p[1] < p[2] and r[0] > r[1] > r[2]:
            out["bear"] = True
            out["bear_strength"] = round(r[0] - r[2], 1)

    return out


def _calc_macd(closes: list, fast: int = 12, slow: int = 26, signal: int = 9) -> dict:
    """
    MACD — Sam 把它當動能背書（輔助確認，不是決策依據，與RSI同層）。
    用法：histogram 翻正/翻負（金叉/死叉）、是否在零軸上方（多頭區/空頭區）。
    """
    out = {"macd": 0, "signal": 0, "hist": 0, "cross": "none", "above_zero": False}
    if len(closes) < slow + signal + 1:
        return out
    ema_f = _calc_ema(closes, fast)
    ema_s = _calc_ema(closes, slow)
    macd_line = [ema_f[i] - ema_s[i] for i in range(len(closes))]
    sig_line  = _calc_ema(macd_line, signal)
    hist = [macd_line[i] - sig_line[i] for i in range(len(closes))]
    out["macd"]       = round(macd_line[-1], 8)
    out["signal"]     = round(sig_line[-1], 8)
    out["hist"]       = round(hist[-1], 8)
    out["above_zero"] = macd_line[-1] > 0
    if len(hist) >= 2:
        if hist[-2] <= 0 < hist[-1]:
            out["cross"] = "bull"   # 金叉
        elif hist[-2] >= 0 > hist[-1]:
            out["cross"] = "bear"   # 死叉
    return out


def _calc_fib(highs: list, lows: list, closes: list, lookback: int = 60) -> dict:
    """
    費波那契回調（Sam 8.7% 高頻工具）：
      回調到 0.618~0.786『黃金口袋』量縮守穩 → 右側進場區
      延伸 1.618 / 2.618 為目標。從近段主擺動（最高高點↔最低低點）量測。
    """
    out = {"in_golden_zone": False, "nearest": "", "swing_dir": "", "golden_lo": 0, "golden_hi": 0}
    n = len(closes)
    if n < 20:
        return out
    lb = min(lookback, n)
    seg_h = highs[-lb:]; seg_l = lows[-lb:]
    hi = max(seg_h); lo = min(seg_l)
    if hi <= lo:
        return out
    price = closes[-1]
    up = seg_l.index(lo) < seg_h.index(hi)   # 低點先出現=上升段
    diff = hi - lo
    levels = ({f: hi - diff * f for f in (0.382, 0.5, 0.618, 0.786)} if up
              else {f: lo + diff * f for f in (0.382, 0.5, 0.618, 0.786)})
    out["swing_dir"] = "up" if up else "down"
    g_lo = min(levels[0.618], levels[0.786]); g_hi = max(levels[0.618], levels[0.786])
    out["golden_lo"] = round(g_lo, 8); out["golden_hi"] = round(g_hi, 8)
    out["in_golden_zone"] = g_lo <= price <= g_hi
    nl = min(levels.items(), key=lambda kv: abs(kv[1] - price))
    out["nearest"] = f"{nl[0]}={nl[1]:.6g}"
    return out


def _detect_cup_handle(highs: list, lows: list, closes: list) -> dict:
    """杯柄（564次）：圓底兩側杯緣等高，右側回後接近杯緣形成柄。目標=杯深等幅上投。"""
    out = {"detected": False, "pattern": "", "rim": 0, "target": 0, "breakout": False}
    if len(closes) < 40:
        return out
    seg = closes[-40:]
    left, mid, right = seg[:8], seg[8:32], seg[32:]
    left_rim = max(left); cup_bottom = min(mid); right_rim = max(right); now = seg[-1]
    depth = left_rim - cup_bottom
    if depth <= 0:
        return out
    if (cup_bottom < left_rim * 0.97 and abs(right_rim - left_rim) / left_rim < 0.05
            and cup_bottom < now):
        out["detected"] = True
        out["pattern"]  = "cup_handle"
        out["rim"]      = round(left_rim, 8)
        out["target"]   = round(left_rim + depth, 8)
        out["breakout"] = now > left_rim          # 突破杯緣=右側確認
    return out


def _detect_channel(highs: list, lows: list, closes: list, lookback: int = 40) -> dict:
    """通道（721次）：高低點回歸斜率同向且平行。回報方向+價格在通道上/中/下緣。"""
    out = {"detected": False, "type": "", "position": ""}
    if len(closes) < lookback:
        return out
    H = highs[-lookback:]; L = lows[-lookback:]; n = lookback
    xs = list(range(n)); mx = sum(xs) / n
    dx = sum((x - mx) ** 2 for x in xs) or 1e-9

    def slope(ys):
        my = sum(ys) / n
        return sum((xs[i] - mx) * (ys[i] - my) for i in range(n)) / dx

    sh, sl_ = slope(H), slope(L)
    if sh * sl_ > 0 and abs(sh - sl_) / (abs(sh) + 1e-9) < 0.6:
        out["detected"] = True
        out["type"] = "up" if sh > 0 else "down"
        upper, lower, price = H[-1], L[-1], closes[-1]
        rng = upper - lower
        if rng > 0:
            pos = (price - lower) / rng
            out["position"] = "upper" if pos > 0.66 else ("lower" if pos < 0.34 else "mid")
    return out


def _detect_retest_hold(highs: list, lows: list, closes: list, volumes: list,
                        lookback: int = 30) -> dict:
    """
    右側進場核心訊號 —— Sam：「突破後縮量回測守穩才進，不追突破K棒」。
      多：近期突破前段壓力，回踩該位上方守住，且回踩量縮 → 真突破確認。
      空：近期跌破前段支撐，反抽該位下方守住，且反抽量縮 → 真跌破確認。
    這是 Sam 74% 等待確認操作的程式化定義。
    """
    out = {"long": False, "short": False, "level": 0}
    n = len(closes)
    if n < lookback + 5:
        return out
    res = max(highs[-lookback:-5]); sup = min(lows[-lookback:-5])
    recent_high = max(highs[-5:]); recent_low = min(lows[-5:])
    price = closes[-1]
    vol_now = volumes[-1]
    vol_avg = sum(volumes[-lookback:-1]) / (lookback - 1) if lookback > 1 else vol_now
    shrink = vol_now < vol_avg * 0.9
    if recent_high > res and price > res and (price - res) / res < 0.015 and shrink:
        out["long"] = True;  out["level"] = round(res, 8)
    if recent_low < sup and price < sup and (sup - price) / sup < 0.015 and shrink:
        out["short"] = True; out["level"] = round(sup, 8)
    return out

# ─────────────────────────────────────────────
# 原主形態識別引擎
# ─────────────────────────────────────────────

def _find_swing_points(highs: list, lows: list, order: int = 5) -> dict:
    """找關鍵擺動高/低點（zigzag 風格），用於形態識別"""
    n = len(highs)
    swing_highs, swing_lows = [], []
    for i in range(order, n - order):
        if highs[i] == max(highs[i-order:i+order+1]):
            swing_highs.append((i, highs[i]))
        if lows[i] == min(lows[i-order:i+order+1]):
            swing_lows.append((i, lows[i]))
    return {"swing_highs": swing_highs[-6:], "swing_lows": swing_lows[-6:]}

# ─────────────────────────────────────────────
# 形態學白名單（只有這些才畫圖送 Vision）
# ─────────────────────────────────────────────
CHART_PATTERN_WHITELIST = {
    "triangle", "ascending_triangle", "descending_triangle",
    "wedge_up", "wedge_down",
    "H_S_top", "H_S_bottom",
    "W_bottom", "M_top",
    "cup_handle",
}

def _generate_pattern_chart_b64(
    symbol: str,
    opens: list, highs: list, lows: list, closes: list, volumes: list,
    pattern_info: dict,
    rsi_values: list,
    timeframe: str = "1H",
) -> str:
    """
    畫 K 線形態學圖，回傳 base64 PNG 字串。
    只在白名單型態有突破時才呼叫，避免浪費資源。
    """
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.patches as mpatches
        import io, base64
        import numpy as np

        n = min(60, len(closes))
        idx = list(range(n))
        o = opens[-n:]; h = highs[-n:]; l = lows[-n:]
        c = closes[-n:]; v = volumes[-n:]

        fig, (ax1, ax2, ax3) = plt.subplots(3, 1, figsize=(12, 7),
            gridspec_kw={"height_ratios": [3, 1, 1]}, facecolor="#131722")
        for ax in (ax1, ax2, ax3):
            ax.set_facecolor("#131722")
            ax.tick_params(colors="#666", labelsize=7)
            ax.spines[:].set_color("#2a2e39")

        # ── K 線 ──
        for i in idx:
            color = "#26a69a" if c[i] >= o[i] else "#ef5350"
            ax1.plot([i, i], [l[i], h[i]], color=color, linewidth=0.8)
            body_h = abs(c[i] - o[i]) or (h[i] - l[i]) * 0.001
            ax1.bar(i, body_h, bottom=min(o[i], c[i]),
                    color=color, width=0.7, linewidth=0)

        # ── 型態標注 ──
        pat = pattern_info.get("pattern", "")
        neckline = pattern_info.get("neckline")
        target   = pattern_info.get("target")
        sl_level = pattern_info.get("sl_level")

        if neckline:
            ax1.axhline(neckline, color="#FFC312", linestyle="--",
                        linewidth=1.2, alpha=0.9)
            ax1.text(n - 1, neckline, f" 頸線 {neckline:.4f}",
                     color="#FFC312", fontsize=7, va="bottom")
        if target:
            ax1.axhline(target, color="#FF6B6B", linestyle=":",
                        linewidth=1, alpha=0.8)
            ax1.text(n - 1, target, f" 目標 {target:.4f}",
                     color="#FF6B6B", fontsize=7, va="bottom")
        if sl_level:
            ax1.axhline(sl_level, color="#48dbfb", linestyle=":",
                        linewidth=1, alpha=0.8)
            ax1.text(n - 1, sl_level, f" SL {sl_level:.4f}",
                     color="#48dbfb", fontsize=7, va="bottom")

        ax1.set_title(f"{symbol}  {timeframe}  |  偵測型態: {pat}",
                      color="white", fontsize=10, pad=4)
        ax1.set_xlim(-1, n + 2)

        # ── 成交量 ──
        for i in idx:
            color = "#26a69a" if c[i] >= o[i] else "#ef5350"
            ax2.bar(i, v[i], color=color, width=0.7, alpha=0.7)
        ax2.set_ylabel("Vol", color="#666", fontsize=7)
        ax2.set_xlim(-1, n + 2)

        # ── RSI ──
        rsi_plot = rsi_values[-(n):]
        ax3.plot(range(len(rsi_plot)), rsi_plot, color="#a29bfe", linewidth=1)
        ax3.axhline(70, color="#FF6B6B", linestyle=":", linewidth=0.8, alpha=0.6)
        ax3.axhline(50, color="#888",    linestyle="--", linewidth=0.8)
        ax3.axhline(30, color="#1dd1a1", linestyle=":", linewidth=0.8, alpha=0.6)
        ax3.set_ylim(10, 90)
        ax3.set_ylabel("RSI", color="#666", fontsize=7)
        ax3.set_xlim(-1, n + 2)
        if rsi_plot:
            ax3.text(len(rsi_plot) - 1, rsi_plot[-1],
                     f" {rsi_plot[-1]:.1f}", color="#a29bfe", fontsize=7)

        plt.tight_layout(pad=0.5)

        buf = io.BytesIO()
        plt.savefig(buf, format="png", dpi=100, bbox_inches="tight",
                    facecolor="#131722")
        plt.close(fig)
        buf.seek(0)
        return base64.b64encode(buf.read()).decode()

    except Exception as e:
        log.warning(f"[CHART] 畫圖失敗: {e}")
        return ""


def _get_breakout_pattern(market: dict) -> tuple[dict, str]:
    """
    從 market dict 找出白名單內有突破/跌破的型態。
    回傳 (pattern_dict, pattern_key)，找不到回傳 ({}, "")
    """
    checks = [
        ("pattern_triangle",    "triangle"),
        ("pattern_hs",          None),   # pattern 名稱在 dict 內
        ("pattern_wm",          None),
        ("pattern_triangle_15m","triangle"),
        ("pattern_hs_15m",      None),
        ("pattern_wm_15m",      None),
    ]
    for key, default_pat in checks:
        p = market.get(key, {})
        if not p or not p.get("detected"):
            continue
        pat_name = p.get("pattern", default_pat or "")
        if pat_name not in CHART_PATTERN_WHITELIST:
            continue
        if p.get("breakout"):   # 已突破/跌破關鍵位
            return p, key
    return {}, ""


def _detect_abcd(closes: list, highs: list, lows: list) -> dict:
    """
    AB=CD 等幅識別（原主最常用的目標價計算法）
    邏輯：找近期的 A（起點）→ B（高/低點）→ C（回調點）
    估算 D = C + (B - A)，保守目標 0.786*(B-A) + C
    """
    swings = _find_swing_points(highs, lows, order=4)
    sh = swings["swing_highs"]
    sl = swings["swing_lows"]
    result = {"detected": False, "pattern": "", "target_conservative": 0, "target_standard": 0}

    # 多頭 AB=CD：低點 A → 高點 B → 回調低點 C → 估算 D（目標高點）
    if len(sl) >= 2 and len(sh) >= 1:
        ai, av = sl[-2]
        bi, bv = sh[-1]
        ci, cv = sl[-1]
        if ai < bi > ci and cv > av:  # 回調但不破A
            ab = bv - av
            d_standard     = cv + ab          # 1.0 目標
            d_conservative  = cv + ab * 0.786  # 0.786 保守目標（原主常用）
            d_aggressive    = cv + ab * 1.272  # 1.272 激進目標
            current = closes[-1]
            result = {
                "detected": True,
                "pattern": "bull_ABCD",
                "A": round(av, 6), "B": round(bv, 6), "C": round(cv, 6),
                "target_conservative": round(d_conservative, 6),
                "target_standard":     round(d_standard, 6),
                "target_aggressive":   round(d_aggressive, 6),
                "in_target_zone": d_conservative <= current <= d_standard * 1.05,
            }

    # 空頭 AB=CD：高點 A → 低點 B → 回調高點 C → 估算 D（目標低點）
    if not result["detected"] and len(sh) >= 2 and len(sl) >= 1:
        ai, av = sh[-2]
        bi, bv = sl[-1] if sl else (0, closes[-1])
        ci, cv = sh[-1]
        sl_last = sl[-1] if sl else (len(closes)-1, closes[-1])
        if len(sh) >= 2 and len(sl) >= 1:
            ai, av = sh[-2]
            bi, bv = sl[-1]
            ci, cv = sh[-1]
            if ai < bi < ci and cv < av:  # 反彈但不破A高點
                ab = av - bv
                d_standard    = cv - ab
                d_conservative = cv - ab * 0.786
                d_aggressive   = cv - ab * 1.272
                result = {
                    "detected": True,
                    "pattern": "bear_ABCD",
                    "A": round(av, 6), "B": round(bv, 6), "C": round(cv, 6),
                    "target_conservative": round(d_conservative, 6),
                    "target_standard":     round(d_standard, 6),
                    "target_aggressive":   round(d_aggressive, 6),
                    "in_target_zone": d_standard * 0.95 <= closes[-1] <= d_conservative,
                }
    return result

def _detect_triangle(highs: list, lows: list, lookback: int = 40) -> dict:
    """
    三角收斂識別
    原主邏輯：量縮收斂末端 = 即將表態
    保守進場：突破收斂上/下緣
    激進進場：突破收斂中軸
    """
    rh = highs[-lookback:]
    rl = lows[-lookback:]
    n  = len(rh)
    if n < 10:
        return {"detected": False}

    # 用線性回歸判斷高點趨勢（下降）和低點趨勢（上升）= 對稱三角
    def linreg_slope(vals):
        xs = list(range(len(vals)))
        mx, my = sum(xs)/len(xs), sum(vals)/len(vals)
        num = sum((x-mx)*(y-my) for x,y in zip(xs,vals))
        den = sum((x-mx)**2 for x in xs)
        return num/den if den else 0

    # 只用擺動點計算
    swings = _find_swing_points(rh, rl, order=3)
    sh_vals = [v for _, v in swings["swing_highs"][-4:]]
    sl_vals = [v for _, v in swings["swing_lows"][-4:]]

    if len(sh_vals) < 2 or len(sl_vals) < 2:
        return {"detected": False}

    high_slope = linreg_slope(sh_vals)
    low_slope  = linreg_slope(sl_vals)

    # 對稱三角：高點下降、低點上升
    is_symmetric = high_slope < -0.0001 and low_slope > 0.0001
    # 上升三角：高點平、低點上升（偏多突破）
    is_ascending = abs(high_slope) < 0.0001 and low_slope > 0.0001
    # 下降三角：高點下降、低點平（偏空突破）
    is_descending = high_slope < -0.0001 and abs(low_slope) < 0.0001

    if not (is_symmetric or is_ascending or is_descending):
        return {"detected": False}

    # 收斂寬度（最初）與當前寬度
    initial_width = max(rh[:10]) - min(rl[:10])
    current_width = max(rh[-5:]) - min(rl[-5:])
    compression   = 1 - (current_width / initial_width) if initial_width > 0 else 0

    # 中軸（當前）
    current_mid = (max(rh[-3:]) + min(rl[-3:])) / 2
    current_price = (rh[-1] + rl[-1]) / 2

    ttype = "symmetric" if is_symmetric else ("ascending" if is_ascending else "descending")
    return {
        "detected":        True,
        "type":            ttype,
        "compression_pct": round(compression * 100, 1),  # 壓縮了多少%
        "initial_width":   round(initial_width, 6),
        "current_width":   round(current_width, 6),
        "midline":         round(current_mid, 6),
        "breakout_target_up":   round(current_mid + initial_width * 0.8, 6),
        "breakout_target_down": round(current_mid - initial_width * 0.8, 6),
        "near_apex": compression > 0.7,  # 收斂超過70% = 接近頂點，即將表態
    }

def _detect_hs(highs: list, lows: list, closes: list) -> dict:
    """
    頭肩頂/底識別
    原主邏輯：
    - 頭肩底 → 等右肩踩穩，突破頸線帶量確認進場，停損守右肩低點
    - 頭肩頂 → 右肩形成後跌破頸線，停損守右肩高點
    """
    swings = _find_swing_points(highs, lows, order=5)
    result = {"detected": False, "pattern": ""}

    # 頭肩底（三個低點，中間最低 = 頭）
    sl = swings["swing_lows"]
    if len(sl) >= 3:
        ls_i, ls_v = sl[-3]   # 左肩
        h_i,  h_v  = sl[-2]   # 頭
        rs_i, rs_v = sl[-1]   # 右肩
        if h_v < ls_v and h_v < rs_v:  # 頭最低
            neckline = max(highs[ls_i:rs_i])  # 兩肩之間的高點 = 頸線
            target   = neckline + (neckline - h_v)  # 頸線 + 頭到頸線的距離
            current  = closes[-1]
            above_neck = current > neckline
            result = {
                "detected":  True,
                "pattern":   "HS_bottom",
                "left_shoulder": round(ls_v, 6),
                "head":          round(h_v, 6),
                "right_shoulder": round(rs_v, 6),
                "neckline":      round(neckline, 6),
                "target":        round(target, 6),
                "breakout":      above_neck,
                "sl_level":      round(rs_v * 0.99, 6),  # 止損守右肩低點
            }

    # 頭肩頂（三個高點，中間最高 = 頭）
    sh = swings["swing_highs"]
    if not result["detected"] and len(sh) >= 3:
        ls_i, ls_v = sh[-3]
        h_i,  h_v  = sh[-2]
        rs_i, rs_v = sh[-1]
        if h_v > ls_v and h_v > rs_v:  # 頭最高
            neckline = min(lows[ls_i:rs_i]) if ls_i < rs_i else lows[ls_i]
            target   = neckline - (h_v - neckline)
            current  = closes[-1]
            below_neck = current < neckline
            result = {
                "detected":  True,
                "pattern":   "HS_top",
                "left_shoulder": round(ls_v, 6),
                "head":          round(h_v, 6),
                "right_shoulder": round(rs_v, 6),
                "neckline":      round(neckline, 6),
                "target":        round(target, 6),
                "breakout":      below_neck,
                "sl_level":      round(rs_v * 1.01, 6),  # 止損守右肩高點
            }
    return result

def _detect_wm(highs: list, lows: list, closes: list) -> dict:
    """
    W底（投間底）/ M頭（雙頂）識別
    原主邏輯：
    - W底：右腳踩穩後突破頸線帶量 = 進場，停損守右腳，目標 = 頸線 + 等幅
    - M頭：右峰下跌破頸線 = 空單，停損守右峰，目標 = 頸線 - 等幅
    """
    swings = _find_swing_points(highs, lows, order=4)
    result = {"detected": False, "pattern": ""}

    # W底：兩個相近低點
    sl = swings["swing_lows"]
    if len(sl) >= 2:
        lf_i, lf_v = sl[-2]  # 左腳
        rf_i, rf_v = sl[-1]  # 右腳
        diff_pct = abs(lf_v - rf_v) / lf_v if lf_v else 1
        if diff_pct < 0.05:  # 兩腳相差 < 5% = 雙底
            neckline_idx_range = highs[lf_i:rf_i] if lf_i < rf_i else [highs[lf_i]]
            neckline = max(neckline_idx_range) if neckline_idx_range else highs[lf_i]
            target   = neckline + (neckline - min(lf_v, rf_v))
            current  = closes[-1]
            result = {
                "detected":  True,
                "pattern":   "W_bottom",
                "left_foot": round(lf_v, 6),
                "right_foot": round(rf_v, 6),
                "neckline":  round(neckline, 6),
                "target":    round(target, 6),
                "breakout":  current > neckline,
                "sl_level":  round(rf_v * 0.99, 6),
            }

    # M頭：兩個相近高點
    sh = swings["swing_highs"]
    if not result["detected"] and len(sh) >= 2:
        lp_i, lp_v = sh[-2]
        rp_i, rp_v = sh[-1]
        diff_pct = abs(lp_v - rp_v) / lp_v if lp_v else 1
        if diff_pct < 0.05:
            neckline_range = lows[lp_i:rp_i] if lp_i < rp_i else [lows[lp_i]]
            neckline = min(neckline_range) if neckline_range else lows[lp_i]
            target   = neckline - (max(lp_v, rp_v) - neckline)
            current  = closes[-1]
            result = {
                "detected":  True,
                "pattern":   "M_top",
                "left_peak": round(lp_v, 6),
                "right_peak": round(rp_v, 6),
                "neckline":  round(neckline, 6),
                "target":    round(target, 6),
                "breakout":  current < neckline,
                "sl_level":  round(rp_v * 1.01, 6),
            }
    return result

def _find_structure(highs: list, lows: list, lookback: int = 20) -> dict:
    """尋找市場結構：近期高點/低點（流動性區域）"""
    recent_h = highs[-lookback:]
    recent_l = lows[-lookback:]
    swing_high = max(recent_h)
    swing_low  = min(recent_l)
    # 前 20 根的高點/低點（潛在流動性聚集）
    prev_high = max(highs[-40:-20]) if len(highs) >= 40 else swing_high
    prev_low  = min(lows[-40:-20]) if len(lows) >= 40 else swing_low
    return {
        "swing_high": round(swing_high, 6),
        "swing_low":  round(swing_low, 6),
        "prev_high":  round(prev_high, 6),
        "prev_low":   round(prev_low, 6),
    }

def _detect_fake_breakout(closes: list, highs: list, lows: list,
                           swing_high: float, swing_low: float) -> dict:
    """
    偵測假突破跡象：
    - 最後一根K是否突破後回縮（長上/下影線）
    - 突破後沒有成交量確認
    """
    if len(closes) < 3:
        return {"fake_up": False, "fake_down": False}
    c0, c1, c2 = closes[-3], closes[-2], closes[-1]
    h0, h1 = highs[-3], highs[-2]
    l0, l1 = lows[-3], lows[-2]

    # 假突破上方（上影線長，收回swing_high以下）
    fake_up = (h1 > swing_high and c1 < swing_high) or \
              (h0 > swing_high and c0 < swing_high and c1 < swing_high)

    # 假突破下方（下影線長，收回swing_low以上）
    fake_down = (l1 < swing_low and c1 > swing_low) or \
                (l0 < swing_low and c0 > swing_low and c1 > swing_low)

    return {"fake_up": fake_up, "fake_down": fake_down}

async def _fetch_market_snapshot(symbol: str) -> dict | None:
    """多時間框架市場快照：日線方向 + 4H結構 + 1H進場訊號 + 15m日內訊號"""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            # 15m（日內進場用）200根
            r15m = await client.get(
                "https://fapi.binance.com/fapi/v1/klines",
                params={"symbol": symbol, "interval": "15m", "limit": 200}
            )
            klines_15m = r15m.json()
            if not klines_15m or isinstance(klines_15m, dict):
                return None

            # 1H（進場用）200根
            r1h = await client.get(
                "https://fapi.binance.com/fapi/v1/klines",
                params={"symbol": symbol, "interval": "1h", "limit": 200}
            )
            klines_1h = r1h.json()
            if not klines_1h or isinstance(klines_1h, dict):
                return None

            # 4H（結構用）100根
            r4h = await client.get(
                "https://fapi.binance.com/fapi/v1/klines",
                params={"symbol": symbol, "interval": "4h", "limit": 100}
            )
            klines_4h = r4h.json()

            # 日線（方向用）60根
            r1d = await client.get(
                "https://fapi.binance.com/fapi/v1/klines",
                params={"symbol": symbol, "interval": "1d", "limit": 60}
            )
            klines_1d = r1d.json()

        def parse(klines):
            return {
                "opens":   [float(k[1]) for k in klines],
                "closes":  [float(k[4]) for k in klines],
                "volumes": [float(k[5]) for k in klines],
                "highs":   [float(k[2]) for k in klines],
                "lows":    [float(k[3]) for k in klines],
            }

        d15m = parse(klines_15m)
        d1h  = parse(klines_1h)
        d4h  = parse(klines_4h) if isinstance(klines_4h, list) and klines_4h else d1h
        d1d  = parse(klines_1d) if isinstance(klines_1d, list) and klines_1d else d1h

        price = d15m["closes"][-1]  # 最即時價格用 15m

        # EMA — 1H
        ema20_1h  = _calc_ema(d1h["closes"], 20)[-1]
        ema99_1h  = _calc_ema(d1h["closes"], 99)[-1]
        ema200_1h = _calc_ema(d1h["closes"], 200)[-1]

        # EMA — 日線（方向判斷用）
        ema20_1d  = _calc_ema(d1d["closes"], 20)[-1]
        ema99_1d  = _calc_ema(d1d["closes"], min(99, len(d1d["closes"])-1))[-1]

        # RSI — 1H
        rsi_1h = _calc_rsi(d1h["closes"], 14)
        # RSI — 4H
        rsi_4h = _calc_rsi(d4h["closes"], 14)

        # ATR（1H）
        trs = [max(d1h["highs"][i] - d1h["lows"][i],
                   abs(d1h["highs"][i] - d1h["closes"][i-1]),
                   abs(d1h["lows"][i]  - d1h["closes"][i-1]))
               for i in range(1, len(d1h["closes"]))]
        atr_1h = sum(trs[-14:]) / 14

        # 成交量分析（原主邏輯：量縮極致 ≠ 跳過，是爆發前兆）
        vol_avg   = sum(d1h["volumes"][-20:]) / 20
        vol_ratio = d1h["volumes"][-1] / vol_avg if vol_avg else 1

        # 量縮程度評估（連續幾根在縮，縮到什麼程度）
        vol_shrink_count = 0
        for i in range(1, min(10, len(d1h["volumes"]))):
            if d1h["volumes"][-(i)] < d1h["volumes"][-(i+1)]:
                vol_shrink_count += 1
            else:
                break
        vol_shrinking = vol_shrink_count >= 3

        # 量縮是否到達極致（近20根最低量 ≈ 當前量，且持續5根以上）
        vol_min_20 = min(d1h["volumes"][-20:])
        vol_extreme_compression = (
            vol_shrink_count >= 5 and
            d1h["volumes"][-1] <= vol_min_20 * 1.3
        )

        # 上漲量縮 = 出貨訊號（原主：越漲量越縮是壞事）
        price_rising = d1h["closes"][-1] > d1h["closes"][-5]
        vol_divergence_bearish = price_rising and vol_ratio < 0.7  # 漲但縮量

        # 下跌量縮到極致 = 洗盤完畢，等底部確認
        price_falling = d1h["closes"][-1] < d1h["closes"][-5]
        vol_compression_bottom = price_falling and vol_extreme_compression

        # RSI 背離偵測 — 需要 1H 與 4H 同時確認，避免單一時框誤判
        closes_15 = d1h["closes"][-15:]
        rsi_1h_prev = _calc_rsi(d1h["closes"][:-8], 14)
        rsi_4h_cur  = _calc_rsi(d4h["closes"], 14)
        rsi_4h_prev = _calc_rsi(d4h["closes"][:-5], 14)
        closes_4h_15 = d4h["closes"][-15:]

        # 1H 底背離：價格新低 + RSI未創新低
        div_bull_1h = (closes_15[-1] < closes_15[-8] and rsi_1h > rsi_1h_prev)
        # 4H 底背離：價格新低 + RSI未創新低
        div_bull_4h = (closes_4h_15[-1] < closes_4h_15[-8] and rsi_4h_cur > rsi_4h_prev)
        # 需兩個時框都有底背離才算確認（Sam：背離需多框共振）
        rsi_bullish_div = div_bull_1h and div_bull_4h

        # 1H 頂背離：價格新高 + RSI未創新高
        div_bear_1h = (closes_15[-1] > closes_15[-8] and rsi_1h < rsi_1h_prev)
        # 4H 頂背離
        div_bear_4h = (closes_4h_15[-1] > closes_4h_15[-8] and rsi_4h_cur < rsi_4h_prev)
        rsi_bearish_div = div_bear_1h and div_bear_4h

        # 連續背離（Sam 吸籌/出貨完成訊號）— 用 1H 多個擺動點，比快照背離強得多
        cont_div = _detect_continuous_divergence(d1h["closes"], d1h["highs"], d1h["lows"])

        # MACD 動能背書（1H，輔助確認層，與RSI同級不作主決策）
        macd_1h = _calc_macd(d1h["closes"])

        # 費波那契黃金回調區（右側進場區判斷）
        fib_1h = _calc_fib(d1h["highs"], d1h["lows"], d1h["closes"], lookback=60)

        # 補齊形態學：杯柄 / 通道（1H）
        pattern_cup_1h     = _detect_cup_handle(d1h["highs"], d1h["lows"], d1h["closes"])
        pattern_channel_1h = _detect_channel(d1h["highs"], d1h["lows"], d1h["closes"], lookback=40)

        # 右側進場確認：突破後縮量回測守穩（Sam核心，不追突破K棒）
        retest_1h = _detect_retest_hold(d1h["highs"], d1h["lows"], d1h["closes"], d1h["volumes"])

        # 市場結構
        structure_15m = _find_structure(d15m["highs"], d15m["lows"], lookback=20)
        structure_1h  = _find_structure(d1h["highs"],  d1h["lows"],  lookback=24)
        structure_4h  = _find_structure(d4h["highs"],  d4h["lows"],  lookback=20)

        # 原主形態識別 — 15m
        pattern_abcd_15m     = _detect_abcd(d15m["closes"], d15m["highs"], d15m["lows"])
        pattern_triangle_15m = _detect_triangle(d15m["highs"], d15m["lows"], lookback=40)
        pattern_hs_15m       = _detect_hs(d15m["highs"], d15m["lows"], d15m["closes"])
        pattern_wm_15m       = _detect_wm(d15m["highs"], d15m["lows"], d15m["closes"])
        fake_bo_15m = _detect_fake_breakout(
            d15m["closes"], d15m["highs"], d15m["lows"],
            structure_15m["swing_high"], structure_15m["swing_low"]
        )

        # 原主形態識別 — 1H
        pattern_abcd     = _detect_abcd(d1h["closes"], d1h["highs"], d1h["lows"])
        pattern_triangle = _detect_triangle(d1h["highs"], d1h["lows"], lookback=40)
        pattern_hs       = _detect_hs(d1h["highs"], d1h["lows"], d1h["closes"])
        pattern_wm       = _detect_wm(d1h["highs"], d1h["lows"], d1h["closes"])

        # 假突破偵測（1H）
        fake_bo = _detect_fake_breakout(
            d1h["closes"], d1h["highs"], d1h["lows"],
            structure_1h["swing_high"], structure_1h["swing_low"]
        )

        # RSI — 15m
        rsi_15m = _calc_rsi(d15m["closes"], 14)

        # 量能 — 15m（補充日內量比）
        vol_avg_15m   = sum(d15m["volumes"][-20:]) / 20
        vol_ratio_15m = round(d15m["volumes"][-1] / vol_avg_15m, 2) if vol_avg_15m else 1.0

        # 日線趨勢
        if d1d["closes"][-1] > ema20_1d > ema99_1d:
            daily_trend = "bull"
        elif d1d["closes"][-1] < ema20_1d < ema99_1d:
            daily_trend = "bear"
        else:
            daily_trend = "neutral"

        # 1H趨勢
        if price > ema99_1h > ema200_1h:
            trend_1h = "bull"
        elif price < ema99_1h < ema200_1h:
            trend_1h = "bear"
        else:
            trend_1h = "neutral"

        # 距離關鍵流動性區域
        dist_to_swing_high = round((structure_1h["swing_high"] - price) / price * 100, 2)
        dist_to_swing_low  = round((price - structure_1h["swing_low"]) / price * 100, 2)

        return {
            "symbol":           symbol,
            "price":            round(price, 8),
            # 趨勢
            "daily_trend":      daily_trend,
            "trend_1h":         trend_1h,
            # EMA（1H）
            "ema20":            round(ema20_1h, 6),
            "ema99":            round(ema99_1h, 6),
            "ema200":           round(ema200_1h, 6),
            # EMA（日線）
            "ema20_1d":         round(ema20_1d, 6),
            "ema99_1d":         round(ema99_1d, 6),
            # RSI
            "rsi_1h":           rsi_1h,
            "rsi_4h":           rsi_4h,
            # 波動
            "atr":              round(atr_1h, 8),
            "atr_pct":          round(atr_1h / price, 4),
            # 量能（原主邏輯版）
            "vol_ratio":                round(vol_ratio, 2),
            "vol_shrinking":            vol_shrinking,
            "vol_shrink_count":         vol_shrink_count,
            "vol_extreme_compression":  vol_extreme_compression,   # 量縮極致→爆發前兆
            "vol_divergence_bearish":   vol_divergence_bearish,    # 上漲量縮→出貨訊號
            "vol_compression_bottom":   vol_compression_bottom,    # 下跌量縮極致→洗盤末端
            # RSI 背離
            "rsi_bullish_divergence":   rsi_bullish_div,           # 價格新低但RSI未新低→多（快照，僅觀察級）
            "rsi_bearish_divergence":   rsi_bearish_div,           # 價格新高但RSI未新高→空（快照，僅觀察級）
            # 連續背離（Sam 吸籌/出貨完成訊號，進場級）
            "rsi_bull_div_continuous":  cont_div["bull"],          # 連續底背離→主力吸籌完成
            "rsi_bear_div_continuous":  cont_div["bear"],          # 連續頂背離→主力出貨
            "div_bull_strength":        cont_div["bull_strength"],
            "div_bear_strength":        cont_div["bear_strength"],
            # MACD 動能背書（輔助層）
            "macd_hist":        macd_1h["hist"],
            "macd_cross":       macd_1h["cross"],        # bull金叉/bear死叉/none
            "macd_above_zero":  macd_1h["above_zero"],
            # 費波那契
            "fib_in_golden_zone": fib_1h["in_golden_zone"],
            "fib_nearest":        fib_1h["nearest"],
            "fib_swing_dir":      fib_1h["swing_dir"],
            # 右側進場確認（突破後縮量回測守穩）
            "retest_hold_long":  retest_1h["long"],
            "retest_hold_short": retest_1h["short"],
            "retest_level":      retest_1h["level"],
            # 市場結構（流動性區域）
            "swing_high_1h":    structure_1h["swing_high"],
            "swing_low_1h":     structure_1h["swing_low"],
            "prev_high_1h":     structure_1h["prev_high"],
            "prev_low_1h":      structure_1h["prev_low"],
            "swing_high_4h":    structure_4h["swing_high"],
            "swing_low_4h":     structure_4h["swing_low"],
            # 假突破
            "fake_breakout_up":   fake_bo["fake_up"],
            "fake_breakout_down": fake_bo["fake_down"],
            # 原主形態 — 1H
            "pattern_abcd":     pattern_abcd,
            "pattern_triangle": pattern_triangle,
            "pattern_hs":       pattern_hs,
            "pattern_wm":       pattern_wm,
            "pattern_cup":      pattern_cup_1h,
            "pattern_channel":  pattern_channel_1h,
            # 15m 時框
            "rsi_15m":              rsi_15m,
            "vol_ratio_15m":        vol_ratio_15m,
            "swing_high_15m":       structure_15m["swing_high"],
            "swing_low_15m":        structure_15m["swing_low"],
            "fake_breakout_up_15m": fake_bo_15m["fake_up"],
            "fake_breakout_down_15m": fake_bo_15m["fake_down"],
            "pattern_abcd_15m":     pattern_abcd_15m,
            "pattern_triangle_15m": pattern_triangle_15m,
            "pattern_hs_15m":       pattern_hs_15m,
            "pattern_wm_15m":       pattern_wm_15m,
            # 距離流動性
            "dist_to_swing_high_pct": dist_to_swing_high,
            "dist_to_swing_low_pct":  dist_to_swing_low,
            "source": "sam_v2",
            # 原始 K 線（供 _sam_decide 畫圖用）
            "_d15m": d15m,
            "_d1h":  d1h,
        }
    except Exception as e:
        log.debug(f"[SNAPSHOT] {symbol} 失敗: {e}")
        return None

# ─────────────────────────────────────────────────────────────────────
# BTC 大方向 — 全局市場環境（Sam：日線200MA判斷牛熊，BTC是山寨指標）
# ─────────────────────────────────────────────────────────────────────
_BTC_REGIME_CACHE: dict = {"regime": "neutral", "btc_price": 0, "ma200": 0, "ma99": 0,
                           "updated_at": 0, "btc_dominance": 0, "dom_trend": "flat",
                           "others_d": 0, "others_trend": "flat", "alt_headwind": False}


async def _fetch_btc_dominance() -> dict:
    """
    宏觀濾鏡 — Sam 跨資產框架（用 BTC.D + OTHERS.D 雙指標判山寨強弱）：
      BTC.D = 比特幣主導率；上升 → 資金回流BTC，山寨承壓。
      OTHERS.D = 山寨指數（總市值扣掉前10大 ≈ TradingView OTHERS.D）；
                 上升 → 資金外溢進中小山寨（山寨季氛圍，做多山寨順風）
                 下降 → 山寨失血（做多山寨逆風）。
    判定 alt_headwind（山寨做多逆風）= BTC.D上升 或 OTHERS.D下降。
    用前一次 cache 數值比對方向，門檻 ±0.3 個百分點。
    """
    prev_btc    = _BTC_REGIME_CACHE.get("btc_dominance", 0)
    prev_others = _BTC_REGIME_CACHE.get("others_d", 0)
    try:
        mcp = None
        for attempt in range(3):                      # CoinGecko 免費版偶爾 429，重試3次
            async with httpx.AsyncClient(timeout=8) as cl:
                r = await cl.get("https://api.coingecko.com/api/v3/global")
            if r.status_code == 200:
                mcp = r.json()["data"]["market_cap_percentage"]
                break
            await asyncio.sleep(2)
        if not mcp:
            raise ValueError("global 連續取得失敗")
        btc_d    = float(mcp.get("btc", 0))
        others_d = round(100 - sum(mcp.values()), 2)   # 扣掉前10大 = OTHERS.D 代理

        def _trend(cur, prev):
            if prev and abs(cur - prev) >= 0.3:
                return "rising" if cur > prev else "falling"
            return "flat"

        dom_trend    = _trend(btc_d, prev_btc)
        others_trend = _trend(others_d, prev_others)
        alt_headwind = (dom_trend == "rising") or (others_trend == "falling")
        return {"btc_dominance": round(btc_d, 2), "dom_trend": dom_trend,
                "others_d": others_d, "others_trend": others_trend,
                "alt_headwind": alt_headwind}
    except Exception as e:
        log.debug(f"[BTC_DOM] 抓取失敗: {e}")
        return {"btc_dominance": prev_btc, "dom_trend": "flat",
                "others_d": prev_others, "others_trend": "flat", "alt_headwind": False}

async def _fetch_btc_regime() -> dict:
    """
    抓 BTC 日線 200MA，判斷整體市場環境。
    Sam 明確說：「日線200MA判斷牛熊，BTC沒破底山寨幣不看空」
    - price > MA200 → 牛市（多單優先，短線空需假突破確認）
    - price < MA200 且 < MA99 → 熊市（空單優先，多單只在假突破/RSI底背離）
    - MA99 < price < MA200 → 中性（兩方向需二次確認）
    """
    global _BTC_REGIME_CACHE
    now = time.time()
    if now - _BTC_REGIME_CACHE.get("updated_at", 0) < 3600:  # 1小時 cache
        return _BTC_REGIME_CACHE

    try:
        async with httpx.AsyncClient(timeout=8) as cl:
            r = await cl.get(
                "https://api.binance.com/api/v3/klines",
                params={"symbol": "BTCUSDT", "interval": "1d", "limit": 210}
            )
            if r.status_code != 200:
                return _BTC_REGIME_CACHE
            klines = r.json()
            closes = [float(k[4]) for k in klines]
            price  = closes[-1]

            def _sma(vals, n):
                return sum(vals[-n:]) / n if len(vals) >= n else vals[-1]

            ma99  = _sma(closes, 99)
            ma200 = _sma(closes, 200)

            if price > ma200:
                regime = "bull"
                note   = f"BTC {price:,.0f} > MA200 {ma200:,.0f} ✅ 牛市"
            elif price < ma200 and price < ma99:
                regime = "bear"
                note   = f"BTC {price:,.0f} < MA200 {ma200:,.0f} & MA99 ⚠️ 熊市"
            else:
                regime = "neutral"
                note   = f"BTC {price:,.0f} 介於 MA99~MA200 — 中性整理"

            dom_info = await _fetch_btc_dominance()

            _BTC_REGIME_CACHE = {
                "regime":    regime,
                "btc_price": price,
                "ma200":     round(ma200, 0),
                "ma99":      round(ma99, 0),
                "note":      note,
                "updated_at": now,
                "btc_dominance": dom_info["btc_dominance"],
                "dom_trend":     dom_info["dom_trend"],
                "others_d":      dom_info["others_d"],
                "others_trend":  dom_info["others_trend"],
                "alt_headwind":  dom_info["alt_headwind"],
            }
            log.info(f"[BTC_REGIME] {note} | BTC.D={dom_info['btc_dominance']}%({dom_info['dom_trend']}) "
                     f"OTHERS.D={dom_info['others_d']}%({dom_info['others_trend']})"
                     f"{' ⚠️山寨做多逆風' if dom_info['alt_headwind'] else ''}")
    except Exception as e:
        log.warning(f"[BTC_REGIME] 抓取失敗: {e}")

    return _BTC_REGIME_CACHE


def _should_ask_sam(market: dict, btc_regime: str = "neutral") -> tuple[bool, str]:
    """
    純規則前置過濾 — 符合 Sam 進場條件才送，不消耗任何 API token。

    Sam 進場條件（從 13,430 筆統計提煉）：
    ① 假突破確認：假突破 = A+ 機會，單獨放行（牛熊皆可）
    ② 量縮極致 + 關鍵位：彈簧壓縮，等第一根帶量K
    ③ 放量（≥1.5x）+ 型態：量能配合結構，才是有效突破
    ④ RSI背離 + 型態：動能轉換訊號，需結構支撐
    ⑤ 純型態無量 → 跳過（Sam：量縮的突破是假突破）
    ⑥ 量縮進行中（非極致）→ 直接跳過

    大方向過濾（Sam：「常線空短線多」/ 「BTC沒破底山寨不看空」）：
    - 牛市：日內短空需假突破確認 / 背離確認，否則只做多
    - 熊市：日內短多需底背離 / 假突破確認，否則只做空
    - 中性：多空都需二次確認
    """
    vol_ratio     = market.get("vol_ratio", 1.0)
    vol_ratio_15m = market.get("vol_ratio_15m", 1.0)
    vol_extreme   = market.get("vol_extreme_compression", False)
    vol_shrinking = market.get("vol_shrinking", False)
    daily_trend   = market.get("daily_trend", "neutral")  # 個幣日線趨勢
    price         = market.get("price", 1.0)

    # ── ⓪ 橫盤洗盤偵測（Sam：橫盤不是機會，是主力在整理籌碼，等方向）──
    # 用 1H swing_high/swing_low 計算近期波幅，振幅<1.5% = 橫盤區間
    sh = market.get("swing_high_1h", 0)
    sl_key = market.get("swing_low_1h", 0)
    if sh and sl_key and price:
        range_pct = (sh - sl_key) / price
        fake_any = (market.get("fake_breakout_up") or market.get("fake_breakout_down") or
                    market.get("fake_breakout_up_15m") or market.get("fake_breakout_down_15m"))
        if range_pct < 0.015 and not vol_extreme and not fake_any:
            return False, f"橫盤洗盤(振幅{range_pct:.1%}<1.5%)，等方向突破"

    # ── ① 量縮進行中（非極致）→ 主力不在，直接跳 ────────────────────
    if vol_shrinking and not vol_extreme and vol_ratio < 0.8 and vol_ratio_15m < 0.9:
        return False, f"量縮({vol_ratio}x)，等表態"

    signals = []

    # ── ② 假突破 → A+ 機會，牛熊皆可放行（Sam：假突破是最強訊號）──
    fake_1h  = market.get("fake_breakout_up") or market.get("fake_breakout_down")
    fake_15m = market.get("fake_breakout_up_15m") or market.get("fake_breakout_down_15m")
    if fake_1h:  signals.append("1H假突破✦")
    if fake_15m: signals.append("15m假突破✦")
    if signals:
        return True, "、".join(signals)   # 假突破直接放行，牛熊都適用

    # ── ②-b RSI 極端早期保護（2026-06-09升級：放在 ⑤ 之前，防止被 vol+pattern 提前通過）──
    # SKYAI 案例：RSI=67.8 + W底型態 + vol 1.76x → 被步驟⑤提前 return True → 4分鐘止損
    # Sam 靈魂：「超買不追多，超賣不追空」— 這個原則優先於型態，假突破例外已在上方處理
    rsi_early = market.get("rsi_1h", 50)
    rsi_bullish_div_early = market.get("rsi_bullish_divergence", False)
    rsi_bearish_div_early = market.get("rsi_bearish_divergence", False)
    if rsi_early > 68:  # 超買區
        # 允許：有空頭信號（打算做空），或已有頂背離（反轉確認）
        is_confirmed_short = (daily_trend == "bear" or rsi_bearish_div_early or
                              market.get("fake_breakout_up"))
        if not is_confirmed_short:
            return False, f"RSI超買({rsi_early:.1f}>68)，非空頭結構不追多，等回落到65以下"
    if rsi_early < 32:  # 超賣區
        # 允許：有多頭信號（打算做多），或已有底背離（反轉確認）
        is_confirmed_long = (daily_trend == "bull" or rsi_bullish_div_early or
                             market.get("fake_breakout_down"))
        if not is_confirmed_long:
            return False, f"RSI超賣({rsi_early:.1f}<32)，非多頭結構不追空，等反彈到35以上"

    # ── ②-c 日線方向過濾（預篩，方向由LLM決定後在硬性規則0再鎖死）──
    # 日線空頭時，若無底背離/假跌破等強訊號，直接不送LLM省token
    # 逆日線方向要送 LLM，必須有「吸籌/出貨完成」級別的證據，不是單張快照背離。
    # Sam：單一背離只是警示（觀察級），連續背離才是主力動作完成（進場級）。
    if daily_trend == "bear":
        has_long_signal = (
            market.get("rsi_bull_div_continuous") or      # 連續底背離=吸籌完成
            market.get("fake_breakout_down") or            # 假跌破洗盤
            market.get("vol_compression_bottom")           # 下跌量縮極致
        )
        if not has_long_signal:
            return False, f"日線偏空且無吸籌完成證據（連續底背離/假跌破/量縮末端），不送LLM"
    if daily_trend == "bull":
        has_short_signal = (
            market.get("rsi_bear_div_continuous") or       # 連續頂背離=出貨
            market.get("fake_breakout_up") or
            market.get("vol_extreme_compression")
        )
        if not has_short_signal:
            return False, f"日線偏多且無出貨完成證據（連續頂背離/假突破/量縮極致），不送LLM"

    # ── ③ 量縮極致 → 彈簧壓縮，單獨放行 ──────────────────────────────
    if vol_extreme:
        return True, f"量縮極致({vol_ratio}x)，彈簧蓄力"
    if market.get("vol_compression_bottom"):
        return True, "下跌量縮極致，洗盤末端"

    # ── ④ 大方向過濾（Sam：「常線空短線多」/ 「BTC沒破底山寨不看空」）──
    # btc_regime: "bull" = BTC>MA200牛市 / "bear" = BTC<MA200&MA99熊市 / "neutral"
    # daily_trend: 個幣日線自己的趨勢方向
    rsi_bullish_div = market.get("rsi_bullish_divergence", False)
    rsi_bearish_div = market.get("rsi_bearish_divergence", False)

    # 牛市環境：短線空單需有背離或假突破支撐（以上已處理假突破），
    #           否則 daily_trend 偏空時才允許（個幣背離大方向）
    if btc_regime == "bull" and daily_trend == "bear":
        if not (rsi_bearish_div or vol_extreme):
            return False, f"BTC牛市但個幣日線偏空({daily_trend})，無背離不進場"

    # 熊市環境：短線多單需有底背離 / 特殊型態支撐
    if btc_regime == "bear" and daily_trend == "bull":
        if not (rsi_bullish_div or market.get("vol_compression_bottom")):
            return False, f"BTC熊市，個幣日線偏多({daily_trend})，無底背離不做多"

    # ── ⑤ 放量（≥1.5x）+ 有型態 → 有效突破條件 ───────────────────────
    has_pattern = any(
        market.get(k, {}).get("detected")
        for k in ["pattern_abcd","pattern_triangle","pattern_hs","pattern_wm","pattern_cup",
                  "pattern_abcd_15m","pattern_triangle_15m","pattern_hs_15m","pattern_wm_15m"]
    )
    vol_surge = vol_ratio >= 1.5 or vol_ratio_15m >= 1.5

    if vol_surge and has_pattern:
        vol_tag = f"1H{vol_ratio}x" if vol_ratio >= 1.5 else f"15m{vol_ratio_15m}x"
        pat_names = []
        for k, n in [("pattern_triangle","三角"),("pattern_hs","頭肩"),
                     ("pattern_wm","WM"),("pattern_abcd","ABCD"),
                     ("pattern_triangle_15m","15m三角"),("pattern_hs_15m","15m頭肩")]:
            if market.get(k, {}).get("detected"):
                pat_names.append(n)
        regime_tag = f"[{btc_regime}]" if btc_regime != "neutral" else ""
        return True, f"{regime_tag}放量{vol_tag}+{'+'.join(pat_names[:2])}"

    # ── ⑤-b M頭/W底方向鎖定（型態本身決定方向，不能反向操作）────────────
    wm = market.get("pattern_wm", {})
    if wm and wm.get("detected"):
        if wm.get("pattern") == "M_top":
            # M頭 = 空頭型態。破頸後反彈到頸線 = 做空機會，不是做多
            # 若目前沒有放量空頭訊號，鎖定只允許空單方向
            if wm.get("breakout"):  # 已破頸線
                likely_long_signal = (market.get("fake_breakout_down") or
                                      market.get("rsi_bullish_divergence"))
                if likely_long_signal:
                    return False, "M頭已破頸，反彈是做空機會（回測頸線空），禁止在此做多"
        if wm.get("pattern") == "W_bottom":
            # W底 = 多頭型態。破頸後回測 = 做多機會，不是做空
            if wm.get("breakout"):
                likely_short_signal = (market.get("fake_breakout_up") or
                                       market.get("rsi_bearish_divergence"))
                if likely_short_signal:
                    return False, "W底已破頸，回測是做多機會（回測頸線多），禁止在此做空"

    # ── ⑥ RSI背離 + 有型態 → 動能轉換佐結構 ──────────────────────────
    # Sam 原則：背離只是警示，不是進場訊號。必須有關鍵位突破才算右側確認。
    rsi_div = rsi_bullish_div or rsi_bearish_div
    if rsi_div and has_pattern:
        # 右側確認定義：假突破收復 OR 型態頸線已突破
        has_breakout = any(
            market.get(k, {}).get("breakout")
            for k in ["pattern_wm", "pattern_hs", "pattern_triangle",
                      "pattern_abcd", "pattern_triangle_15m", "pattern_hs_15m"]
        )
        fake_confirm = (market.get("fake_breakout_up") or market.get("fake_breakout_down") or
                        market.get("fake_breakout_up_15m") or market.get("fake_breakout_down_15m"))
        right_side_confirmed = has_breakout or fake_confirm

        if rsi_bullish_div and not right_side_confirmed:
            return False, "底背離但尚未突破任何關鍵壓力位（頸線/前高/趨勢線），等右側突破收盤確認再進多"
        if rsi_bearish_div and not right_side_confirmed:
            return False, "頂背離但尚未跌破任何關鍵支撐位（頸線/前低/趨勢線），等右側跌破收盤確認再進空"

        # 熊市底背離額外要求假突破確認
        if btc_regime == "bear" and rsi_bullish_div:
            fake_down = (market.get("fake_breakout_down") or market.get("fake_breakout_down_15m"))
            if not fake_down:
                return False, "熊市底背離需右側假突破確認才進多，目前無右側訊號"
        # 牛市頂背離額外要求假突破確認
        if btc_regime == "bull" and rsi_bearish_div:
            fake_up = (market.get("fake_breakout_up") or market.get("fake_breakout_up_15m"))
            if not fake_up:
                return False, "牛市頂背離需右側假突破確認才進空，目前無右側訊號"

        div_tag = "多頭背離" if rsi_bullish_div else "空頭背離"
        return True, f"RSI{div_tag}+型態+右側關鍵位突破確認"

    # ── ⑦ RSI 橫盤保護（RSI 在中間區間 = 沒方向，不浪費 API）────────────
    # RSI 42~58 = 多空均衡，沒有動能偏向，類似價格振幅橫盤的概念
    # 例外：假突破（已 return）/ 量縮極致（已 return）/ RSI背離（已 return）
    rsi_1h = market.get("rsi_1h", 50)
    if 42 <= rsi_1h <= 58 and not vol_extreme and not rsi_div:
        return False, f"RSI橫盤區({rsi_1h:.1f}，42~58無方向)，等動能確立"

    # ── ⑧ RSI 方向過濾（Sam：超賣不做空，超買不做多）────────────────────
    # 超賣區(RSI<35)做空 = 在最脆弱位置追空，容易被反彈止損
    # 超買區(RSI>65)做多 = 在最脆弱位置追多，容易被回調止損
    # 例外：假突破、RSI背離不受此限（已在上面提前 return）
    if vol_surge and not has_pattern:
        pass  # 無型態的純量能訊號，讓下面走
    if vol_surge or has_pattern:
        # 判斷此次訊號傾向方向
        likely_short = (
            market.get("fake_breakout_up") or
            market.get("rsi_bearish_divergence") or
            daily_trend == "bear"
        )
        likely_long = (
            market.get("fake_breakout_down") or
            market.get("rsi_bullish_divergence") or
            daily_trend == "bull"
        )
        if not likely_short and not likely_long:
            # 方向不明確時，RSI 極端值直接跳過
            if rsi_1h < 35:
                return False, f"RSI超賣({rsi_1h:.1f}<35)方向不明，不追空不追多"
            if rsi_1h > 65:
                return False, f"RSI超買({rsi_1h:.1f}>65)方向不明，不追空不追多"
        elif likely_short and rsi_1h < 35:
            return False, f"RSI超賣({rsi_1h:.1f}<35)，空頭做在最弱點，等RSI回升再評估"
        elif likely_long and rsi_1h > 65:
            return False, f"RSI超買({rsi_1h:.1f}>65)，多頭追在最貴位置，等RSI回落再評估"

    # ── 不符合任何進場條件 → 跳過 ─────────────────────────────────────
    if has_pattern:
        return False, f"有型態但量縮({vol_ratio}x)無背離無假突破，等訊號"
    return False, f"無訊號(量{vol_ratio}x)"


async def _get_candidate_signals() -> list:
    import random
    watchlist = SAM_WATCHLIST if SAM_WATCHLIST else await _refresh_watchlist()
    must  = [s for s in ["BTCUSDT", "ETHUSDT", "SOLUSDT"] if s in watchlist]
    rest  = [s for s in watchlist if s not in must]
    batch = must + random.sample(rest, min(2, len(rest)))  # 節省API：必掃3+隨機2=5個

    snapshots = []
    for sym in batch:
        snap = await _fetch_market_snapshot(sym)
        if snap:
            snapshots.append(snap)
        await asyncio.sleep(0.15)
    log.info(f"[SCAN] 掃描 {len(snapshots)} 個幣：{[s['symbol'] for s in snapshots]}")
    return snapshots

# ─────────────────────────────────────────────
# 思考快照存檔
# ─────────────────────────────────────────────
def _save_thinking(symbol: str, market: dict, brain_view: str, decision: dict):
    try:
        logs = json.loads(THINKING_FILE.read_text()) if THINKING_FILE.exists() else []
        logs.append({
            "symbol":    symbol,
            "timestamp": datetime.now(TZ8).isoformat(),
            "market":    market,
            "brain_view": brain_view,
            "approved":       decision.get("approved"),
            "side":           decision.get("side"),
            "score":          decision.get("score"),
            "leverage":       decision.get("leverage", 1),
            "reason":         decision.get("reason", ""),
            "strategy_type":  decision.get("strategy_type", "EMA_PULLBACK"),
            "sl_pct":         decision.get("sl_pct"),
            "tp_pct":    decision.get("tp_pct"),
            "skip_reason": decision.get("skip_reason", ""),
            # 誘多/空觀察記錄（學習樣本，累積後分析）
            "trap_observation": decision.get("trap_observation", None),
            "fake_breakout_up": market.get("fake_breakout_up", False),
            "fake_breakout_down": market.get("fake_breakout_down", False),
        })
        # 動態容量：從500開始，超過後每次多加500
        current_limit = max(500, (len(logs) // 500 + 1) * 500)
        THINKING_FILE.write_text(json.dumps(logs[-current_limit:], ensure_ascii=False, indent=2))
    except Exception as e:
        log.warning(f"[THINKING] 存檔失敗: {e}")


# ─────────────────────────────────────────────
# LLM 自動修復：brain_server 重啟
# ─────────────────────────────────────────────
async def _auto_repair_brain_server():
    """
    LLM 連續失敗 >= 3 次時：
    1. 先重啟 brain_server（最常見的修復方式）
    2. 等 15 秒後測試是否恢復
    3. 若還是失敗，發 TG 通知（需人工介入）
    4. 每 30 分鐘最多嘗試一次，避免重複重啟
    """
    global _LLM_FAIL_COUNT, _LLM_ALERTED
    import subprocess, sys

    # 避免頻繁重啟（30 分鐘冷卻）
    now = time.time()
    last_repair = getattr(_auto_repair_brain_server, "_last_run", 0)
    if now - last_repair < 1800:
        return
    _auto_repair_brain_server._last_run = now

    log.warning(f"[AUTO_REPAIR] LLM 連續失敗 {_LLM_FAIL_COUNT} 次，嘗試重啟 brain_server...")

    try:
        # 找並終止舊的 brain_server
        result = subprocess.run(
            ["pgrep", "-f", "brain_server.py"],
            capture_output=True, text=True
        )
        pids = result.stdout.strip().split("\n")
        for pid in pids:
            if pid.strip():
                subprocess.run(["kill", pid.strip()], capture_output=True)
                log.info(f"[AUTO_REPAIR] 終止舊 brain_server PID={pid.strip()}")
        await asyncio.sleep(2)

        # 重新啟動
        log.info(f"[AUTO_REPAIR] 啟動新 brain_server...")
        subprocess.Popen(
            [sys.executable, "-u", str(_BRAIN_SERVER_PATH)],
            stdout=open(str(_BRAIN_SERVER_PATH.parent.parent / "brain_bot.log"), "a"),
            stderr=subprocess.STDOUT,
            cwd=str(_BRAIN_SERVER_PATH.parent.parent),
        )
        await asyncio.sleep(15)  # 等待啟動

        # 測試是否恢復
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(BRAIN_ASK_URL, json={"question": "health check"})
            answer = r.json().get("answer", "")
            is_fail = ("Could not resolve" in answer or "無法回應" in answer)

        if not is_fail:
            log.info("[AUTO_REPAIR] ✅ brain_server 重啟成功，LLM 恢復正常")
            _LLM_FAIL_COUNT = 0
            _LLM_ALERTED    = False
            asyncio.create_task(_tg(
                "🔧 <b>Sam 自動修復成功</b>\n"
                "brain_server 已重啟，LLM 恢復正常\n"
                "Sam 重新上線 ✅"
            ))
        else:
            raise RuntimeError("重啟後仍然失敗")

    except Exception as e:
        log.error(f"[AUTO_REPAIR] 修復失敗: {e}")
        if not _LLM_ALERTED:
            _LLM_ALERTED = True
            asyncio.create_task(_tg(
                "🚨 <b>Sam 大腦當機！需要你介入</b>\n\n"
                f"LLM 連續失敗 {_LLM_FAIL_COUNT} 次\n"
                "自動重啟 brain_server 失敗\n\n"
                "可能原因：\n"
                "• Anthropic API 服務中斷\n"
                "• API Key 過期或超出額度\n\n"
                "請檢查 brain_server.log 或 Anthropic 帳戶狀態"
            ))

# ─────────────────────────────────────────────
# 核心決策引擎 — 原主靈魂版
# ─────────────────────────────────────────────
async def _sam_decide(market: dict) -> dict:
    """
    原主靈魂決策流程：
    第一問 — 主力視角：這個幣現在主力在幹嘛？
    第二問 — 結構化決策：根據分析給出明確JSON
    知識庫 — 接入2140原主相關段落
    門檻 — 75分以上才動
    """
    import re
    symbol = market.get("symbol", "?")
    price  = market.get("price", 0)

    # 前置過濾（原主邏輯：量縮進行中跳過，但量縮極致是爆發前兆要留意）
    vol_ratio = market.get("vol_ratio", 1)
    vol_extreme = market.get("vol_extreme_compression", False)
    vol_shrinking = market.get("vol_shrinking", False)

    if vol_shrinking and not vol_extreme and vol_ratio < 0.7:
        # 量縮進行中（非極致）→ 等待，不進場
        log.info(f"[PRE-FILTER] {symbol} 量縮進行中(vol_ratio={vol_ratio}, count={market.get('vol_shrink_count')})，等待表態")
        _save_thinking(symbol, market, "量縮等待中", {
            "approved": False, "score": 0,
            "skip_reason": f"量縮進行中(連縮{market.get('vol_shrink_count')}根)，等量縮極致或量放出再判斷"
        })
        return {"approved": False, "reason": "量縮等待，非極致", "score": 0,
                "sl_pct": 0.01, "tp_pct": 0.02, "brain_view": "量縮等待中"}

    # 從2140知識庫取得原主對當前市場狀況的相關見解
    market_query = f"{symbol} {'做空' if market.get('daily_trend')=='bear' else '做多'} 流動性 結構"
    knowledge_ctx = _get_knowledge(market_query, top_k=3)
    # 額外查：假突破與心理
    if market.get("fake_breakout_up") or market.get("fake_breakout_down"):
        knowledge_ctx += _get_knowledge("假突破 洗盤 主力", top_k=2)

    try:
        # ── 第一問：完整靈魂思考 ──
        # 先查形態相關知識
        patterns_found = [p for p in [
            market.get('pattern_abcd'), market.get('pattern_triangle'),
            market.get('pattern_hs'), market.get('pattern_wm')
        ] if p and p.get('detected')]
        if patterns_found:
            ptype = patterns_found[0].get('pattern','')
            knowledge_ctx += _get_knowledge(f"{ptype} 進場 目標 停損", top_k=2)

        # 日內突破單有額外的突破上下文
        intraday_ctx = ""
        if market.get("intraday") and market.get("intraday_context"):
            intraday_ctx = f"""
⚡【日內突破訊號 — 已通過帶量突破前置過濾】
{market['intraday_context']}

這是 Sam 日內突破策略的核心訊號：
- 已確認：收盤突破結構位 + 量 ≥ 1.5x + 實體 ≥ 40%（非影線）
- 你現在的任務：判斷這是「真突破」還是「假突破騙局」
- 重點看：突破位前方有沒有密集套牢區 / 主力是否在這裡出貨 / BTC方向是否配合
- 進場類型建議：{'直接右側進（量≥2.5x）' if market.get('vol_ratio',0)>=2.5 else '等二次確認（量1.5~2.5x）——等回調縮量守穩再進，不追突破K棒'}
- 止損位：{market.get('intraday_sl_price')} （結構失效點，{market.get('intraday_sl_pct')}%）
"""

        q1 = f"""你是 sam，一個正在內化「2140原主」交易思維的 AI。
你的靈魂來自2140五年直播裡說過的每一句判斷，你的目標是把那些思維變成真正的本能。

2140原主的核心哲學：
- 幣圈是「先射箭再畫靶」：主力先行動，消息永遠滯後。
- 多空都要先想劇本，讓市場告訴你哪邊先觸發。
- 不追市，不偏空也不偏多，讓結構和量能說話。
- EMA只是背景，不是分析主軸。
- 所有指標都會失效——2018年熊市BTC跌85%但全程沒跌破200MA。永遠不能只靠一個指標。
- 當所有人看同一個方向，主力可能已準備反向。反市場不是逆勢，是提前看穿。
- 思考順序不可顛倒：主力在幹嘛 → 流動性在哪 → 量能怎樣 → 最後指標背書。
{intraday_ctx}
現在看 {symbol}，當前價格：{price}
{f"""
⚡【關鍵點位觸發通知】
{market.get('watch_context', '')}
重要：這是你之前標記的關鍵點位，現在到了。
不要沿用之前的方向預設，根據此刻的量能、結構、情緒從零判斷。
""" if market.get('watch_context') else ''}

【關鍵結構位】
4H 波段高點={market.get('swing_high_4h')}  低點={market.get('swing_low_4h')}
1H 近高（空頭止損）={market.get('swing_high_1h')}（距今{market.get('dist_to_swing_high_pct',0):+.2f}%）
1H 近低（多頭止損）={market.get('swing_low_1h')}（距今{market.get('dist_to_swing_low_pct',0):+.2f}%）
日線趨勢：{market.get('daily_trend')}  1H趨勢：{market.get('trend_1h')}
BTC大方向：{_BTC_REGIME_CACHE.get('regime','unknown')}（BTC={_BTC_REGIME_CACHE.get('btc_price',0):,.0f} / MA200={_BTC_REGIME_CACHE.get('ma200',0):,.0f}）
宏觀濾鏡：BTC.D={_BTC_REGIME_CACHE.get('btc_dominance',0)}%（{_BTC_REGIME_CACHE.get('dom_trend','flat')}）  OTHERS.D山寨指數={_BTC_REGIME_CACHE.get('others_d',0)}%（{_BTC_REGIME_CACHE.get('others_trend','flat')}）{'　⚠️ 山寨做多逆風（BTC.D升或OTHERS.D跌），山寨多單提高門檻' if _BTC_REGIME_CACHE.get('alt_headwind') else '　山寨資金面中性/順風'}
{'⚡ BTC牛市環境 — 跟多為主，短線空需假突破+背離雙重確認' if _BTC_REGIME_CACHE.get('regime')=='bull' else ''}
{'⚠️ BTC熊市環境 — 跟空為主，日內短多只在底背離+量縮末端' if _BTC_REGIME_CACHE.get('regime')=='bear' else ''}
{'🔄 BTC中性整理 — 多空均需二次確認，等BTC方向選擇' if _BTC_REGIME_CACHE.get('regime')=='neutral' else ''}

【量能 — 主力腳印】
量比：{market.get('vol_ratio')}x  連縮：{market.get('vol_shrink_count')}根
ATR：{market.get('atr_pct',0)*100:.2f}%  RSI 1H={market.get('rsi_1h')} / 4H={market.get('rsi_4h')}
{'⚡ 量縮極致 — 彈簧壓縮到底！在關鍵位掛好訂單等第一根帶量大K，不管噴上噴下跟著走，不要現在市價進' if market.get('vol_extreme_compression') else ''}
{'⚠️ 上漲量縮 — 主力可能在出貨，偏空，不追' if market.get('vol_divergence_bearish') else ''}
{'🔔 下跌量縮極致 — 洗盤末端跡象，等帶量反K確認底部才進' if market.get('vol_compression_bottom') else ''}

【動能背離】
{'📈 RSI多頭背離（快照，僅觀察級） — 價格創低但RSI未創低' if market.get('rsi_bullish_divergence') else ''}
{'📉 RSI空頭背離（快照，僅觀察級） — 價格創高但RSI未創高' if market.get('rsi_bearish_divergence') else ''}
{f'🟢🟢 連續底背離（進場級！主力吸籌完成）— K線低點越來越低但RSI低點越墊越高，強度{market.get("div_bull_strength",0)} → 這才是Sam真正抄底的左側訊號' if market.get('rsi_bull_div_continuous') else ''}
{f'🔴🔴 連續頂背離（進場級！主力出貨）— K線高點越來越高但RSI高點越來越低，強度{market.get("div_bear_strength",0)}' if market.get('rsi_bear_div_continuous') else ''}
{'　提醒：單張快照背離只是警示，不能當進場依據；連續背離才是主力動作完成。' if (market.get('rsi_bullish_divergence') or market.get('rsi_bearish_divergence')) and not (market.get('rsi_bull_div_continuous') or market.get('rsi_bear_div_continuous')) else ''}

【假突破偵測】
上方假突破：{market.get('fake_breakout_up')}  下方假突破：{market.get('fake_breakout_down')}

【MACD 動能背書（輔助層，與RSI同級，不單獨決策）】
MACD柱={market.get('macd_hist')}  {'🟢金叉' if market.get('macd_cross')=='bull' else ('🔴死叉' if market.get('macd_cross')=='bear' else '無交叉')}  {'零軸上方(多頭區)' if market.get('macd_above_zero') else '零軸下方(空頭區)'}

【費波那契回調】
擺動方向={market.get('fib_swing_dir')}  最近關鍵位={market.get('fib_nearest')}
{'⭐ 價格落在 0.618~0.786 黃金口袋 — 若此處量縮守穩=右側回調進場區' if market.get('fib_in_golden_zone') else ''}

【右側進場確認 — 突破後縮量回測守穩（不追突破K棒）】
{f'✅ 多方右側成立：已突破 {market.get("retest_level")}，回踩守穩且量縮 → 這就是Sam等的右側點' if market.get('retest_hold_long') else ''}
{f'✅ 空方右側成立：已跌破 {market.get("retest_level")}，反抽守不住且量縮 → 右側做空點' if market.get('retest_hold_short') else ''}
{'（尚無回測守穩確認 — 若只是剛突破那根，屬於追單，等回踩縮量再進）' if not market.get('retest_hold_long') and not market.get('retest_hold_short') else ''}

【形態識別】
AB=CD等幅：{_fmt_pattern(market.get('pattern_abcd'))}
三角收斂：{_fmt_pattern(market.get('pattern_triangle'))}
頭肩頂/底：{_fmt_pattern(market.get('pattern_hs'))}
W底/M頭：{_fmt_pattern(market.get('pattern_wm'))}
杯柄：{'✅ 杯柄成型' + ('（已突破杯緣' + str(market.get('pattern_cup',{}).get('rim')) + '，目標' + str(market.get('pattern_cup',{}).get('target')) + '）' if market.get('pattern_cup',{}).get('breakout') else '（未突破杯緣，等突破）') if market.get('pattern_cup',{}).get('detected') else '無'}
通道：{(market.get('pattern_channel',{}).get('type','') + '通道，價格在' + market.get('pattern_channel',{}).get('position','') + '緣') if market.get('pattern_channel',{}).get('detected') else '無'}

【均線背景（參考用）】
1H EMA99={market.get('ema99') or 'N/A'}  EMA200={market.get('ema200') or 'N/A'}
日線 EMA20={market.get('ema20_1d') or 'N/A'}  EMA99={market.get('ema99_1d') or 'N/A'}

{"【我過去對類似情況的判斷（2140）】\n" + knowledge_ctx if knowledge_ctx else ""}

請依照以下結構分析（每點簡潔，不要廢話）：

### 1. 主力在幹嘛？
吸籌/洗盤/出貨/無動作 — 說理由，從量能和結構判斷，不是從均線判斷。

### 2. 多頭劇本
- 需要什麼條件才能翻多？（突破哪個關鍵位？需要什麼量能配合？）
- 目標在哪？止損在哪？
- 這個劇本現在的機率有多高？

### 3. 空頭劇本
- 繼續空的條件是什麼？（守不住哪個位？）
- 目標在哪？止損在哪？
- 這個劇本現在的機率有多高？

### 4. 是否有誘多/誘空跡象？（觀察記錄，不急著反向）
- 誘多跡象：剛突破前高但量未放大 / K棒收回突破位 / RSI超買 / 日線偏空方向
- 誘空跡象：剛跌破前低後立刻拉回 / 長下影線 / RSI超賣 / 日線偏多方向
- 現階段做法：
    有跡象 → 在 reason 裡標注「疑似誘多」或「疑似誘空」，降低自信分數
    不急著做反向，等其他維度也支持再說
    這是觀察學習期，記錄比進場更重要

### 5. 哪個劇本更合理？
- 直接說你更傾向哪邊，為什麼（量能/形態/流動性/主力行為，不是因為EMA）
- 如果兩個劇本都不明確，就說「觀望，等觸發條件」

### 5. 現在的操作建議
左側佈局 / 等右側確認 / 觀望 — 以及具體的觸發條件"""

        # ── 形態學白名單：有突破才畫圖送 Vision ──────────────────
        breakout_pat, breakout_key = _get_breakout_pattern(market)
        chart_b64 = ""
        if breakout_pat:
            timeframe_label = "15m" if "15m" in breakout_key else "1H"
            src = market.get("_d15m") if "15m" in breakout_key else market.get("_d1h")
            rsi_vals = []
            if src:
                try:
                    rsi_vals = [_calc_rsi(src["closes"][:i+1], 14)
                                for i in range(14, len(src["closes"]))]
                except Exception:
                    rsi_vals = []
                chart_b64 = _generate_pattern_chart_b64(
                    symbol      = symbol,
                    opens       = src["opens"],
                    highs       = src["highs"],
                    lows        = src["lows"],
                    closes      = src["closes"],
                    volumes     = src["volumes"],
                    pattern_info= breakout_pat,
                    rsi_values  = rsi_vals,
                    timeframe   = timeframe_label,
                )
            if chart_b64:
                log.info(f"[CHART] {symbol} 偵測到 {breakout_pat.get('pattern')} 突破，送圖給大腦 Vision")

        async with httpx.AsyncClient(timeout=45) as client:
            payload1 = {"question": q1}
            if chart_b64:
                payload1["chart_b64"] = chart_b64
            r1 = await client.post(BRAIN_ASK_URL, json=payload1)
            brain_view = r1.json().get("answer", "")

        # ── LLM 健康檢查 ────────────────────────────────────────────
        global _LLM_FAIL_COUNT, _LLM_ALERTED
        _is_llm_fail = ("Could not resolve authentication" in brain_view or
                        "LLM 暫時無法回應" in brain_view or
                        "無法回應" in brain_view)
        if _is_llm_fail:
            _LLM_FAIL_COUNT += 1
            log.warning(f"[LLM_HEALTH] 連續失敗 {_LLM_FAIL_COUNT} 次")
            if _LLM_FAIL_COUNT >= 3:
                await _auto_repair_brain_server()
        else:
            if _LLM_FAIL_COUNT > 0:
                log.info(f"[LLM_HEALTH] ✅ LLM 恢復正常（之前連續失敗 {_LLM_FAIL_COUNT} 次）")
                if _LLM_ALERTED:
                    asyncio.create_task(_tg("🟢 <b>Sam 大腦恢復正常</b>\nLLM 已恢復回應，Sam 重新上線"))
            _LLM_FAIL_COUNT = 0
            _LLM_ALERTED    = False

        log.info(f"[BRAIN_v2] {symbol}: {brain_view[:100]}...")

        # ── 第二問：結構化決策 ──
        q2 = f"""根據你對 {symbol} 的完整分析：
「{brain_view[:800]}」

現在給出交易決策。

【sam 的進場哲學（這些是你自己的原則）】

合約做法（你現在在做合約）：
  右側為主 — 等突破確認才進，止損清晰
  左側極少做 — 只在底部結構完整（W底/頭肩底頸線已突破）才考慮

【⛔ 絕對禁止的進場情況 — 違反 Sam 核心原則】
1. 逆勢單沒有右側確認：趨勢向下時做多、趨勢向上時做空，必須等右側突破確認，不能只靠背離或型態預判就進場
2. AB=CD 超跌後直接做多：價格已超過 D 點目標，代表趨勢動能強，不是反彈機會，是危險區域
3. 單一時框 RSI 背離就進場：背離必須 1H + 4H 同時出現，單一時框背離不算確認
4. 型態未定型就進場：頭肩底/W底 右肩還沒確認、頸線還沒突破，禁止提前進場
5. M頭/W底 辨識錯誤就進場：必須確認有兩個相近高/低點，單邊下跌的 AB=CD 不是 M頭
6. M頭破頸後做多：M頭是空頭型態，破頸後的反彈是「回測頸線做空」的機會，不是做多訊號。正確操作：等反彈至頸線位置、確認頸線守住不漲過 → 做空，目標 = 頸線往下等幅
7. W底破頸後做空：同理，W底是多頭型態，破頸後的回測是「回測頸線做多」的機會，不是做空訊號
8. 只有超賣/背離/底部猜測就做多：RSI超賣、底背離、AB=CD到達D點，這些都只是「可能反轉的警示」，不是進場訊號。多單必須等價格實際突破並收盤站上某個明確關鍵壓力位（頸線、前高、下降趨勢線、重要均線）才進場。「超賣不會更超賣」這種思維在趨勢市場會死得很難看。
9. 空單只有超買/頂背離就做空：同理，空單必須等價格實際跌破並收盤站下某個明確支撐位（頸線、前低、上升趨勢線）才進場。

【策略分類 — 必須從以下選一個最符合的，不能全寫EMA】
你必須根據最強訊號選擇策略類型：

1. FAKE_BREAKOUT   — 假突破（觀察學習階段）：
     誘多跡象：突破前高後量未持續 / K棒有長上影線收回 / RSI超買 / 日線偏空
     誘空跡象：跌破前低後立刻拉回 / 下影線超長 / RSI超賣 / 日線偏多
     ⚠️ 目前階段：不急著做反向單，先觀察記錄
       → 若有誘多/空跡象，在 reason 裡標注「疑似誘多」或「疑似誘空」
       → 按正常邏輯判斷，若整體不夠強（<75分）就跳過
       → 若確實進場了，事後對比結果，這是學習樣本
       → 累積足夠樣本後再考慮反向策略
2. VOLUME_SQUEEZE  — 量縮爆發：量縮極致後等第一根帶量突破才進，量縮本身不進場
3. PATTERN_ABCD    — AB=CD等幅：B點回撤結束，C到D等幅做反轉
4. PATTERN_HS      — 頭肩頂/底：右肩形成，頸線突破確認方向
5. PATTERN_WM      — W底/M頭：雙底/雙頂頸線突破，測算目標
6. PATTERN_TRIANGLE — 三角收斂突破：收斂末端放量突破，做方向
7. RSI_DIVERGE     — RSI背離：價格創新高/低但RSI反向，動能衰竭信號
8. LIQUIDITY_HUNT  — 流動性獵殺：主力先掃止損（誘空後拉）或誘多後砸
     與 FAKE_BREAKOUT 的區別：
     FAKE_BREAKOUT = 突破後回縮，你抓回縮那根進場
     LIQUIDITY_HUNT = 主力先掃流動性集中區（止損群），掃完才是真方向
     ⚠️ LIQUIDITY_HUNT 進場必須用 right_side 或 watch_breakout，禁止 left_side：
        掃止損那根K棒 = 主力還在行動中，不是進場點
        必須等「下一根帶量反轉K棒」收盤確認方向才進
        RSI超賣 + 放量 ≠ 底部確認，那只是獵殺的開始
        若沒有反轉K棒 → 填 entry_type="watch_breakout"，等確認後系統重跑分析
9. STRUCTURE_BREAK — 結構突破：突破前高/前低+量能確認，趨勢延續
10. EMA_PULLBACK   — EMA回踩（最後選擇）：只有完全沒有其他更強訊號時才用

【市場是活的 — 分析要多維度，不能只靠單一工具】
所有指標都有失效的時候。2018熊市BTC跌85%，全程沒跌破200MA——
EMA有用，但它只是眾多視角之一，不是真理。

【評分時要考慮的維度（每個都是參考，不是單選題）】
① 主力行為：量能是否有主力進場的痕跡？成交量異常放大？
② 流動性：哪裡有止損集中區？主力可能往哪裡掃？
③ 市場結構：前高前低有沒有守住？有沒有假突破形態？
④ 形態訊號：三角收斂/頭肩/W底M頭有沒有觸發？
⑤ 量能確認：突破是否有量？量縮突破要打折扣
⑥ RSI多空：超買超賣 + 背離訊號一起看
⑦ 均線背景（EMA/SMA）：方向參考，但單獨靠它不足以進場

評分邏輯：
- 只有①的訊號 → 最多65分
- ①+②或①+③ → 最多72分
- ①+②+③ 三者一致 → 可以到80+
- 再加形態/量能確認 → A+（85+）

【多空劇本都想過了嗎？】
進場前先問自己反向劇本：
- 做多：什麼情況會讓我錯？主力出貨的位置在哪？
- 做空：什麼情況會讓我錯？主力吸籌的位置在哪？
反向劇本越強，分數越低

✅ 進場條件：多個維度指向同一方向，且有明確止損位
❌ 不進場：追市 / 量縮中 / 只有EMA一個理由 / 自己都覺得「勉強」

【entry_type 三種選法 — 必須選一個】
① right_side   → Sam 的主場（74%操作）。定義不是「現在漲了就追」，而是：
                 價格已『突破』關鍵位 → 回踩該位 → 縮量守穩沒跌回 → 才進。
                 ⚠️ 突破當根直接市價追 = 追單，不是右側。沒看到回測守穩就用 watch_breakout。
                 上方數據若顯示「右側進場確認成立」(retest_hold)，那才是真右側點，可加分。
② left_side    → 提前在結構低/高點佈局，接受被洗風險。
                 只在『連續背離吸籌完成 + 假突破洗盤確認』的底部才用，且系統會自動縮倉50%。
③ watch_breakout → 這個點位很關鍵，但現在不到位或方向未明
                   → 不鎖死方向，等價格到達那個關鍵位後，重新判斷此刻情緒再進場
                   → 系統會在價格接近時重跑完整分析，你那時候再決定做多還是做空

⚠️ 關鍵原則：越是靠近重要支撐/壓力，越不能鎖死方向
   早上看多 → 掛限價買 → 下午市場反轉 → 同樣的點位變成空頭訊號 → 打止損
   這就是「早鎖死方向」的代價。watch_breakout 就是為了避免這個。

⚠️ 如果你的 reason 有「等待」「離進場區還有距離」「掛單」「埋伏」等字眼
   → 必須用 entry_type="watch_breakout"，填 watch_level（你認為重要的那個價位）
   → side 填你當下認為的傾向，但系統到了那個點位會重新判斷，不一定沿用

watch_breakout 範例：
  "entry_type": "watch_breakout",
  "watch_level": 0.0372,
  "watch_condition": "breakdown_below",   ← 或 "breakout_above"（哪個方向接近就填哪個）
  "approved": false,
  "reason": "0.0372是前低支撐，若跌破這裡市場情緒確認偏空，到了再重新判斷"

目前數據：
量比={market.get('vol_ratio')}  RSI 1H={market.get('rsi_1h')} / 4H={market.get('rsi_4h')}
量縮極致={market.get('vol_extreme_compression')}  上漲量縮={market.get('vol_divergence_bearish')}
假突破上={market.get('fake_breakout_up')}  假突破下={market.get('fake_breakout_down')}
偵測到的形態：{'、'.join([p.get('pattern','') for p in [market.get('pattern_abcd'), market.get('pattern_hs'), market.get('pattern_wm'), market.get('pattern_triangle')] if p and p.get('detected')]) or '無'}

【⚠️ 止損設置原則（2140核心思維）— 每種策略止損邏輯不同，不能以偏概全】
止損 ≠ 「我願意虧幾%」，止損 = 「我的進場論點在哪裡被市場否定的位置」

第一步：先確認你用的是哪種策略形態
第二步：套用該形態對應的止損邏輯
第三步：算出從進場價到那個「被否定位」的%距離 → 這才是 sl_pct

各策略形態的止損放置邏輯：
┌─────────────────────────────────────────────────────────────────┐
│ W底/雙底做多   → SL守右腳低點下方0.3%  ← 跌破=W底無效          │
│ M頂/雙頂做空   → SL守右頂高點上方0.3%  ← 突破=空頭失效          │
│ 三角收斂做多   → SL守突破K棒低點下方0.3%（保守：收斂下緣下方）    │
│ 三角收斂做空   → SL守突破K棒高點上方0.3%（保守：收斂上緣上方）    │
│ 頭肩底做多     → SL守右肩低點下方0.3%  ← 不是頸線，是右肩！      │
│ 頭肩頂做空     → SL守右肩高點上方0.3%  ← 不是頸線，是右肩！      │
│ 杯柄做多       → SL守柄部最低點下方0.3%                          │
│ 假突破做空     → SL守假突破K棒最高點上方0.3% ← 繼續漲=真突破     │
│ 假跌破做多     → SL守假跌破K棒最低點下方0.3% ← 繼續跌=真跌破     │
│ 上升通道回測   → SL守通道下緣下方0.5%  ← 實體K棒收在外才算破     │
│ 下降通道回測   → SL守通道上緣上方0.5%                            │
│ 量縮爆發突破   → SL守突破K棒低點下方0.3% ← 最精準，SL可以近      │
│ RSI底部背離    → SL守背離最低點下方0.3%                          │
│ 流動性獵殺反轉 → SL守獵殺K棒最低點下方0.5%                       │
└─────────────────────────────────────────────────────────────────┘

⚠️ 避免整數關口（30000/50000等），主力最喜歡掃那裡
⚠️ SL < 1.5%：除非量縮爆發突破，否則太容易被洗
⚠️ SL > 8%：論點不清晰，建議不進場

⚠️ 重要：只輸出純 JSON 物件，第一個字元必須是 {{，最後一個字元必須是 }}，絕對不要加 markdown、不要加說明文字、不要加 ```json：
{{"approved": true/false, "side": "long"/"short", "strategy_type": "上方10個策略之一", "reason": "【策略名稱】具體描述", "score": 0-100, "entry_type": "right_side"/"left_side"/"watch_breakout", "watch_level": 0, "watch_condition": "", "sl_pct": 0.02, "tp_pct": 0.03, "leverage": 2, "skip_reason": "", "pattern_target": 0, "trap_observation": null}}

score：85+=A+ / 75-84=A / 60-74=B觀察 / <60忽略
leverage：85+低波動→3-5x / 75-84→2-3x / 左側預判→1-2x"""

        async with httpx.AsyncClient(timeout=30) as client:
            r2 = await client.post(BRAIN_ASK_URL, json={"question": q2})
            answer2 = r2.json().get("answer", "{}")

        import re as _re
        # 去除 markdown、多餘前後文字，保留 JSON 物件
        clean = _re.sub(r'```(?:json)?\s*|\s*```', '', answer2).strip()
        result = None

        # 嘗試 1：直接 parse（格式正確時最快）
        try:
            result = json.loads(clean)
        except json.JSONDecodeError:
            pass

        # 嘗試 2：找最外層完整 { ... }（支援巢狀，處理前後有說明文字）
        if result is None:
            start = clean.find('{')
            if start != -1:
                depth, end = 0, -1
                for idx, ch in enumerate(clean[start:], start):
                    if ch == '{': depth += 1
                    elif ch == '}':
                        depth -= 1
                        if depth == 0:
                            end = idx + 1
                            break
                if end > start:
                    try:
                        result = json.loads(clean[start:end])
                    except json.JSONDecodeError:
                        pass

        # 嘗試 3：用 regex 抽 key-value（最後手段，應對截斷或格式錯亂）
        if result is None:
            try:
                approved_m = _re.search(r'"approved"\s*:\s*(true|false)', clean)
                score_m    = _re.search(r'"score"\s*:\s*(\d+)', clean)
                side_m     = _re.search(r'"side"\s*:\s*"(long|short)"', clean)
                reason_m   = _re.search(r'"reason"\s*:\s*"([^"]*)"', clean)
                if approved_m and score_m:
                    result = {
                        "approved": approved_m.group(1) == "true",
                        "score":    int(score_m.group(1)),
                        "side":     side_m.group(1) if side_m else "long",
                        "reason":   reason_m.group(1) if reason_m else "regex解析",
                        "sl_pct": 0.02, "tp_pct": 0.03, "leverage": 2,
                    }
                    log.warning(f"[DECIDE_v2] {symbol}: JSON截斷，用regex補救 score={result['score']}")
            except Exception:
                pass
        if result is not None:
            score = result.get("score", 0)

            # 硬性規則 0：逆日線方向 = 抄底/摸頭，必須是 Sam 那種「吸籌完成」的左側
            # （2026-06-11 改寫，取代原「無例外封殺」鈍器）
            # Sam 真實邏輯：逆大方向不是禁止，而是門檻極高 —— 只在主力吸籌/出貨
            # 完成的證據出現時才做。證據 = 連續背離（不是單張快照）+ 假突破洗盤確認，
            # 且宏觀（BTC環境 / BTC.D）不能同向打臉。其餘一律擋（這就是之前 SUI/ETH/
            # NEAR/GRASS/SOL 抄底吞滿倉的根源）。
            _daily = market.get("daily_trend", "neutral")
            _side  = result.get("side", "long")
            _alt_headwind = _BTC_REGIME_CACHE.get("alt_headwind", False)
            _btc_regime   = _BTC_REGIME_CACHE.get("regime", "neutral")

            if _daily == "bear" and _side == "long" and result.get("approved"):
                _has_accum = (market.get("rsi_bull_div_continuous") and
                              (market.get("fake_breakout_down") or
                               market.get("vol_compression_bottom")))
                _macro_block = (_btc_regime == "bear") or _alt_headwind
                if not _has_accum or _macro_block:
                    result["approved"]   = False
                    if _macro_block and _has_accum:
                        result["skip_reason"] = "日線偏空+宏觀同向打臉（BTC熊/BTC.D升），抄底等宏觀轉向"
                    else:
                        result["skip_reason"] = "日線偏空做多需『連續底背離+假跌破洗盤』吸籌完成證據，目前只是猜底，跳過"
            if _daily == "bull" and _side == "short" and result.get("approved"):
                _has_distrib = (market.get("rsi_bear_div_continuous") and
                                market.get("fake_breakout_up"))
                if not _has_distrib:
                    result["approved"]   = False
                    result["skip_reason"] = "日線偏多做空需『連續頂背離+假突破出貨』證據，目前只是摸頭，跳過"

            # 硬性規則 1：低於75分不批准（這是原主的底線，不是過濾，是自律）
            if score < MIN_SCORE:
                result["approved"] = False
                if not result.get("skip_reason"):
                    result["skip_reason"] = f"分數{score}<{MIN_SCORE}，原主靈魂：低於A級不動手"

            # 硬性規則 2：量縮不進場（量能是主力腳印，量縮代表主力不在）
            if market.get("vol_ratio", 1) < 0.8 and result.get("approved"):
                result["approved"] = False
                result["skip_reason"] = f"量比{market.get('vol_ratio')}過低，量縮不進場"

            # 軟性警示：vol_ratio = 1.0 疑似資料異常，記錄但不強制擋（Sam自己判斷）
            if market.get("vol_ratio", 1) == 1.0 and result.get("approved"):
                result["data_warning"] = "量比=1.0，可能是量能資料未正常取得，請Sam納入不確定性考量"

            # 軟性警示：reason 含猶豫字眼，降低分數而非直接拒絕
            reason_text = result.get("reason", "")
            hesitation_words = ["勉強", "不算強", "不確定", "勉為其難", "將就", "湊合"]
            matched = next((w for w in hesitation_words if w in reason_text), None)
            if matched and result.get("approved"):
                adjusted = max(score - 10, 0)
                result["score"] = adjusted
                result["score_adjusted"] = f"reason含猶豫字眼'{matched}'，分數從{score}降至{adjusted}"
                if adjusted < MIN_SCORE:
                    result["approved"] = False
                    result["skip_reason"] = f"猶豫感覺+分數降至{adjusted}<75，原主：任何勉強都不動手"

            result["brain_view"]       = brain_view
            result["market_snapshot"]  = market
            result["decided_at"]       = datetime.now(TZ8).isoformat()
            result["knowledge_used"]   = bool(knowledge_ctx)
            result["entry_type"]       = result.get("entry_type", "right_side")

            log.info(f"[DECIDE_v2] {symbol}: approved={result.get('approved')} "
                     f"score={score} side={result.get('side')} | {result.get('reason','')[:60]}")
            _save_thinking(symbol, market, brain_view, result)
            return result

        log.warning(f"[DECIDE_v2] {symbol}: JSON解析失敗: {answer2[:100]}")
        return {"approved": False, "reason": "解析失敗", "score": 0,
                "sl_pct": 0.01, "tp_pct": 0.02, "brain_view": brain_view}

    except Exception as e:
        log.error(f"[DECIDE_v2] {symbol} 錯誤: {e}")
        return {"approved": False, "reason": str(e), "score": 0,
                "sl_pct": 0.01, "tp_pct": 0.02, "brain_view": ""}

# ─────────────────────────────────────────────
# 虛擬進場（分段TP版）
# ─────────────────────────────────────────────
def virtual_enter(state: dict, signal: dict, decision: dict) -> dict | None:
    symbol   = signal.get("symbol", "BTCUSDT")
    side     = decision.get("side", "long")
    price    = signal.get("price", 0)
    if not price:
        return None

    # 跳過已持倉
    if symbol in state["positions"]:
        return None

    risk_amt = state["equity"] * RISK_PER_TRADE

    # ── 左側 / 逆勢單自動縮倉（Sam：左側預判只用 1-2x，接受被洗就不能下重手）──
    # 左側佈局、或方向與個幣日線相反 = 試探性抄底/摸頭，風險砍半。
    _et   = decision.get("entry_type", "right_side")
    _dt   = signal.get("daily_trend", "neutral")
    _counter_trend = (side == "long" and _dt == "bear") or (side == "short" and _dt == "bull")
    _size_note = ""
    if _et == "left_side" or _counter_trend:
        risk_amt *= 0.5
        _size_note = ("左側佈局" if _et == "left_side" else "逆日線試探") + "，風險砍半至50%"
        log.info(f"[SIZE_DOWN] {symbol} {_size_note}（risk {risk_amt:.2f}U）")

    sl_pct   = decision.get("sl_pct", 0.015)
    # LLM 有時回傳百分比（如 3.8）而非小數（0.038），自動修正
    if sl_pct > 1:
        sl_pct = sl_pct / 100
    # 止損上限保護：超過 12% 視為 LLM 異常，拒絕進場
    if sl_pct > 0.12:
        log.warning(f"[ENTER_REJECT] {symbol} sl_pct={sl_pct*100:.1f}% 超過上限 12%，拒絕進場（LLM 異常）")
        return None
    tp1_pct  = decision.get("tp_pct", 0.03)    # TP1 = 2R 目標
    if tp1_pct > 1:
        tp1_pct = tp1_pct / 100
    tp2_pct  = tp1_pct * 1.75                   # TP2 ≈ 3.5R
    tp3_pct  = tp1_pct * 2.75                   # TP3 ≈ 5.5R
    leverage = max(1, min(5, int(decision.get("leverage", 2))))

    if side == "long":
        sl_price  = price * (1 - sl_pct)
        tp1_price = price * (1 + tp1_pct)
        tp2_price = price * (1 + tp2_pct)
        tp3_price = price * (1 + tp3_pct)
    else:
        sl_price  = price * (1 + sl_pct)
        tp1_price = price * (1 - tp1_pct)
        tp2_price = price * (1 - tp2_pct)
        tp3_price = price * (1 - tp3_pct)

    # ── LIQUIDITY_HUNT 反轉確認保護（2026-06-09升級）──
    # SOL 案例：掃止損當根直接進場（intraday_candles_elapsed=0）→ 5分鐘止損
    # Sam 靈魂：「假突破後方向確認才是真方向」，獵殺完成≠立刻反轉，要等反轉K棒
    # 規則：LIQUIDITY_HUNT 策略，entry_type 禁止 left_side（掃止損當根）
    strategy_type = decision.get("strategy_type", "")
    entry_type    = decision.get("entry_type", "right_side")
    if strategy_type == "LIQUIDITY_HUNT" and entry_type == "left_side":
        log.warning(f"[ENTER_REJECT] {symbol} LIQUIDITY_HUNT+left_side 拒絕：獵殺當根不進場，等下一根反轉K棒確認")
        return None

    # ── SL 合理性檢查：止損必須在入場價的正確方向，且不能是負數 ──
    if sl_price <= 0:
        log.warning(f"[ENTER_REJECT] {symbol} SL={sl_price:.6g} <= 0，拒絕進場（LLM sl_pct 異常: {decision.get('sl_pct')}）")
        return None
    if side == "long" and sl_price >= price:
        log.warning(f"[ENTER_REJECT] {symbol} LONG 但 SL={sl_price:.6g} >= entry={price:.6g}，拒絕進場")
        return None
    if side == "short" and sl_price <= price:
        log.warning(f"[ENTER_REJECT] {symbol} SHORT 但 SL={sl_price:.6g} <= entry={price:.6g}，拒絕進場")
        return None

    sl_dist  = abs(price - sl_price)
    # ⚠️ 槓桿不應放大風險！
    # 正確邏輯：qty = risk_amt / sl_dist（止損觸發時損失剛好 = risk_amt）
    # 錯誤邏輯：qty = risk_amt * leverage / sl_dist（槓桿倍數放大了風險金額）
    # 槓桿在期貨中只影響保證金需求（margin = notional / leverage），不影響 P&L
    qty      = risk_amt / sl_dist if sl_dist > 0 else 0
    notional = qty * price

    # notional 上限保護：單倉不超過資金 40%，避免過度集中
    MAX_NOTIONAL = state["equity"] * 0.40
    if notional > MAX_NOTIONAL:
        qty      = MAX_NOTIONAL / price
        notional = MAX_NOTIONAL
        log.info(f"[POSITION_CAP] {symbol} notional 超過40%上限，縮減至 {notional:.1f}U")

    # qty=0 代表 sl_dist=0（SL 跟進場價一樣），無法計算倉位，拒絕進場
    if qty <= 0 or notional <= 0:
        log.warning(f"[ENTER_REJECT] {symbol} qty={qty:.4f} notional={notional:.4f}，sl_dist={sl_dist:.8f}，拒絕進場")
        return None

    trade_id = f"SB_{uuid.uuid4().hex[:8]}"
    position = {
        "id":          trade_id,
        "symbol":      symbol,
        "side":        side,
        "entry_px":    price,
        "sl_price":    sl_price,
        "tp_price":    tp1_price,    # 當前TP目標（隨段位推進）
        "tp1_price":   tp1_price,
        "tp2_price":   tp2_price,
        "tp3_price":   tp3_price,
        "tp_stage":    1,            # 目前在第幾段（1/2/3）
        "sl_pct":      sl_pct,
        "tp_pct":      tp1_pct,
        "qty":         round(qty, 6),
        "qty_remaining": round(qty, 6),   # 剩餘數量
        "notional":    round(notional, 2),
        "risk_amt":    round(risk_amt, 2),
        "leverage":    leverage,
        "reason":      decision.get("reason", ""),
        "score":       decision.get("score", 0),
        "daily_trend": signal.get("daily_trend", ""),
        "rsi_1h":      signal.get("rsi_1h", 50),
        "vol_ratio":   signal.get("vol_ratio", 1),
        "knowledge_used": decision.get("knowledge_used", False),
        "source":      "sam_v2",
        "entered_at":  datetime.now(TZ8).isoformat(),
        # 日內單專用欄位
        "intraday":       signal.get("intraday", False),
        "intraday_tf":    signal.get("intraday_tf", "15m"),   # 計算K棒的時間框架
        "intraday_candles_elapsed": 0,                         # 已過幾根K棒
        "intraday_entry_candle_ts": None,                      # 進場那根K棒的開盤時間（UTC）
    }

    state["positions"][symbol] = position
    log.info(f"[ENTER_v2] {side.upper()} {symbol} @ {price:.6g}  "
             f"SL={sl_price:.6g}  TP1={tp1_price:.6g}  TP2={tp2_price:.6g}  "
             f"score={decision.get('score')}  lev={leverage}x")

    score      = decision.get("score", 0)
    score_bar  = "█" * (score // 10) + "░" * (10 - score // 10)
    brain_view = decision.get("brain_view", "")
    preview    = brain_view[:180].replace("\n", " ") + "..." if len(brain_view) > 180 else brain_view
    side_txt   = "🟢 做多" if side == "long" else "🔴 做空"
    trend_txt  = "📈多頭" if signal.get("daily_trend") == "bull" else \
                 "📉空頭" if signal.get("daily_trend") == "bear" else "➡️中性"
    kb_txt     = "✅ 已參考2140知識庫" if decision.get("knowledge_used") else ""

    # 自動截圖進場當下 K 線圖
    tf = signal.get("intraday_tf", "15m")
    interval = {"1m":"1","3m":"3","5m":"5","15m":"15","30m":"30","1h":"60","4h":"240"}.get(tf, "15")
    _capture_chart(symbol, source="sam", interval=interval,
                   note=f"{side} score={decision.get('score',0)} @ {price:.6g}")

    asyncio.create_task(_tg(
        f"🧠 <b>sam v2 決定進場</b>\n"
        f"━━━━━━━━━━━━━━━━\n\n"
        f"<b>📊 市場分析</b>\n"
        f"  {symbol}  日線：{trend_txt}\n"
        f"  RSI(1H)={signal.get('rsi_1h')}  量比={signal.get('vol_ratio')}x\n"
        f"  流動性高點：{signal.get('swing_high_1h')}  低點：{signal.get('swing_low_1h')}\n"
        f"  {'⚠️ 假突破上方偵測到' if signal.get('fake_breakout_up') else ''}"
        f"{'⚠️ 假突破下方偵測到' if signal.get('fake_breakout_down') else ''}\n\n"
        f"<b>💭 主力思維</b>\n"
        f"  {preview}\n"
        f"  {kb_txt}\n\n"
        f"<b>⚡ 決策</b>\n"
        f"  {side_txt}  {leverage}x槓桿\n"
        f"  信心 [{score_bar}] {score}/100\n"
        f"  進場：{price:.6g}\n"
        f"  止損：{sl_price:.6g}（{sl_pct*100:.1f}%）\n"
        f"  TP1：{tp1_price:.6g}（出場33%，移保本）\n"
        f"  TP2：{tp2_price:.6g}（出場剩50%，移TP1）\n"
        f"  TP3：{tp3_price:.6g}（全出）\n"
        f"  倉位：{notional:.1f}U  風險：{risk_amt:.2f}U\n\n"
        f"💬 <i>{decision.get('reason','')}</i>"
    ))
    return position

# ─────────────────────────────────────────────
# 持倉管理 — 分段TP + 移動止損
# ─────────────────────────────────────────────
async def check_positions(state: dict):
    if not state["positions"]:
        return

    to_close = []
    to_update = []

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            for symbol, pos in list(state["positions"].items()):
                try:
                    r = await client.get(
                        f"https://fapi.binance.com/fapi/v1/ticker/price?symbol={symbol}"
                    )
                    px = float(r.json()["price"])
                except Exception:
                    continue

                side    = pos["side"]
                entry   = pos["entry_px"]
                sl      = pos["sl_price"]
                tp1     = pos["tp1_price"]
                tp2     = pos.get("tp2_price", tp1 * 1.5)
                tp3     = pos.get("tp3_price", tp1 * 2.0)
                stage   = pos.get("tp_stage", 1)
                qty     = pos.get("qty_remaining", pos["qty"])

                # ── 日內單：3根K棒強制離場（INTRADAY_BAR_EXIT=False 時停用）──
                if INTRADAY_BAR_EXIT and pos.get("intraday"):
                    tf    = pos.get("intraday_tf", "15m")
                    tf_sec = 15 * 60 if tf == "15m" else 5 * 60
                    # 計算進場後已過幾根完整K棒
                    entered_at_str = pos.get("entered_at", "")
                    try:
                        from datetime import timezone as _tz
                        entered_dt = datetime.fromisoformat(entered_at_str.replace("Z", "+00:00"))
                        now_dt     = datetime.now(_tz.utc)
                        elapsed_sec = (now_dt - entered_dt).total_seconds()
                        candles_elapsed = int(elapsed_sec // tf_sec)
                        pos["intraday_candles_elapsed"] = candles_elapsed
                    except Exception:
                        candles_elapsed = 0

                    if candles_elapsed >= 3:
                        pnl   = (px - entry) * qty if side == "long" else (entry - px) * qty
                        pnl_r = pnl / pos["risk_amt"] if pos["risk_amt"] else 0
                        to_close.append({
                            "symbol": symbol, "pos": pos,
                            "exit_px": px, "pnl": round(pnl, 4),
                            "pnl_r": round(pnl_r, 3),
                            "outcome": "win" if pnl > 0 else "loss",
                            "exit_why": f"⏰ 日內3根{tf}K棒強制離場",
                            "qty_closed": qty, "full_close": True,
                        })
                        log.info(f"[INTRADAY_FORCE_EXIT] {symbol} 已過 {candles_elapsed} 根{tf}K棒，強制平倉 @ {px:.6g}  pnl={pnl:+.4f}U")
                        continue  # 不再判斷SL/TP

                hit_sl = (side == "long"  and px <= sl) or \
                         (side == "short" and px >= sl)

                # TP 分段判斷
                hit_tp1 = stage == 1 and (
                    (side == "long"  and px >= tp1) or
                    (side == "short" and px <= tp1)
                )
                hit_tp2 = stage == 2 and (
                    (side == "long"  and px >= tp2) or
                    (side == "short" and px <= tp2)
                )
                hit_tp3 = stage == 3 and (
                    (side == "long"  and px >= tp3) or
                    (side == "short" and px <= tp3)
                )

                if hit_sl:
                    pnl = (px - entry) * qty if side == "long" else (entry - px) * qty
                    pnl_r = pnl / pos["risk_amt"] if pos["risk_amt"] else 0
                    to_close.append({
                        "symbol": symbol, "pos": pos,
                        "exit_px": sl, "pnl": round(pnl, 4),
                        "pnl_r": round(pnl_r, 3),
                        "outcome": "win" if pnl > 0 else "loss",
                        "exit_why": "SL（止損/保本）", "qty_closed": qty, "full_close": True,
                    })

                elif hit_tp3:
                    pnl = (tp3 - entry) * qty if side == "long" else (entry - tp3) * qty
                    pnl_r = pnl / pos["risk_amt"] if pos["risk_amt"] else 0
                    to_close.append({
                        "symbol": symbol, "pos": pos,
                        "exit_px": tp3, "pnl": round(pnl, 4),
                        "pnl_r": round(pnl_r, 3),
                        "outcome": "win", "exit_why": "TP3 全出", "full_close": True,
                    })

                elif hit_tp2:
                    # 出場剩餘的 50%，SL 移到 TP1
                    close_qty = qty * 0.5
                    pnl = (tp2 - entry) * close_qty if side == "long" else (entry - tp2) * close_qty
                    to_update.append({
                        "symbol": symbol,
                        "new_sl": tp1,          # SL 移到 TP1
                        "new_tp": tp3,           # 下個目標 TP3
                        "new_qty": qty * 0.5,
                        "qty_closed": close_qty,
                        "tp_stage": 3,
                        "partial_pnl": round(pnl, 4),
                        "exit_px": tp2,
                        "exit_why": "TP2 出50%，SL移TP1",
                    })

                elif hit_tp1:
                    # 出場 33%，SL 移到保本（入場價）
                    close_qty = qty * 0.33
                    pnl = (tp1 - entry) * close_qty if side == "long" else (entry - tp1) * close_qty
                    to_update.append({
                        "symbol": symbol,
                        "new_sl": entry,        # SL 移到保本
                        "new_tp": tp2,          # 下個目標 TP2
                        "new_qty": qty * 0.67,
                        "qty_closed": close_qty,
                        "tp_stage": 2,
                        "partial_pnl": round(pnl, 4),
                        "exit_px": tp1,
                        "exit_why": "TP1 出33%，SL移保本",
                    })

    except Exception as e:
        log.error(f"[CHECK] 錯誤: {e}")

    # 執行部分出場
    for upd in to_update:
        sym = upd["symbol"]
        if sym not in state["positions"]:
            continue
        pos = state["positions"][sym]
        partial_pnl = upd["partial_pnl"]
        state["equity"]  = round(state["equity"]  + partial_pnl, 4)
        state["capital"] = round(state["capital"] + partial_pnl, 4)
        pos["sl_price"]      = upd["new_sl"]
        pos["tp_price"]      = upd["new_tp"]
        pos["qty_remaining"] = round(upd["new_qty"], 6)
        pos["tp_stage"]      = upd["tp_stage"]
        # ✅ 累計記錄部分已實現盈虧（讓 TG bot / dashboard 能正確統計）
        pos["partial_pnl"]   = round(pos.get("partial_pnl", 0) + partial_pnl, 4)

        log.info(f"[PARTIAL] {upd['exit_why']} {sym}  "
                 f"出場 pnl={partial_pnl:+.4f}U  剩餘qty={pos['qty_remaining']:.6f}  "
                 f"新SL={pos['sl_price']:.6g}  新TP={pos['tp_price']:.6g}")

        # ✅ 寫入成交紀錄（type=partial，讓儀表板「成交」tab 能顯示）
        partial_trade = {
            **pos,
            "id":           pos["id"] + f"_tp{upd['tp_stage']}",
            "exit_px":      upd["exit_px"],
            "exit_why":     upd["exit_why"],
            "pnl":          partial_pnl,
            "pnl_r":        round(partial_pnl / pos.get("risk_amt", 10), 3),
            "outcome":      "win" if partial_pnl > 0 else "loss",
            "type":         "partial",
            "tp_stage_hit": upd["tp_stage"],
            "qty_closed":   upd["qty_closed"],
            "equity_after": state["equity"],
            "exited_at":    datetime.now(TZ8).isoformat(),
        }
        save_trade(partial_trade)

        asyncio.create_task(_tg(
            f"📊 <b>分段出場</b> {sym}\n"
            f"{upd['exit_why']}\n"
            f"出場損益：{partial_pnl:+.4f}U\n"
            f"新止損：{pos['sl_price']:.6g}　新目標：{pos['tp_price']:.6g}\n"
            f"剩餘倉位：{pos['qty_remaining']:.6f}"
        ))

    # 執行完全平倉
    for c in to_close:
        _close_position(state, c)

def _close_position(state: dict, close: dict):
    symbol  = close["symbol"]
    pos     = close["pos"]
    pnl     = close["pnl"]
    outcome = close["outcome"]

    state["equity"]  = round(state["equity"]  + pnl, 4)
    state["capital"] = round(state["capital"] + pnl, 4)
    state["total_trades"] += 1
    if outcome == "win":
        state["wins"] += 1
        state["loss_streak"] = 0
    else:
        state["losses"] += 1
        state["loss_streak"] = state.get("loss_streak", 0) + 1
        # Sam 鐵律：連虧3筆暫停24小時冷靜，不換邊繼續玩
        if state["loss_streak"] >= 3:
            state["entry_pause_until"] = time.time() + 86400
            state["loss_streak"] = 0
            log.warning("[GUARD] 連虧3筆 → 暫停新倉24小時（持倉照常管理）")
            asyncio.create_task(_tg(
                "🛑 <b>Sam 連虧3筆 — 啟動24h冷靜</b>\n"
                "暫停開新倉，現有持倉照常管理。\n"
                "Sam鐵律：虧損後換邊繼續是賭徒，不是交易者。"))

    if state["equity"] > state["peak_equity"]:
        state["peak_equity"] = state["equity"]
    dd = (state["peak_equity"] - state["equity"]) / state["peak_equity"]
    if dd > state["max_drawdown"]:
        state["max_drawdown"] = round(dd, 4)

    del state["positions"][symbol]

    log.info(f"[CLOSE] {close['exit_why']} {pos['side'].upper()} {symbol} "
             f"entry={pos['entry_px']:.6g} exit={close['exit_px']:.6g} "
             f"pnl={pnl:+.4f}U ({close['pnl_r']:+.2f}R) "
             f"equity={state['equity']:.2f}U")

    wr = state["wins"] / state["total_trades"] if state["total_trades"] else 0
    result_emoji = "✅ 獲利" if outcome == "win" else "❌ 止損"
    # 計算含分段出場的總 pnl（partial_pnl 已在分段時加入 equity，這裡補足記錄用）
    partial_already = pos.get("partial_pnl", 0)
    total_pnl       = round(pnl + partial_already, 4)
    total_pnl_r     = round(total_pnl / pos.get("risk_amt", 10), 3)

    asyncio.create_task(_tg(
        f"🧠 <b>sam v2 出場</b>\n\n"
        f"{result_emoji} <b>{symbol}</b> — {close['exit_why']}\n"
        f"進場：{pos['entry_px']:.6g} → 出場：{close['exit_px']:.6g}\n"
        f"本次損益：{pnl:+.4f}U　總損益：{total_pnl:+.4f}U（<b>{total_pnl_r:+.2f}R</b>）\n"
        f"目前資金：<b>{state['equity']:.2f}U</b>\n"
        f"勝率：{state['wins']}/{state['total_trades']}（{wr:.0%}）"
    ))

    # ── 即時複盤推送 ────────────────────────────────────────────
    duration_min = 0
    if pos.get("entered_at"):
        try:
            opened = datetime.fromisoformat(pos["entered_at"].replace("Z", "+00:00"))
            duration_min = int((datetime.now(timezone.utc) - opened).total_seconds() / 60)
        except Exception:
            pass
    llm_reason  = pos.get("reason", "無記錄")[:100]
    score       = pos.get("score", 0)
    asyncio.create_task(_tg(
        f"📋 <b>複盤｜{symbol} {pos.get('side','').upper()}單</b>\n"
        f"{'✅ 獲利' if outcome == 'win' else '❌ 虧損'}：{total_pnl:+.4f}U（{total_pnl_r:+.2f}R）\n"
        f"進場理由：{llm_reason}\n"
        f"信心分：{score}/100\n"
        f"出場原因：{close['exit_why']}\n"
        f"開單時間：{pos.get('entered_at','')[:16].replace('T',' ')} UTC\n"
        f"持倉時間：{duration_min}分鐘"
    ))

    # 反思
    right  = "主力思維正確，方向與市場一致" if outcome == "win" else "風控執行，保住本金"
    wrong  = "" if outcome == "win" else "需回顧進場時的主力分析是否有誤判"
    lesson = "繼續保持A級訊號紀律" if outcome == "win" else "下次回顧市場結構與主力動向是否一致"
    trade  = {**pos, "exit_px": close["exit_px"], "exit_why": close["exit_why"],
              "pnl": total_pnl,        # ✅ 含分段出場的完整 pnl
              "pnl_r": total_pnl_r,
              "outcome": outcome,
              "equity_after": state["equity"],
              "exited_at": datetime.now(TZ8).isoformat()}
    save_trade(trade)
    (REFLECTION_DIR / f"{pos['id']}.json").write_text(
        json.dumps({**trade, "what_went_right": right,
                    "what_went_wrong": wrong, "lesson": lesson},
                   ensure_ascii=False, indent=2)
    )

# ─────────────────────────────────────────────
# 績效
# ─────────────────────────────────────────────
def log_performance(state: dict):
    total = state["total_trades"]
    wr    = state["wins"] / total if total else 0
    trades = load_trades()
    pnl_rs = [t.get("pnl_r", 0) for t in trades]
    gw = sum(r for r in pnl_rs if r > 0)
    gl = abs(sum(r for r in pnl_rs if r < 0))
    pf = gw / gl if gl else 0
    log.info(f"[PERF] equity={state['equity']:.2f}U  trades={total}  "
             f"WR={wr:.0%}  PF={pf:.2f}  DD={state['max_drawdown']:.1%}  "
             f"positions={len(state['positions'])}")

# ─────────────────────────────────────────────
# 日內短線掃描（Sam 2140 突破心法）
# ─────────────────────────────────────────────
async def _intraday_pre_filter(symbol: str) -> tuple[bool, dict, str]:
    """
    Sam（2140）日內突破策略過濾器 — 純規則，零 LLM 成本

    ★ 核心心法（2140 原話內化）★
      「玩極短線我一定是右側交易，不做逆勢。」
      「帶量突破前高，那時候我就進場。」
      「突破後等回彩守住再攻，這叫二次確認，勝率高。」
      「突破量跟上最好；突破不帶量，還是要小心。」

    ★ 觸發條件（三個都要符合才送 LLM）★
      1. 最後一根15m K棒收盤突破前20根結構高點/低點
      2. 突破K棒量 ≥ 均量 1.5x（真突破門票）
      3. 突破K棒實體 ≥ 40%（收盤確認，不是影線假突破）

    ★ 進場分類（傳給 LLM 決策）★
      BREAKOUT_DIRECT  — 量 ≥ 2.5x，直接右側進場
      BREAKOUT_PULLBACK — 量 1.5~2.5x，後2根縮量守穩（二次確認）
      BREAKOUT_WATCH   — 量 1.5~2.5x，尚未看到回調守穩
    """
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            # 取 50 根 15m K 線（前20根算均量，最後一根判斷突破）
            r15 = await c.get(
                f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}&interval=15m&limit=50"
            )
            k15 = r15.json()
            if not k15 or isinstance(k15, dict) or len(k15) < 25:
                return False, {}, "無法取得K線"

            # ── 最後一根K棒（當前已收盤）──
            cur   = k15[-1]
            o, h, l, c_price = float(cur[1]), float(cur[2]), float(cur[3]), float(cur[4])
            v_cur = float(cur[5])
            price = c_price

            # ── 前20根結構與均量 ──
            ref = k15[-21:-1]   # 往前20根，不含當根
            prev_highs = [float(k[2]) for k in ref]
            prev_lows  = [float(k[3]) for k in ref]
            prev_vols  = [float(k[5]) for k in ref]
            struct_high = max(prev_highs)
            struct_low  = min(prev_lows)
            avg_vol     = sum(prev_vols) / len(prev_vols)

            vol_ratio   = round(v_cur / avg_vol, 2) if avg_vol > 0 else 0
            body_size   = abs(c_price - o)
            candle_range = h - l
            body_ratio  = round(body_size / candle_range, 2) if candle_range > 0 else 0

            # ── 濾掉量不夠 ──
            if vol_ratio < 1.5:
                return False, {}, f"量不足（{vol_ratio}x < 1.5x），非帶量突破"

            # ── 偵測突破方向 ──
            breakout_long  = c_price > struct_high and o < struct_high
            breakout_short = c_price < struct_low  and o > struct_low

            if not breakout_long and not breakout_short:
                return False, {}, f"未突破結構位（high={struct_high:.4g} low={struct_low:.4g}）"

            # ── 實體確認（不是影線假突破）──
            if body_ratio < 0.40:
                return False, {}, f"實體太小（{body_ratio:.0%}），影線突破不可信"

            # ── 突破量太貼（剛剛過，可能假突破）──
            if breakout_long  and (c_price - struct_high) / struct_high < 0.0008:
                return False, {}, "突破幅度太小（<0.08%），可能假突破"
            if breakout_short and (struct_low - c_price) / struct_low  < 0.0008:
                return False, {}, "跌破幅度太小（<0.08%），可能假突破"

            # ── 判斷進場類型 ──
            direction = "long" if breakout_long else "short"
            if vol_ratio >= 2.5:
                entry_type = "BREAKOUT_DIRECT" if direction == "long" else "BREAKDOWN_DIRECT"
            else:
                # 看後2根是否已出現縮量守穩
                pullback_hold = False
                for k in range(1, min(3, len(k15) - (len(k15) - 1))):
                    pass  # 當根是最後一根，無後續K棒，只能交給LLM判斷
                entry_type = "BREAKOUT_WATCH" if direction == "long" else "BREAKDOWN_WATCH"

            # ── RSI 15m ──
            closes_all = [float(k[4]) for k in k15]
            gains  = [max(closes_all[i]-closes_all[i-1], 0) for i in range(1, len(closes_all))]
            losses = [max(closes_all[i-1]-closes_all[i], 0) for i in range(1, len(closes_all))]
            ag = sum(gains[-14:])/14; al = sum(losses[-14:])/14
            rsi = round(100 - 100/(1+ag/al), 1) if al > 0 else 50

            # ── 1H 結構位（給 LLM 用）──
            r1h = await c.get(
                f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}&interval=1h&limit=24"
            )
            k1h = r1h.json()
            h1h = max(float(k[2]) for k in k1h)
            l1h = min(float(k[3]) for k in k1h)

            # ── SL 計算（結構失效點 + 0.3% 緩衝）──
            if direction == "long":
                sl_price = round(struct_low  * 0.997, 8)
                sl_pct   = round((price - sl_price) / price * 100, 2)
            else:
                sl_price = round(struct_high * 1.003, 8)
                sl_pct   = round((sl_price - price) / price * 100, 2)

            mkt = {
                "symbol":    symbol,
                "price":     price,
                "side":      direction,
                "vol_ratio": vol_ratio,
                "vol_trend_up": True,          # 突破K棒已帶量，量趨勢向上
                "rsi_15m":   rsi,
                "rsi_1h":    rsi,
                "rsi_4h":    rsi,
                "struct_high_15m": struct_high,
                "struct_low_15m":  struct_low,
                "swing_high_1h":   h1h,
                "swing_low_1h":    l1h,
                "h1h":       h1h,
                "l1h":       l1h,
                "near_high": direction == "long",
                "near_low":  direction == "short",
                "body_ratio": body_ratio,
                "entry_type": entry_type,
                "daily_trend": "neutral",
                "vol_extreme_compression": False,
                "vol_divergence_bearish":  False,
                "fake_breakout_up":        False,
                "fake_breakout_down":      False,
                "pattern_abcd":     {"detected": False},
                "pattern_hs":       {"detected": False},
                "pattern_wm":       {"detected": False},
                "pattern_triangle": {"detected": False},
                "intraday":         True,
                "intraday_tf":      "15m",
                "intraday_sl_price": sl_price,
                "intraday_sl_pct":   sl_pct,
                # 給 LLM 的完整突破描述
                "intraday_context": (
                    f"15m {'帶量突破' if direction=='long' else '帶量跌破'}結構{'高點' if direction=='long' else '低點'}"
                    f"（{'前高' if direction=='long' else '前低'}={struct_high if direction=='long' else struct_low:.4g}）"
                    f"  突破量={vol_ratio}x均量  K棒實體={body_ratio:.0%}"
                    f"  類型={entry_type}"
                    f"（{'量≥2.5x直接右側' if vol_ratio>=2.5 else '量1.5~2.5x等二次確認'}）"
                    f"  止損結構位={sl_price:.4g}（{sl_pct:.1f}%）"
                )
            }

            return True, mkt, f"✅ {direction.upper()} 突破 vol={vol_ratio}x 實體={body_ratio:.0%} 類型={entry_type}"

    except Exception as e:
        return False, {}, f"資料異常: {e}"


async def _run_intraday_scan(state: dict):
    """
    日內短線自動掃描：過濾 → LLM 決策 → 自動進場
    每日最多 MAX_INTRADAY_PER_DAY 筆，不超過總倉位上限
    """
    if not ENABLE_INTRADAY:
        log.debug("[INTRADAY] 日內單已停用（ENABLE_INTRADAY=False）")
        return

    # 連虧3筆冷靜期內，日內單同樣不開新倉
    if time.time() < state.get("entry_pause_until", 0):
        log.info("[INTRADAY] 冷靜期中，暫停日內開新倉")
        return

    # 統計今日已進日內單數量
    today = datetime.now().strftime("%Y-%m-%d")
    intraday_today = sum(
        1 for pos in state["positions"].values()
        if pos.get("intraday") and pos.get("entered_at", "").startswith(today)
    )
    if intraday_today >= MAX_INTRADAY_PER_DAY:
        log.info(f"[INTRADAY] 今日已進 {intraday_today} 筆日內單，達上限，停止掃描")
        return

    if len(state["positions"]) >= 5:
        log.info(f"[INTRADAY] 總倉位已達5個，跳過日內掃描")
        return

    passed_count = 0
    now_ts = time.time()
    for sym in INTRADAY_SYMBOLS:
        if sym in state["positions"]:
            continue

        # 冷卻檢查：30 分鐘內已問過 LLM 的幣直接跳過
        last_ts = _intraday_last_scan.get(sym, 0)
        cooldown_remain = INTRADAY_COOLDOWN - (now_ts - last_ts)
        if cooldown_remain > 0:
            log.debug(f"[INTRADAY_CD] {sym} 冷卻中，還剩 {cooldown_remain/60:.0f} 分鐘")
            continue

        ok, mkt, reason = await _intraday_pre_filter(sym)
        if not ok:
            log.debug(f"[INTRADAY_FILTER] {sym} ❌ {reason}")
            continue

        passed_count += 1
        log.info(f"[INTRADAY_FILTER] {sym} ✅ 通過 vol={mkt['vol_ratio']}x RSI={mkt['rsi_15m']} near_high={mkt['near_high']} near_low={mkt['near_low']}")

        # 送 LLM 決策（日內特化 prompt 透過 intraday=True 傳入）
        decision = await _sam_decide(mkt)
        score    = decision.get("score", 0)

        # ✅ 記錄冷卻時間（無論進場與否，30 分鐘內不再問同一幣）
        _intraday_last_scan[sym] = time.time()

        if decision.get("approved") and score >= MIN_SCORE_INTRADAY:
            pos = virtual_enter(state, mkt, decision)
            if pos:
                save_state(state)
                log.info(f"[INTRADAY_ENTER] {sym} score={score} → 日內單進場（門檻{MIN_SCORE_INTRADAY}分）")
                intraday_today += 1
                if intraday_today >= MAX_INTRADAY_PER_DAY:
                    break
                if len(state["positions"]) >= 5:
                    break
        else:
            log.info(f"[INTRADAY_SKIP] {sym} score={score} | {decision.get('skip_reason','')[:50]}")

    if passed_count == 0:
        log.info("[INTRADAY] 全部幣種未通過預篩選，0 次 LLM 呼叫")


# ─────────────────────────────────────────────
# 主迴圈
# ─────────────────────────────────────────────
async def scan_loop():
    global SAM_WATCHLIST
    log.info("🧠 sam'sBrain v2 啟動 — 主力思維版")
    log.info(f"   進場門檻：{MIN_SCORE}分  分段TP：TP1/TP2/TP3  知識庫：{'已接入' if KNOWLEDGE_AVAILABLE else '未接入'}")
    state = load_state()
    log.info(f"   equity={state['equity']:.2f}U  positions={len(state['positions'])}  trades={state['total_trades']}")

    SAM_WATCHLIST = await _refresh_watchlist()
    _wl_count = 0

    while True:
        state = load_state()
        _wl_count += 1
        if _wl_count % 60 == 0:
            SAM_WATCHLIST = await _refresh_watchlist()

        # 1. 先管持倉（冷靜期內持倉照常管理，不影響出場）
        await check_positions(state)
        save_state(state)

        # 情緒保護：連虧3筆後暫停『開新倉』24小時（Sam鐵律，不換邊繼續）
        _pause_until = state.get("entry_pause_until", 0)
        _paused = time.time() < _pause_until

        # 2. 掃市場找A+訊號（冷靜期內不開新倉）
        if not _paused and len(state["positions"]) < 3:
            # 2a. 取得 BTC 大方向（每小時更新一次 cache）
            btc_ctx  = await _fetch_btc_regime()
            btc_regime = btc_ctx.get("regime", "neutral")

            signals = await _get_candidate_signals()
            for sig in signals:
                sym = sig.get("symbol", "")
                if sym in state["positions"]:
                    continue

                # ── 前置過濾：純規則判斷 + BTC大方向過濾 ──
                should_ask, filter_reason = _should_ask_sam(sig, btc_regime=btc_regime)
                if not should_ask:
                    log.debug(f"[FILTER] {sym} 跳過：{filter_reason}")
                    _save_thinking(sym, sig, filter_reason, {
                        "approved": False, "score": 0,
                        "skip_reason": f"[前置過濾] {filter_reason}"
                    })
                    continue

                # ── 訊號記憶冷卻：同幣同理由 30 分鐘內不重複打 API ──
                import time as _time
                _now = _time.time()
                _mem = _SIGNAL_MEMORY.get(sym, {})
                _same_reason = (_mem.get("reason") == filter_reason)
                _within_cooldown = (_now - _mem.get("ts", 0)) < SIGNAL_COOLDOWN_SEC
                if _same_reason and _within_cooldown:
                    log.debug(f"[COOLDOWN] {sym} 訊號未變({filter_reason[:30]})，"
                              f"冷卻中({int(SIGNAL_COOLDOWN_SEC/60)}分鐘)，跳過")
                    continue
                # 訊號有變化 or 冷卻過了 → 更新記憶，放行
                _SIGNAL_MEMORY[sym] = {"reason": filter_reason, "ts": _now}
                log.info(f"[FILTER] {sym} 放行 → Sam 思考中（訊號：{filter_reason}）")

                decision = await _sam_decide(sig)
                score    = decision.get("score", 0)

                if decision.get("approved") and score >= MIN_SCORE:
                    pos = virtual_enter(state, sig, decision)
                    if pos:
                        save_state(state)
                        if len(state["positions"]) >= 3:
                            break
                elif decision.get("entry_type") == "watch_breakout" and decision.get("watch_level"):
                    # watch_breakout：Sam 識別到關鍵點位，到了那裡要重新判斷方向，不鎖死方向
                    # 原主哲學：越是到關鍵點位越要重新思考，不能早上看多就一路掛單等，市場情緒可能已變
                    state.setdefault("watchlist", {})[sym] = {
                        "score": score,
                        "side": None,          # 不鎖死方向，到了再判斷
                        "reason": decision.get("reason", ""),
                        "entry_type": "watch_breakout",
                        "watch_level": decision.get("watch_level", 0),
                        "watch_condition": decision.get("watch_condition", ""),
                        "watch_why": decision.get("reason", ""),  # 為什麼這個點位重要
                        "noted_at": datetime.now(TZ8).isoformat(),
                    }
                    log.info(f"[WATCH_BREAKOUT] {sym} → 關鍵位 {decision.get('watch_level')}（{decision.get('watch_condition')}），到了重新判斷方向")
                elif 60 <= score < MIN_SCORE:
                    # 放觀察清單（B訊號，等機會）
                    state.setdefault("watchlist", {})[sym] = {
                        "score": score,
                        "side": decision.get("side"),
                        "reason": decision.get("reason", ""),
                        "entry_type": "score_watch",
                        "noted_at": datetime.now(TZ8).isoformat(),
                    }
                    log.info(f"[WATCH] {sym} score={score} → 觀察清單，等更好機會")
                else:
                    log.info(f"[SKIP] {sym} score={score} | {decision.get('skip_reason') or decision.get('reason','')[:60]}")

        # 3. 檢查 watch_breakout 觸發條件（每次 scan 都跑）
        watch_triggered = []
        for sym, info in list(state.get("watchlist", {}).items()):
            if info.get("entry_type") != "watch_breakout":
                continue
            if sym in state["positions"]:
                watch_triggered.append(sym)
                continue
            watch_level = info.get("watch_level", 0)
            watch_condition = info.get("watch_condition", "")
            if not watch_level or not watch_condition:
                continue
            try:
                # 取最新價格確認是否觸發
                async with httpx.AsyncClient(timeout=10) as client:
                    r = await client.get(
                        "https://fapi.binance.com/fapi/v1/ticker/price",
                        params={"symbol": sym}
                    )
                    current_price = float(r.json().get("price", 0))
                triggered = (
                    (watch_condition == "breakout_above" and current_price > watch_level) or
                    (watch_condition == "breakdown_below" and current_price < watch_level)
                )
                if triggered and len(state["positions"]) < 3:
                    # 觸發！到了關鍵點位 → 重新跑完整市場分析，此刻情緒才是進場依據
                    # 原主哲學：不能鎖死早上的方向，到了點位要重新感受市場
                    log.info(f"[WATCH_TRIGGERED] {sym} 到達關鍵位 {watch_level} → 重新判斷此刻方向...")
                    log.info(f"[WATCH_TRIGGERED] {sym} 到達關鍵位 {watch_level}")
                    fresh_sig = await _fetch_market_snapshot(sym)
                    if fresh_sig:
                        # 在 prompt 裡告訴 Sam：你之前就盯著這個點位，現在到了，重新判斷
                        fresh_sig["watch_context"] = (
                            f"你之前識別到 {watch_level} 是關鍵點位（原因：{info.get('watch_why','')[:100]}）。"
                            f"現在價格已到達/突破這個位置（當前價: {current_price}）。"
                            f"這是重新判斷的時機——不要沿用之前的方向判斷，"
                            f"根據此刻的市場結構、量能、情緒重新決定做多還是做空，或者不做。"
                        )
                        new_decision = await _sam_decide(fresh_sig)
                        new_score = new_decision.get("score", 0)
                        if new_decision.get("approved") and new_score >= MIN_SCORE:
                            pos = virtual_enter(state, fresh_sig, new_decision)
                            if pos:
                                log.info(f"[WATCH_ENTER] {sym} @{current_price} → {new_decision.get('side')} score={new_score}")
                                await _tg(
                                    f"🚀 <b>{sym}</b> 關鍵位重判後進場！\n"
                                    f"方向: {new_decision.get('side')} @{current_price}\n"
                                    f"分數: {new_score}\n"
                                    f"{new_decision.get('reason','')[:100]}"
                                )
                        else:
                            log.info(f"[WATCH_SKIP] {sym} 到達關鍵位但重判後不進場: score={new_score} | {new_decision.get('skip_reason','')[:60]}")
                    watch_triggered.append(sym)
                    save_state(state)
            except Exception as e:
                log.warning(f"[WATCH_CHECK] {sym} 價格查詢失敗: {e}")
        for sym in watch_triggered:
            state["watchlist"].pop(sym, None)

        # 4. 日內短線掃描（每5分鐘，帶過濾，只在有機會時用 LLM）
        await _run_intraday_scan(state)
        save_state(state)

        # 5. 清理過期觀察清單（超過4小時的移除）
        now_ts = datetime.now(timezone.utc)
        stale = []
        for sym, info in state.get("watchlist", {}).items():
            try:
                noted = datetime.fromisoformat(info["noted_at"])
                if (now_ts - noted).total_seconds() > 14400:
                    stale.append(sym)
            except Exception:
                stale.append(sym)
        for sym in stale:
            state["watchlist"].pop(sym, None)

        state["last_scan"] = datetime.now(TZ8).isoformat()
        save_state(state)
        log_performance(state)

        await asyncio.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    asyncio.run(scan_loop())
