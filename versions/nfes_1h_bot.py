#!/usr/bin/env python3
"""
NFES 1H 日內策略 Bot — 獨立版（可在他人機器運行）
═══════════════════════════════════════════════════════
架構：
  層一 4H  → Adaptive Supertrend(4,14) 判斷多空方向
  層二 1H  → Adaptive Supertrend(3,14) 進場訊號 + 量能確認

進場邏輯：
  4H 多頭 + 1H 翻轉/回調 + 量能 1.2x → 做多
  4H 空頭 + 1H 翻轉/回調 + 量能 1.2x → 做空

風控：
  SL  = 1.2 × ATR(14, 1H)
  TP1 = 2.0R（出場 33%，SL 移到進場價保本）
  TP2 = 3.5R（出場剩餘 50%，SL 移到 TP1）
  TP3 = 5.5R（全出）
  日內強制出場：進場後 4 根 1H K棒（= 4 小時）未出場 → 市價平倉

RSI 過濾（1H）：
  空單：RSI 斜率向下 + RSI < 55，或 RSI 曾反彈 35~40 未突破後重新下彎
  多單：RSI 斜率向上 + RSI > 45，或 RSI 曾回踩 60~65 守住後重新上揚

啟動：
  pip install ccxt requests numpy
  python nfes_1h_bot.py
═══════════════════════════════════════════════════════
"""
from __future__ import annotations
import json
import logging
import os
import time
import threading
from pathlib import Path
from datetime import datetime, timezone, timedelta

import numpy as np
import ccxt
import requests

# ═══════════════════════════════════════════════════════════════
#  ★★★  使用者設定區（只需改這裡）  ★★★
# ═══════════════════════════════════════════════════════════════

BINANCE_API_KEY    = os.getenv("BINANCE_API_KEY",    "")   # Binance API Key
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "")   # Binance API Secret

TG_TOKEN  = os.getenv("TG_TOKEN",  "")      # Telegram Bot Token
TG_CHAT   = os.getenv("TG_CHAT",   "")      # Telegram Chat ID

PAPER_MODE = True       # True = 模擬單（不真實下單），False = 實單
INITIAL_CAPITAL = 1000  # 初始資金（USDT）

# 掃描幣種（手動填，避免掃全市場消耗 API）
# 建議填主流幣，流動性好、波動合理
SYMBOLS = [
    "BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT", "BNB/USDT:USDT",
    "XRP/USDT:USDT", "DOGE/USDT:USDT", "ADA/USDT:USDT", "AVAX/USDT:USDT",
    "LINK/USDT:USDT", "DOT/USDT:USDT", "LTC/USDT:USDT", "NEAR/USDT:USDT",
    "APT/USDT:USDT", "SUI/USDT:USDT",  "OP/USDT:USDT",  "ARB/USDT:USDT",
    "INJ/USDT:USDT", "ATOM/USDT:USDT", "UNI/USDT:USDT", "TIA/USDT:USDT",
]

# ═══════════════════════════════════════════════════════════════
#  策略參數（不建議修改）
# ═══════════════════════════════════════════════════════════════

# 時間框架
TF_DIR   = "4h"    # 層一：方向
TF_ENTRY = "1h"    # 層二：進場
LIMIT    = 200     # K棒數量

# 自適應 Supertrend 參數
D_SENS   = 4;  D_ATR = 14   # 4H 方向層
H1_SENS  = 3;  H1_ATR = 14  # 1H 進場層

# 量能
VOL_MA      = 20    # 量能均線
VOL_MULT    = 1.2   # 量能倍數門檻

# 風控 R 倍數
SL_MULT = 1.2
TP1_R   = 2.0
TP2_R   = 3.5
TP3_R   = 5.5

# 4根1H K棒強制出場（= 4 小時）
INTRADAY_CANDLES = 4
INTRADAY_HOURS   = INTRADAY_CANDLES  # 1H × 4 = 4h

# RSI 過濾參數（1H）
RSI_PERIOD          = 14
SHORT_RSI_SLOPE_MAX = 55   # 空單：RSI < 55
LONG_RSI_SLOPE_MIN  = 45   # 多單：RSI > 45
SHORT_BOUNCE_LO     = 35; SHORT_BOUNCE_HI = 40   # 空單：反彈失敗區間
LONG_BOUNCE_LO      = 60; LONG_BOUNCE_HI  = 65   # 多單：回踩支撐區間

