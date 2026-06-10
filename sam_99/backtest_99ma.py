#!/usr/bin/env python3
"""
99MA 二次突破回測計算器（多空版）
==================================
邏輯：
  多單：跌破 SMA99 → 回踩 → 二次突破向上 → TP=entry+AB / SL=SMA99×0.998
  空單：突破 SMA99 → 回測 → 二次跌破向下 → TP=entry-AB / SL=SMA99×1.002

  出場上限：64 根 15m（16H）

資料庫：
  - backtest_99ma_db.json  每次跑完自動累積，不覆蓋舊資料
  - 同一幣種同一日期不重複計算

用法：
  python3 backtest_99ma.py              # 掃市值前 30 名，90天
  python3 backtest_99ma.py BTC ETH SOL  # 指定幣種
  python3 backtest_99ma.py --top 50     # 掃前 50 名
  python3 backtest_99ma.py --report     # 只看資料庫累積報告，不跑新回測
"""

import sys, json, time, argparse, os
import requests
from datetime import datetime, timezone, timedelta
from pathlib import Path

TG_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "8779609140:AAHGfIR0hOL_I12NATRuiKlftuTuUvqzeYk")
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID",   "1768177615")

def tg(msg: str):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT_ID, "text": msg, "parse_mode": "HTML"},
            timeout=10)
    except Exception as e:
        print(f"  TG 發送失敗: {e}")

TZ8            = timezone(timedelta(hours=8))
SMA_PERIOD     = 99
MIN_VOL_RATIO  = 1.2
FIRST_BREAK_EXPIRY = 16    # 根
SL_LONG_MULT   = 0.998     # 多單止損：SMA99 × 0.998
SL_SHORT_MULT  = 1.002     # 空單止損：SMA99 × 1.002
MAX_HOLD_BARS  = 64        # 最多持有 64 根（16H）
DB_PATH        = Path(__file__).parent / "backtest_99ma_db.json"

# ──────────────────────────────────────────────────────────────────
def fetch_klines(symbol: str, interval: str = "15m", days: int = 90) -> list[dict]:
    end_ms   = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = end_ms - days * 86400 * 1000
    url      = "https://fapi.binance.com/fapi/v1/klines"
    bars     = []
    while start_ms < end_ms:
        params = {"symbol": symbol, "interval": interval,
                  "startTime": start_ms, "limit": 1500}
        try:
            r    = requests.get(url, params=params, timeout=15)
            data = r.json()
            if not isinstance(data, list) or not data:
                break
            for k in data:
                bars.append({
                    "ts"    : k[0],
                    "open"  : float(k[1]),
                    "high"  : float(k[2]),
                    "low"   : float(k[3]),
                    "close" : float(k[4]),
                    "volume": float(k[5]),
                })
            start_ms = data[-1][0] + 1
            if len(data) < 1500:
                break
            time.sleep(0.1)
        except Exception as e:
            print(f"  ⚠ fetch {symbol} {interval} 失敗: {e}")
            break
    return bars

def sma(closes: list[float], period: int):
    if len(closes) < period:
        return None
    return sum(closes[-period:]) / period

def consol_avg_vol(bars, end_idx, above: bool, lookback: int = 30) -> float:
    vols    = []
    sma_val = sma([b["close"] for b in bars[:end_idx]], SMA_PERIOD)
    if not sma_val:
        return 0
    for b in bars[max(0, end_idx-lookback):end_idx]:
        if above and b["close"] >= sma_val:
            vols.append(b["volume"])
        elif not above and b["close"] < sma_val:
            vols.append(b["volume"])
    return sum(vols) / len(vols) if vols else 0

# ──────────────────────────────────────────────────────────────────
def simulate_exits(bars, closes, entry_idx, entry, tp, sl_mult, side, sma_now):
    """從 entry_idx 之後模擬出場，回傳 (result, exit_price)"""
    for j in range(entry_idx + 1, min(entry_idx + MAX_HOLD_BARS + 1, len(bars))):
        fbar     = bars[j]
        fsma     = sma(closes[:j], SMA_PERIOD) or sma_now
        sl_price = fsma * sl_mult

        if side == "long":
            if fbar["low"] <= sl_price:
                return "loss", sl_price
            if fbar["high"] >= tp:
                return "win", tp
        else:  # short
            if fbar["high"] >= sl_price:
                return "loss", sl_price
            if fbar["low"] <= tp:
                return "win", tp
    return "timeout", None

