#!/usr/bin/env python3
"""
NFES Signal Bot — Y.Algo 自適應 Supertrend 訊號機器人
───────────────────────────────────────────────────────────────
功能：
  ‧ 每分鐘從 Binance 拉取 K 線資料
  ‧ 完整還原 Pine Script NFES Enhanced Strategy 訊號邏輯
  ‧ 偵測到 ▲+/▲/▼+/▼ 訊號時，直接呼叫 execute_signal()
  ‧ 不需要 TradingView、不需要 Webhook，完全本地運行
  ‧ 訊號去重：同一根 K 棒只觸發一次

核心邏輯（與 Pine Script 一致）：
  ‧ Adaptive Supertrend (ATR14, sensitivity=3)
  ‧ Vol filter: volume > SMA(vol,20) × 1.2
  ‧ RSI(14) 動量
  ‧ MTF EMA50（1H）趨勢過濾
  ‧ 訊號模式：Trend / Strong Trend / Volume Filter / Counter
  ‧ SL = entry ± ATR×1.0
  ‧ TP1/TP2/TP3 = entry ± ATR × 1.5/2.5/3.5R

啟動方式：
  python nfes_signal_bot.py
───────────────────────────────────────────────────────────────
"""
from __future__ import annotations
import logging
import os
import time
from pathlib import Path
from datetime import datetime, timezone

import numpy as np

# ── 自動載入 .env ──────────────────────────────────────────────
_env_file = Path(__file__).parent / ".env"
if _env_file.exists():
    for _line in _env_file.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

import ccxt
import requests

# ── 從 nexus_webhook 匯入執行函式 ─────────────────────────────
from nexus_webhook import execute_signal, tg, PAPER_MODE

# ═══════════════════════════════════════════════════════════════
# 策略名稱
# ═══════════════════════════════════════════════════════════════
STRATEGY_NAME = "NFES 強化版"

# ═══════════════════════════════════════════════════════════════
# 設定（對應 Pine Script 預設值）
# ═══════════════════════════════════════════════════════════════
SYMBOL        = "BTC/USDT"          # ccxt 格式
TIMEFRAME     = "15m"               # 主圖週期
MTF_TIMEFRAME = "1h"                # 高週期濾網
SCAN_INTERVAL = 60                  # 每幾秒掃描一次（秒）
LIMIT         = 300                 # 拉取K棒數量（足夠計算指標）

# 狀態檔（讓 portfolio_app 讀取）
STATE_FILE    = Path(__file__).parent / "nfes_bot_state.json"

# 趨勢帶
SENSITIVITY  = 3
ATR_PERIOD   = 14

# 量能
VOL_MA_LEN  = 20
VOL_MULT    = 1.2

# 進場確認
PULLBACK_BARS = 2

# 多週期
USE_MTF     = True
MTF_EMA_LEN = 50

# 風控
SL_ATR_MULT    = 1.0
TP1_MULT       = 1.5
TP2_MULT       = 2.5
TP3_MULT       = 3.5

# 訊號模式："Trend" / "Strong Trend" / "Volume Filter" / "Counter"
SIGNAL_MODE    = "Trend"

# ── 日誌 ─────────────────────────────────────────────────────
LOG_FILE = Path("nfes_signal_bot.log")
_fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
_fh  = logging.FileHandler(LOG_FILE, encoding="utf-8")
_fh.setFormatter(_fmt)
log = logging.getLogger("nfes")
log.setLevel(logging.INFO)
log.addHandler(_fh)
log.addHandler(logging.StreamHandler())

# ═══════════════════════════════════════════════════════════════
# 指標計算函式
# ═══════════════════════════════════════════════════════════════

def sma(arr: np.ndarray, period: int) -> np.ndarray:
    result = np.full(len(arr), np.nan)
    for i in range(period - 1, len(arr)):
        result[i] = arr[i - period + 1 : i + 1].mean()
    return result

def ema(arr: np.ndarray, period: int) -> np.ndarray:
    result = np.full(len(arr), np.nan)
    k = 2.0 / (period + 1)
    # 找第一個非 nan 的位置，用 SMA 作為起始值
    start = period - 1
    if start >= len(arr):
        return result
    result[start] = np.nanmean(arr[:start + 1])
    for i in range(start + 1, len(arr)):
        result[i] = arr[i] * k + result[i - 1] * (1 - k)
    return result

def atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int) -> np.ndarray:
    n = len(close)
    tr = np.full(n, np.nan)
    for i in range(1, n):
        hl = high[i] - low[i]
        hc = abs(high[i] - close[i - 1])
        lc = abs(low[i]  - close[i - 1])
        tr[i] = max(hl, hc, lc)
    tr[0] = high[0] - low[0]
    return sma(tr, period)  # Pine Script ta.atr = RMA ≈ SMA 近似

def rma(arr: np.ndarray, period: int) -> np.ndarray:
    """Wilder RMA（ta.atr 內部使用）"""
    result = np.full(len(arr), np.nan)
    alpha = 1.0 / period
    start = period - 1
    if start >= len(arr):
        return result
    result[start] = np.nanmean(arr[:start + 1])
    for i in range(start + 1, len(arr)):
        if not np.isnan(result[i - 1]) and not np.isnan(arr[i]):
            result[i] = alpha * arr[i] + (1 - alpha) * result[i - 1]
    return result

def true_atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int) -> np.ndarray:
    n = len(close)
    tr = np.full(n, np.nan)
    for i in range(1, n):
        hl = high[i] - low[i]
        hc = abs(high[i] - close[i - 1])
        lc = abs(low[i]  - close[i - 1])
        tr[i] = max(hl, hc, lc)
    tr[0] = high[0] - low[0]
    return rma(tr, period)

def calc_rsi(close: np.ndarray, period: int = 14) -> np.ndarray:
    n = len(close)
    rsi_arr = np.full(n, np.nan)
    delta = np.diff(close)
    gain  = np.where(delta > 0, delta, 0.0)
    loss  = np.where(delta < 0, -delta, 0.0)
    if n < period + 1:
        return rsi_arr
    avg_gain = rma(np.concatenate([[np.nan], gain]), period)
    avg_loss = rma(np.concatenate([[np.nan], loss]), period)
    for i in range(n):
        if not np.isnan(avg_gain[i]) and not np.isnan(avg_loss[i]):
            if avg_loss[i] == 0:
                rsi_arr[i] = 100.0
            else:
                rs = avg_gain[i] / avg_loss[i]
                rsi_arr[i] = 100.0 - 100.0 / (1 + rs)
    return rsi_arr

def calc_adaptive_supertrend(
    high: np.ndarray, low: np.ndarray, close: np.ndarray,
    sensitivity: int = 3, atr_period: int = 14
):
    """
    自適應 Supertrend：
    atr_ratio = fast_atr(3) / atr(14)
    adaptive_factor = sensitivity × clamp(atr_ratio, 0.5, 2.0)
    方向: +1=多頭 / -1=空頭
    """
    n = len(close)
    atr14    = true_atr(high, low, close, atr_period)
    atr3     = true_atr(high, low, close, 3)
    src      = (high + low) / 2.0   # hl2

    trend_upper = np.full(n, np.nan)
    trend_lower = np.full(n, np.nan)
    trend_dir   = np.zeros(n, dtype=int)

    for i in range(n):
        if np.isnan(atr14[i]) or np.isnan(atr3[i]):
            trend_dir[i] = trend_dir[i - 1] if i > 0 else 1
            continue

        ratio   = atr3[i] / max(atr14[i], 1e-9)
        factor  = sensitivity * max(0.5, min(2.0, ratio))
        ub = src[i] + factor * atr14[i]
        lb = src[i] - factor * atr14[i]

        if i == 0:
            trend_upper[i] = ub
            trend_lower[i] = lb
            trend_dir[i]   = 1
            continue

        # 上帶：只能縮小（或收盤穿越時重設）
        prev_ub = trend_upper[i - 1] if not np.isnan(trend_upper[i - 1]) else ub
        if ub < prev_ub or close[i - 1] > prev_ub:
            trend_upper[i] = ub
        else:
            trend_upper[i] = prev_ub

        # 下帶：只能擴大（或收盤穿越時重設）
        prev_lb = trend_lower[i - 1] if not np.isnan(trend_lower[i - 1]) else lb
        if lb > prev_lb or close[i - 1] < prev_lb:
            trend_lower[i] = lb
        else:
            trend_lower[i] = prev_lb

        # 方向
        if close[i] > trend_upper[i - 1]:
            trend_dir[i] = 1
        elif close[i] < trend_lower[i - 1]:
            trend_dir[i] = -1
        else:
            trend_dir[i] = trend_dir[i - 1]

    return trend_upper, trend_lower, trend_dir

