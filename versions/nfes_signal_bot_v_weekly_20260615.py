#!/usr/bin/env python3
"""
NFES MTF Signal Bot — 三層多週期濾網訊號機器人
───────────────────────────────────────────────────────────────
架構（對應 nfes_mtf.pine）：
  層一 日線（D）  → Supertrend(4,14) 判斷多空方向 + EMA50 輔助
  層二 4H        → Supertrend(3,14) 進場訊號 + 量能 1.2x 確認
  （層三 1H 預設關閉）

進場邏輯：
  日線多頭 + 4H 翻轉/持續+回調 + 量能 → 做多
  日線空頭 + 4H 翻轉/持續+回調 + 量能 → 做空

風控參數：
  SL   = 1.2× ATR(4H)
  TP1  = 2.0R（出場 33%）
  TP2  = 3.5R（出場剩餘 50%）
  TP3  = 5.5R（全出）

其他：
  ‧ 次根 K 棒開盤進場（使用已收盤K棒，非重繪）
  ‧ 訊號去重：同一根 4H K 棒只觸發一次
  ‧ PAPER_MODE 由 nexus_webhook.py 控制

啟動方式：
  python nfes_signal_bot.py
───────────────────────────────────────────────────────────────
"""
from __future__ import annotations
import logging
import os
import time
from pathlib import Path
from datetime import datetime, timezone, timedelta

import numpy as np

# ── 自動載入 .env ──────────────────────────────────────────────
_env_file = Path(__file__).parent / ".env"
if _env_file.exists():
    for _line in _env_file.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

import json
import threading
import ccxt
import requests

# ── 從 nexus_webhook 匯入執行函式 ─────────────────────────────
from nexus_webhook import execute_signal, tg, PAPER_MODE

# ═══════════════════════════════════════════════════════════════
# 策略名稱
# ═══════════════════════════════════════════════════════════════
STRATEGY_NAME = "NFES MTF v2"

# ═══════════════════════════════════════════════════════════════
# 設定（對應 nfes_mtf.pine 預設值）
# ═══════════════════════════════════════════════════════════════
TIMEFRAME     = "4h"                # 層二：主圖進場週期
D_TIMEFRAME   = "1d"                # 層一：日線方向濾網
SCAN_INTERVAL = 360                 # 每 6 分鐘掃一次（全市場約 673 支）
TOP_N         = 9999                # 全市場（排除上架未滿30天）
LIMIT         = 200                 # 4H K棒數量（足夠計算指標）
D_LIMIT       = 100                 # 日線K棒數量

# 狀態檔（讓 portfolio_app 讀取）
STATE_FILE    = Path(__file__).parent / "nfes_bot_state.json"

# JSONBin 雲端同步（與 ema99_bot 共用同一個 .env）
# jsonbin 已移除（Render 免費額度用完，改純本地儲存）

# ── 層一：日線 Supertrend ──────────────────────────────────────
D_SENSITIVITY = 4
D_ATR_PERIOD  = 14
D_EMA_LEN     = 50        # EMA50 輔助確認

# ── 層二：4H Supertrend ───────────────────────────────────────
SENSITIVITY   = 3
ATR_PERIOD    = 14

# 量能
VOL_MA_LEN    = 20
VOL_MULT      = 1.2

# 進場確認（回調碰趨勢帶）
PULLBACK_BARS = 2

# 風控（R 倍數）
SL_ATR_MULT   = 1.2        # SL = entry ± ATR × 1.2
TP1_R         = 2.0        # TP1 = 2.0R（出場 33%）
TP2_R         = 3.5        # TP2 = 3.5R（出場 50% 剩餘）
TP3_R         = 5.5        # TP3 = 5.5R（全出）

# 持倉監控頻率（秒）— 獨立 thread，與訊號掃描分開
MONITOR_INTERVAL = 30      # 每 30 秒檢查止損/止盈，降低跳空風險

# ── 主流幣白名單（開倉保證金 100%；非主流幣 50%）─────────────────
MAJOR_COINS = {
    "BTCUSDT","ETHUSDT","BNBUSDT","SOLUSDT","XRPUSDT",
    "DOGEUSDT","ADAUSDT","AVAXUSDT","DOTUSDT","LINKUSDT",
    "UNIUSDT","MATICUSDT","LTCUSDT","ATOMUSDT","NEARUSDT",
    "FILUSDT","AAVEUSDT","INJUSDT","SUIUSDT","APTUSDT",
    "BCHUSDT","ETCUSDT","XLMUSDT","ALGOUSDT","ICPUSDT",
}

# ── BTC SMA200 空單過濾（多頭市場只接受強訊號日內短空）──────────
BTC_SMA_LEN           = 200          # 日線 SMA 週期
INTRADAY_SHORT_HOURS  = 4            # 多頭市場空單：持倉最多 4 小時

# ── v2 強化參數 ──────────────────────────────────────────────
# 1. 流動性過濾：24h 成交量（USDT）必須 > 此值才可進場
MIN_QUOTE_VOLUME      = 5_000_000    # $5M/24h，過濾小幣（DODOX、RAVE 等）