# ──────────────────────────────────────────────────────────────────
def backtest_symbol(symbol: str, days: int = 90) -> dict:
    bars = fetch_klines(symbol + "USDT", "15m", days)
    if len(bars) < SMA_PERIOD + 20:
        return {"symbol": symbol, "signals": 0, "error": "資料不足"}

    closes  = [b["close"] for b in bars]
    signals = []

    # ── 多單狀態機 ────────────────────────────────────────────────
    stage_l = "watching"
    fb_idx_l = None
    a_pt_l   = None

    # ── 空單狀態機 ────────────────────────────────────────────────
    stage_s  = "watching"
    fb_idx_s = None
    a_pt_s   = None

    for i in range(SMA_PERIOD, len(bars)):
        bar     = bars[i]
        close   = bar["close"]
        hi      = bar["high"]
        lo      = bar["low"]
        vol     = bar["volume"]
        sma_now = sma(closes[:i], SMA_PERIOD)
        if sma_now is None:
            continue

        sma_prev = sma(closes[:i-1], SMA_PERIOD) or sma_now
        sma_up   = sma_now > sma_prev
        sma_down = sma_now < sma_prev

        sma_i2   = sma(closes[:i-2], SMA_PERIOD)
        if sma_i2 is None:
            continue
        prev2_close = bars[i-2]["close"]

        # ════════ 多單狀態機 ════════
        if stage_l == "watching":
            if close > sma_now and prev2_close <= sma_i2 and sma_up:
                stage_l  = "first_broke"
                fb_idx_l = i
                a_pt_l   = None

        elif stage_l == "first_broke":
            if i - fb_idx_l > FIRST_BREAK_EXPIRY:
                stage_l = "watching"
            elif close < sma_now:
                a_pt_l = lo
                stage_l = "pulled_back"

        elif stage_l == "pulled_back":
            if i - fb_idx_l > FIRST_BREAK_EXPIRY:
                stage_l = "watching"
                # fall through to short machine
            else:
                if close < sma_now:
                    a_pt_l = min(a_pt_l, lo) if a_pt_l else lo

                second_break = (close > sma_now and prev2_close <= sma_i2 and sma_up)
                avg_vol = consol_avg_vol(bars, i, above=False)
                vol_ok  = avg_vol > 0 and vol >= avg_vol * MIN_VOL_RATIO

                if second_break and vol_ok and a_pt_l:
                    entry = close
                    ab    = entry - a_pt_l
                    tp    = entry + ab
                    sl    = sma_now * SL_LONG_MULT
                    if sl < entry and ab > 0:
                        result, exit_px = simulate_exits(bars, closes, i, entry, tp, SL_LONG_MULT, "long", sma_now)
                        pnl_r = round((exit_px - entry) / (entry - sl), 2) if exit_px else 0
                        signals.append({
                            "side"  : "long",
                            "ts"    : datetime.fromtimestamp(bar["ts"]/1000, TZ8).strftime("%Y-%m-%d %H:%M"),
                            "entry" : round(entry, 6),
                            "tp"    : round(tp, 6),
                            "sl"    : round(sl, 6),
                            "ab_pct": round(ab / entry * 100, 2),
                            "result": result,
                            "pnl_r" : pnl_r,
                        })
                    stage_l = "watching"

        # ════════ 空單狀態機 ════════
        if stage_s == "watching":
            if close < sma_now and prev2_close >= sma_i2 and sma_down:
                stage_s  = "first_broke"
                fb_idx_s = i
                a_pt_s   = None

        elif stage_s == "first_broke":
            if i - fb_idx_s > FIRST_BREAK_EXPIRY:
                stage_s = "watching"
            elif close > sma_now:
                a_pt_s = hi
                stage_s = "pulled_back"

        elif stage_s == "pulled_back":
            if i - fb_idx_s > FIRST_BREAK_EXPIRY:
                stage_s = "watching"
            else:
                if close > sma_now:
                    a_pt_s = max(a_pt_s, hi) if a_pt_s else hi

                second_break_s = (close < sma_now and prev2_close >= sma_i2 and sma_down)
                avg_vol_s = consol_avg_vol(bars, i, above=True)
                vol_ok_s  = avg_vol_s > 0 and vol >= avg_vol_s * MIN_VOL_RATIO

                if second_break_s and vol_ok_s and a_pt_s:
                    entry = close
                    ab    = a_pt_s - entry
                    tp    = entry - ab
                    sl    = sma_now * SL_SHORT_MULT
                    if sl > entry and ab > 0:
                        result, exit_px = simulate_exits(bars, closes, i, entry, tp, SL_SHORT_MULT, "short", sma_now)
                        pnl_r = round((entry - exit_px) / (sl - entry), 2) if exit_px else 0
                        signals.append({
                            "side"  : "short",
                            "ts"    : datetime.fromtimestamp(bar["ts"]/1000, TZ8).strftime("%Y-%m-%d %H:%M"),
                            "entry" : round(entry, 6),
                            "tp"    : round(tp, 6),
                            "sl"    : round(sl, 6),
                            "ab_pct": round(ab / entry * 100, 2),
                            "result": result,
                            "pnl_r" : pnl_r,
                        })
                    stage_s = "watching"

    # ── 統計 ──────────────────────────────────────────────────────
    if not signals:
        return {"symbol": symbol, "signals": 0, "wins": 0, "losses": 0,
                "timeouts": 0, "win_rate": 0.0, "avg_r": 0.0, "details": []}

    long_sigs  = [s for s in signals if s["side"] == "long"]
    short_sigs = [s for s in signals if s["side"] == "short"]
    wins       = [s for s in signals if s["result"] == "win"]
    losses     = [s for s in signals if s["result"] == "loss"]
    timeouts   = [s for s in signals if s["result"] == "timeout"]
    total      = len(signals)
    win_rate   = len(wins) / total * 100 if total else 0
    avg_r      = (sum(s["pnl_r"] for s in wins + losses) /
                  len(wins + losses)) if (wins or losses) else 0

    long_w  = sum(1 for s in long_sigs  if s["result"] == "win")
    short_w = sum(1 for s in short_sigs if s["result"] == "win")

    return {
        "symbol"    : symbol,
        "signals"   : total,
        "wins"      : len(wins),
        "losses"    : len(losses),
        "timeouts"  : len(timeouts),
        "win_rate"  : round(win_rate, 1),
        "avg_r"     : round(avg_r, 2),
        "long_sigs" : len(long_sigs),
        "long_wins" : long_w,
        "short_sigs": len(short_sigs),
        "short_wins": short_w,
        "details"   : signals,
    }