# ═══════════════════════════════════════════════════════════════
# 訊號偵測（完整還原 Pine Script 邏輯）
# ═══════════════════════════════════════════════════════════════

def detect_signals(ohlcv: list, mtf_ohlcv: list) -> dict | None:
    """
    傳入 K 棒資料，回傳最新一根K棒的訊號 dict 或 None
    ohlcv 格式: [[timestamp, open, high, low, close, volume], ...]
    """
    if len(ohlcv) < LIMIT // 2:
        log.warning("K棒數量不足")
        return None

    o_arr = np.array([x[1] for x in ohlcv], dtype=float)
    h_arr = np.array([x[2] for x in ohlcv], dtype=float)
    l_arr = np.array([x[3] for x in ohlcv], dtype=float)
    c_arr = np.array([x[4] for x in ohlcv], dtype=float)
    v_arr = np.array([x[5] for x in ohlcv], dtype=float)

    # ── 指標計算 ──────────────────────────────────────────────
    atr14 = true_atr(h_arr, l_arr, c_arr, ATR_PERIOD)
    trend_upper, trend_lower, trend_dir = calc_adaptive_supertrend(
        h_arr, l_arr, c_arr, SENSITIVITY, ATR_PERIOD
    )

    vol_avg    = sma(v_arr, VOL_MA_LEN)
    rsi_arr    = calc_rsi(c_arr, 14)

    # 高週期 EMA50
    mtf_closes = np.array([x[4] for x in mtf_ohlcv], dtype=float) if mtf_ohlcv else np.array([])
    mtf_ema50  = ema(mtf_closes, MTF_EMA_LEN) if len(mtf_closes) >= MTF_EMA_LEN else np.array([])
    # 最新 MTF EMA 值
    if len(mtf_ema50) > 0 and not np.isnan(mtf_ema50[-1]):
        mtf_ema_val = mtf_ema50[-1]
    else:
        mtf_ema_val = None

    # ── 最新兩根K棒 ─────────────────────────────────────────
    i   = len(c_arr) - 1   # 最新已收盤K棒（index -1）
    i_1 = i - 1

    if np.isnan(atr14[i]) or np.isnan(vol_avg[i]) or np.isnan(rsi_arr[i]):
        return None

    close_now   = c_arr[i]
    high_now    = h_arr[i]
    low_now     = l_arr[i]
    atr_now     = atr14[i]
    vol_now     = v_arr[i]
    vol_avg_now = vol_avg[i]
    rsi_now     = rsi_arr[i]

    dir_now = trend_dir[i]
    dir_pre = trend_dir[i_1] if i_1 >= 0 else dir_now

    upper_now = trend_upper[i]
    lower_now = trend_lower[i]
    upper_pre = trend_upper[i_1] if i_1 >= 0 else upper_now
    lower_pre = trend_lower[i_1] if i_1 >= 0 else lower_now

    # 趨勢翻轉
    trend_flip_up = (dir_now ==  1 and dir_pre == -1)
    trend_flip_dn = (dir_now == -1 and dir_pre ==  1)
    trend_up      = (dir_now ==  1)
    trend_dn      = (dir_now == -1)

    # 量能
    vol_strong = (vol_now > vol_avg_now * VOL_MULT)

    # RSI 動量
    rsi_pre  = rsi_arr[i_1] if i_1 >= 0 else rsi_now
    mom_bull = (rsi_pre <= 30 and rsi_now > 30)   # crossover(rsi, 30)
    mom_bear = (rsi_pre >= 70 and rsi_now < 70)   # crossunder(rsi, 70)

    # MTF 過濾
    mtf_bull = (mtf_ema_val is None) or (close_now > mtf_ema_val)
    mtf_bear = (mtf_ema_val is None) or (close_now < mtf_ema_val)
    use_mtf  = USE_MTF and mtf_ema_val is not None

    # 回調確認：最近 PULLBACK_BARS 根K棒曾碰觸趨勢帶
    pullback_long  = False
    pullback_short = False
    for j in range(1, PULLBACK_BARS + 1):
        idx = i - j
        if idx < 0:
            break
        if not np.isnan(trend_lower[idx]) and l_arr[idx] <= trend_lower[idx] * 1.003:
            pullback_long = True
        if not np.isnan(trend_upper[idx]) and h_arr[idx] >= trend_upper[idx] * 0.997:
            pullback_short = True

    # ── 基礎訊號 ──────────────────────────────────────────────
    base_long  = trend_flip_up or (trend_up and pullback_long  and close_now > lower_now)
    base_short = trend_flip_dn or (trend_dn and pullback_short and close_now < upper_now)

    raw_long  = False
    raw_short = False

    if SIGNAL_MODE == "Trend":
        raw_long  = base_long  and (not use_mtf or mtf_bull)
        raw_short = base_short and (not use_mtf or mtf_bear)

    elif SIGNAL_MODE == "Strong Trend":
        raw_long  = base_long  and vol_strong and (not use_mtf or mtf_bull)
        raw_short = base_short and vol_strong and (not use_mtf or mtf_bear)

    elif SIGNAL_MODE == "Volume Filter":
        raw_long  = base_long  and vol_strong
        raw_short = base_short and vol_strong

    elif SIGNAL_MODE == "Counter":
        raw_long  = trend_dn and mom_bull
        raw_short = trend_up and mom_bear

    # 強/弱訊號
    strong_long  = raw_long  and vol_strong
    weak_long    = raw_long  and not vol_strong
    strong_short = raw_short and vol_strong
    weak_short   = raw_short and not vol_strong

    # ── 無訊號 ───────────────────────────────────────────────
    if not (strong_long or weak_long or strong_short or weak_short):
        return None

    # ── 計算 TP/SL ──────────────────────────────────────────
    if strong_long or weak_long:
        side   = "long"
        signal = "▲+" if strong_long else "▲"
        sl     = round(close_now - atr_now * SL_ATR_MULT, 2)
        tp1    = round(close_now + atr_now * TP1_MULT, 2)
        tp2    = round(close_now + atr_now * TP2_MULT, 2)
        tp3    = round(close_now + atr_now * TP3_MULT, 2)
    else:
        side   = "short"
        signal = "▼+" if strong_short else "▼"
        sl     = round(close_now + atr_now * SL_ATR_MULT, 2)
        tp1    = round(close_now - atr_now * TP1_MULT, 2)
        tp2    = round(close_now - atr_now * TP2_MULT, 2)
        tp3    = round(close_now - atr_now * TP3_MULT, 2)

    # K棒時間戳（去重用）
    bar_ts = ohlcv[i][0]

    return {
        "symbol":  SYMBOL.replace("/", ""),
        "side":    side,
        "signal":  signal,
        "entry":   close_now,
        "tp1":     tp1,
        "tp2":     tp2,
        "tp3":     tp3,
        "sl":      sl,
        "_bar_ts": bar_ts,   # 內部去重欄位，不送往 execute_signal
    }

