#!/usr/bin/env python3
"""
SMA99 + NFES 每週策略診斷
─────────────────────────────────────────────────────────────
每週一自動分析上週 log + 交易記錄，TG 發送：
  - 哪個過濾條件攔了幾次、實際數值分布
  - 勝率 / 盈虧比
  - 具體建議參考數值（不自動修改，由人決定）
─────────────────────────────────────────────────────────────
"""
import json, re, asyncio, httpx
from datetime import datetime, timezone, timedelta
from pathlib import Path
from collections import defaultdict

AI_DIR    = Path(__file__).parent
TG_TOKEN  = "8005879844:AAG8DJoaphzsweVmdvMB6SNphJdRy0osQGo"
TG_CHAT   = "1768177615"

# ── Telegram ──────────────────────────────────────────────────────
async def tg(text: str):
    async with httpx.AsyncClient(timeout=30) as c:
        await c.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML"},
        )

# ── 時間範圍 ──────────────────────────────────────────────────────
def week_range():
    now = datetime.now(timezone.utc)
    mon = now - timedelta(days=now.weekday())
    last_mon = mon - timedelta(days=7)
    last_sun = mon - timedelta(days=1)
    return (
        last_mon.replace(hour=0, minute=0, second=0, microsecond=0),
        last_sun.replace(hour=23, minute=59, second=59),
        last_mon.strftime("%m/%d"),
        last_sun.strftime("%m/%d"),
    )