# ──────────────────────────────────────────────────────────────────
# 資料庫：累積每次回測結果
# ──────────────────────────────────────────────────────────────────
def load_db() -> dict:
    if DB_PATH.exists():
        with open(DB_PATH) as f:
            return json.load(f)
    return {}   # { symbol: { "runs": [...], "signals": N, "wins": N, ... } }

def save_db(db: dict):
    with open(DB_PATH, "w") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)

def merge_to_db(db: dict, result: dict, run_date: str, days: int):
    """把新一次回測結果合併進資料庫（依 symbol + run_date 去重）"""
    sym = result["symbol"]
    if sym not in db:
        db[sym] = {"runs": [], "signals": 0, "wins": 0, "losses": 0,
                   "timeouts": 0, "long_sigs": 0, "long_wins": 0,
                   "short_sigs": 0, "short_wins": 0}

    entry = db[sym]
    # 去重：同一 run_date + days 不重複
    already = any(r["date"] == run_date and r["days"] == days
                  for r in entry["runs"])
    if already:
        return False

    entry["runs"].append({
        "date"      : run_date,
        "days"      : days,
        "signals"   : result["signals"],
        "wins"      : result["wins"],
        "losses"    : result["losses"],
        "timeouts"  : result["timeouts"],
        "win_rate"  : result["win_rate"],
        "avg_r"     : result["avg_r"],
        "long_sigs" : result.get("long_sigs", 0),
        "long_wins" : result.get("long_wins", 0),
        "short_sigs": result.get("short_sigs", 0),
        "short_wins": result.get("short_wins", 0),
    })
    # 滾動累計（全部 runs 加總）
    entry["signals"]    = sum(r["signals"]    for r in entry["runs"])
    entry["wins"]       = sum(r["wins"]       for r in entry["runs"])
    entry["losses"]     = sum(r["losses"]     for r in entry["runs"])
    entry["timeouts"]   = sum(r["timeouts"]   for r in entry["runs"])
    entry["long_sigs"]  = sum(r["long_sigs"]  for r in entry["runs"])
    entry["long_wins"]  = sum(r["long_wins"]  for r in entry["runs"])
    entry["short_sigs"] = sum(r["short_sigs"] for r in entry["runs"])
    entry["short_wins"] = sum(r["short_wins"] for r in entry["runs"])
    total = entry["wins"] + entry["losses"]
    entry["cum_win_rate"] = round(entry["wins"] / total * 100, 1) if total else 0
    entry["runs_count"]   = len(entry["runs"])
    return True