# ═══════════════════════════════════════════════════════════════
# 狀態管理（供 portfolio_app 讀取）
# ═══════════════════════════════════════════════════════════════

from nexus_webhook import USDT_PER_TRADE, LEVERAGE

_nfes_state: dict = {
    "strategy": STRATEGY_NAME,
    "positions": {},   # symbol -> position dict
    "trades":    [],   # 歷史出場記錄
    "capital":   0,    # 由 ema99_bot_state 共用，不獨立維護
    "last_run":  "",
}

def _save_state():
    """將 NFES 持倉與歷史記錄寫入 JSON，供 portfolio_app 讀取"""
    _nfes_state["last_run"] = datetime.now(timezone.utc).isoformat()
    STATE_FILE.write_text(
        json.dumps(_nfes_state, ensure_ascii=False, indent=2)
    )

def _record_open(payload: dict):
    """記錄新開倉至 state"""
    sym      = payload["symbol"]
    side     = payload["side"]
    entry    = payload["entry"]
    notional = USDT_PER_TRADE * LEVERAGE
    margin   = USDT_PER_TRADE
    qty      = round(notional / entry, 6)

    _nfes_state["positions"][sym] = {
        "strategy" : STRATEGY_NAME,
        "side"     : side,
        "signal"   : payload.get("signal", ""),
        "entry_px" : entry,
        "cur_px"   : entry,
        "sl"       : payload["sl"],
        "tp1"      : payload["tp1"],
        "tp2"      : payload["tp2"],
        "tp3"      : payload["tp3"],
        "margin"   : margin,
        "notional" : notional,
        "lev"      : LEVERAGE,
        "qty"      : qty,
        "partial"  : False,
        "entry_ts" : datetime.now(timezone.utc).isoformat(),
    }
    _save_state()