def in_week(line: str, w_start, w_end) -> bool:
    """判斷 log 行的時間戳是否在本週範圍內"""
    m = re.match(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", line)
    if not m:
        return False
    try:
        dt = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        # log 是本機時間（UTC+8），轉換
        dt = dt - timedelta(hours=8)
        return w_start <= dt <= w_end
    except:
        return False

# ── 解析 SMA99 log ─────────────────────────────────────────────────
def parse_sma99_log(w_start, w_end) -> dict:
    log_file = AI_DIR / "ema99_bot.log"
    if not log_file.exists():
        return {}

    adx_blocked   = []   # 被 ADX 擋掉的實際值
    bb_blocked    = []   # 被 BB 上緣擋掉的 bb_pos
    slope_blocked = []   # 被 SLOPE 擋掉的值
    entries       = 0
    filter_counts = defaultdict(int)

    for line in log_file.read_text(encoding="utf-8", errors="ignore").splitlines():
        if not in_week(line, w_start, w_end):
            continue

        # ADX_FILTER
        m = re.search(r"\[ADX_FILTER\].*ADX=([\d.]+)", line)
        if m:
            adx_blocked.append(float(m.group(1)))
            filter_counts["ADX"] += 1

        # BB_FILTER
        m = re.search(r"\[BB_FILTER\].*BB (\d+)%", line)
        if m:
            bb_blocked.append(int(m.group(1)))
            filter_counts["BB_上緣"] += 1

        # SLOPE_FILTER
        m = re.search(r"\[SLOPE_FILTER\].*斜率=([-\d.]+)", line)
        if m:
            slope_blocked.append(float(m.group(1)))
            filter_counts["SLOPE"] += 1

        # 進場
        if "ENTRY LONG" in line or "ENTRY SHORT" in line:
            entries += 1

    return {
        "filter_counts" : dict(filter_counts),
        "entries"       : entries,
        "adx_blocked"   : adx_blocked,
        "bb_blocked"    : bb_blocked,
        "slope_blocked" : slope_blocked,
    }

# ── 解析 NFES log ──────────────────────────────────────────────────
def parse_nfes_log(w_start, w_end) -> dict:
    log_file = AI_DIR / "nfes_signal_bot.log"
    if not log_file.exists():
        return {}

    rsi_blocked   = []
    vol_blocked   = 0
    entries       = 0
    filter_counts = defaultdict(int)

    for line in log_file.read_text(encoding="utf-8", errors="ignore").splitlines():
        if not in_week(line, w_start, w_end):
            continue

        # RSI_FILTER
        m = re.search(r"\[RSI_FILTER\].*RSI=([\d.]+)", line)
        if m:
            rsi_blocked.append(float(m.group(1)))
            filter_counts["RSI"] += 1

        # 量能不足
        if "vol=" in line and "avg" in line and "跳過" in line:
            vol_blocked += 1
            filter_counts["量能"] += 1

        # 進場
        if "ENTRY" in line and ("LONG" in line or "SHORT" in line):
            entries += 1

    return {
        "filter_counts" : dict(filter_counts),
        "entries"       : entries,
        "rsi_blocked"   : rsi_blocked,
        "vol_blocked"   : vol_blocked,
    }

# ── 讀取交易結果 ───────────────────────────────────────────────────
def load_trades(state_file: Path, w_start, w_end) -> list:
    if not state_file.exists():
        return []
    data = json.loads(state_file.read_text(encoding="utf-8"))
    trades = data.get("trades", [])
    result = []
    for t in trades:
        ts = t.get("exit_ts") or t.get("exited_at") or ""
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if w_start <= dt <= w_end:
                result.append(t)
        except:
            pass
    return result

def trade_stats(trades):
    if not trades:
        return None
    wins  = [t for t in trades if t.get("pnl", 0) > 0]
    loses = [t for t in trades if t.get("pnl", 0) <= 0]
    pnl   = sum(t.get("pnl", 0) for t in trades)
    avg_w = sum(t["pnl"] for t in wins)  / len(wins)  if wins  else 0
    avg_l = sum(abs(t["pnl"]) for t in loses) / len(loses) if loses else 0
    rr    = avg_w / avg_l if avg_l else 0
    return {
        "count"  : len(trades),
        "wins"   : len(wins),
        "wr"     : len(wins) / len(trades),
        "pnl"    : pnl,
        "avg_win": avg_w,
        "avg_loss": avg_l,
        "rr"     : rr,
    }

# ── 產生診斷建議 ───────────────────────────────────────────────────
def diagnose_sma99(log_data: dict, trades: list) -> list[str]:
    lines = []
    total_signals = log_data.get("entries", 0) + sum(log_data.get("filter_counts", {}).values())

    # ADX 分析
    adx_vals = log_data.get("adx_blocked", [])
    if adx_vals:
        avg_adx  = sum(adx_vals) / len(adx_vals)
        max_adx  = max(adx_vals)
        pct      = len(adx_vals) / max(total_signals, 1) * 100
        lines.append(
            f"📊 ADX 過濾：攔截 {len(adx_vals)} 次（占 {pct:.0f}%）\n"
            f"   被攔數值：平均 {avg_adx:.1f}，最高 {max_adx:.1f}（門檻現在 15）\n"
            f"   {'⚠️ 攔截率過高，市場整體 ADX 偏低，可考慮降至 12~13' if pct > 70 else '✅ 攔截率合理'}"
        )
    else:
        lines.append("✅ ADX 本週未觸發過濾")

    # BB 上緣分析
    bb_vals = log_data.get("bb_blocked", [])
    if bb_vals:
        under_100 = [v for v in bb_vals if v <= 100]  # 在 BB 範圍內的
        pct = len(bb_vals) / max(total_signals, 1) * 100
        note = ""
        if under_100:
            avg_under = sum(under_100) / len(under_100)
            note = f"\n   其中 {len(under_100)} 次在 BB 範圍內（平均 {avg_under:.0f}%），其餘已超出 BB 上緣（追高）"
        lines.append(
            f"📊 BB上緣 過濾：攔截 {len(bb_vals)} 次（占 {pct:.0f}%）{note}\n"
            f"   門檻現在 92%，{'⚠️ 可考慮放寬至 95%' if len(under_100) > 3 else '✅ 多為超出 BB，攔截合理'}"
        )
    else:
        lines.append("✅ BB上緣 本週未觸發過濾")

    # SLOPE 分析
    # 邏輯說明：bot 條件是 slope > threshold 才擋（e.g. threshold=-0.00005）
    # log 裡出現的都是「被擋掉」的 → slope > -0.00005（斜率不夠負）
    # 其中 slope < 0 = 均線確實向下，只是不夠陡，這些是「被過度過濾」的候選
    slope_vals = log_data.get("slope_blocked", [])
    if slope_vals:
        # 從 bot 檔讀取目前門檻值
        try:
            bot_code = (AI_DIR / "ema99_bot.py").read_text(encoding="utf-8")
            m_thresh = re.search(r"SHORT_EMA_SLOPE_THRESHOLD\s*=\s*([-\d.]+)", bot_code)
            cur_thresh = float(m_thresh.group(1)) if m_thresh else -0.00005
        except:
            cur_thresh = -0.00005

        # 被攔但斜率為負（均線確實向下，只是不夠陡）
        over_filtered = [v for v in slope_vals if v < 0]
        # 被攔且斜率為正或零（均線還在上升，攔得正確）
        correct_block = [v for v in slope_vals if v >= 0]
        pct = len(slope_vals) / max(total_signals, 1) * 100

        note = (
            f"\n   ✅ 正確攔截（斜率≥0，均線向上）：{len(correct_block)} 次"
            f"\n   ⚠️ 過度過濾（斜率<0，均線向下但不夠陡）：{len(over_filtered)} 次"
        )
        if over_filtered:
            avg_neg = sum(over_filtered) / len(over_filtered)
            note += f"，平均斜率 {avg_neg:.5f}"
            suggest = f"⚠️ 建議放寬至 {avg_neg*0.8:.5f}" if len(over_filtered) > 5 else "✅ 目前門檻合理"
        else:
            suggest = "✅ 攔截均為斜率向上，門檻正確"

        lines.append(
            f"📊 SLOPE 過濾（空單）：攔截 {len(slope_vals)} 次（占 {pct:.0f}%）{note}\n"
            f"   門檻現在 {cur_thresh}，{suggest}"
        )
    else:
        lines.append("✅ SLOPE 本週未觸發過濾")

    # 交易結果
    s = trade_stats(trades)
    if s:
        rr_note = "⚠️ 盈虧比偏低" if s["rr"] < 1.2 else ("✅ 盈虧比良好" if s["rr"] >= 1.5 else "")
        lines.append(
            f"📈 交易結果：{s['count']}筆 勝率{s['wr']:.0%} 損益{s['pnl']:+.1f}U\n"
            f"   平均獲利 {s['avg_win']:.1f}U / 平均虧損 {s['avg_loss']:.1f}U / RR={s['rr']:.2f}x {rr_note}"
        )
    elif log_data.get("entries", 0) == 0:
        lines.append("📈 本週零進場：市場條件未達，等待中")

    return lines

def diagnose_nfes(log_data: dict, trades: list) -> list[str]:
    lines = []
    total_signals = log_data.get("entries", 0) + sum(log_data.get("filter_counts", {}).values())

    # RSI 分析
    rsi_vals = log_data.get("rsi_blocked", [])
    if rsi_vals:
        avg_rsi = sum(rsi_vals) / len(rsi_vals)
        pct     = len(rsi_vals) / max(total_signals, 1) * 100
        lines.append(
            f"📊 RSI 過濾：攔截 {len(rsi_vals)} 次（占 {pct:.0f}%）\n"
            f"   被攔平均 RSI={avg_rsi:.1f}（空單門檻>50，多單門檻<65）\n"
            f"   {'⚠️ 攔截率過高，可考慮調整門檻' if pct > 60 else '✅ 攔截率合理'}"
        )
    else:
        lines.append("✅ RSI 本週未觸發過濾")

    # 量能
    vol = log_data.get("vol_blocked", 0)
    if vol > 0:
        pct = vol / max(total_signals, 1) * 100
        lines.append(
            f"📊 量能過濾：攔截 {vol} 次（占 {pct:.0f}%）\n"
            f"   {'⚠️ 量能要求可能過嚴（現在 1.2x 均量）' if pct > 50 else '✅ 合理'}"
        )

    # 交易結果
    s = trade_stats(trades)
    if s:
        intraday = [t for t in trades if t.get("reason") == "intraday_exit"]
        sl       = [t for t in trades if t.get("reason") == "stop_loss"]
        extra = ""
        if sl:
            extra = f"\n   止損 {len(sl)} 次（{sum(t['pnl'] for t in sl):+.1f}U）"
        if intraday:
            id_wr = sum(1 for t in intraday if t.get("pnl", 0) > 0) / len(intraday)
            extra += f"  日內到期 {len(intraday)} 次（勝率{id_wr:.0%}）"
        lines.append(
            f"📈 交易結果：{s['count']}筆 勝率{s['wr']:.0%} 損益{s['pnl']:+.1f}U\n"
            f"   RR={s['rr']:.2f}x{extra}"
        )
    elif log_data.get("entries", 0) == 0:
        lines.append("📈 本週零進場：Supertrend 未翻轉或信號被過濾")

    return lines

# ── 主流程 ─────────────────────────────────────────────────────────
async def main():
    w_start, w_end, ws, we = week_range()
    print(f"[診斷] 分析週期 {ws} ~ {we}")

    sma99_log   = parse_sma99_log(w_start, w_end)
    nfes_log    = parse_nfes_log(w_start, w_end)
    sma99_trades = load_trades(AI_DIR / "ema99_bot_state.json",  w_start, w_end)
    nfes_trades  = load_trades(AI_DIR / "nfes_bot_state.json",   w_start, w_end)

    sma99_diag = diagnose_sma99(sma99_log, sma99_trades)
    nfes_diag  = diagnose_nfes(nfes_log,  nfes_trades)

    msg = (
        f"🔬 <b>策略週診斷 {ws}~{we}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>【SMA99 Bot】</b>\n"
        + "\n".join(sma99_diag) +
        f"\n\n<b>【NFES Bot】</b>\n"
        + "\n".join(nfes_diag) +
        f"\n━━━━━━━━━━━━━━━━━━━━━\n"
        f"📌 以上為建議參考數值，需人工確認後再修改策略。"
    )

    await tg(msg)
    print(f"[診斷] TG 已發送")
    print(msg)

if __name__ == "__main__":
    asyncio.run(main())