def print_report(db: dict, min_signals: int = 5):
    """印出累積資料庫報告"""
    rows = [(sym, d) for sym, d in db.items() if d.get("signals", 0) >= min_signals]
    rows.sort(key=lambda x: -x[1]["cum_win_rate"])

    print(f"\n{'='*75}")
    print(f"{'累積觀察名單':^75}")
    print(f"{'='*75}")
    print(f"{'Symbol':<10} {'累積訊號':>7} {'多勝/多總':>10} {'空勝/空總':>10} "
          f"{'累積勝率':>8} {'runs':>5}")
    print(f"{'-'*75}")

    good, mid, bad = [], [], []
    for sym, d in rows:
        l_rate = d["long_wins"]  / d["long_sigs"]  * 100 if d["long_sigs"]  else 0
        s_rate = d["short_wins"] / d["short_sigs"] * 100 if d["short_sigs"] else 0
        tag = ""
        if d["cum_win_rate"] >= 50:
            tag = "✅"
            good.append(sym)
        elif d["cum_win_rate"] < 35:
            tag = "⚠️"
            bad.append(sym)
        else:
            tag = "👀"
            mid.append(sym)
        print(f"{sym:<10} {d['signals']:>7}  "
              f"{d['long_wins']:>3}/{d['long_sigs']:<4} ({l_rate:>4.0f}%)  "
              f"{d['short_wins']:>3}/{d['short_sigs']:<4} ({s_rate:>4.0f}%)  "
              f"{d['cum_win_rate']:>6.1f}%  {d.get('runs_count',1):>4}  {tag}")

    all_sig = sum(d["signals"] for _, d in rows)
    all_win = sum(d["wins"] for _, d in rows)
    print(f"{'-'*75}")
    print(f"{'合計':<10} {all_sig:>7}                              "
          f"{all_win/all_sig*100:>6.1f}%" if all_sig else "")
    print(f"\n✅ 累積勝率≥50% : {', '.join(good)}")
    print(f"👀 觀察中 35~50%: {', '.join(mid)}")
    print(f"⚠️  累積勝率<35% : {', '.join(bad)}")
    print(f"\n資料庫路徑：{DB_PATH}")
    print(f"（每次跑回測自動累積，不覆蓋舊資料）\n")

# ──────────────────────────────────────────────────────────────────
def get_top_symbols(n: int = 30) -> list[str]:
    try:
        stable = {"USDT","USDC","BUSD","DAI","TUSD","USDP","FDUSD","FRAX","USDS",
                  "USDE","USYC","USD1","GUSD","LUSD","PYUSD","CRVUSD","USDD","CUSD"}
        coins  = []
        pages_needed = (n + 249) // 250  # 每頁最多 250
        for page in range(1, pages_needed + 1):
            per_page = min(250, n - len(coins))
            url = (f"https://api.coingecko.com/api/v3/coins/markets"
                   f"?vs_currency=usd&order=market_cap_desc"
                   f"&per_page={per_page}&page={page}&sparkline=false")
            resp = requests.get(url, timeout=15)
            data = resp.json()
            if not isinstance(data, list):
                break
            for c in data:
                sym = c["symbol"].upper()
                if sym not in stable:
                    coins.append(sym)
            if len(data) < per_page:
                break
            time.sleep(0.5)  # CoinGecko rate limit
        print(f"  CoinGecko 取得市值前 {n} 名 → 過濾穩定幣後 {len(coins)} 支")
        return coins[:n]
    except Exception as e:
        print(f"  ⚠ CoinGecko 取得失敗: {e}")
        return ["BTC","ETH","BNB","SOL","XRP","ADA","AVAX","DOGE","LINK","DOT"]