def _record_close(sym: str, reason: str, pnl: float = 0.0):
    """記錄平倉至 trades，並移除持倉"""
    pos = _nfes_state["positions"].pop(sym, None)
    if pos:
        _nfes_state["trades"].append({
            "sym"     : sym,
            "strategy": STRATEGY_NAME,
            "side"    : pos.get("side", ""),
            "entry_px": pos.get("entry_px", 0),
            "pnl"     : round(pnl, 2),
            "reason"  : reason,
            "exit_ts" : datetime.now(timezone.utc).isoformat(),
        })
        # 只保留最近 50 筆
        _nfes_state["trades"] = _nfes_state["trades"][-50:]
    _save_state()

# ═══════════════════════════════════════════════════════════════
# 資料拉取
# ═══════════════════════════════════════════════════════════════

import json as _json

def fetch_data(exch: ccxt.Exchange):
    ohlcv     = exch.fetch_ohlcv(SYMBOL, TIMEFRAME, limit=LIMIT)
    mtf_ohlcv = exch.fetch_ohlcv(SYMBOL, MTF_TIMEFRAME, limit=MTF_EMA_LEN + 10) if USE_MTF else []
    return ohlcv, mtf_ohlcv

# ═══════════════════════════════════════════════════════════════
# 主迴圈
# ═══════════════════════════════════════════════════════════════

def main():
    log.info("=" * 60)
    mode_str = "🟡 PAPER MODE" if PAPER_MODE else "🔴 LIVE MODE"
    log.info(f"NFES Signal Bot 啟動 {mode_str}")
    log.info(f"交易對: {SYMBOL}  週期: {TIMEFRAME}  模式: {SIGNAL_MODE}")
    log.info(f"MTF: {MTF_TIMEFRAME} EMA{MTF_EMA_LEN}  掃描間隔: {SCAN_INTERVAL}s")
    log.info("=" * 60)
    tg(f"🤖 <b>NFES Signal Bot 啟動</b>\n{mode_str}\n{SYMBOL} {TIMEFRAME} | {SIGNAL_MODE}")

    # 讀取既有狀態（重啟不清空歷史）
    if STATE_FILE.exists():
        try:
            saved = _json.loads(STATE_FILE.read_text())
            _nfes_state["positions"] = saved.get("positions", {})
            _nfes_state["trades"]    = saved.get("trades",    [])
        except Exception:
            pass

    exch = ccxt.binanceusdm({
        "apiKey":  os.getenv("BINANCE_API_KEY",    ""),
        "secret":  os.getenv("BINANCE_API_SECRET", ""),
        "options": {"defaultType": "future"},
    })

    last_bar_ts: int | None = None  # 去重：已觸發的K棒時間戳

    while True:
        try:
            ohlcv, mtf_ohlcv = fetch_data(exch)
            # 使用倒數第二根（已確認收盤），避免最新K棒未收盤造成假訊號
            closed_ohlcv = ohlcv[:-1]

            result = detect_signals(closed_ohlcv, mtf_ohlcv)

            # 每次掃描都更新狀態時間戳（heartbeat）
            _save_state()

            if result is None:
                log.debug("無訊號")
            else:
                bar_ts = result.pop("_bar_ts")  # 取出去重欄位

                if bar_ts == last_bar_ts:
                    log.debug(f"訊號已觸發過（bar_ts={bar_ts}），略過")
                else:
                    last_bar_ts = bar_ts
                    ts_str = datetime.fromtimestamp(bar_ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                    log.info(f"🔔 訊號觸發！{result['signal']} {result['symbol']} "
                             f"entry={result['entry']} SL={result['sl']} "
                             f"TP1={result['tp1']} TP2={result['tp2']} TP3={result['tp3']} "
                             f"@ {ts_str}")
                    # 記錄開倉
                    _record_open(result)
                    execute_signal(result)

        except ccxt.NetworkError as e:
            log.warning(f"網路錯誤: {e}，30s後重試")
            time.sleep(30)
            continue
        except Exception as e:
            log.error(f"例外: {e}", exc_info=True)
            tg(f"❌ NFES Bot 例外: {e}")

        time.sleep(SCAN_INTERVAL)


if __name__ == "__main__":
    main()