# 風控：每筆虧損上限（佔可用資金比例）
RISK_PER_TRADE = 0.02   # 2%
MAX_LEVERAGE   = 5      # 最大槓桿
MAX_POSITIONS  = 3      # 同時最多持倉數

# 止損後冷卻
SL_COOLDOWN_H      = 12   # 止損後同幣冷卻 12h
INTRADAY_COOL_H    = 2    # 日內虧損平倉冷卻 2h

SCAN_INTERVAL    = 120   # 掃描間隔（秒），1H 策略不需要太頻繁
MONITOR_INTERVAL = 20    # 持倉監控間隔（秒）

# ═══════════════════════════════════════════════════════════════
#  日誌
# ═══════════════════════════════════════════════════════════════

LOG_FILE = Path("nfes_1h_bot.log")
_fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
_fh  = logging.FileHandler(LOG_FILE, encoding="utf-8")
_fh.setFormatter(_fmt)
log = logging.getLogger("nfes1h")
log.setLevel(logging.INFO)
log.addHandler(_fh)
log.addHandler(logging.StreamHandler())

# ═══════════════════════════════════════════════════════════════
#  Telegram
# ═══════════════════════════════════════════════════════════════

def tg(text: str):
    if not TG_TOKEN or not TG_CHAT:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML"},
            timeout=8
        )
    except Exception as e:
        log.warning(f"TG 發送失敗: {e}")

# ═══════════════════════════════════════════════════════════════
#  技術指標
# ═══════════════════════════════════════════════════════════════

def _rma(arr: np.ndarray, p: int) -> np.ndarray:
    out = np.full(len(arr), np.nan)
    alpha = 1.0 / p
    start = p - 1
    if start >= len(arr):
        return out
    out[start] = np.nanmean(arr[:start + 1])
    for i in range(start + 1, len(arr)):
        if not np.isnan(out[i-1]) and not np.isnan(arr[i]):
            out[i] = alpha * arr[i] + (1 - alpha) * out[i-1]
    return out

def _sma(arr: np.ndarray, p: int) -> np.ndarray:
    out = np.full(len(arr), np.nan)
    for i in range(p - 1, len(arr)):
        out[i] = arr[i - p + 1: i + 1].mean()
    return out

def _true_atr(h, l, c, p):
    n = len(c)
    tr = np.full(n, np.nan)
    for i in range(1, n):
        tr[i] = max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1]))
    tr[0] = h[0] - l[0]
    return _rma(tr, p)

def _calc_rsi(c, p=14):
    n = len(c)
    out = np.full(n, np.nan)
    delta = np.diff(c)
    gain  = np.where(delta > 0, delta, 0.0)
    loss  = np.where(delta < 0, -delta, 0.0)
    if n < p + 1:
        return out
    ag = _rma(np.concatenate([[np.nan], gain]), p)
    al = _rma(np.concatenate([[np.nan], loss]), p)
    for i in range(n):
        if not np.isnan(ag[i]) and not np.isnan(al[i]):
            out[i] = 100.0 if al[i] == 0 else 100.0 - 100.0 / (1 + ag[i] / al[i])
    return out

def _adaptive_supertrend(h, l, c, sens, atr_p):
    n = len(c)
    atr14 = _true_atr(h, l, c, atr_p)
    atr3  = _true_atr(h, l, c, 3)
    src   = (h + l) / 2.0
    upper = np.full(n, np.nan)
    lower = np.full(n, np.nan)
    direc = np.zeros(n, dtype=int)

    for i in range(n):
        if np.isnan(atr14[i]) or np.isnan(atr3[i]):
            direc[i] = direc[i-1] if i > 0 else 1
            continue
        ratio  = atr3[i] / max(atr14[i], 1e-9)
        factor = sens * max(0.5, min(2.0, ratio))
        ub = src[i] + factor * atr14[i]
        lb = src[i] - factor * atr14[i]
        if i == 0:
            upper[i] = ub; lower[i] = lb; direc[i] = 1; continue
        prev_ub = upper[i-1] if not np.isnan(upper[i-1]) else ub
        prev_lb = lower[i-1] if not np.isnan(lower[i-1]) else lb
        upper[i] = ub if (ub < prev_ub or c[i-1] > prev_ub) else prev_ub
        lower[i] = lb if (lb > prev_lb or c[i-1] < prev_lb) else prev_lb
        if   c[i] > upper[i-1]: direc[i] =  1
        elif c[i] < lower[i-1]: direc[i] = -1
        else:                   direc[i] = direc[i-1]

    return upper, lower, direc