# 2. RSI 過濾（v2.1 動能斜率版，2026-06-06）
# 原條件備份：versions/nfes_signal_bot_v2_rsi_original_20260606.py
# 原條件：SHORT_RSI_MIN=50, LONG_RSI_MAX=65
RSI_PERIOD            = 14
# v2.1 新條件改用斜率+區間判斷（見 _rsi_ok_short / _rsi_ok_long）
SHORT_RSI_SLOPE_MAX   = 55           # 空單：RSI < 55（不在明顯超買）
LONG_RSI_SLOPE_MIN    = 45           # 多單：RSI > 45（不在明顯超賣）
RSI_BOUNCE_SHORT_LO   = 35           # 反彈失敗區間下緣（空單）
RSI_BOUNCE_SHORT_HI   = 40           # 反彈失敗區間上緣（空單）
RSI_BOUNCE_LONG_LO    = 60           # 回測支撐區間下緣（多單）
RSI_BOUNCE_LONG_HI    = 65           # 回測支撐區間上緣（多單）

# 3. 止損後同幣冷卻延長
STOPLOSS_COOLDOWN_H   = 24           # 止損後冷卻 24h（原 1h）
INTRADAY_LOSS_COOL_H  = 4            # 日內虧損到期冷卻 4h

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

def detect_signals(ohlcv_4h: list, ohlcv_d: list) -> dict | None:
    """
    三層 MTF 訊號偵測（對應 nfes_mtf.pine）
    ohlcv_4h : 4H K棒列表（主圖訊號層）
    ohlcv_d  : 日線K棒列表（方向濾網層）
    回傳訊號 dict 或 None
    """
    if len(ohlcv_4h) < 50 or len(ohlcv_d) < D_ATR_PERIOD + D_EMA_LEN:
        return None   # 新幣K棒不足，靜默跳過

    # ── 層一：日線 ────────────────────────────────────────────
    dh = np.array([x[2] for x in ohlcv_d], dtype=float)
    dl = np.array([x[3] for x in ohlcv_d], dtype=float)
    dc = np.array([x[4] for x in ohlcv_d], dtype=float)

    _, _, d_dir = calc_adaptive_supertrend(dh, dl, dc, D_SENSITIVITY, D_ATR_PERIOD)
    d_ema50     = ema(dc, D_EMA_LEN)

    d_bull = (d_dir[-1] == 1)   # 日線多頭
    d_bear = (d_dir[-1] == -1)  # 日線空頭
    # EMA50 輔助（僅輔助確認，不硬性過濾）
    d_ema_ok_long  = np.isnan(d_ema50[-1]) or (dc[-1] > d_ema50[-1])
    d_ema_ok_short = np.isnan(d_ema50[-1]) or (dc[-1] < d_ema50[-1])

    # 日線方向（ema50 輔助：ema 沒有明顯衝突才放行）
    day_long_ok  = d_bull and d_ema_ok_long
    day_short_ok = d_bear and d_ema_ok_short

    if not day_long_ok and not day_short_ok:
        log.debug(f"日線方向不明確，略過 (d_dir={d_dir[-1]} d_ema={d_ema50[-1]:.1f} dc={dc[-1]:.1f})")
        return None

    # ── 層二：4H 進場 ─────────────────────────────────────────
    h4_h = np.array([x[2] for x in ohlcv_4h], dtype=float)
    h4_l = np.array([x[3] for x in ohlcv_4h], dtype=float)
    h4_c = np.array([x[4] for x in ohlcv_4h], dtype=float)
    h4_v = np.array([x[5] for x in ohlcv_4h], dtype=float)

    atr4h                       = true_atr(h4_h, h4_l, h4_c, ATR_PERIOD)
    h4_upper, h4_lower, h4_dir = calc_adaptive_supertrend(h4_h, h4_l, h4_c, SENSITIVITY, ATR_PERIOD)

    vol_avg = sma(h4_v, VOL_MA_LEN)

    i   = len(h4_c) - 1
    i_1 = i - 1

    if np.isnan(atr4h[i]) or np.isnan(vol_avg[i]):
        return None

    close_now = h4_c[i]
    atr_now   = atr4h[i]
    vol_now   = h4_v[i]
    vol_avg_n = vol_avg[i]

    dir_now = h4_dir[i]
    dir_pre = h4_dir[i_1] if i_1 >= 0 else dir_now

    # 4H 趨勢狀態
    flip_up = (dir_now ==  1 and dir_pre == -1)   # 剛翻多
    flip_dn = (dir_now == -1 and dir_pre ==  1)   # 剛翻空
    cont_up = (dir_now ==  1)                      # 多頭持續
    cont_dn = (dir_now == -1)                      # 空頭持續

    # 量能確認
    vol_ok = (vol_now > vol_avg_n * VOL_MULT)

    # 回調確認（最近 PULLBACK_BARS 根曾觸及趨勢帶）
    pb_long = pb_short = False
    for j in range(1, PULLBACK_BARS + 1):
        idx = i - j
        if idx < 0:
            break
        if not np.isnan(h4_lower[idx]) and h4_l[idx] <= h4_lower[idx] * 1.003:
            pb_long = True
        if not np.isnan(h4_upper[idx]) and h4_h[idx] >= h4_upper[idx] * 0.997:
            pb_short = True

    # 4H 進場條件（翻轉 or 回調延續）
    entry_long  = flip_up or (cont_up and pb_long  and h4_c[i] > h4_lower[i])
    entry_short = flip_dn or (cont_dn and pb_short and h4_c[i] < h4_upper[i])

    # ── v2.1：RSI 動能斜率過濾（2026-06-06）─────────────────────
    # 原條件：空單RSI>50 / 多單RSI<65（過嚴，熊市幾乎全攔）
    # 新條件：擇一滿足
    #   條件1（斜率）：RSI方向正確 + RSI在合理區間
    #   條件2（反彈測試失敗）：RSI曾觸及測試區間但未突破
    rsi4h = calc_rsi(h4_c, RSI_PERIOD)
    rsi_now  = rsi4h[i]   if not np.isnan(rsi4h[i])   else 50.0
    rsi_prev = rsi4h[i-1] if i >= 1 and not np.isnan(rsi4h[i-1]) else rsi_now
    rsi_p2   = rsi4h[i-2] if i >= 2 and not np.isnan(rsi4h[i-2]) else rsi_prev
    rsi_p3   = rsi4h[i-3] if i >= 3 and not np.isnan(rsi4h[i-3]) else rsi_p2

    # 空單 RSI 判斷
    if entry_short:
        slope_down = (rsi_now < rsi_prev < rsi_p2)          # 最近3根RSI下降
        c1_short   = slope_down and rsi_now < SHORT_RSI_SLOPE_MAX  # 條件1：斜率向下+RSI<55
        # 條件2：過去5根內曾進入35~40區間，但未突破40，且當前RSI仍在下彎
        rsi_window = [rsi4h[max(0,i-k)] for k in range(5) if not np.isnan(rsi4h[max(0,i-k)])]
        bounce_touched = any(RSI_BOUNCE_SHORT_LO <= r <= RSI_BOUNCE_SHORT_HI for r in rsi_window)
        bounce_failed  = max(rsi_window) < RSI_BOUNCE_SHORT_HI + 2  # 未站上40以上
        c2_short = bounce_touched and bounce_failed and (rsi_now < rsi_prev)  # 條件2：反彈失敗+重新下彎
        if not (c1_short or c2_short):
            log.info(f"[RSI_FILTER] 空單跳過：4H RSI={rsi_now:.1f} 斜率不符（slope_down={slope_down}, bounce={c2_short}）")
            entry_short = False

    # 多單 RSI 判斷
    if entry_long:
        slope_up  = (rsi_now > rsi_prev > rsi_p2)           # 最近3根RSI上升
        c1_long   = slope_up and rsi_now > LONG_RSI_SLOPE_MIN    # 條件1：斜率向上+RSI>45
        rsi_window = [rsi4h[max(0,i-k)] for k in range(5) if not np.isnan(rsi4h[max(0,i-k)])]
        pullback_touched = any(RSI_BOUNCE_LONG_LO <= r <= RSI_BOUNCE_LONG_HI for r in rsi_window)
        pullback_held    = min(rsi_window) > RSI_BOUNCE_LONG_LO - 2   # 未跌破60以下
        c2_long = pullback_touched and pullback_held and (rsi_now > rsi_prev)  # 條件2：回測支撐+重新上揚
        if not (c1_long or c2_long):
            log.info(f"[RSI_FILTER] 多單跳過：4H RSI={rsi_now:.1f} 斜率不符（slope_up={slope_up}, pullback={c2_long}）")
            entry_long = False

    # ── 綜合三層條件 ─────────────────────────────────────────
    raw_long  = entry_long  and vol_ok and day_long_ok
    raw_short = entry_short and vol_ok and day_short_ok

    if not raw_long and not raw_short:
        return None

    # 強/弱（vol_ok 已是必要條件，此處用 flip 區分強弱）
    if raw_long:
        side   = "long"
        signal = "▲+" if flip_up else "▲"
    else:
        side   = "short"
        signal = "▼+" if flip_dn else "▼"

    # ── 風控計算（基於 ATR，R 倍數）────────────────────────────
    sl_dist = atr_now * SL_ATR_MULT    # SL 距離

    # 自動判斷小數位數（避免超小幣種被 round(x, 2) 歸零）
    def _smart_round(val: float) -> float:
        if val == 0:
            return 0.0
        import math
        mag = -int(math.floor(math.log10(abs(val)))) + 3
        digits = max(2, mag)
        return round(val, digits)

    if side == "long":
        sl  = _smart_round(close_now - sl_dist)
        tp1 = _smart_round(close_now + sl_dist * TP1_R)
        tp2 = _smart_round(close_now + sl_dist * TP2_R)
        tp3 = _smart_round(close_now + sl_dist * TP3_R)
    else:
        sl  = _smart_round(close_now + sl_dist)
        tp1 = _smart_round(max(close_now - sl_dist * TP1_R, close_now * 0.01))
        tp2 = _smart_round(max(close_now - sl_dist * TP2_R, close_now * 0.005))
        tp3 = _smart_round(max(close_now - sl_dist * TP3_R, close_now * 0.001))

    # 安全檢查：TP/SL 不可為 0 或負數
    if sl == 0 or tp1 == 0 or tp3 == 0 or tp1 <= 0 or tp3 <= 0:
        log.warning(f"[SIGNAL] {side} 風控計算異常 sl={sl} tp1={tp1} tp3={tp3}，跳過此訊號")
        return None

    # 空單額外驗證：SL距離不可超過進場價（否則TP會是負數）
    if side == "short" and sl_dist * TP3_R >= close_now:
        log.warning(f"[SIGNAL] 空單 ATR過大（sl_dist={sl_dist:.6g} × {TP3_R}R >= entry={close_now:.6g}），跳過")
        return None

    bar_ts = ohlcv_4h[i][0]

    log.debug(
        f"訊號生成 {signal} | 日線={'多' if d_bull else '空'} "
        f"d_ema={'OK' if (d_ema_ok_long if side=='long' else d_ema_ok_short) else 'X'} "
        f"4H_dir={'多' if dir_now==1 else '空'} flip={flip_up or flip_dn} "
        f"vol={vol_now:.0f}>avg*{VOL_MULT}={vol_avg_n*VOL_MULT:.0f}"
    )

    return {
        "symbol":  "UNKNOWN",   # 由主迴圈依掃描的幣種覆寫
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

from nexus_webhook import calc_trade_params

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
    """記錄新開倉至 state，並從共用資金池扣除保證金"""
    from shared_capital import adjust_capital
    sym      = payload["symbol"]
    side     = payload["side"]
    entry    = payload["entry"]
    signal   = payload.get("signal", "")
    margin, lev = calc_trade_params(signal)
    if margin <= 0:
        log.warning(f"_record_open {sym} 跳過：共用資金池不足或已達倉位上限")
        return

    # ── 非主流幣：保證金上限 50% ─────────────────────────────────
    raw_sym = sym.replace("USDT", "USDT")   # 已是 XYZUSDT 格式
    if raw_sym not in MAJOR_COINS:
        margin = round(margin * 0.5, 2)
        margin = max(margin, 10.0)
        log.info(f"_record_open {sym} 非主流幣，保證金縮減 50% → {margin:.0f} USDT")

    notional = margin * lev
    qty      = round(notional / entry, 6)
    adjust_capital(-margin, f"NFES open {sym}")

    entry_ts = datetime.now(timezone.utc)

    # ── 日內短空旗標（由 payload 帶入）───────────────────────────
    intraday = payload.get("intraday_short", False)
    exit_after_ts = (
        (entry_ts + timedelta(hours=INTRADAY_SHORT_HOURS)).isoformat()
        if intraday else None
    )

    _nfes_state["positions"][sym] = {
        "strategy"      : STRATEGY_NAME,
        "side"          : side,
        "signal"        : signal,
        "entry_px"      : entry,
        "cur_px"        : entry,
        "sl"            : payload["sl"],
        "tp1"           : payload["tp1"],
        "tp2"           : payload["tp2"],
        "tp3"           : payload["tp3"],
        "margin"        : margin,
        "notional"      : notional,
        "lev"           : lev,
        "qty"           : qty,
        "partial"       : False,
        "entry_ts"      : entry_ts.isoformat(),
        "intraday_short": intraday,
        "exit_after_ts" : exit_after_ts,
    }
    _save_state()

def _record_close(sym: str, reason: str, pnl: float = 0.0):
    """記錄平倉至 trades，歸還保證金 + 盈虧至共用資金池"""
    from shared_capital import adjust_capital
    pos = _nfes_state["positions"].pop(sym, None)
    if pos:
        margin = pos.get("margin", 0)
        adjust_capital(margin + pnl, f"NFES close {sym} [{reason}]")
        pnl_rounded = round(pnl, 2)
        _nfes_state["trades"].append({
            "sym"     : sym,
            "strategy": STRATEGY_NAME,
            "side"    : pos.get("side", ""),
            "entry_px": pos.get("entry_px", 0),
            "exit_px" : pos.get("cur_px", 0),
            "tp1"     : pos.get("tp1"),
            "tp2"     : pos.get("tp2"),
            "tp3"     : pos.get("tp3"),
            "sl"      : pos.get("sl"),
            "pnl"     : pnl_rounded,
            "reason"  : reason,
            "entry_ts": pos.get("entry_ts", ""),
            "exit_ts" : datetime.now(timezone.utc).isoformat(),
        })
        # 累計盈虧獨立追蹤（不受 trade 筆數上限影響）
        _nfes_state["total_realized_pnl"] = round(
            _nfes_state.get("total_realized_pnl", 0.0) + pnl_rounded, 2
        )
        _nfes_state["total_trades"] = _nfes_state.get("total_trades", 0) + 1
        if pnl_rounded > 0:
            _nfes_state["total_wins"] = _nfes_state.get("total_wins", 0) + 1
        # v2：記錄止損冷卻（24h）或日內虧損冷卻（4h）
        if reason == "stop_loss" and pnl < 0:
            _nfes_state.setdefault("cooldown", {})[sym] = {
                "until": (datetime.now(timezone.utc) + timedelta(hours=STOPLOSS_COOLDOWN_H)).isoformat(),
                "reason": "stop_loss"
            }
        elif reason == "intraday_exit" and pnl < 0:
            _nfes_state.setdefault("cooldown", {})[sym] = {
                "until": (datetime.now(timezone.utc) + timedelta(hours=INTRADAY_LOSS_COOL_H)).isoformat(),
                "reason": "intraday_loss"
            }
        # 只保留最近 500 筆（保留更多歷史）
        _nfes_state["trades"] = _nfes_state["trades"][-500:]
    _save_state()

# ═══════════════════════════════════════════════════════════════
# 持倉監控：止盈 / 止損 / 移動止損
# ═══════════════════════════════════════════════════════════════

def _get_price(exch: ccxt.Exchange, binance_sym: str) -> float | None:
    """取得即時成交價（用 ticker）"""
    try:
        ccxt_sym = binance_sym.replace("USDT", "/USDT:USDT") if "/" not in binance_sym else binance_sym
        t = exch.fetch_ticker(ccxt_sym)
        return float(t["last"])
    except Exception as e:
        log.warning(f"fetch_ticker {binance_sym} 失敗: {e}")
        return None

def monitor_positions(exch: ccxt.Exchange):
    """掃描所有持倉，執行止損/止盈，更新現價"""
    positions = _nfes_state["positions"]
    if not positions:
        return

    for sym, pos in list(positions.items()):
        cur = _get_price(exch, sym)
        if cur is None:
            continue

        # 更新現價
        pos["cur_px"] = cur

        entry  = pos["entry_px"]
        sl     = pos["sl"]
        tp1    = pos["tp1"]
        tp2    = pos["tp2"]
        tp3    = pos["tp3"]
        side   = pos.get("side", "long")
        qty    = pos.get("qty", 0)
        notional = pos.get("notional", 0)

        # 即時計算未實現損益
        is_long_flag = side == "long"
        pos["unrealized_pnl"] = round(
            (cur - entry) / entry * notional * (1 if is_long_flag else -1), 2
        )
        tp2_hit = pos.get("tp2_hit", False)
        tp1_hit = pos.get("tp1_hit", False)
        is_long = side == "long"

        def pnl_usd(exit_px):
            return (exit_px - entry) / entry * notional * (1 if is_long else -1)

        # ── 日內模式：4H 時間到強制平倉（多空通用）────────────────
        if pos.get("intraday_short"):
            exit_after = pos.get("exit_after_ts")
            if exit_after:
                try:
                    exit_dt = datetime.fromisoformat(exit_after)
                    if datetime.now(timezone.utc) >= exit_dt:
                        pnl = pnl_usd(cur)
                        icon = "🟢 LONG" if is_long else "🔴 SHORT"
                        msg = (
                            f"⏰ <b>NFES 日內模式時間到平倉</b>\n"
                            f"{icon} {sym}\n"
                            f"進場: {entry:.6g}  現價: {cur:.6g}  持倉4H到期\n"
                            f"損益: {'+' if pnl>=0 else ''}{pnl:.2f} USDT"
                        )
                        log.info(f"[INTRADAY] {sym} 4H到期平倉 cur={cur} pnl={pnl:.2f}")
                        tg(msg)
                        _record_close(sym, "intraday_exit", pnl)
                        continue
                except Exception as _e:
                    log.warning(f"intraday 時間解析失敗: {_e}")

        # ── 止損觸發 ────────────────────────────────────────────
        sl_hit = (cur <= sl) if is_long else (cur >= sl)
        if sl_hit:
            pnl = pnl_usd(sl)
            icon = "🔴" if is_long else "🟢"
            reason = "stop_loss"
            msg = (
                f"🛑 <b>NFES 止損出場</b>\n"
                f"{icon} {'LONG' if is_long else 'SHORT'} {sym}\n"
                f"進場: {entry:.6g}  止損: {sl:.6g}  現價: {cur:.6g}\n"
                f"損益: {'+' if pnl>=0 else ''}{pnl:.2f} USDT"
            )
            log.info(f"[SL] {sym} 止損 cur={cur} sl={sl} pnl={pnl:.2f}")
            tg(msg)
            _record_close(sym, reason, pnl)
            continue

        # ── TP3 全出 ────────────────────────────────────────────
        tp3_hit = (cur >= tp3) if is_long else (cur <= tp3)
        if tp3_hit and tp1_hit:
            pnl = pnl_usd(tp3)
            msg = (
                f"🎯 <b>NFES TP3 全出</b>\n"
                f"{'🟢 LONG' if is_long else '🔴 SHORT'} {sym}\n"
                f"進場: {entry:.6g}  TP3: {tp3:.6g}  現價: {cur:.6g}\n"
                f"損益: +{pnl:.2f} USDT"
            )
            log.info(f"[TP3] {sym} 全出 cur={cur} tp3={tp3} pnl={pnl:.2f}")
            tg(msg)
            _record_close(sym, "tp3", pnl)
            continue

        # ── TP2 出場（剩餘 50%）─────────────────────────────────
        tp2_price_hit = (cur >= tp2) if is_long else (cur <= tp2)
        if tp2_price_hit and tp1_hit and not tp2_hit:
            pos["tp2_hit"] = True
            pos["partial"] = True
            # SL 移到 TP1（鎖利）
            pos["sl"] = tp1
            pnl = pnl_usd(tp2) * (1/3)   # 約剩 1/3 倉
            msg = (
                f"🎯 <b>NFES TP2 部分出場</b>\n"
                f"{'🟢 LONG' if is_long else '🔴 SHORT'} {sym}\n"
                f"TP2: {tp2:.6g}  SL 移至 TP1: {tp1:.6g}\n"
                f"預估損益: +{pnl:.2f} USDT"
            )
            log.info(f"[TP2] {sym} 部分出場 cur={cur} tp2={tp2}")
            tg(msg)
            _save_state()
            continue

        # ── TP1 出場（第一批 33%）───────────────────────────────
        tp1_price_hit = (cur >= tp1) if is_long else (cur <= tp1)
        if tp1_price_hit and not tp1_hit:
            pos["tp1_hit"] = True
            pos["partial"] = True
            # SL 移到 breakeven（進場價）
            pos["sl"] = entry
            pnl = pnl_usd(tp1) * (1/3)
            msg = (
                f"🎯 <b>NFES TP1 部分出場</b>\n"
                f"{'🟢 LONG' if is_long else '🔴 SHORT'} {sym}\n"
                f"TP1: {tp1:.6g}  SL 移至保本: {entry:.6g}\n"
                f"預估損益: +{pnl:.2f} USDT"
            )
            log.info(f"[TP1] {sym} 部分出場 cur={cur} tp1={tp1}")
            tg(msg)
            _save_state()
            continue

    _save_state()


# ═══════════════════════════════════════════════════════════════
# 資料拉取
# ═══════════════════════════════════════════════════════════════

# ── CoinGecko 市值排名快取（每小時更新一次）────────────────────
_mcap_cache: dict = {"ranks": {}, "ts": 0}

def _get_mcap_ranks(top_n: int = 300) -> dict:
    """從 CoinGecko 取市值排名，回傳 {symbol_upper: rank}，快取 1 小時"""
    now = time.time()
    if _mcap_cache["ranks"] and now - _mcap_cache["ts"] < 3600:
        return _mcap_cache["ranks"]
    ranks = {}
    try:
        per_page = 250
        pages = (top_n + per_page - 1) // per_page
        for page in range(1, pages + 1):
            r = requests.get(
                "https://api.coingecko.com/api/v3/coins/markets",
                params={
                    "vs_currency": "usd",
                    "order": "market_cap_desc",
                    "per_page": per_page,
                    "page": page,
                    "sparkline": False,
                },
                timeout=10,
            )
            if not r.ok:
                break
            for c in r.json():
                ranks[c["symbol"].upper()] = c["market_cap_rank"] or 9999
        _mcap_cache["ranks"] = ranks
        _mcap_cache["ts"] = now
        log.info(f"[MCAP] CoinGecko 市值排名更新，共 {len(ranks)} 筆")
    except Exception as e:
        log.warning(f"[MCAP] CoinGecko 取得失敗，沿用快取：{e}")
    return _mcap_cache["ranks"]


def get_top_symbols(exch: ccxt.Exchange) -> list[str]:
    """依市值排名取前 TOP_N 支 Binance USDT 永續合約（排除上架未滿30天）"""
    from datetime import datetime, timezone, timedelta
    exch.load_markets()
    one_month_ago = datetime.now(timezone.utc) - timedelta(days=30)

    # 取得市值排名（快取 1 小時）
    mcap_ranks = _get_mcap_ranks(top_n=1000)  # 全市場模式，拉足夠多的市值資料

    # 建立 Binance 合約清單，過濾上架未滿30天
    valid_syms = []
    for sym, mkt in exch.markets.items():
        if not sym.endswith("/USDT:USDT"):
            continue
        listed = mkt.get("info", {}).get("onboardDate")
        if listed:
            listed_dt = datetime.fromtimestamp(int(listed) / 1000, tz=timezone.utc)
            if listed_dt > one_month_ago:
                continue  # 排除剛上架
        base = sym.split("/")[0]
        rank = mcap_ranks.get(base, 9999)
        valid_syms.append((sym, rank))

    # 按市值排名排序，取前 TOP_N
    valid_syms.sort(key=lambda x: x[1])
    result = [s[0] for s in valid_syms[:TOP_N]]
    log.info(f"[MCAP] 掃描清單：前 {TOP_N} 支（市值排名）共 {len(result)} 支")
    return result


def fetch_data(exch: ccxt.Exchange, symbol: str):
    ohlcv_4h = exch.fetch_ohlcv(symbol, TIMEFRAME,   limit=LIMIT)
    ohlcv_d  = exch.fetch_ohlcv(symbol, D_TIMEFRAME, limit=D_LIMIT)
    return ohlcv_4h, ohlcv_d


_btc_sma200_cache: dict = {"value": None, "ts": 0}

def get_btc_sma200(exch: ccxt.Exchange) -> float | None:
    """取得 BTC 日線 SMA200，快取 5 分鐘"""
    now = time.time()
    if _btc_sma200_cache["value"] and now - _btc_sma200_cache["ts"] < 300:
        return _btc_sma200_cache["value"]
    try:
        ohlcv = exch.fetch_ohlcv("BTC/USDT:USDT", "1d", limit=BTC_SMA_LEN + 5)
        closes = np.array([x[4] for x in ohlcv], dtype=float)
        sma = float(np.mean(closes[-BTC_SMA_LEN:]))
        _btc_sma200_cache["value"] = sma
        _btc_sma200_cache["ts"]    = now
        return sma
    except Exception as e:
        log.warning(f"get_btc_sma200 失敗: {e}")
        return None

# ═══════════════════════════════════════════════════════════════
# 主迴圈
# ═══════════════════════════════════════════════════════════════

def main():
    log.info("=" * 60)
    mode_str = "🟡 PAPER MODE" if PAPER_MODE else "🔴 LIVE MODE"
    log.info(f"NFES MTF Signal Bot 啟動 {mode_str}")
    log.info(f"掃描範圍: Top{TOP_N} USDT永續  主圖: {TIMEFRAME}  日線濾網: {D_TIMEFRAME}")
    log.info(f"4H ST({SENSITIVITY},{ATR_PERIOD})  日線ST({D_SENSITIVITY},{D_ATR_PERIOD}) EMA{D_EMA_LEN}")
    log.info(f"SL={SL_ATR_MULT}×ATR  TP1={TP1_R}R TP2={TP2_R}R TP3={TP3_R}R  掃描間隔: {SCAN_INTERVAL}s")
    log.info("=" * 60)
    tg(
        f"🤖 <b>NFES MTF Bot 啟動</b>\n{mode_str}\n"
        f"Top{TOP_N} 永續 | 4H+日線 MTF\n"
        f"SL={SL_ATR_MULT}×ATR  TP={TP1_R}R/{TP2_R}R/{TP3_R}R  間隔{SCAN_INTERVAL//60}分"
    )

    # 讀取既有狀態（重啟不清空歷史）
    if STATE_FILE.exists():
        try:
            saved = json.loads(STATE_FILE.read_text())
            _nfes_state["positions"]          = saved.get("positions", {})
            _nfes_state["trades"]             = saved.get("trades",    [])
            _nfes_state["total_realized_pnl"] = saved.get("total_realized_pnl", 0.0)
            _nfes_state["total_trades"]        = saved.get("total_trades", 0)
            _nfes_state["total_wins"]          = saved.get("total_wins",  0)
            _nfes_state["cooldown"]            = saved.get("cooldown",    {})
        except Exception:
            pass

    exch = ccxt.binanceusdm({
        "apiKey"         : os.getenv("BINANCE_API_KEY",    ""),
        "secret"         : os.getenv("BINANCE_API_SECRET", ""),
        "options"        : {"defaultType": "future", "fetchCurrencies": False},
        "enableRateLimit": True,
    })

    # ── Thread 1：高頻持倉監控（每 30 秒）────────────────────────
    # 只對已持倉幣種拉 ticker，API 消耗極低
    def _monitor_loop():
        while True:
            try:
                monitor_positions(exch)
            except Exception as e:
                log.error(f"monitor_loop 例外: {e}")
            time.sleep(MONITOR_INTERVAL)

    t = threading.Thread(target=_monitor_loop, daemon=True, name="monitor")
    t.start()
    log.info(f"✅ 持倉監控 thread 啟動（每 {MONITOR_INTERVAL}s）")

    # ── Thread 2（主線程）：訊號掃描（每 5 分鐘）─────────────────
    # ── 修復：從 state 恢復已記錄的 bar_ts，重啟後不重複進場 ────
    last_bar_ts: dict[str, int] = {}
    _bar_ts_file = Path(__file__).parent / "nfes_bar_ts.json"
    try:
        if _bar_ts_file.exists():
            last_bar_ts = json.loads(_bar_ts_file.read_text(encoding="utf-8"))
            log.info(f"✅ 載入 bar_ts 去重記錄，共 {len(last_bar_ts)} 筆")
    except Exception as _e:
        log.warning(f"載入 bar_ts 失敗: {_e}")

    def _save_bar_ts():
        try:
            _bar_ts_file.write_text(json.dumps(last_bar_ts), encoding="utf-8")
        except Exception as _e:
            log.warning(f"儲存 bar_ts 失敗: {_e}")

    while True:
        try:
            # 取 Top N 幣種掃描新訊號
            try:
                symbols = get_top_symbols(exch)
                log.info(f"掃描 {len(symbols)} 個幣種...")
            except Exception as e:
                log.warning(f"get_top_symbols 失敗: {e}，30s後重試")
                time.sleep(30)
                continue

            signals_found = 0

            # ── v2：帶入 24h 成交量快取供流動性過濾 ─────────────
            try:
                tickers_v = exch.fetch_tickers(params={"type": "future"})
                vol_cache = {
                    s.replace("/USDT:USDT","USDT").replace("/",""):
                    float(t.get("quoteVolume") or 0)
                    for s, t in tickers_v.items()
                    if s.endswith("/USDT:USDT")
                }
            except Exception:
                vol_cache = {}

            for sym in symbols:
                # ── v2：流動性過濾 ────────────────────────────────
                sym_clean = sym.replace("/USDT:USDT","USDT").replace("/","")
                vol_24h = vol_cache.get(sym_clean, 0)
                if vol_24h < MIN_QUOTE_VOLUME:
                    log.debug(f"[VOL_FILTER] {sym_clean} 跳過：24h量={vol_24h/1e6:.1f}M < {MIN_QUOTE_VOLUME/1e6:.0f}M")
                    continue

                try:
                    ohlcv_4h, ohlcv_d = fetch_data(exch, sym)
                except ccxt.NetworkError as e:
                    log.warning(f"{sym} 網路錯誤: {e}，跳過")
                    continue
                except Exception as e:
                    log.debug(f"{sym} 資料錯誤: {e}，跳過")
                    continue

                closed_4h = ohlcv_4h[:-1]
                closed_d  = ohlcv_d[:-1]

                result = detect_signals(closed_4h, closed_d)
                if result is None:
                    continue

                bar_ts = result.pop("_bar_ts")

                if last_bar_ts.get(sym) == bar_ts:
                    continue

                # ── 修復 Bug1：SL 方向驗證 ──────────────────────────
                entry = result.get("entry", 0)
                sl    = result.get("sl", 0)
                side  = result.get("side", "long")
                if side == "long" and sl >= entry:
                    log.warning(
                        f"⚠️ 跳過 {sym}：多單 SL({sl}) >= entry({entry})，訊號異常"
                    )
                    continue
                if side == "short" and sl <= entry:
                    log.warning(
                        f"⚠️ 跳過 {sym}：空單 SL({sl}) <= entry({entry})，訊號異常"
                    )
                    continue

                last_bar_ts[sym] = bar_ts
                _save_bar_ts()   # 修復 Bug2：持久化，重啟後不重入
                signals_found += 1

                result["symbol"] = sym.replace("/USDT:USDT", "USDT").replace("/", "")

                # ── v2：止損冷卻檢查 ─────────────────────────────
                sym_clean2 = result["symbol"]
                cool = _nfes_state.get("cooldown", {}).get(sym_clean2)
                if cool:
                    until_dt = datetime.fromisoformat(cool["until"])
                    if datetime.now(timezone.utc) < until_dt:
                        remaining_h = (until_dt - datetime.now(timezone.utc)).seconds // 3600
                        log.info(f"[COOLDOWN] {sym_clean2} 跳過：{cool['reason']} 冷卻中，剩 {remaining_h}h")
                        continue

                # ── BTC SMA200 多空過濾 ──────────────────────────────
                btc_price = _get_price(exch, "BTCUSDT")
                btc_sma   = get_btc_sma200(exch)
                if btc_price and btc_sma:
                    btc_bull = btc_price > btc_sma
                    side_now = result.get("side")

                    if side_now == "short" and btc_bull:
                        # 多頭市場：空單只接受強訊號，改日內模式
                        if "+" not in result.get("signal", ""):
                            log.info(
                                f"⛔ 空單 {result['symbol']} 略過：BTC({btc_price:.0f}) > SMA200({btc_sma:.0f})"
                                f"，非強訊號 {result['signal']}"
                            )
                            continue
                        result["intraday_short"] = True
                        log.info(
                            f"⚠️ 空單 {result['symbol']} 日內模式：BTC > SMA200"
                            f"，強訊號 {result['signal']} 允許，4H後強制平倉"
                        )

                    elif side_now == "long" and not btc_bull:
                        # 熊市：多單只接受強訊號，改日內模式
                        if "+" not in result.get("signal", ""):
                            log.info(
                                f"⛔ 多單 {result['symbol']} 略過：BTC({btc_price:.0f}) < SMA200({btc_sma:.0f})"
                                f"，非強訊號 {result['signal']}"
                            )
                            continue
                        result["intraday_short"] = True   # 複用同一旗標控制日內模式
                        log.info(
                            f"⚠️ 多單 {result['symbol']} 日內模式：BTC < SMA200"
                            f"，強訊號 {result['signal']} 允許，4H後強制平倉"
                        )

                ts_str = datetime.fromtimestamp(bar_ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                log.info(
                    f"🔔 {result['signal']} {result['symbol']}  "
                    f"entry={result['entry']}  SL={result['sl']}  "
                    f"TP1={result['tp1']} TP2={result['tp2']} TP3={result['tp3']}  @{ts_str}"
                )
                # ── 共用倉位上限檢查 ─────────────────────────────
                from shared_capital import get_total_positions, MAX_POS_PER_BOT
                n_own = len(_nfes_state["positions"])
                _, _, total = get_total_positions()
                is_strong   = "+" in result.get("signal", "")
                if n_own >= MAX_POS_PER_BOT and not is_strong:
                    log.info(f"NFES 持倉 {n_own}/5 已滿，略過非強訊號 {result['symbol']}")
                    continue
                if total >= 10 and not is_strong:
                    log.info(f"合計持倉 {total}/10 已滿，略過非強訊號 {result['symbol']}")
                    continue

                _record_open(result)
                execute_signal(result)

            log.info(f"本輪掃描完成，發現 {signals_found} 個訊號")
            _save_state()

        except Exception as e:
            log.error(f"主迴圈例外: {e}", exc_info=True)
            tg(f"❌ NFES Bot 例外: {e}")

        time.sleep(SCAN_INTERVAL)


if __name__ == "__main__":
    main()