# ──────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="99MA 二次突破回測計算器（多空版）")
    parser.add_argument("symbols",      nargs="*", help="指定幣種（如 BTC ETH）")
    parser.add_argument("--top",        type=int, default=200)
    parser.add_argument("--days",       type=int, default=90)
    parser.add_argument("--min-signals",type=int, default=5)
    parser.add_argument("--report",     action="store_true", help="只看累積報告，不跑新回測")
    args = parser.parse_args()

    db = load_db()

    if args.report:
        print_report(db, args.min_signals)
        return

    symbols  = [s.upper() for s in args.symbols] if args.symbols else get_top_symbols(args.top)
    run_date = datetime.now(TZ8).strftime("%Y-%m-%d")

    print(f"\n{'='*65}")
    print(f"99MA 二次突破回測  |  多空版  |  {args.days}天  |  {len(symbols)}支幣")
    print(f"{'='*65}\n")

    results = []
    for sym in symbols:
        print(f"  {sym:<10}", end=" ", flush=True)
        r = backtest_symbol(sym, args.days)
        results.append(r)
        if r.get("error"):
            print(f"⚠ {r['error']}")
            continue
        new = merge_to_db(db, r, run_date, args.days)
        flag = "✚" if new else "="
        print(f"{flag} 訊號={r['signals']}(多{r.get('long_sigs',0)}/空{r.get('short_sigs',0)})  "
              f"勝率={r['win_rate']}%  avgR={r['avg_r']:+.2f}")
        time.sleep(0.3)

    save_db(db)

    # ── 本次結果摘要 ──────────────────────────────────────────────
    valid = [r for r in results if r.get("signals", 0) >= args.min_signals]
    valid.sort(key=lambda x: -x["win_rate"])

    print(f"\n{'='*70}")
    print(f"{'本次結果':^70}")
    print(f"{'='*70}")
    print(f"{'Symbol':<10} {'訊號':>5} {'多W/T':>8} {'空W/T':>8} {'勝率':>7} {'avgR':>6}")
    print(f"{'-'*70}")
    for r in valid:
        lw = r.get("long_wins",0);  lt = r.get("long_sigs",0)
        sw = r.get("short_wins",0); st = r.get("short_sigs",0)
        bar = "▓" * int(r["win_rate"] / 10)
        print(f"{r['symbol']:<10} {r['signals']:>5}  "
              f"{lw:>2}/{lt:<3}  {sw:>2}/{st:<3}  "
              f"{r['win_rate']:>6.1f}%  {r['avg_r']:>+.2f}R  {bar}")

    if valid:
        all_sig = sum(r["signals"] for r in valid)
        all_win = sum(r["wins"] for r in valid)
        print(f"{'-'*70}")
        print(f"{'合計':<10} {all_sig:>5}                   {all_win/all_sig*100:>6.1f}%")

    # ── 累積資料庫報告 ─────────────────────────────────────────────
    print_report(db, args.min_signals)

    # ── TG 週報 ────────────────────────────────────────────────────
    if not args.report:
        _send_tg_report(db, args.min_signals, run_date, len(symbols))

def _send_tg_report(db: dict, min_signals: int, run_date: str, total_coins: int):
    rows = [(sym, d) for sym, d in db.items() if d.get("signals", 0) >= min_signals]
    rows.sort(key=lambda x: -x[1]["cum_win_rate"])

    good = [(s, d) for s, d in rows if d["cum_win_rate"] >= 50]
    mid  = [(s, d) for s, d in rows if 35 <= d["cum_win_rate"] < 50]
    bad  = [(s, d) for s, d in rows if d["cum_win_rate"] < 35]

    lines = [f"📊 <b>99MA 回測週報 {run_date}</b>",
             f"掃描：市值前 {total_coins} 名 | 資料庫累積 {len(rows)} 幣\n"]

    if good:
        lines.append("✅ <b>高勝率（≥50%）建議優先</b>")
        for s, d in good[:10]:
            lw = d["long_wins"]; lt = d["long_sigs"]
            sw = d["short_wins"]; st = d["short_sigs"]
            lines.append(f"  {s:<8} {d['cum_win_rate']:.0f}%  多{lw}/{lt} 空{sw}/{st}  ({d.get('runs_count',1)}次)")

    if mid:
        lines.append("\n👀 <b>觀察中（35~50%）</b>")
        for s, d in mid[:8]:
            lines.append(f"  {s:<8} {d['cum_win_rate']:.0f}%  ({d.get('runs_count',1)}次)")

    if bad:
        lines.append("\n⚠️ <b>低勝率（&lt;35%）假突破多</b>")
        lines.append("  " + "  ".join(s for s, _ in bad[:12]))

    lines.append(f"\n🗃 DB路徑：backtest_99ma_db.json")
    tg("\n".join(lines))
    print("\n✅ TG 週報已發送")

if __name__ == "__main__":
    main()