# ═══════════════════════════════════════════════════════════════
#  訊號偵測
# ═══════════════════════════════════════════════════════════════

def detect_signal(ohlcv_4h: list, ohlcv_1h: list) -> dict | None:
    if len(ohlcv_4h) < 50 or len(ohlcv_1h) < 50:
        return None

    # ── 層一：4H 方向 ──────────────────────────────────────────
    h4h = np.array([x[2] for x in ohlcv_4h], dtype=float)
    h4l = np.array([x[3] for x in ohlcv_4h], dtype=float)
    h4c = np.array([x[4] for x in ohlcv_4h], dtype=float)
    _, _, d4 = _adaptive_supertrend(h4h, h4l, h4c, D_SENS, D_ATR)
    dir_4h = d4[-1]   # +1 多頭 / -1 空頭

    # ── 層二：1H 進場 ──────────────────────────────────────────
    h1h = np.array([x[2] for x in ohlcv_1h], dtype=float)
    h1l = np.array([x[3] for x in ohlcv_1h], dtype=float)
    h1c = np.array([x[4] for x in ohlcv_1h], dtype=float)
    h1v = np.array([x[5] for x in ohlcv_1h], dtype=float)

    atr1h_arr         = _true_atr(h1h, h1l, h1c, H1_ATR)
    h1_upper, h1_lower, h1_dir = _adaptive_supertrend(h1h, h1l, h1c, H1_SENS, H1_ATR)
    vol_ma_arr        = _sma(h1v, VOL_MA)
    rsi_arr           = _calc_rsi(h1c, RSI_PERIOD)

    i   = len(h1c) - 1
    i_1 = i - 1

    atr_now  = atr1h_arr[i]
    vol_now  = h1v[i]
    vol_avg  = vol_ma_arr[i]
    close    = h1c[i]

    if np.isnan(atr_now) or np.isnan(vol_avg) or vol_avg == 0:
        return None

    # 翻轉 / 持續
    flip_up = h1_dir[i] ==  1 and h1_dir[i_1] == -1
    flip_dn = h1_dir[i] == -1 and h1_dir[i_1] ==  1
    cont_up = h1_dir[i] ==  1
    cont_dn = h1_dir[i] == -1

    # 回調觸及趨勢帶
    pb_long = pb_short = False
    for j in range(1, 3):
        idx = i - j
        if idx < 0: break
        if not np.isnan(h1_lower[idx]) and h1l[idx] <= h1_lower[idx] * 1.003:
            pb_long = True
        if not np.isnan(h1_upper[idx]) and h1h[idx] >= h1_upper[idx] * 0.997:
            pb_short = True

    # 量能
    vol_ok = vol_now > vol_avg * VOL_MULT

    # 1H 進場條件
    entry_long  = (flip_up or (cont_up  and pb_long  and close > h1_lower[i]))
    entry_short = (flip_dn or (cont_dn  and pb_short and close < h1_upper[i]))

    # 4H 方向過濾
    if dir_4h ==  1: entry_short = False   # 4H 多頭：不做空
    if dir_4h == -1: entry_long  = False   # 4H 空頭：不做多

    # RSI 過濾
    rsi_now  = rsi_arr[i]   if not np.isnan(rsi_arr[i])   else 50.0
    rsi_p1   = rsi_arr[i-1] if i>=1 and not np.isnan(rsi_arr[i-1]) else rsi_now
    rsi_p2   = rsi_arr[i-2] if i>=2 and not np.isnan(rsi_arr[i-2]) else rsi_p1

    if entry_short:
        rsi_win = [rsi_arr[max(0,i-k)] for k in range(5) if not np.isnan(rsi_arr[max(0,i-k)])]
        slope_dn   = rsi_now < rsi_p1 < rsi_p2
        c1 = slope_dn and rsi_now < SHORT_RSI_SLOPE_MAX
        bounce_in  = any(SHORT_BOUNCE_LO <= r <= SHORT_BOUNCE_HI for r in rsi_win)
        bounce_fail= max(rsi_win) < SHORT_BOUNCE_HI + 2
        c2 = bounce_in and bounce_fail and (rsi_now < rsi_p1)
        if not (c1 or c2):
            log.info(f"[RSI] 空單跳過 RSI={rsi_now:.1f}")
            entry_short = False

    if entry_long:
        rsi_win = [rsi_arr[max(0,i-k)] for k in range(5) if not np.isnan(rsi_arr[max(0,i-k)])]
        slope_up   = rsi_now > rsi_p1 > rsi_p2
        c1 = slope_up and rsi_now > LONG_RSI_SLOPE_MIN
        pull_in    = any(LONG_BOUNCE_LO <= r <= LONG_BOUNCE_HI for r in rsi_win)
        pull_held  = min(rsi_win) > LONG_BOUNCE_LO - 2
        c2 = pull_in and pull_held and (rsi_now > rsi_p1)
        if not (c1 or c2):
            log.info(f"[RSI] 多單跳過 RSI={rsi_now:.1f}")
            entry_long = False

    if not (entry_long or entry_short) or not vol_ok:
        return None

    side   = "long" if entry_long else "short"
    signal = ("▲+" if flip_up else "▲") if side == "long" else ("▼+" if flip_dn else "▼")

    # 風控計算
    sl_dist = atr_now * SL_MULT

    def sr(v):
        if v == 0: return 0.0
        import math
        d = max(2, -int(math.floor(math.log10(abs(v)))) + 3)
        return round(v, d)

    if side == "long":
        sl  = sr(close - sl_dist)
        tp1 = sr(close + sl_dist * TP1_R)
        tp2 = sr(close + sl_dist * TP2_R)
        tp3 = sr(close + sl_dist * TP3_R)
    else:
        sl  = sr(close + sl_dist)
        tp1 = sr(close - sl_dist * TP1_R)
        tp2 = sr(close - sl_dist * TP2_R)
        tp3 = sr(close - sl_dist * TP3_R)

    if sl == 0 or tp1 == 0:
        return None

    return {
        "side": side, "signal": signal,
        "entry": close, "sl": sl,
        "tp1": tp1, "tp2": tp2, "tp3": tp3,
        "atr": atr_now,
        "_bar_ts": ohlcv_1h[i][0],
    }

