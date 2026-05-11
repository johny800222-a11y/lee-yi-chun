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

import json
import threading
import ccxt
import requests

# ── 從 nexus_webhook 匯入執行函式 ─────────────────────────────
from nexus_webhook import execute_signal, tg, PAPER_MODE

# ═══════════════════════════════════════════════════════════════
# 策略名稱
# ═══════════════════════════════════════════════════════════════
STRATEGY_NAME = "NFES MTF"

# ═══════════════════════════════════════════════════════════════
# 設定（對應 nfes_mtf.pine 預設值）
# ═══════════════════════════════════════════════════════════════
TIMEFRAME     = "4h"                # 層二：主圖進場週期
D_TIMEFRAME   = "1d"                # 層一：日線方向濾網
SCAN_INTERVAL = 300                 # 每 5 分鐘掃一次（4H 訊號不需更頻繁）
TOP_N         = 100                 # 掃描 Top N USDT 永續合約
LIMIT         = 200                 # 4H K棒數量（足夠計算指標）
D_LIMIT       = 100                 # 日線K棒數量

# 狀態檔（讓 portfolio_app 讀取）
STATE_FILE    = Path(__file__).parent / "nfes_bot_state.json"

# JSONBin 雲端同步（與 ema99_bot 共用同一個 .env）
JSONBIN_API_KEY     = os.getenv("JSONBIN_API_KEY", "")
JSONBIN_NFES_BIN_ID = os.getenv("JSONBIN_NFES_BIN_ID", "")

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
    if side == "long":
        sl  = round(close_now - sl_dist, 2)
        tp1 = round(close_now + sl_dist * TP1_R, 2)
        tp2 = round(close_now + sl_dist * TP2_R, 2)
        tp3 = round(close_now + sl_dist * TP3_R, 2)
    else:
        sl  = round(close_now + sl_dist, 2)
        tp1 = round(close_now - sl_dist * TP1_R, 2)
        tp2 = round(close_now - sl_dist * TP2_R, 2)
        tp3 = round(close_now - sl_dist * TP3_R, 2)

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

def _jsonbin_save_worker(snapshot: dict):
    """背景執行：retry 3 次，完全不阻塞主執行緒"""
    if not JSONBIN_API_KEY or not JSONBIN_NFES_BIN_ID:
        return
    url     = f"https://api.jsonbin.io/v3/b/{JSONBIN_NFES_BIN_ID}"
    headers = {"X-Master-Key": JSONBIN_API_KEY, "Content-Type": "application/json"}
    for attempt in range(3):
        try:
            r = requests.put(url, headers=headers, json=snapshot, timeout=15)
            if r.ok:
                log.info(f"jsonbin sync ok (attempt {attempt+1})")
                return
            log.warning(f"jsonbin save HTTP {r.status_code} (attempt {attempt+1})")
        except Exception as e:
            log.warning(f"jsonbin save failed (attempt {attempt+1}): {e}")
        time.sleep(5)
    log.error("jsonbin sync failed after 3 attempts")

def _jsonbin_save():
    """非同步推送至 JSONBin（背景 thread，不影響監控精度）"""
    snapshot = json.loads(json.dumps(_nfes_state))   # deep copy
    t = threading.Thread(target=_jsonbin_save_worker, args=(snapshot,), daemon=True)
    t.start()

def _save_state():
    """將 NFES 持倉與歷史記錄寫入 JSON，供 portfolio_app 讀取"""
    _nfes_state["last_run"] = datetime.now(timezone.utc).isoformat()
    STATE_FILE.write_text(
        json.dumps(_nfes_state, ensure_ascii=False, indent=2)
    )
    _jsonbin_save()   # 非同步，立即返回

def _record_open(payload: dict):
    """記錄新開倉至 state"""
    sym    = payload["symbol"]
    side   = payload["side"]
    entry  = payload["entry"]
    signal = payload.get("signal", "")
    margin, lev = calc_trade_params(signal)   # 動態：總資金10% × 訊號槓桿
    notional    = margin * lev
    qty         = round(notional / entry, 6)

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
        "lev"      : lev,
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
        tp2_hit = pos.get("tp2_hit", False)
        tp1_hit = pos.get("tp1_hit", False)
        is_long = side == "long"

        def pnl_usd(exit_px):
            return (exit_px - entry) / entry * notional * (1 if is_long else -1)

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

def get_top_symbols(exch: ccxt.Exchange) -> list[str]:
    """取得 Top N USDT 永續合約（依24h成交量排序）"""
    exch.load_markets()
    tickers = exch.fetch_tickers(params={"type": "future"})
    cands = [
        (sym, float(t.get("quoteVolume") or 0))
        for sym, t in tickers.items()
        if sym.endswith("/USDT:USDT") and float(t.get("quoteVolume") or 0) > 0
    ]
    cands.sort(key=lambda x: x[1], reverse=True)
    return [c[0] for c in cands[:TOP_N]]


def fetch_data(exch: ccxt.Exchange, symbol: str):
    ohlcv_4h = exch.fetch_ohlcv(symbol, TIMEFRAME,   limit=LIMIT)
    ohlcv_d  = exch.fetch_ohlcv(symbol, D_TIMEFRAME, limit=D_LIMIT)
    return ohlcv_4h, ohlcv_d

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
            _nfes_state["positions"] = saved.get("positions", {})
            _nfes_state["trades"]    = saved.get("trades",    [])
        except Exception:
            pass

    exch = ccxt.binanceusdm({
        "apiKey"         : os.getenv("BINANCE_API_KEY",    ""),
        "secret"         : os.getenv("BINANCE_API_SECRET", ""),
        "options"        : {"defaultType": "future"},
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

            for sym in symbols:
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

                ts_str = datetime.fromtimestamp(bar_ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                log.info(
                    f"🔔 {result['signal']} {result['symbol']}  "
                    f"entry={result['entry']}  SL={result['sl']}  "
                    f"TP1={result['tp1']} TP2={result['tp2']} TP3={result['tp3']}  @{ts_str}"
                )
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