# ═══════════════════════════════════════════════════════════════
#  狀態管理
# ═══════════════════════════════════════════════════════════════

STATE_FILE = Path("nfes_1h_state.json")

_state: dict = {
    "capital":           INITIAL_CAPITAL,
    "positions":         {},
    "trades":            [],
    "total_trades":      0,
    "total_wins":        0,
    "total_realized_pnl": 0.0,
    "cooldown":          {},
}

def _save():
    _state["last_run"] = datetime.now(timezone.utc).isoformat()
    STATE_FILE.write_text(json.dumps(_state, ensure_ascii=False, indent=2))

def _load():
    if STATE_FILE.exists():
        try:
            saved = json.loads(STATE_FILE.read_text())
            for k in ("capital","positions","trades","total_trades",
                      "total_wins","total_realized_pnl","cooldown"):
                if k in saved:
                    _state[k] = saved[k]
            log.info(f"✅ 載入狀態：capital={_state['capital']:.2f} pos={len(_state['positions'])}")
        except Exception as e:
            log.warning(f"載入狀態失敗: {e}")

def _open_position(sym: str, result: dict):
    cap = _state["capital"]
    risk_amt = cap * RISK_PER_TRADE
    sl_pct   = abs(result["entry"] - result["sl"]) / result["entry"]
    if sl_pct <= 0:
        return

    # 計算保證金與槓桿
    notional = risk_amt / sl_pct
    lev = min(MAX_LEVERAGE, max(1, int(notional / (cap * 0.2))))
    margin = round(notional / lev, 2)
    margin = min(margin, cap * 0.25)   # 單倉最多佔 25% 資金
    if margin < 5 or cap < margin:
        log.warning(f"資金不足或保證金過小，跳過 {sym}")
        return

    qty = round(margin * lev / result["entry"], 6)
    _state["capital"] -= margin

    _state["positions"][sym] = {
        "side":     result["side"],
        "signal":   result["signal"],
        "entry_px": result["entry"],
        "cur_px":   result["entry"],
        "sl":       result["sl"],
        "tp1":      result["tp1"],
        "tp2":      result["tp2"],
        "tp3":      result["tp3"],
        "margin":   margin,
        "notional": round(margin * lev, 2),
        "lev":      lev,
        "qty":      qty,
        "tp1_hit":  False,
        "tp2_hit":  False,
        "entry_ts": datetime.now(timezone.utc).isoformat(),
        "exit_after_ts": (
            datetime.now(timezone.utc) + timedelta(hours=INTRADAY_HOURS)
        ).isoformat(),
    }
    _save()
    log.info(f"✅ 開倉 {sym} {result['side']} entry={result['entry']} "
             f"margin={margin:.0f} lev={lev}x sl={result['sl']} tp1={result['tp1']}")

def _close_position(sym: str, reason: str, exit_px: float):
    pos = _state["positions"].pop(sym, None)
    if not pos:
        return
    entry    = pos["entry_px"]
    notional = pos["notional"]
    is_long  = pos["side"] == "long"
    pnl      = round((exit_px - entry) / entry * notional * (1 if is_long else -1), 2)
    margin   = pos["margin"]

    _state["capital"] += margin + pnl
    _state["total_realized_pnl"] = round(_state["total_realized_pnl"] + pnl, 2)
    _state["total_trades"] += 1
    if pnl > 0:
        _state["total_wins"] += 1

    _state["trades"].append({
        "sym":      sym,
        "side":     pos["side"],
        "entry_px": entry,
        "exit_px":  exit_px,
        "tp1": pos.get("tp1"), "tp2": pos.get("tp2"), "tp3": pos.get("tp3"),
        "sl":  pos.get("sl"),
        "pnl":     pnl,
        "reason":  reason,
        "entry_ts": pos.get("entry_ts", ""),
        "exit_ts":  datetime.now(timezone.utc).isoformat(),
    })
    _state["trades"] = _state["trades"][-500:]

    # 冷卻
    cool_h = SL_COOLDOWN_H if reason == "stop_loss" and pnl < 0 else (
             INTRADAY_COOL_H if reason == "intraday_exit" and pnl < 0 else 0)
    if cool_h:
        _state["cooldown"][sym] = {
            "until": (datetime.now(timezone.utc) + timedelta(hours=cool_h)).isoformat(),
            "reason": reason,
        }
    _save()
    return pnl

# ═══════════════════════════════════════════════════════════════
#  持倉監控
# ═══════════════════════════════════════════════════════════════

def _get_price(exch, sym_clean: str) -> float | None:
    try:
        ccxt_sym = sym_clean if "/" in sym_clean else sym_clean.replace("USDT", "/USDT:USDT")
        t = exch.fetch_ticker(ccxt_sym)
        return float(t["last"])
    except Exception as e:
        log.warning(f"fetch_ticker {sym_clean} 失敗: {e}")
        return None

def monitor(exch):
    for sym, pos in list(_state["positions"].items()):
        cur = _get_price(exch, sym)
        if cur is None:
            continue
        pos["cur_px"] = cur
        entry  = pos["entry_px"]
        notional = pos["notional"]
        is_long  = pos["side"] == "long"
        pnl_now  = round((cur - entry) / entry * notional * (1 if is_long else -1), 2)
        pos["unrealized_pnl"] = pnl_now

        sl  = pos["sl"]
        tp1 = pos["tp1"]; tp2 = pos["tp2"]; tp3 = pos["tp3"]
        tp1_hit = pos.get("tp1_hit", False)
        tp2_hit = pos.get("tp2_hit", False)

        icon = "🟢 LONG" if is_long else "🔴 SHORT"

        # ── 日內強制出場（4根1H K棒 = 4小時）─────────────────────
        try:
            exit_dt = datetime.fromisoformat(pos["exit_after_ts"])
            if datetime.now(timezone.utc) >= exit_dt:
                pnl = _close_position(sym, "intraday_exit", cur)
                msg = (f"⏰ <b>NFES 1H 日內出場</b>\n{icon} {sym}\n"
                       f"進場: {entry:.6g}  現價: {cur:.6g}\n"
                       f"損益: {'+' if pnl>=0 else ''}{pnl:.2f} USDT  "
                       f"| 資金: {_state['capital']:.2f} U")
                log.info(f"[INTRADAY] {sym} 4H到期 cur={cur} pnl={pnl:.2f}")
                tg(msg)
                continue
        except Exception:
            pass

        # ── 止損 ───────────────────────────────────────────────
        sl_hit = (cur <= sl) if is_long else (cur >= sl)
        if sl_hit:
            pnl = _close_position(sym, "stop_loss", sl)
            msg = (f"🛑 <b>NFES 1H 止損</b>\n{icon} {sym}\n"
                   f"進場: {entry:.6g}  止損: {sl:.6g}  現價: {cur:.6g}\n"
                   f"損益: {pnl:+.2f} USDT  | 資金: {_state['capital']:.2f} U")
            log.info(f"[SL] {sym} cur={cur} sl={sl} pnl={pnl:.2f}")
            tg(msg)
            continue

        # ── TP3 全出 ───────────────────────────────────────────
        tp3_hit = (cur >= tp3) if is_long else (cur <= tp3)
        if tp3_hit and tp1_hit:
            pnl = _close_position(sym, "tp3", tp3)
            msg = (f"🎯 <b>NFES 1H TP3 全出</b>\n{icon} {sym}\n"
                   f"進場: {entry:.6g}  TP3: {tp3:.6g}\n"
                   f"損益: +{pnl:.2f} USDT  | 資金: {_state['capital']:.2f} U")
            log.info(f"[TP3] {sym} pnl={pnl:.2f}")
            tg(msg)
            continue

        # ── TP2 部分出場 ───────────────────────────────────────
        tp2_hit_now = (cur >= tp2) if is_long else (cur <= tp2)
        if tp2_hit_now and tp1_hit and not tp2_hit:
            pos["tp2_hit"] = True
            pos["sl"]      = tp1   # SL 移到 TP1
            est_pnl = round((tp2 - entry) / entry * notional * (1 if is_long else -1) / 3, 2)
            msg = (f"🎯 <b>NFES 1H TP2 部分出場（約剩1/3）</b>\n{icon} {sym}\n"
                   f"TP2: {tp2:.6g}  SL 移至 TP1: {tp1:.6g}\n"
                   f"預估損益: +{est_pnl:.2f} USDT")
            log.info(f"[TP2] {sym} cur={cur}")
            tg(msg)
            _save()
            continue

        # ── TP1 部分出場 ───────────────────────────────────────
        tp1_hit_now = (cur >= tp1) if is_long else (cur <= tp1)
        if tp1_hit_now and not tp1_hit:
            pos["tp1_hit"] = True
            pos["sl"]      = entry   # SL 移到保本
            est_pnl = round((tp1 - entry) / entry * notional * (1 if is_long else -1) / 3, 2)
            msg = (f"🎯 <b>NFES 1H TP1 部分出場（1/3）</b>\n{icon} {sym}\n"
                   f"TP1: {tp1:.6g}  SL 移至保本: {entry:.6g}\n"
                   f"預估損益: +{est_pnl:.2f} USDT")
            log.info(f"[TP1] {sym} cur={cur}")
            tg(msg)
            _save()
            continue

    _save()

# ═══════════════════════════════════════════════════════════════
#  主迴圈
# ═══════════════════════════════════════════════════════════════

def main():
    _load()

    mode_str = "🟡 PAPER MODE（模擬）" if PAPER_MODE else "🔴 LIVE MODE（實單）"
    log.info("=" * 60)
    log.info(f"NFES 1H 日內 Bot 啟動  {mode_str}")
    log.info(f"資金: {_state['capital']:.2f} U  掃描幣種: {len(SYMBOLS)} 支")
    log.info(f"4H定方向 + 1H進場 + 4H日內強制出場")
    log.info("=" * 60)

    tg(f"🤖 <b>NFES 1H 日內 Bot 啟動</b>\n{mode_str}\n"
       f"資金: {_state['capital']:.2f} U\n"
       f"掃幣: {len(SYMBOLS)} 支 | 最多持倉: {MAX_POSITIONS}\n"
       f"SL={SL_MULT}×ATR  TP={TP1_R}R/{TP2_R}R/{TP3_R}R  4H強制出場")

    exch = ccxt.binanceusdm({
        "apiKey":          BINANCE_API_KEY,
        "secret":          BINANCE_API_SECRET,
        "options":         {"defaultType": "future"},
        "enableRateLimit": True,
    })

    # 持倉監控 Thread
    def _mon_loop():
        while True:
            try:
                monitor(exch)
            except Exception as e:
                log.error(f"monitor 例外: {e}")
            time.sleep(MONITOR_INTERVAL)

    threading.Thread(target=_mon_loop, daemon=True, name="monitor").start()
    log.info(f"✅ 持倉監控啟動（每 {MONITOR_INTERVAL}s）")

    # 訊號去重（同一根1H K棒只觸發一次）
    last_bar_ts: dict = {}
    bar_ts_file = Path("nfes_1h_bar_ts.json")
    if bar_ts_file.exists():
        try:
            last_bar_ts = json.loads(bar_ts_file.read_text())
        except Exception:
            pass

    while True:
        try:
            log.info(f"── 掃描開始 {datetime.now(timezone.utc).strftime('%H:%M UTC')} "
                     f"| 持倉: {len(_state['positions'])} | 資金: {_state['capital']:.2f} U ──")

            for sym in SYMBOLS:
                sym_clean = sym.replace("/USDT:USDT", "USDT")

                # 持倉上限
                if len(_state["positions"]) >= MAX_POSITIONS:
                    log.info(f"持倉已滿 {MAX_POSITIONS}，停止新開倉")
                    break

                # 已有同幣持倉
                if sym_clean in _state["positions"]:
                    continue

                # 冷卻中
                cool = _state["cooldown"].get(sym_clean)
                if cool:
                    try:
                        until = datetime.fromisoformat(cool["until"])
                        if datetime.now(timezone.utc) < until:
                            log.debug(f"[COOL] {sym_clean} 冷卻中，跳過")
                            continue
                    except Exception:
                        pass

                try:
                    ohlcv_4h = exch.fetch_ohlcv(sym, TF_DIR,   limit=LIMIT)
                    ohlcv_1h = exch.fetch_ohlcv(sym, TF_ENTRY, limit=LIMIT)
                except Exception as e:
                    log.warning(f"{sym} 拉K棒失敗: {e}")
                    continue

                # 使用已收盤K棒（排除最後一根未完成）
                result = detect_signal(ohlcv_4h[:-1], ohlcv_1h[:-1])
                if result is None:
                    continue

                bar_ts = result.pop("_bar_ts")
                if last_bar_ts.get(sym) == bar_ts:
                    continue   # 同一根K棒已處理

                last_bar_ts[sym] = bar_ts
                bar_ts_file.write_text(json.dumps(last_bar_ts))

                result["symbol"] = sym_clean
                entry  = result["entry"]
                sl     = result["sl"]
                side   = result["side"]
                signal = result["signal"]

                # SL 方向驗證
                if side == "long"  and sl >= entry: continue
                if side == "short" and sl <= entry: continue

                tp1 = result["tp1"]
                tp2 = result["tp2"]
                tp3 = result["tp3"]

                log.info(f"🔔 {signal} {sym_clean}  entry={entry}  SL={sl}  "
                         f"TP1={tp1} TP2={tp2} TP3={tp3}")

                tg(f"📋 {'🟡 PAPER' if PAPER_MODE else '🔴 LIVE'} {'NFES 1H'}\n\n"
                   f"{'🟢 LONG' if side=='long' else '🔴 SHORT'} {sym_clean}  {signal}\n\n"
                   f"Entry : {entry}\n"
                   f"TP1   : {tp1}  TP2: {tp2}  TP3: {tp3}\n"
                   f"SL    : {sl}\n"
                   f"日內出場：進場後 {INTRADAY_HOURS}H 強制平倉")

                if not PAPER_MODE:
                    # TODO: 接真實下單（ccxt create_order）
                    pass

                _open_position(sym_clean, result)

            log.info(f"── 掃描結束，等待 {SCAN_INTERVAL}s ──")

        except Exception as e:
            log.error(f"主迴圈例外: {e}", exc_info=True)
            tg(f"❌ NFES 1H Bot 例外: {e}")

        time.sleep(SCAN_INTERVAL)


if __name__ == "__main__":
    main()
