#!/usr/bin/env python3
"""
投資組合追蹤 App — 後端 API + 前端 PWA
─────────────────────────────────────────────
本機：python3 portfolio_app.py → http://localhost:5050
雲端：部署到 Render，從 JSONBin 讀取 bot state
"""
from __future__ import annotations
import json
import os
import time
from pathlib import Path
from datetime import datetime, timezone

import requests as _requests
from flask import Flask, jsonify, request, send_from_directory, Response

# ── 路徑 & 雲端設定 ─────────────────────────────────────────────
BASE_DIR        = Path(__file__).parent
EQUITY_DAILY_FILE    = BASE_DIR / "equity_daily.json"   # 每日 08:00 資金快照
INITIAL_CAPITAL      = 10_000.0                          # 觀察期起始資金
BOT_STATE_FILE       = BASE_DIR / "ema99_bot_state.json"
NFES_STATE_FILE      = BASE_DIR / "nfes_bot_state.json"
MANUAL_FILE          = BASE_DIR / "portfolio_manual.json"
SAM_TRADES_FILE      = BASE_DIR / "sam_99" / "brain" / "sams_brain_trades.json"
SAM_STATE_FILE       = BASE_DIR / "sam_99" / "brain" / "sams_brain_state.json"

# GitHub Gist — 讓 Render 雲端也能讀到 bot 狀態
GITHUB_TOKEN   = os.getenv("GITHUB_TOKEN2", "")
GIST_ID        = os.getenv("GIST_ID",      "627811c22b1d404fdcbb6115f81fd1e7")
GIST_FILENAME  = "portfolio_state.json"

app = Flask(__name__, static_folder=str(BASE_DIR))

# ─────────────────────────────────────────────────────────────────
# GitHub Gist 工具
# ─────────────────────────────────────────────────────────────────
_gist_cache: dict = {}
_gist_cache_ts: float = 0.0

GIST_RAW_URL = f"https://gist.githubusercontent.com/johny800222-a11y/{GIST_ID}/raw/{GIST_FILENAME}"

def _gist_get() -> dict | None:
    """從 GitHub Gist public raw URL 讀取 portfolio 狀態，cache 30 秒（不需要 token）"""
    global _gist_cache, _gist_cache_ts
    now = time.time()
    if _gist_cache and now - _gist_cache_ts < 30:
        return _gist_cache
    try:
        r = _requests.get(GIST_RAW_URL, timeout=8)
        if r.ok:
            data = json.loads(r.text)
            _gist_cache.update(data)
            _gist_cache_ts = now
            return data
    except Exception:
        pass
    return None

def _gist_put(data: dict) -> bool:
    """把 portfolio 狀態寫入 GitHub Gist"""
    try:
        r = _requests.patch(
            f"https://api.github.com/gists/{GIST_ID}",
            headers={"Authorization": f"token {GITHUB_TOKEN}",
                     "Accept": "application/vnd.github+json",
                     "Content-Type": "application/json"},
            json={"files": {GIST_FILENAME: {"content": json.dumps(data, ensure_ascii=False)}}},
            timeout=8,
        )
        if r.ok:
            _gist_cache.update(data)
            _gist_cache_ts = time.time()
        return r.ok
    except Exception:
        return False

# JSONBin 保留空殼（相容舊 env vars，不再使用）
JSONBIN_API_KEY = ""
JSONBIN_BIN_ID = ""
JSONBIN_NFES_BIN_ID = ""
JSONBIN_MANUAL_BIN_ID = ""
def _jsonbin_get(bin_id): return None
def _jsonbin_put(bin_id, data): return False

# ─────────────────────────────────────────────────────────────────
# 手動持倉（本機 JSON 或 JSONBin）
# ─────────────────────────────────────────────────────────────────
def load_manual() -> dict:
    # 優先用 JSONBin（雲端模式），否則讀本機檔案
    if JSONBIN_API_KEY and JSONBIN_MANUAL_BIN_ID:
        data = _jsonbin_get(JSONBIN_MANUAL_BIN_ID)
        if data is not None:
            return data
    if MANUAL_FILE.exists():
        return json.loads(MANUAL_FILE.read_text())
    return {"positions": []}

def save_manual(data: dict):
    # 同時存本機 + 雲端
    MANUAL_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    if JSONBIN_API_KEY and JSONBIN_MANUAL_BIN_ID:
        _jsonbin_put(JSONBIN_MANUAL_BIN_ID, data)

def fmt_sym(sym: str) -> str:
    """把 'BTC/USDT:USDT' 或 'BTCUSDT' 變成 'BTC'"""
    base = sym.split("/")[0]          # ccxt: 'BTC/USDT:USDT' → 'BTC'
    # 若已是 'BTCUSDT' 格式，去掉尾部 USDT
    if "/" not in sym and base.endswith("USDT"):
        base = base[:-4]
    return base

_live_price_cache: dict = {}
_live_price_ts: float = 0.0

def get_live_prices() -> dict[str, float]:
    """從幣安公開 API 取得即時期貨價格（cache 15 秒）"""
    global _live_price_cache, _live_price_ts
    if time.time() - _live_price_ts < 15:
        return _live_price_cache
    try:
        resp = _requests.get(
            "https://fapi.binance.com/fapi/v1/ticker/price", timeout=5
        ).json()
        _live_price_cache = {r["symbol"]: float(r["price"]) for r in resp}
        _live_price_ts = time.time()
    except Exception:
        pass
    return _live_price_cache

def to_binance_sym(ccxt_sym: str) -> str:
    """'1000PEPE/USDT:USDT' → '1000PEPEUSDT'；'PLAYUSDT' → 'PLAYUSDT'（已是正確格式）"""
    if "/" not in ccxt_sym:
        # NFES bot 已存為 'PLAYUSDT' 格式，直接回傳
        return ccxt_sym if ccxt_sym.endswith("USDT") else ccxt_sym + "USDT"
    base = ccxt_sym.split("/")[0]
    return base + "USDT"


# ─────────────────────────────────────────────────────────────────
# API 路由
# ─────────────────────────────────────────────────────────────────

def load_bot_state() -> dict:
    """優先讀本機 JSON（本機模式），否則從 Gist public raw URL 讀（Render 雲端模式）"""
    if BOT_STATE_FILE.exists():
        return json.loads(BOT_STATE_FILE.read_text())
    # 雲端 Render：從 Gist public raw URL 讀（不需要 token）
    data = _gist_get()
    if data:
        return data
    return {}

def load_nfes_state() -> dict:
    """讀取 NFES bot 狀態（本機 JSON）"""
    if NFES_STATE_FILE.exists():
        return json.loads(NFES_STATE_FILE.read_text())
    return {}

def _record_daily_equity(capital: float) -> None:
    """每天 08:00 記錄一次資金快照（台灣時間 = UTC+8）"""
    from datetime import timedelta
    now_tw = datetime.now(timezone.utc) + timedelta(hours=8)
    today  = now_tw.strftime("%Y-%m-%d")
    hour   = now_tw.hour

    # 讀取現有快照
    if EQUITY_DAILY_FILE.exists():
        snapshots: dict = json.loads(EQUITY_DAILY_FILE.read_text())
    else:
        snapshots = {"start": INITIAL_CAPITAL, "daily": {}}

    # 08:00~08:59 且今天還沒記錄 → 寫入
    if hour == 8 and today not in snapshots.get("daily", {}):
        snapshots.setdefault("daily", {})[today] = round(capital, 2)
        EQUITY_DAILY_FILE.write_text(json.dumps(snapshots, ensure_ascii=False, indent=2))

    # 確保有起始點（第一次執行時補上）
    if not snapshots.get("daily"):
        snapshots["daily"]["2026-05-19"] = INITIAL_CAPITAL  # 觀察期第一天
        EQUITY_DAILY_FILE.write_text(json.dumps(snapshots, ensure_ascii=False, indent=2))


def _get_equity_curve(capital: float) -> list:
    """回傳 [{date, capital}] 的資產曲線，最後一筆補上當前資金"""
    if EQUITY_DAILY_FILE.exists():
        snapshots = json.loads(EQUITY_DAILY_FILE.read_text())
    else:
        snapshots = {"start": INITIAL_CAPITAL, "daily": {}}

    daily = snapshots.get("daily", {})
    # 排序日期
    points = sorted(daily.items())
    # 確保有起始點
    if not points or points[0][0] > "2026-05-19":
        points.insert(0, ("2026-05-19", INITIAL_CAPITAL))
    # 加上今天的即時資金
    from datetime import timedelta
    today = (datetime.now(timezone.utc) + timedelta(hours=8)).strftime("%Y-%m-%d")
    if not points or points[-1][0] < today:
        points.append((today, round(capital, 2)))
    else:
        # 今天已有快照，更新成即時資金
        points[-1] = (today, round(capital, 2))

    return [{"date": d, "capital": c} for d, c in points]


def _build_positions(positions: dict, strategy: str, live_prices: dict) -> list:
    """將 bot state positions dict 轉換為前端用的 list（含策略標籤）"""
    result = []
    for sym, p in positions.items():
        entry    = p.get("entry_px", 0)
        sl       = p.get("trail_sl") or p.get("sl", 0)
        margin   = p.get("margin", 0)
        lev      = p.get("lev", 1)
        notional = p.get("notional", margin)
        partial  = p.get("partial", False)
        entry_ts = p.get("entry_ts", "")
        side     = p.get("side", "long")

        bsym      = to_binance_sym(sym)
        live_cur  = live_prices.get(bsym)
        cur       = live_cur if live_cur else p.get("cur_px", entry)
        using_live = live_cur is not None

        if entry:
            pnl_pct = (cur - entry) / entry * 100 * (1 if side == "long" else -1)
            pnl_usd = (cur - entry) * p.get("qty", 0) * (1 if side == "long" else -1)
        else:
            pnl_pct = pnl_usd = 0

        sl_triggered = (cur < sl) if side == "long" else (cur > sl)

        result.append({
            "sym"         : fmt_sym(sym),
            "full_sym"    : sym,
            "strategy"    : strategy,
            "side"        : side,
            "entry_px"    : round(entry, 8),
            "cur_px"      : round(cur, 8),
            "sl"          : round(sl, 8),
            "tp1"         : round(p.get("tp1", 0), 8),
            "tp2"         : round(p.get("tp2", 0), 8),
            "tp3"         : round(p.get("tp3", 0), 8),
            "margin"      : round(margin, 2),
            "notional"    : round(notional, 2),
            "lev"         : lev,
            "pnl_pct"     : round(pnl_pct, 2),
            "pnl_usd"     : round(pnl_usd, 2),
            "partial"     : partial,
            "entry_ts"    : entry_ts,
            "using_live"  : using_live,
            "sl_triggered": sl_triggered,
        })
    return result


@app.route("/api/crypto")
def api_crypto():
    """讀取雙策略 bot state，回傳合併加密幣持倉（共用資金）"""
    # ── 雲端模式（Render）：本機無 state 檔 ───────────────────────────
    # Gist 存的是 _build_crypto_data() 的完整輸出（positions 已是 list），
    # 直接回傳即可，不可再丟進 _build_positions()（那會對 list 呼叫 .items() → 500）
    if not BOT_STATE_FILE.exists():
        data = _gist_get()
        if data and isinstance(data.get("positions"), list):
            data.setdefault("source", "gist")
            return jsonify(data)
        return jsonify({
            "positions": [], "capital": 0, "last_run": "",
            "recent_trades": [], "equity_curve": [], "total_pnl": 0,
            "source": "gist-empty",
        })

    # ── 本機模式：從 raw bot state（positions 為 dict）逐筆處理 ────────
    ema_state  = load_bot_state()
    nfes_state = load_nfes_state()
    live_prices = get_live_prices()

    # ── EMA99 持倉 ────────────────────────────────────────────
    ema_positions = _build_positions(
        ema_state.get("positions", {}),
        strategy  = "EMA99",
        live_prices = live_prices,
    )
    ema_trades  = ema_state.get("trades", [])
    ema_capital = ema_state.get("capital", 0)
    ema_last    = ema_state.get("last_run", "")

    # ── NFES 持倉 ─────────────────────────────────────────────
    nfes_positions = _build_positions(
        nfes_state.get("positions", {}),
        strategy  = nfes_state.get("strategy", "NFES 強化版"),
        live_prices = live_prices,
    )
    nfes_trades = nfes_state.get("trades", [])
    nfes_last   = nfes_state.get("last_run", "")

    # ── 合併（共用同一個 Binance 帳號資金）────────────────────
    # 真實總資產 = 可用資金 + NFES/EMA99 所有持倉保證金（已從資金池扣除的部分）
    nfes_locked  = sum(p.get("margin", 0) for p in nfes_state.get("positions", {}).values())
    ema_locked   = sum(p.get("margin", 0) for p in ema_state.get("positions",  {}).values())
    total_capital = ema_capital + nfes_locked + ema_locked

    all_positions = ema_positions + nfes_positions

    # 合併 trades，標記策略名稱，最新 20 筆
    all_trades = []
    for t in ema_trades:
        tc = dict(t)
        tc["sym"]      = fmt_sym(tc.get("sym", ""))
        tc["strategy"] = tc.get("strategy", "EMA99")
        all_trades.append(tc)
    for t in nfes_trades:
        tc = dict(t)
        tc["sym"]      = fmt_sym(tc.get("sym", ""))
        tc["strategy"] = tc.get("strategy", "NFES 強化版")
        all_trades.append(tc)
    recent = sorted(all_trades, key=lambda x: x.get("exit_ts", ""))

    # last_run = 兩者取較新的
    last_run = max(ema_last, nfes_last) if ema_last and nfes_last else (ema_last or nfes_last)

    source = "jsonbin" if (JSONBIN_API_KEY and JSONBIN_BIN_ID) else "local"
    # 每日 08:00 記錄快照（用總資產）
    _record_daily_equity(total_capital)
    # 資產曲線（每日快照）
    equity_curve = _get_equity_curve(total_capital)
    # 總損益 = 總資產 - 起始資金
    total_pnl = round(total_capital - INITIAL_CAPITAL, 2)
    return jsonify({
        "positions"    : all_positions,
        "capital"      : round(total_capital, 2),
        "last_run"     : last_run,
        "recent_trades": recent,
        "equity_curve" : equity_curve,   # [{date, capital}] 每日資金快照
        "total_pnl"    : total_pnl,      # 真實總損益
        "source"       : source,
    })


@app.route("/api/debug")
def api_debug():
    """排錯用：顯示執行環境狀態"""
    import os
    gist_test = None
    gist_err = None
    try:
        r = _requests.get(GIST_RAW_URL, timeout=8)
        gist_test = {"status": r.status_code, "ok": r.ok, "len": len(r.text), "preview": r.text[:100]}
    except Exception as e:
        gist_err = str(e)

    return jsonify({
        "GIST_RAW_URL"     : GIST_RAW_URL,
        "GIST_ID"          : GIST_ID,
        "GITHUB_TOKEN_set" : bool(GITHUB_TOKEN),
        "BOT_STATE_exists" : BOT_STATE_FILE.exists(),
        "BOT_STATE_path"   : str(BOT_STATE_FILE),
        "gist_fetch"       : gist_test,
        "gist_err"         : gist_err,
        "load_bot_state"   : load_bot_state().get("capital", "KEY_MISSING"),
    })


@app.route("/api/manual", methods=["GET"])
def get_manual():
    return jsonify(load_manual())


@app.route("/api/manual", methods=["POST"])
def add_manual():
    """新增手動持倉（台股/美股/ETF）"""
    body = request.json
    required = ["sym", "market", "avg_cost", "qty", "sl"]
    for f in required:
        if f not in body:
            return jsonify({"error": f"missing field: {f}"}), 400

    data = load_manual()
    # 若已存在同 sym + market 則更新
    existing = next((p for p in data["positions"]
                     if p["sym"] == body["sym"] and p["market"] == body["market"]), None)
    entry = {
        "id"       : existing["id"] if existing else int(time.time() * 1000),
        "sym"      : body["sym"].upper(),
        "market"   : body["market"],          # TW / US / ETF / CRYPTO_MANUAL
        "avg_cost" : float(body["avg_cost"]),
        "qty"      : float(body["qty"]),
        "sl"       : float(body["sl"]),
        "cur_px"   : float(body.get("cur_px", body["avg_cost"])),
        "note"     : body.get("note", ""),
        "entry_ts" : body.get("entry_ts", datetime.now(timezone.utc).isoformat()),
    }
    if existing:
        idx = data["positions"].index(existing)
        data["positions"][idx] = entry
    else:
        data["positions"].append(entry)

    save_manual(data)
    return jsonify({"ok": True, "entry": entry})


@app.route("/api/manual/<int:pos_id>", methods=["PATCH"])
def update_manual(pos_id):
    """更新現價"""
    body = request.json
    data = load_manual()
    for p in data["positions"]:
        if p["id"] == pos_id:
            if "cur_px" in body: p["cur_px"] = float(body["cur_px"])
            if "sl"     in body: p["sl"]     = float(body["sl"])
            if "note"   in body: p["note"]   = body["note"]
            save_manual(data)
            return jsonify({"ok": True})
    return jsonify({"error": "not found"}), 404


@app.route("/api/manual/<int:pos_id>", methods=["DELETE"])
def delete_manual(pos_id):
    data = load_manual()
    before = len(data["positions"])
    data["positions"] = [p for p in data["positions"] if p["id"] != pos_id]
    save_manual(data)
    return jsonify({"ok": len(data["positions"]) < before})


# ─────────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────
# sam 思考視覺化頁面
# ─────────────────────────────────────────────────────────────────
SAM_THINKING_HTML = r"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>🧠 sam 的思考</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: #0d1117; color: #e6edf3; font-family: -apple-system, sans-serif; padding: 16px; }
  h1 { font-size: 20px; margin-bottom: 4px; }
  .sub { color: #8b949e; font-size: 13px; margin-bottom: 20px; }
  .card { background: #161b22; border: 1px solid #30363d; border-radius: 12px; padding: 16px; margin-bottom: 14px; }
  .card.approved { border-left: 4px solid #3fb950; }
  .card.rejected { border-left: 4px solid #8b949e; }
  .card.long  { border-left: 4px solid #3fb950; }
  .card.short { border-left: 4px solid #f85149; }
  .sym { font-size: 18px; font-weight: 700; display: flex; align-items: center; gap: 8px; }
  .badge { font-size: 11px; padding: 2px 8px; border-radius: 10px; font-weight: 600; }
  .badge.long  { background: #1a3a26; color: #3fb950; }
  .badge.short { background: #3a1a1a; color: #f85149; }
  .badge.skip  { background: #2a2a2a; color: #8b949e; }
  .ts { color: #8b949e; font-size: 12px; margin-top: 2px; margin-bottom: 12px; }
  .section { margin-bottom: 10px; }
  .section-title { font-size: 11px; font-weight: 600; color: #8b949e; text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 6px; }
  .market-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 6px; }
  .market-item { background: #0d1117; border-radius: 6px; padding: 6px 8px; }
  .market-item .label { font-size: 10px; color: #8b949e; }
  .market-item .value { font-size: 13px; font-weight: 600; }
  .trend-bull { color: #3fb950; }
  .trend-bear { color: #f85149; }
  .trend-neutral { color: #d29922; }
  .thinking-text { font-size: 13px; line-height: 1.7; color: #c9d1d9; background: #0d1117; border-radius: 8px; padding: 10px 12px; white-space: pre-wrap; }
  .decision-row { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-top: 8px; }
  .score-bar { flex: 1; min-width: 120px; }
  .score-label { font-size: 11px; color: #8b949e; margin-bottom: 3px; }
  .bar-bg { background: #21262d; border-radius: 4px; height: 8px; overflow: hidden; }
  .bar-fill { height: 100%; border-radius: 4px; transition: width 0.6s ease; }
  .lev-badge { background: #1c2a3a; color: #58a6ff; border-radius: 6px; padding: 3px 10px; font-size: 13px; font-weight: 700; }
  .reason { font-size: 13px; color: #c9d1d9; font-style: italic; margin-top: 8px; }
  .stats-bar { display: flex; gap: 12px; background: #161b22; border: 1px solid #30363d; border-radius: 10px; padding: 12px 16px; margin-bottom: 16px; flex-wrap: wrap; }
  .stat { text-align: center; }
  .stat .n { font-size: 20px; font-weight: 700; }
  .stat .l { font-size: 11px; color: #8b949e; }
  .refresh-btn { position: fixed; bottom: 20px; right: 20px; background: #238636; color: #fff; border: none; border-radius: 50%; width: 48px; height: 48px; font-size: 22px; cursor: pointer; box-shadow: 0 4px 12px rgba(0,0,0,0.4); }
  .empty { text-align: center; color: #8b949e; padding: 40px; }
  .flow { display: flex; align-items: center; gap: 6px; margin-bottom: 10px; font-size: 12px; color: #8b949e; flex-wrap: wrap; }
  .flow-step { background: #21262d; border-radius: 6px; padding: 3px 8px; }
  .flow-arrow { color: #30363d; }
</style>
</head>
<body>
<h1>🧠 sam 的思考過程</h1>
<p class="sub">每一筆都是我自己的判斷，沒有固定公式</p>

<div class="stats-bar" id="stats-bar">
  <div class="stat"><div class="n" id="stat-total">-</div><div class="l">掃描</div></div>
  <div class="stat"><div class="n" id="stat-approved" style="color:#3fb950">-</div><div class="l">進場</div></div>
  <div class="stat"><div class="n" id="stat-equity" style="color:#58a6ff">-</div><div class="l">資金(U)</div></div>
  <div class="stat"><div class="n" id="stat-trades">-</div><div class="l">完成交易</div></div>
  <div class="stat"><div class="n" id="stat-positions">-</div><div class="l">持倉中</div></div>
</div>

<div class="flow">
  <span class="flow-step">📡 市場掃描</span>
  <span class="flow-arrow">→</span>
  <span class="flow-step">🔍 結構分析</span>
  <span class="flow-arrow">→</span>
  <span class="flow-step">💭 自由判斷</span>
  <span class="flow-arrow">→</span>
  <span class="flow-step">⚡ 決策輸出</span>
</div>

<div id="list"><div class="empty">載入中...</div></div>
<button class="refresh-btn" onclick="load()">↻</button>

<script>
async function load() {
  const r = await fetch('/api/sam/thinking');
  const d = await r.json();
  const logs = d.thinking || [];
  const state = d.state || {};

  document.getElementById('stat-total').textContent = logs.length;
  document.getElementById('stat-approved').textContent = logs.filter(l=>l.approved).length;
  document.getElementById('stat-equity').textContent = (state.equity||1000).toFixed(0);
  document.getElementById('stat-trades').textContent = state.total_trades || 0;
  document.getElementById('stat-positions').textContent = Object.keys(state.positions||{}).length;

  const el = document.getElementById('list');
  if (!logs.length) { el.innerHTML = '<div class="empty">還沒有思考記錄，sam 正在掃描中...</div>'; return; }

  el.innerHTML = logs.map(log => {
    const approved = log.approved;
    const side = log.side || '';
    const score = log.score || 0;
    const lev = log.leverage || 1;
    const m = log.market || {};
    const trend = m.trend || 'neutral';
    const trendLabel = trend==='bull' ? '<span class="trend-bull">📈 多頭</span>' : trend==='bear' ? '<span class="trend-bear">📉 空頭</span>' : '<span class="trend-neutral">➡️ 中性</span>';
    const ts = (log.timestamp||'').substring(0,16).replace('T',' ');
    const barColor = score>=70?'#3fb950':score>=55?'#d29922':'#8b949e';
    const sideClass = !approved?'skip':side==='long'?'long':'short';
    const sideLabel = !approved?'跳過':side==='long'?'做多':'做空';
    const levStars = '⚡'.repeat(lev);

    return `<div class="card ${sideClass}">
      <div class="sym">
        ${log.symbol||'?'}
        <span class="badge ${sideClass}">${sideLabel}</span>
        ${approved ? '<span class="lev-badge">'+levStars+' '+lev+'x</span>' : ''}
      </div>
      <div class="ts">${ts}</div>

      <div class="section">
        <div class="section-title">📊 我看到的市場</div>
        <div class="market-grid">
          <div class="market-item"><div class="label">趨勢</div><div class="value">${trendLabel}</div></div>
          <div class="market-item"><div class="label">ATR%</div><div class="value">${((m.atr_pct||0)*100).toFixed(2)}%</div></div>
          <div class="market-item"><div class="label">量比</div><div class="value">${(m.vol_ratio||0).toFixed(1)}x</div></div>
          <div class="market-item"><div class="label">EMA99</div><div class="value">${(m.ema99||0).toFixed(4)}</div></div>
          <div class="market-item"><div class="label">EMA200</div><div class="value">${(m.ema200||0).toFixed(4)}</div></div>
          <div class="market-item"><div class="label">現價</div><div class="value">${(m.price||0).toFixed(4)}</div></div>
        </div>
      </div>

      <div class="section">
        <div class="section-title">💭 我的判斷</div>
        <div class="thinking-text">${(log.brain_view||'（無）').substring(0,400)}${(log.brain_view||'').length>400?'...':''}</div>
      </div>

      <div class="section">
        <div class="section-title">⚡ 我的決策</div>
        <div class="score-bar">
          <div class="score-label">信心 ${score}/100</div>
          <div class="bar-bg"><div class="bar-fill" style="width:${score}%;background:${barColor}"></div></div>
        </div>
        <div class="reason">${log.reason||''}</div>
      </div>
    </div>`;
  }).join('');
}
load();
setInterval(load, 60000);
</script>
</body>
</html>"""

# 前端 HTML（PWA）
# ─────────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"/>
<meta name="apple-mobile-web-app-capable" content="yes"/>
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent"/>
<meta name="theme-color" content="#0d0d0d"/>
<title>投資組合</title>
<style>
  :root {
    --bg: #0d0d0d;
    --card: #1c1c1e;
    --card2: #2c2c2e;
    --text: #f2f2f7;
    --sub: #8e8e93;
    --green: #30d158;
    --red: #ff453a;
    --orange: #ff9f0a;
    --blue: #0a84ff;
    --purple: #bf5af2;
    --sep: #38383a;
    --radius: 16px;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; -webkit-tap-highlight-color: transparent; }
  body { background: var(--bg); color: var(--text); font-family: -apple-system, 'SF Pro Display', sans-serif;
         padding-bottom: env(safe-area-inset-bottom, 20px); }

  /* ── Header ── */
  .header { padding: 56px 20px 0; display: flex; justify-content: space-between; align-items: flex-end; }
  .header h1 { font-size: 34px; font-weight: 700; }
  .header .refresh-btn { background: var(--card2); border: none; color: var(--blue); padding: 8px 16px;
                         border-radius: 20px; font-size: 14px; cursor: pointer; }

  /* ── Summary Bar ── */
  .summary { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 12px; padding: 16px 20px; }
  .sum-card { background: var(--card); border-radius: var(--radius); padding: 14px; }
  .sum-card .label { font-size: 11px; color: var(--sub); text-transform: uppercase; letter-spacing: .5px; margin-bottom: 4px; }
  .sum-card .value { font-size: 18px; font-weight: 600; }

  /* ── Tabs ── */
  .tabs { display: flex; gap: 8px; padding: 0 20px 12px; overflow-x: auto; }
  .tabs::-webkit-scrollbar { display: none; }
  .tab { flex-shrink: 0; padding: 7px 16px; border-radius: 20px; font-size: 14px; font-weight: 500;
         background: var(--card2); color: var(--sub); border: none; cursor: pointer; transition: all .2s; }
  .tab.active { background: var(--blue); color: #fff; }

  /* ── Strategy Badge ── */
  .strategy-tag { font-size: 11px; font-weight: 700; padding: 2px 8px; border-radius: 6px;
                  margin-right: 6px; letter-spacing: .3px; }
  .strat-ema99  { background: rgba(10,132,255,.2); color: var(--blue); }
  .strat-nfes   { background: rgba(191,90,242,.25); color: var(--purple); }

  /* ── Section ── */
  .section { padding: 0 20px; margin-bottom: 28px; }
  .section-title { font-size: 22px; font-weight: 700; margin-bottom: 12px; display: flex;
                   align-items: center; justify-content: space-between; }
  .section-title .add-btn { font-size: 13px; color: var(--blue); background: none; border: none;
                             cursor: pointer; font-weight: 500; }

  /* ── Position Card ── */
  .pos-card { background: var(--card); border-radius: var(--radius); padding: 16px; margin-bottom: 12px; }
  .pos-top { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 12px; }
  .pos-sym { font-size: 22px; font-weight: 700; }
  .pos-badge { font-size: 12px; font-weight: 600; padding: 4px 10px; border-radius: 20px; }
  .badge-crypto { background: rgba(10,132,255,.2); color: var(--blue); }
  .badge-TW  { background: rgba(255,159,10,.2); color: var(--orange); }
  .badge-US  { background: rgba(48,209,88,.2); color: var(--green); }
  .badge-ETF { background: rgba(191,90,242,.2); color: var(--purple); }

  .pos-row { display: flex; justify-content: space-between; margin-bottom: 8px; }
  .pos-item { display: flex; flex-direction: column; }
  .pos-item .lbl { font-size: 11px; color: var(--sub); margin-bottom: 2px; }
  .pos-item .val { font-size: 15px; font-weight: 500; }
  .pos-item .val.green { color: var(--green); }
  .pos-item .val.red   { color: var(--red); }

  .pos-footer { display: flex; gap: 8px; margin-top: 12px; }
  .pos-footer .tag { font-size: 12px; padding: 4px 10px; border-radius: 8px; background: var(--card2); color: var(--sub); }
  .pos-footer .tag.partial { background: rgba(48,209,88,.1); color: var(--green); }

  .divider { height: 1px; background: var(--sep); margin: 10px 0; }

  /* ── Empty State ── */
  .empty { text-align: center; padding: 40px 20px; color: var(--sub); }
  .empty .icon { font-size: 40px; margin-bottom: 12px; }

  /* ── Modal ── */
  .modal-overlay { display: none; position: fixed; inset: 0; background: rgba(0,0,0,.7);
                   z-index: 100; align-items: flex-end; }
  .modal-overlay.open { display: flex; }
  .modal { background: var(--card); border-radius: 20px 20px 0 0; padding: 24px 20px;
           width: 100%; padding-bottom: calc(24px + env(safe-area-inset-bottom, 0px)); }
  .modal h2 { font-size: 20px; font-weight: 700; margin-bottom: 20px; }
  .modal label { display: block; font-size: 13px; color: var(--sub); margin-bottom: 4px; margin-top: 14px; }
  .modal input, .modal select {
    width: 100%; background: var(--card2); border: none; border-radius: 10px;
    padding: 12px; color: var(--text); font-size: 16px; }
  .modal-btns { display: flex; gap: 12px; margin-top: 20px; }
  .btn-cancel { flex: 1; background: var(--card2); color: var(--sub); border: none;
                padding: 14px; border-radius: 12px; font-size: 16px; cursor: pointer; }
  .btn-save   { flex: 2; background: var(--blue); color: #fff; border: none;
                padding: 14px; border-radius: 12px; font-size: 16px; font-weight: 600; cursor: pointer; }

  /* ── SL Triggered Warning ── */
  .sl-warning { background: rgba(255,69,58,.15); border: 1px solid rgba(255,69,58,.4);
                border-radius: 10px; padding: 10px 14px; margin-bottom: 8px;
                font-size: 13px; color: var(--red); display: flex; align-items: center; gap: 8px; }

  /* ── Pull-to-Refresh ── */
  .ptr-indicator {
    position: fixed; top: 0; left: 0; right: 0; z-index: 200;
    display: flex; align-items: center; justify-content: center;
    height: 0; overflow: hidden; background: var(--card);
    font-size: 13px; color: var(--sub); transition: height .2s;
    border-bottom: 1px solid var(--sep);
  }
  .ptr-indicator.visible { height: 44px; }
  .ptr-indicator.refreshing { color: var(--blue); }
  @keyframes spin { to { transform: rotate(360deg); } }
  .ptr-spinner { display: inline-block; width: 14px; height: 14px; border: 2px solid var(--blue);
                 border-top-color: transparent; border-radius: 50%; animation: spin .6s linear infinite;
                 margin-right: 6px; }

  /* ── Delete swipe hint ── */
  .pos-card-wrap { position: relative; }
  .pos-del-btn { position: absolute; right: 0; top: 0; bottom: 0; width: 70px; background: var(--red);
                 border-radius: 0 var(--radius) var(--radius) 0; border: none; color: #fff;
                 font-size: 13px; cursor: pointer; display: none; }
  .pos-card-wrap:hover .pos-del-btn,
  .pos-card-wrap.show-del .pos-del-btn { display: flex; align-items: center; justify-content: center; }

  /* ── Trades ── */
  .trade-row { display: flex; justify-content: space-between; padding: 10px 0; border-bottom: 1px solid var(--sep); }
  .trade-row:last-child { border-bottom: none; }
  .trade-sym { font-weight: 600; font-size: 15px; }
  .trade-reason { font-size: 12px; color: var(--sub); margin-top: 2px; }
  .trade-pnl { text-align: right; font-size: 15px; font-weight: 600; }
  .trade-ts { font-size: 11px; color: var(--sub); margin-top: 2px; }

  /* ── Trade Filter ── */
  .trade-filter { display: flex; gap: 8px; margin-bottom: 14px; flex-wrap: wrap; align-items: center; }
  .filter-btn { background: var(--card2); border: none; color: var(--sub); padding: 6px 14px;
                border-radius: 20px; font-size: 13px; cursor: pointer; transition: all .2s; }
  .filter-btn.active { background: var(--blue); color: #fff; }
  .filter-custom { display: flex; gap: 6px; align-items: center; flex-wrap: wrap; }
  .filter-custom input[type=date] { background: var(--card2); border: none; color: var(--text);
    padding: 5px 10px; border-radius: 10px; font-size: 13px; }
  .filter-custom button { background: var(--card2); border: none; color: var(--blue);
    padding: 5px 12px; border-radius: 10px; font-size: 13px; cursor: pointer; }

  /* ── Trade Stats Bar ── */
  .trade-stats { display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; margin-bottom: 14px; }
  .ts-card { background: var(--card2); border-radius: 12px; padding: 10px; text-align: center; }
  .ts-card .ts-label { font-size: 10px; color: var(--sub); margin-bottom: 3px; }
  .ts-card .ts-val { font-size: 15px; font-weight: 700; }

  /* ── Equity Chart ── */
  .equity-wrap { background: var(--card); border-radius: var(--radius); padding: 16px; margin-bottom: 14px; }
  .equity-title { font-size: 13px; color: var(--sub); margin-bottom: 10px; }
  .equity-wrap canvas { width: 100% !important; }

</style>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
</head>
<body>

<div class="ptr-indicator" id="ptr-indicator">
  <span id="ptr-text">↓ 下拉更新</span>
</div>

<div class="header">
  <h1>📊 投資組合</h1>
  <button class="refresh-btn" id="refresh-btn" onclick="loadAll()">↻ 更新</button>
</div>

<!-- 總覽 -->
<div class="summary">
  <div class="sum-card">
    <div class="label">總資金</div>
    <div class="value" id="sum-total">—</div>
  </div>
  <div class="sum-card">
    <div class="label">已投入</div>
    <div class="value" id="sum-invested">—</div>
  </div>
  <div class="sum-card">
    <div class="label">未實現</div>
    <div class="value" id="sum-pnl">—</div>
  </div>
</div>

<!-- 分頁 -->
<div class="tabs">
  <button class="tab active" onclick="switchTab('all', this)">全部</button>
  <button class="tab" onclick="switchTab('CRYPTO', this)">🔷 加密</button>
  <button class="tab" onclick="switchTab('EMA99', this)">📈 EMA99</button>
  <button class="tab" onclick="switchTab('NFES', this)">🔮 NFES強化版</button>
  <button class="tab" onclick="switchTab('TW', this)">🇹🇼 台股</button>
  <button class="tab" onclick="switchTab('US', this)">🇺🇸 美股</button>
  <button class="tab" onclick="switchTab('ETF', this)">📦 ETF</button>
  <button class="tab" onclick="switchTab('trades', this)">📋 紀錄</button>
</div>

<div id="main-content"></div>

<!-- 新增持倉 Modal -->
<div class="modal-overlay" id="modal">
  <div class="modal">
    <h2 id="modal-title">新增持倉</h2>
    <label>市場</label>
    <select id="m-market">
      <option value="TW">🇹🇼 台股</option>
      <option value="US">🇺🇸 美股</option>
      <option value="ETF">📦 ETF</option>
    </select>
    <label>代號（如 3017、TSLA）</label>
    <input id="m-sym" type="text" placeholder="股票代號"/>
    <label>平均成本（每股）</label>
    <input id="m-cost" type="number" step="any" placeholder="0.00"/>
    <label>持有數量（股/張）</label>
    <input id="m-qty" type="number" step="any" placeholder="1"/>
    <label>止損價格</label>
    <input id="m-sl" type="number" step="any" placeholder="0.00"/>
    <label>現價（選填）</label>
    <input id="m-cur" type="number" step="any" placeholder="留空 = 等同成本"/>
    <div class="modal-btns">
      <button class="btn-cancel" onclick="closeModal()">取消</button>
      <button class="btn-save" onclick="saveManual()">儲存</button>
    </div>
  </div>
</div>

<script>
let currentTab = 'all';
let cryptoData  = { positions: [], capital: 0, recent_trades: [], trade_pnl_sum: 0, pnl_offset: 0 };
let manualData  = { positions: [] };
let editingId   = null;

let _loading = false;
async function loadAll() {
  if (_loading) return;
  _loading = true;
  const btn = document.getElementById('refresh-btn');
  const ptr = document.getElementById('ptr-indicator');
  const ptrText = document.getElementById('ptr-text');
  if (btn) { btn.disabled = true; btn.textContent = '更新中...'; }
  if (ptr) { ptr.classList.add('visible','refreshing');
             ptrText.innerHTML = '<span class="ptr-spinner"></span>更新中...'; }
  try {
    const [cr, mn] = await Promise.all([
      fetch('/api/crypto').then(r => r.json()),
      fetch('/api/manual').then(r => r.json()),
    ]);
    cryptoData = cr;
    manualData = mn;
    renderSummary();
    renderTab(currentTab);
  } catch(e) {
    console.error(e);
  } finally {
    _loading = false;
    if (btn) { btn.disabled = false; btn.textContent = '↻ 更新'; }
    if (ptr) {
      ptrText.textContent = '↓ 下拉更新';
      ptr.classList.remove('visible','refreshing');
    }
  }
}

function renderSummary() {
  // 加密幣
  const cryptoInvested = cryptoData.positions.reduce((s, p) => s + p.margin, 0);
  const cryptoPnl      = cryptoData.positions.reduce((s, p) => s + p.pnl_usd, 0);
  // 手動
  const manInvested = manualData.positions.reduce((s, p) => s + p.avg_cost * p.qty, 0);
  const manPnl      = manualData.positions.reduce((s, p) => s + (p.cur_px - p.avg_cost) * p.qty, 0);

  const totalCapital = cryptoData.capital + manInvested;
  const totalInvested = cryptoInvested + manInvested;
  const totalPnl = cryptoPnl + manPnl;

  document.getElementById('sum-total').textContent    = '$' + fmt(totalCapital);
  document.getElementById('sum-invested').textContent = '$' + fmt(totalInvested);
  const pnlEl = document.getElementById('sum-pnl');
  pnlEl.textContent = (totalPnl >= 0 ? '+' : '') + '$' + fmt(totalPnl);
  pnlEl.style.color = totalPnl >= 0 ? 'var(--green)' : 'var(--red)';
}

function switchTab(tab, el) {
  currentTab = tab;
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  el.classList.add('active');
  renderTab(tab);
}

function renderTab(tab) {
  const el = document.getElementById('main-content');
  if (tab === 'trades') {
    el.innerHTML = '<div id="tab-content"></div>';
    renderTrades();
    return;
  }

  let html = '';

  // 加密幣區塊（可按策略篩選）
  if (tab === 'all' || tab === 'CRYPTO' || tab === 'EMA99' || tab === 'NFES') {
    let positions = cryptoData.positions;
    if (tab === 'EMA99') positions = positions.filter(p => p.strategy === 'EMA99');
    if (tab === 'NFES')  positions = positions.filter(p => p.strategy && p.strategy.includes('NFES'));

    const sectionTitle = tab === 'EMA99'  ? '📈 EMA99 持倉'
                       : tab === 'NFES'   ? '🔮 NFES 強化版持倉'
                       : '🔷 加密幣持倉';
    html += `<div class="section">
      <div class="section-title"><span>${sectionTitle}</span></div>`;
    if (!positions.length) {
      html += `<div class="empty"><div class="icon">📭</div><div>目前無持倉</div></div>`;
    } else {
      const triggered = positions.filter(p => p.sl_triggered);
      if (triggered.length) {
        html += `<div class="sl-warning">⚠️ <b>${triggered.map(p=>p.sym).join('、')}</b> 即時價已跌破止損線！請確認是否手動平倉</div>`;
      }
      positions.forEach(p => { html += renderCryptoCard(p); });
    }
    html += '</div>';
    if (cryptoData.last_run) {
      const d = new Date(cryptoData.last_run);
      html += `<p style="text-align:center;font-size:11px;color:var(--sub);margin-top:-18px;margin-bottom:16px">
        上次更新：${d.toLocaleString('zh-TW')}</p>`;
    }
  }

  // 手動持倉
  ['TW', 'US', 'ETF'].forEach(market => {
    if (tab !== 'all' && tab !== market) return;
    const label = market === 'TW' ? '🇹🇼 台股' : market === 'US' ? '🇺🇸 美股' : '📦 ETF';
    const positions = manualData.positions.filter(p => p.market === market);
    html += `<div class="section">
      <div class="section-title">
        <span>${label}持倉</span>
        <button class="add-btn" onclick="openModal('${market}')">＋ 新增</button>
      </div>`;
    if (!positions.length) {
      html += `<div class="empty"><div class="icon">📭</div><div>點右上角 ＋ 新增</div></div>`;
    } else {
      positions.forEach(p => { html += renderManualCard(p, market); });
    }
    html += '</div>';
  });

  el.innerHTML = html;
}

function strategyTag(strategy) {
  if (!strategy) return '';
  const cls = strategy.includes('NFES') ? 'strat-nfes' : 'strat-ema99';
  return `<span class="strategy-tag ${cls}">${strategy}</span>`;
}

function fmtEntryTs(ts) {
  if (!ts) return '';
  try {
    const d = new Date(ts);
    const MM = String(d.getMonth()+1).padStart(2,'0');
    const DD = String(d.getDate()).padStart(2,'0');
    const hh = String(d.getHours()).padStart(2,'0');
    const mm = String(d.getMinutes()).padStart(2,'0');
    return `${d.getFullYear()}/${MM}/${DD} ${hh}:${mm}`;
  } catch(e) { return ''; }
}

function renderCryptoCard(p) {
  const pnlColor   = p.pnl_pct >= 0 ? 'green' : 'red';
  const pnlSign    = p.pnl_pct >= 0 ? '▲' : '▼';
  const isLong     = (p.side || 'long') === 'long';
  const slPct      = p.entry_px ? ((p.sl - p.entry_px) / p.entry_px * 100).toFixed(1) : 0;
  const liveTag    = p.using_live ? '🟢 即時' : '🟡 存檔';
  const cardBorder = p.sl_triggered ? 'border:1px solid rgba(255,69,58,.5);' : '';
  const dirLabel   = isLong ? '<span style="color:var(--green)">▲ LONG</span>' : '<span style="color:var(--red)">▼ SHORT</span>';
  const entryTimeStr = fmtEntryTs(p.entry_ts);
  // TP 目標（NFES 才有 tp1/tp2/tp3）
  // 做多：價格漲才獲利 ↑ 綠；做空：價格跌才獲利 ↓ 紅
  const tpColor  = isLong ? 'var(--green)' : 'var(--red)';
  const tpArrow  = isLong ? '↑' : '↓';
  const tpTitle  = isLong ? '🎯 做多止盈' : '🎯 做空止盈';
  function tpPct(tp) {
    if (!p.entry_px || !tp) return '';
    const pct = isLong
      ? ((tp - p.entry_px) / p.entry_px * 100).toFixed(1)
      : ((p.entry_px - tp) / p.entry_px * 100).toFixed(1);
    return ` <span style="font-size:11px;color:var(--sub)">(+${pct}%)</span>`;
  }
  const tpHtml = (p.tp1 && p.tp1 > 0) ? `
    <div class="divider"></div>
    <div style="font-size:11px;color:var(--sub);margin-bottom:6px">${tpTitle} ${tpArrow}</div>
    <div class="pos-row">
      <div class="pos-item"><span class="lbl">TP1</span><span class="val" style="color:${tpColor}">${tpArrow} ${fmtPx(p.tp1)}${tpPct(p.tp1)}</span></div>
      <div class="pos-item" style="text-align:center"><span class="lbl">TP2</span><span class="val" style="color:${tpColor}">${tpArrow} ${fmtPx(p.tp2)}${tpPct(p.tp2)}</span></div>
      <div class="pos-item" style="text-align:right"><span class="lbl">TP3</span><span class="val" style="color:${tpColor}">${tpArrow} ${fmtPx(p.tp3)}${tpPct(p.tp3)}</span></div>
    </div>` : '';

  return `<div class="pos-card" style="${cardBorder}">
    <div class="pos-top">
      <div class="pos-sym">${strategyTag(p.strategy)}${p.sym}${entryTimeStr ? ` <span style="font-size:12px;font-weight:400;color:var(--sub)">(${entryTimeStr})</span>` : ''} ${p.sl_triggered ? '⚠️' : ''}</div>
      <span class="pos-badge badge-crypto">${dirLabel} ${p.lev}×</span>
    </div>
    <div class="pos-row">
      <div class="pos-item">
        <span class="lbl">均價（進場）</span>
        <span class="val">${fmtPx(p.entry_px)}</span>
      </div>
      <div class="pos-item" style="text-align:center">
        <span class="lbl">現價 <span style="font-size:10px">${liveTag}</span></span>
        <span class="val">${fmtPx(p.cur_px)}</span>
      </div>
      <div class="pos-item" style="text-align:right">
        <span class="lbl">獲利</span>
        <span class="val ${pnlColor}">${pnlSign} ${Math.abs(p.pnl_pct)}%</span>
      </div>
    </div>
    <div class="divider"></div>
    <div class="pos-row">
      <div class="pos-item">
        <span class="lbl">🛑 止損價</span>
        <span class="val red">${fmtPx(p.sl)}<span style="font-size:11px;color:var(--sub)"> (${slPct}%)</span></span>
      </div>
      <div class="pos-item" style="text-align:right">
        <span class="lbl">已投入保證金</span>
        <span class="val">$${fmt(p.margin)}</span>
      </div>
    </div>
    ${tpHtml}
    <div class="pos-footer">
      ${p.partial ? '<span class="tag partial">50% 已出場</span>' : ''}
      ${p.sl_triggered ? '<span class="tag" style="background:rgba(255,69,58,.2);color:var(--red)">⚠️ 止損觸發</span>' : ''}
      <span class="tag">損益 ${p.pnl_usd >= 0 ? '+' : ''}$${fmt(p.pnl_usd)}</span>
    </div>
  </div>`;
}

function renderManualCard(p, market) {
  const pnlAmt = (p.cur_px - p.avg_cost) * p.qty;
  const pnlPct = p.avg_cost ? ((p.cur_px - p.avg_cost) / p.avg_cost * 100) : 0;
  const pnlColor = pnlPct >= 0 ? 'green' : 'red';
  const pnlSign  = pnlPct >= 0 ? '▲' : '▼';
  const slPct    = p.avg_cost ? ((p.sl - p.avg_cost) / p.avg_cost * 100).toFixed(1) : 0;
  const badgeClass = `badge-${market}`;
  const invested = p.avg_cost * p.qty;

  return `<div class="pos-card-wrap" id="wrap-${p.id}">
  <div class="pos-card" oncontextmenu="toggleDel(${p.id}); return false;">
    <div class="pos-top">
      <div class="pos-sym">${p.sym}</div>
      <span class="pos-badge ${badgeClass}">${market}</span>
    </div>
    <div class="pos-row">
      <div class="pos-item">
        <span class="lbl">均價</span>
        <span class="val">${fmtPx(p.avg_cost)}</span>
      </div>
      <div class="pos-item" style="text-align:center">
        <span class="lbl">現價</span>
        <span class="val" onclick="updatePrice(${p.id}, ${p.cur_px})" style="cursor:pointer">
          ${fmtPx(p.cur_px)} ✏️</span>
      </div>
      <div class="pos-item" style="text-align:right">
        <span class="lbl">獲利</span>
        <span class="val ${pnlColor}">${pnlSign} ${Math.abs(pnlPct).toFixed(2)}%</span>
      </div>
    </div>
    <div class="divider"></div>
    <div class="pos-row">
      <div class="pos-item">
        <span class="lbl">🛑 止損</span>
        <span class="val red">${fmtPx(p.sl)}<span style="font-size:11px;color:var(--sub)"> (${slPct}%)</span></span>
      </div>
      <div class="pos-item" style="text-align:right">
        <span class="lbl">已投入</span>
        <span class="val">$${fmt(invested)}</span>
      </div>
    </div>
    <div class="pos-footer">
      <span class="tag">損益 ${pnlAmt >= 0 ? '+' : ''}$${fmt(pnlAmt)}</span>
      <span class="tag">${p.qty} 股</span>
      ${p.note ? `<span class="tag">${p.note}</span>` : ''}
    </div>
  </div>
  <button class="pos-del-btn" onclick="deleteManual(${p.id})">刪除</button>
  </div>`;
}

// ── Trade filter state ─────────────────────────────────────────
let _tradeRange = '30';   // '7' | '30' | 'custom'
let _tradeFrom  = '';
let _tradeTo    = '';
let _equityChart = null;

function _filterTrades(trades) {
  const now = Date.now();
  let from, to;
  if (_tradeRange === '7')  { from = now - 7  * 86400000; to = now; }
  if (_tradeRange === '30') { from = now - 30 * 86400000; to = now; }
  if (_tradeRange === 'custom') {
    from = _tradeFrom ? new Date(_tradeFrom).getTime() : 0;
    to   = _tradeTo   ? new Date(_tradeTo).getTime() + 86400000 : now;
  }
  return trades.filter(t => {
    const ts = t.exit_ts ? new Date(t.exit_ts).getTime() : 0;
    return ts >= from && ts <= to;
  });
}

function _setRange(r) {
  _tradeRange = r;
  document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
  const btn = document.getElementById('fb-' + r);
  if (btn) btn.classList.add('active');
  document.getElementById('custom-range').style.display = r === 'custom' ? 'flex' : 'none';
  renderTrades();
}

function _applyCustomRange() {
  _tradeFrom = document.getElementById('trade-from').value;
  _tradeTo   = document.getElementById('trade-to').value;
  renderTrades();
}

function _drawEquity(trades) {
  const INIT = 10000;
  // 優先用每日快照曲線
  const curve = cryptoData.equity_curve || [];
  const labels = [], data = [];
  if (curve.length >= 2) {
    curve.forEach(pt => {
      labels.push(pt.date ? pt.date.slice(5) : '');  // MM-DD
      data.push(pt.capital);
    });
  } else {
    // fallback：用 trades 累積（舊邏輯）
    const sorted = [...trades].sort((a,b) => new Date(a.exit_ts||0) - new Date(b.exit_ts||0));
    let cum = 0;
    sorted.forEach(t => {
      cum += (t.pnl || 0);
      const d = t.exit_ts ? new Date(t.exit_ts).toLocaleDateString('zh-TW',{month:'2-digit',day:'2-digit'}) : '';
      labels.push(d);
      data.push(parseFloat((INIT + cum).toFixed(2)));
    });
    labels.unshift('起始'); data.unshift(INIT);
  }

  const ctx = document.getElementById('equity-canvas');
  if (!ctx) return;
  if (_equityChart) { _equityChart.destroy(); _equityChart = null; }

  const lastVal = data[data.length - 1];
  const lineColor = lastVal >= INIT ? '#30d158' : '#ff453a';
  const fillColor = lastVal >= INIT ? 'rgba(48,209,88,.15)' : 'rgba(255,69,58,.15)';

  _equityChart = new Chart(ctx, {
    type: 'line',
    data: {
      labels,
      datasets: [{
        data,
        borderColor: lineColor,
        backgroundColor: fillColor,
        borderWidth: 2,
        pointRadius: data.length <= 20 ? 3 : 0,
        pointHoverRadius: 5,
        fill: true,
        tension: 0.3,
      }]
    },
    options: {
      responsive: true,
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            label: ctx => {
              const diff = ctx.parsed.y - INIT;
              return `${ctx.parsed.y.toFixed(0)} U  (${diff >= 0 ? '+' : ''}${diff.toFixed(2)})`;
            }
          }
        }
      },
      scales: {
        x: { ticks: { color:'#8e8e93', font:{size:10}, maxTicksLimit:8 }, grid:{color:'rgba(255,255,255,.05)'} },
        y: {
          ticks: {
            color:'#8e8e93', font:{size:10},
            callback: v => v.toFixed(0) + ' U'
          },
          grid:{ color:'rgba(255,255,255,.05)' }
        }
      }
    }
  });
}

function renderTrades() {
  const allTrades = (cryptoData.recent_trades || []).slice().reverse();  // 新→舊 for list
  const filtered  = _filterTrades(allTrades);

  // ── 統計 ───────────────────────────────────────────────────
  const wins    = filtered.filter(t => t.pnl > 0);
  const losses  = filtered.filter(t => t.pnl <= 0);
  // 總損益：全期用真實值（capital-10000），篩選期間用 trades 加總
  const isAllTime  = (_tradeRange === '30' && filtered.length === allTrades.length) || _tradeRange === 'all';
  const totalPnl   = (_tradeRange === '30' && filtered.length >= allTrades.length)
                     ? (cryptoData.total_pnl || 0)
                     : filtered.reduce((s,t) => s + (t.pnl||0), 0);
  const winRate = filtered.length ? (wins.length / filtered.length * 100) : 0;
  const avgWin  = wins.length   ? wins.reduce((s,t) => s+t.pnl,0) / wins.length   : 0;
  const avgLoss = losses.length ? losses.reduce((s,t) => s+t.pnl,0) / losses.length : 0;
  const pnlCol  = totalPnl >= 0 ? 'var(--green)' : 'var(--red)';

  let html = `<div class="section">
  <div class="section-title">📋 交易紀錄</div>

  <!-- 篩選器 -->
  <div class="trade-filter">
    <button class="filter-btn${_tradeRange==='7'?' active':''}"  id="fb-7"      onclick="_setRange('7')">7天</button>
    <button class="filter-btn${_tradeRange==='30'?' active':''}" id="fb-30"     onclick="_setRange('30')">30天</button>
    <button class="filter-btn${_tradeRange==='custom'?' active':''}" id="fb-custom" onclick="_setRange('custom')">自定義</button>
    <div class="filter-custom" id="custom-range" style="display:${_tradeRange==='custom'?'flex':'none'}">
      <input type="date" id="trade-from" value="${_tradeFrom}">
      <span style="color:var(--sub)">～</span>
      <input type="date" id="trade-to"   value="${_tradeTo}">
      <button onclick="_applyCustomRange()">套用</button>
    </div>
  </div>

  <!-- 統計 -->
  <div class="trade-stats">
    <div class="ts-card">
      <div class="ts-label">總損益</div>
      <div class="ts-val" style="color:${pnlCol}">${totalPnl>=0?'+':''}$${fmt(totalPnl)}</div>
    </div>
    <div class="ts-card">
      <div class="ts-label">勝率</div>
      <div class="ts-val">${winRate.toFixed(0)}%</div>
    </div>
    <div class="ts-card">
      <div class="ts-label">均獲利</div>
      <div class="ts-val" style="color:var(--green)">${avgWin>0?'+':''}$${fmt(avgWin)}</div>
    </div>
    <div class="ts-card">
      <div class="ts-label">均虧損</div>
      <div class="ts-val" style="color:var(--red)">${fmt(avgLoss)}</div>
    </div>
  </div>

  <!-- 資產曲線 -->
  <div class="equity-wrap">
    <div class="equity-title">📈 資產曲線（每日 08:00 快照）起始 10,000U → 現在 ${(cryptoData.capital||10000).toFixed(0)}U</div>
    <canvas id="equity-canvas" height="160"></canvas>
  </div>

  <!-- 交易列表 -->
  <div class="pos-card">`;

  if (!filtered.length) {
    html += `<div class="empty"><div class="icon">📭</div><div>此區間無交易記錄</div></div>`;
  } else {
    filtered.forEach(t => {
      const sign = t.pnl >= 0 ? '+' : '';
      const col  = t.pnl >= 0 ? 'var(--green)' : 'var(--red)';
      const ts   = t.exit_ts ? new Date(t.exit_ts).toLocaleString('zh-TW', {month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}) : '';
      const reasonMap = {
        'partial_tp':'部分止盈','tp1':'TP1 止盈','tp2':'TP2 止盈','tp3':'TP3 止盈',
        'stop_loss':'止損','trail_stop':'移動止損','breakeven':'保本出場'
      };
      const reasonLabel = reasonMap[t.reason] || t.reason;
      const stratCls = t.strategy && t.strategy.includes('NFES') ? 'strat-nfes' : 'strat-ema99';
      const stratName = t.strategy || 'EMA99';
      html += `<div class="trade-row">
        <div>
          <div class="trade-sym"><span class="strategy-tag ${stratCls}">${stratName}</span>${t.sym}</div>
          <div class="trade-reason">${reasonLabel}</div>
        </div>
        <div>
          <div class="trade-pnl" style="color:${col}">${sign}$${fmt(t.pnl)}</div>
          <div class="trade-ts">${ts}</div>
        </div>
      </div>`;
    });
  }
  html += `</div></div>`;

  // 注入 HTML 後繪圖
  const el = document.getElementById('tab-content');
  el.innerHTML = html;
  _drawEquity(_filterTrades(cryptoData.recent_trades || []));
  return '';  // renderTrades 由此直接操作 DOM
}

// ── Modal ──────────────────────────────────────────────────────
function openModal(market) {
  editingId = null;
  document.getElementById('modal-title').textContent = '新增持倉';
  document.getElementById('m-market').value = market;
  document.getElementById('m-sym').value  = '';
  document.getElementById('m-cost').value = '';
  document.getElementById('m-qty').value  = '';
  document.getElementById('m-sl').value   = '';
  document.getElementById('m-cur').value  = '';
  document.getElementById('modal').classList.add('open');
}
function closeModal() {
  document.getElementById('modal').classList.remove('open');
}
async function saveManual() {
  const body = {
    sym      : document.getElementById('m-sym').value.trim(),
    market   : document.getElementById('m-market').value,
    avg_cost : parseFloat(document.getElementById('m-cost').value),
    qty      : parseFloat(document.getElementById('m-qty').value),
    sl       : parseFloat(document.getElementById('m-sl').value),
    cur_px   : parseFloat(document.getElementById('m-cur').value) || undefined,
  };
  if (!body.sym || isNaN(body.avg_cost) || isNaN(body.qty) || isNaN(body.sl)) {
    alert('請填寫所有必填欄位');
    return;
  }
  await fetch('/api/manual', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body) });
  closeModal();
  loadAll();
}
async function updatePrice(id, cur) {
  const v = prompt('輸入現價：', cur);
  if (v === null) return;
  await fetch(`/api/manual/${id}`, { method:'PATCH', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ cur_px: parseFloat(v) }) });
  loadAll();
}
async function deleteManual(id) {
  if (!confirm('確認刪除此持倉？')) return;
  await fetch(`/api/manual/${id}`, { method:'DELETE' });
  loadAll();
}
function toggleDel(id) {
  const w = document.getElementById('wrap-' + id);
  w.classList.toggle('show-del');
}

// ── Format helpers ──
function fmt(v) {
  if (Math.abs(v) >= 1000) return v.toLocaleString('en-US', {minimumFractionDigits:0, maximumFractionDigits:0});
  return v.toLocaleString('en-US', {minimumFractionDigits:2, maximumFractionDigits:2});
}
function fmtPx(v) {
  if (!v) return '—';
  if (v >= 100)  return v.toFixed(2);
  if (v >= 1)    return v.toFixed(4);
  return v.toFixed(6);
}

// 點空白處關閉刪除按鈕
document.addEventListener('click', e => {
  if (!e.target.closest('.pos-card-wrap')) {
    document.querySelectorAll('.pos-card-wrap.show-del').forEach(w => w.classList.remove('show-del'));
  }
});
// Modal 點外部關閉
document.getElementById('modal').addEventListener('click', e => {
  if (e.target === document.getElementById('modal')) closeModal();
});

// ── 下拉更新（Pull-to-Refresh）手勢 ──
(function(){
  let startY = 0, pulling = false;
  const THRESHOLD = 70;
  const ptr = document.getElementById('ptr-indicator');
  const ptrText = document.getElementById('ptr-text');

  document.addEventListener('touchstart', e => {
    if (window.scrollY === 0) { startY = e.touches[0].clientY; pulling = true; }
  }, { passive: true });

  document.addEventListener('touchmove', e => {
    if (!pulling) return;
    const dy = e.touches[0].clientY - startY;
    if (dy > 0 && dy < THRESHOLD + 20) {
      ptr.classList.add('visible');
      ptr.classList.remove('refreshing');
      ptrText.textContent = dy > THRESHOLD ? '放開更新 ↑' : '↓ 繼續下拉';
    }
  }, { passive: true });

  document.addEventListener('touchend', e => {
    if (!pulling) return;
    const dy = e.changedTouches[0].clientY - startY;
    pulling = false;
    if (dy > THRESHOLD) { loadAll(); }
    else { ptr.classList.remove('visible'); ptrText.textContent = '↓ 下拉更新'; }
  }, { passive: true });
})();

// 啟動
loadAll();
// 每 30 秒自動更新
setInterval(loadAll, 30000);
</script>
</body>
</html>"""

STRATEGY99_HTML = r"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"/>
<title>99戰法</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0d0d0d;color:#e8e8e8;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;padding-bottom:env(safe-area-inset-bottom,20px)}
.header{background:#111;padding:14px 16px;display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid #222;position:sticky;top:0;z-index:100}
.header h1{font-size:17px;font-weight:700}
.refresh-btn{background:#1e3a5f;color:#5ba4f5;border:none;border-radius:8px;padding:7px 14px;font-size:13px;cursor:pointer;-webkit-tap-highlight-color:transparent}
.stats-row{display:grid;grid-template-columns:repeat(2,1fr);gap:10px;padding:14px 14px 0}
.stat-card{background:#161616;border:1px solid #222;border-radius:14px;padding:14px 16px;cursor:default}
.stat-card.clickable{cursor:pointer;-webkit-tap-highlight-color:transparent}
.stat-card.clickable:active{opacity:.75}
.stat-card .lbl{font-size:11px;color:#666;margin-bottom:4px}
.stat-card .val{font-size:24px;font-weight:700;line-height:1.1}
.green{color:#26a69a}.red{color:#ef5350}.yellow{color:#ffd700}.blue{color:#5ba4f5}
.section{padding:14px 14px 0}
.sec-title{font-size:12px;font-weight:600;color:#777;letter-spacing:.8px;text-transform:uppercase;margin-bottom:10px}
.pos-card{background:#161616;border:1px solid #252525;border-radius:16px;padding:14px;margin-bottom:10px}
.pos-head{display:flex;justify-content:space-between;align-items:center;margin-bottom:12px}
.pos-sym{font-size:17px;font-weight:700}
.badge{font-size:11px;font-weight:600;padding:3px 10px;border-radius:20px}
.badge.long{background:#0d2b1f;color:#26a69a}
.badge.short{background:#2b0d0d;color:#ef5350}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.info-item{background:#1c1c1c;border-radius:10px;padding:9px 11px}
.info-item .k{font-size:10px;color:#555;margin-bottom:3px}
.info-item .v{font-size:13px;font-weight:600}
.tp-row{display:flex;gap:6px;margin-top:8px}
.tp-chip{flex:1;background:#1c1c1c;border-radius:10px;padding:7px 6px;text-align:center}
.tp-chip .tl{font-size:10px;color:#555}
.tp-chip .tv{font-size:12px;font-weight:600;margin-top:2px}
.tp-chip.hit{border:1px solid #26a69a40;background:#0d2b1f}
.tp-chip.hit .tv{color:#26a69a}
.pnl-bar{display:flex;justify-content:space-between;align-items:center;margin-top:10px;padding:9px 12px;border-radius:10px}
.pnl-bar.pos{background:#0d2b1f}.pnl-bar.neg{background:#2b0d0d}
.pnl-bar .pl{font-size:11px;color:#666}
.pnl-bar .pv{font-size:15px;font-weight:700}
.pnl-bar.pos .pv{color:#26a69a}.pnl-bar.neg .pv{color:#ef5350}
.sig-card{background:#161616;border:1px solid #222;border-radius:12px;padding:11px 14px;margin-bottom:8px;display:flex;justify-content:space-between;align-items:center;gap:8px}
.sig-sym{font-size:14px;font-weight:600;flex-shrink:0}
.stage-badges{display:flex;flex-wrap:wrap;gap:5px;justify-content:flex-end}
.sb{font-size:10px;padding:3px 8px;border-radius:20px;font-weight:600;white-space:nowrap}
.sb.fb{background:#1a2b3b;color:#5ba4f5}
.sb.pb{background:#1a2b1a;color:#66bb6a}
.sb.fb_s{background:#2b1a2b;color:#ce93d8}
.sb.pb_s{background:#2b1a1a;color:#ef9a9a}
.empty{text-align:center;padding:36px 16px;color:#444;font-size:13px}
/* Modal */
.modal-overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.8);z-index:200;align-items:flex-end;justify-content:center}
.modal-overlay.show{display:flex}
.modal{background:#141414;border-radius:20px 20px 0 0;width:100%;max-width:560px;max-height:85vh;overflow-y:auto;padding:20px 16px 32px}
.modal-title{font-size:15px;font-weight:700;margin-bottom:14px;display:flex;justify-content:space-between;align-items:center}
.modal-close{background:none;border:none;color:#666;font-size:22px;cursor:pointer;line-height:1}
.trade-card{background:#1c1c1c;border-radius:12px;padding:12px 14px;margin-bottom:8px}
.trade-top{display:flex;justify-content:space-between;align-items:center;margin-bottom:6px}
.trade-sym{font-size:14px;font-weight:700}
.trade-pnl{font-size:15px;font-weight:700}
.trade-pnl.pos{color:#26a69a}.trade-pnl.neg{color:#ef5350}
.trade-meta{font-size:11px;color:#555;line-height:1.6}
/* Chart */
.chart-btn{margin-top:10px;width:100%;background:#1c1c1c;border:1px dashed #333;border-radius:10px;padding:9px;font-size:12px;color:#888;cursor:pointer;-webkit-tap-highlight-color:transparent;display:flex;align-items:center;justify-content:center;gap:6px}
.chart-btn.has-chart{border-color:#5ba4f540;color:#5ba4f5;background:#0d1f33}
.chart-img-wrap{margin-bottom:12px}
.chart-img-wrap img{width:100%;border-radius:10px;display:block}
.chart-img-meta{font-size:11px;color:#555;margin-top:5px;text-align:right}
.chart-upload-btn{width:100%;background:#1e3a5f;color:#5ba4f5;border:none;border-radius:10px;padding:11px;font-size:13px;font-weight:600;cursor:pointer;margin-top:4px}
.upd{font-size:11px;color:#333;text-align:center;padding:14px}
</style>
</head>
<body>
<div class="header">
  <h1>📊 99戰法</h1>
  <button class="refresh-btn" onclick="loadData()">↺ 刷新</button>
</div>
<div id="root"><div class="empty">載入中...</div></div>
<div class="upd" id="upd"></div>

<!-- 交易記錄 Modal -->
<div class="modal-overlay" id="tradeModal" onclick="closeModal(event)">
  <div class="modal">
    <div class="modal-title">最近 10 筆交易 <button class="modal-close" onclick="closeModal()">×</button></div>
    <div id="modalBody"></div>
  </div>
</div>

<!-- K線圖 Modal -->
<div class="modal-overlay" id="chartModal" onclick="closeChartModal(event)">
  <div class="modal">
    <div class="modal-title"><span id="chartModalTitle">K線圖</span><button class="modal-close" onclick="closeChartModal()">×</button></div>
    <div id="chartModalBody"></div>
    <input type="file" id="chartFileInput" accept="image/*" style="display:none" onchange="doUpload()"/>
    <button class="chart-upload-btn" onclick="document.getElementById('chartFileInput').click()">📷 新增截圖</button>
  </div>
</div>

<script>
let _tradeHistory=[], _chartSym='', _chartSource='99';
function closeModal(e){
  if(!e||e.target===document.getElementById('tradeModal'))
    document.getElementById('tradeModal').classList.remove('show');
}
function closeChartModal(e){
  if(!e||e.target===document.getElementById('chartModal'))
    document.getElementById('chartModal').classList.remove('show');
}
async function openChartModal(sym, source){
  _chartSym=sym; _chartSource=source;
  document.getElementById('chartModalTitle').textContent=sym+' K線圖';
  document.getElementById('chartModalBody').innerHTML='<div class="empty">載入中...</div>';
  document.getElementById('chartModal').classList.add('show');
  await refreshChartModal();
}
async function refreshChartModal(){
  const symKey=_chartSym.replace(/\//g,'_').replace(/:/g,'_');
  const r=await fetch(`/api/position/charts/${_chartSource}/${symKey}`);
  const d=await r.json();
  const charts=(d.charts||[]).slice().reverse();
  let h='';
  if(!charts.length){h='<div class="empty" style="padding:20px">尚無截圖，點下方新增</div>';}
  else charts.forEach(c=>{
    const dt=c.uploaded_at.replace(/(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})/,'$1/$2/$3 $4:$5');
    h+=`<div class="chart-img-wrap">
      <img src="/position-chart/${c.filename}" loading="lazy" onclick="this.requestFullscreen&&this.requestFullscreen()"/>
      <div class="chart-img-meta">${dt}${c.note?' · '+c.note:''}</div>
    </div>`;
  });
  document.getElementById('chartModalBody').innerHTML=h;
}
async function doUpload(){
  const inp=document.getElementById('chartFileInput');
  if(!inp.files[0])return;
  const fd=new FormData();
  fd.append('symbol',_chartSym);
  fd.append('source',_chartSource);
  fd.append('file',inp.files[0]);
  document.getElementById('chartModalBody').innerHTML='<div class="empty">上傳中...</div>';
  await fetch('/api/position/chart',{method:'POST',body:fd});
  inp.value='';
  await refreshChartModal();
  await loadData();
}
function openTradeModal(){
  const th=_tradeHistory.slice(-10).reverse();
  let h='';
  if(!th.length){h='<div class="empty">尚無交易記錄</div>';}
  else{
    th.forEach(t=>{
      const pnl=t.pnl||0;
      const side=t.side==='long'?'多':'空';
      const win=t.win?'✅ 獲利':'❌ 虧損';
      h+=`<div class="trade-card">
        <div class="trade-top">
          <span class="trade-sym">${t.symbol||'--'} <span style="font-size:11px;font-weight:400;color:#666">${side}</span></span>
          <span class="trade-pnl ${pnl>=0?'pos':'neg'}">${pnl>=0?'+':''}$${Number(pnl).toFixed(2)}</span>
        </div>
        <div class="trade-meta">
          ${win} &nbsp;·&nbsp; ${t.reason||'--'}<br>
          進場 ${Number(t.entry).toFixed(4)} → 出場 ${Number(t.exit||0).toFixed(4)}<br>
          ${t.opened_at?new Date(t.opened_at).toLocaleString('zh-TW',{timeZone:'Asia/Taipei',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}):''} ~ ${t.closed_at?new Date(t.closed_at).toLocaleString('zh-TW',{timeZone:'Asia/Taipei',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}):''}
        </div>
      </div>`;
    });
  }
  document.getElementById('modalBody').innerHTML=h;
  document.getElementById('tradeModal').classList.add('show');
}
function fmt(n,d=4){if(n==null)return'--';return Number(n).toFixed(d);}
function fmtPct(n){if(n==null)return'--';return(n>0?'+':'')+Number(n).toFixed(2)+'%';}
let _chartCounts={};
async function loadChartCounts(symbols){
  const meta=await fetch('/api/position/charts/99/__meta__').then(r=>r.json()).catch(()=>({charts:{}}));
  // fetch meta.json directly to count
  const r2=await fetch('/api/position/chartmeta').then(r=>r.json()).catch(()=>({}));
  _chartCounts={};
  Object.entries(r2).forEach(([k,arr])=>{
    if(k.startsWith('99:'))_chartCounts[k.slice(3)]=arr.length;
  });
}
async function loadData(){
  try{
    const r=await fetch('/api/99/state');
    const d=await r.json();
    _tradeHistory=d.trade_history||[];
    await loadChartCounts(Object.keys(d.positions||{}));
    render(d);
    document.getElementById('upd').textContent='更新：'+new Date().toLocaleTimeString('zh-TW');
  }catch(e){document.getElementById('root').innerHTML='<div class="empty">載入失敗，請重試</div>';}
}
function render(d){
  const pos=d.positions||{};
  const sigs=d.signals||{};
  const cap=d.capital||0;
  const trades=d.total_trades||0;
  const wins=d.wins||0;
  const losses=d.losses||0;
  const wr=trades>0?(wins/trades*100).toFixed(1):'--';
  let html='';
  // Stats grid
  html+=`<div class="stats-row">
    <div class="stat-card"><div class="lbl">可用資金</div><div class="val yellow">$${Number(cap).toFixed(2)}</div></div>
    <div class="stat-card"><div class="lbl">持倉數</div><div class="val">${Object.keys(pos).length}</div></div>
    <div class="stat-card clickable" onclick="openTradeModal()"><div class="lbl">總交易 ▶</div><div class="val">${trades}</div></div>
    <div class="stat-card"><div class="lbl">勝率</div><div class="val ${parseFloat(wr)>=50?'green':'red'}">${wr}%</div></div>
  </div>`;
  // Positions
  html+='<div class="section"><div class="sec-title">持倉中</div>';
  const posArr=Object.values(pos);
  if(!posArr.length){html+='<div class="empty">目前無持倉</div>';}
  else posArr.forEach(p=>{
    const isLong=p.side==='long';
    const ep=p.entry||0,sl=p.sl_price||0;
    const slPct=ep>0?((sl-ep)/ep*100):0;
    const rPnl=p.realized_pnl||0;
    const opened=p.opened_at?new Date(p.opened_at).toLocaleString('zh-TW',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}):'--';
    const symClean=(p.symbol||'').replace('/USDT:USDT','');
    const symKey=(p.symbol||'').replace(/\//g,'_').replace(/:/g,'_');
    const hasChart=(_chartCounts[symKey]||0)>0;
    html+=`<div class="pos-card">
      <div class="pos-head">
        <span class="pos-sym">${symClean}</span>
        <span class="badge ${p.side}">${isLong?'多':'空'} ${p.leverage||1}x</span>
      </div>
      <div class="grid2">
        <div class="info-item"><div class="k">進場價</div><div class="v">${fmt(ep,4)}</div></div>
        <div class="info-item"><div class="k">止損價</div><div class="v" style="color:${isLong?'#ef5350':'#26a69a'}">${fmt(sl,4)} (${fmtPct(slPct)})</div></div>
        <div class="info-item"><div class="k">保證金</div><div class="v">$${Number(p.margin||0).toFixed(2)}</div></div>
        <div class="info-item"><div class="k">開倉時間</div><div class="v" style="font-size:11px">${opened}</div></div>
      </div>
      <div class="tp-row">
        <div class="tp-chip${p.tp1_hit?' hit':''}"><div class="tl">TP1</div><div class="tv">${fmt(p.tp1_price,4)}</div></div>
        <div class="tp-chip${p.tp2_hit?' hit':''}"><div class="tl">TP2</div><div class="tv">${fmt(p.tp2_price,4)}</div></div>
        <div class="tp-chip"><div class="tl">TP3</div><div class="tv">${fmt(p.tp3_price,4)}</div></div>
      </div>
      <div class="pnl-bar ${rPnl>=0?'pos':'neg'}">
        <span class="pl">已實現盈虧</span>
        <span class="pv">${rPnl>=0?'+':''}$${Number(rPnl).toFixed(2)}</span>
      </div>
      <button class="chart-btn${hasChart?' has-chart':''}" onclick="openChartModal('${p.symbol}','99')">
        ${hasChart?`🖼️ 查看K線圖 (${_chartCounts[symKey]}張)`:'📷 新增K線截圖'}
      </button>
    </div>`;
  });
  html+='</div>';
  // Signals link card
  const activeSigs=Object.entries(sigs).filter(([,s])=>s.stage!=='watching'||s.stage_s!=='watching_s');
  const longCount=activeSigs.filter(([,s])=>s.stage!=='watching').length;
  const shortCount=activeSigs.filter(([,s])=>s.stage_s!=='watching_s').length;
  html+=`<div class="section">
    <a href="/99-signals" style="display:block;background:#161616;border:1px solid #2a2a2a;border-radius:14px;padding:16px;text-decoration:none;color:inherit">
      <div style="display:flex;justify-content:space-between;align-items:center">
        <div>
          <div style="font-size:13px;font-weight:600;margin-bottom:4px">🔍 訊號觀察</div>
          <div style="font-size:12px;color:#666">活躍 ${activeSigs.length} 個 · 📈多${longCount} 📉空${shortCount}</div>
        </div>
        <div style="font-size:20px;color:#444">›</div>
      </div>
    </a>
  </div>`;
  document.getElementById('root').innerHTML=html;
}
loadData();
setInterval(loadData,15000);
</script>
</body>
</html>"""

SIGNALS99_HTML = r"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"/>
<title>99戰法 訊號觀察</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0d0d0d;color:#e8e8e8;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;padding-bottom:env(safe-area-inset-bottom,20px)}
.header{background:#111;padding:14px 16px;display:flex;align-items:center;gap:12px;border-bottom:1px solid #222;position:sticky;top:0;z-index:100}
.back-btn{background:none;border:none;color:#5ba4f5;font-size:17px;cursor:pointer;padding:2px 0;-webkit-tap-highlight-color:transparent}
.header h1{font-size:17px;font-weight:700;flex:1}
.refresh-btn{background:#1e3a5f;color:#5ba4f5;border:none;border-radius:8px;padding:7px 14px;font-size:13px;cursor:pointer}
.filter-row{display:flex;gap:8px;padding:12px 14px;overflow-x:auto}
.filter-btn{flex-shrink:0;padding:6px 14px;border-radius:20px;border:1px solid #2a2a2a;background:#161616;color:#888;font-size:12px;cursor:pointer;-webkit-tap-highlight-color:transparent}
.filter-btn.active{background:#1e3a5f;border-color:#5ba4f5;color:#5ba4f5}
.summary-row{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;padding:0 14px 10px}
.sum-box{background:#161616;border:1px solid #222;border-radius:12px;padding:10px 12px;text-align:center}
.sum-box .sl{font-size:10px;color:#666;margin-bottom:3px}
.sum-box .sv{font-size:16px;font-weight:700}
.green{color:#26a69a}.red{color:#ef5350}.blue{color:#5ba4f5}
.sig-list{padding:0 14px}
.sig-card{background:#161616;border:1px solid #222;border-radius:14px;padding:13px 14px;margin-bottom:8px;display:flex;justify-content:space-between;align-items:center;gap:10px}
.sig-sym{font-size:15px;font-weight:700;min-width:70px}
.stage-badges{display:flex;flex-wrap:wrap;gap:6px;justify-content:flex-end}
.sb{font-size:11px;padding:4px 10px;border-radius:20px;font-weight:600;white-space:nowrap}
.sb.fb{background:#1a2b3b;color:#5ba4f5}
.sb.pb{background:#1a2b1a;color:#66bb6a}
.sb.fb_s{background:#2b1a2b;color:#ce93d8}
.sb.pb_s{background:#2b1a1a;color:#ef9a9a}
.empty{text-align:center;padding:40px 16px;color:#444;font-size:13px}
.upd{font-size:11px;color:#333;text-align:center;padding:14px}
</style>
</head>
<body>
<div class="header">
  <button class="back-btn" onclick="history.back()">‹ 返回</button>
  <h1>🔍 訊號觀察</h1>
  <button class="refresh-btn" onclick="loadData()">↺</button>
</div>
<div class="filter-row">
  <button class="filter-btn active" onclick="setFilter('all',this)">全部</button>
  <button class="filter-btn" onclick="setFilter('long',this)">📈 多頭</button>
  <button class="filter-btn" onclick="setFilter('short',this)">📉 空頭</button>
  <button class="filter-btn" onclick="setFilter('pb',this)">等待進場</button>
  <button class="filter-btn" onclick="setFilter('fb',this)">一次突破</button>
</div>
<div id="summary" class="summary-row"></div>
<div id="list" class="sig-list"><div class="empty">載入中...</div></div>
<div class="upd" id="upd"></div>
<script>
let _sigs=[], _filter='all';
function setFilter(f,btn){
  _filter=f;
  document.querySelectorAll('.filter-btn').forEach(b=>b.classList.remove('active'));
  btn.classList.add('active');
  renderList();
}
function getFiltered(){
  return _sigs.filter(([,s])=>{
    if(_filter==='long')return s.stage!=='watching';
    if(_filter==='short')return s.stage_s!=='watching_s';
    if(_filter==='pb')return s.stage==='pulled_back'||s.stage_s==='pulled_back_s';
    if(_filter==='fb')return s.stage==='first_broke'||s.stage_s==='first_broke_s';
    return true;
  });
}
function renderList(){
  const list=getFiltered();
  const longs=list.filter(([,s])=>s.stage!=='watching').length;
  const shorts=list.filter(([,s])=>s.stage_s!=='watching_s').length;
  const pbCount=list.filter(([,s])=>s.stage==='pulled_back'||s.stage_s==='pulled_back_s').length;
  document.getElementById('summary').innerHTML=`
    <div class="sum-box"><div class="sl">活躍訊號</div><div class="sv">${list.length}</div></div>
    <div class="sum-box"><div class="sl">多/空</div><div class="sv"><span class="green">${longs}</span>/<span class="red">${shorts}</span></div></div>
    <div class="sum-box"><div class="sl">等待進場</div><div class="sv blue">${pbCount}</div></div>
  `;
  if(!list.length){document.getElementById('list').innerHTML='<div class="empty">無符合訊號</div>';return;}
  let h='';
  list.forEach(([sym,s])=>{
    const clean=sym.replace('/USDT:USDT','');
    const stage=s.stage||'watching', stage_s=s.stage_s||'watching_s';
    h+=`<div class="sig-card">
      <span class="sig-sym">${clean}</span>
      <div class="stage-badges">
        ${stage==='first_broke'?'<span class="sb fb">多 一次突破</span>':''}
        ${stage==='pulled_back'?'<span class="sb pb">多 等待進場</span>':''}
        ${stage_s==='first_broke_s'?'<span class="sb fb_s">空 一次突破</span>':''}
        ${stage_s==='pulled_back_s'?'<span class="sb pb_s">空 等待進場</span>':''}
      </div>
    </div>`;
  });
  document.getElementById('list').innerHTML=h;
}
async function loadData(){
  try{
    const r=await fetch('/api/99/state');
    const d=await r.json();
    const sigs=d.signals||{};
    _sigs=Object.entries(sigs).filter(([,s])=>s.stage!=='watching'||s.stage_s!=='watching_s');
    renderList();
    document.getElementById('upd').textContent='更新：'+new Date().toLocaleTimeString('zh-TW');
  }catch{document.getElementById('list').innerHTML='<div class="empty">載入失敗</div>';}
}
loadData();
setInterval(loadData,15000);
</script>
</body>
</html>"""

SAM_DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"/>
<title>Sam 交易</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0d0d0d;color:#e8e8e8;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;padding-bottom:env(safe-area-inset-bottom,20px)}
.header{background:#111;padding:14px 16px;display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid #222;position:sticky;top:0;z-index:100}
.header h1{font-size:17px;font-weight:700}
.refresh-btn{background:#1e3a5f;color:#5ba4f5;border:none;border-radius:8px;padding:7px 14px;font-size:13px;cursor:pointer;-webkit-tap-highlight-color:transparent}
.stats-row{display:grid;grid-template-columns:repeat(2,1fr);gap:10px;padding:14px 14px 0}
.stat-card{background:#161616;border:1px solid #222;border-radius:14px;padding:14px 16px}
.stat-card .lbl{font-size:11px;color:#666;margin-bottom:4px}
.stat-card .val{font-size:24px;font-weight:700;line-height:1.1}
.green{color:#26a69a}.red{color:#ef5350}.yellow{color:#ffd700}.blue{color:#5ba4f5}
.section{padding:14px 14px 0}
.sec-title{font-size:12px;font-weight:600;color:#777;letter-spacing:.8px;text-transform:uppercase;margin-bottom:10px}
/* BTC + F&G row */
.info-row{display:grid;grid-template-columns:1fr 1fr;gap:10px;padding:14px 14px 0}
.info-box{background:#161616;border:1px solid #222;border-radius:14px;padding:13px 14px}
.info-box .ib-title{font-size:11px;color:#666;margin-bottom:6px}
.fg-val{font-size:28px;font-weight:700;line-height:1}
.fg-label{font-size:12px;margin-top:4px;font-weight:600}
.fg-bar{height:5px;border-radius:3px;margin-top:8px;background:#222}
.fg-fill{height:100%;border-radius:3px;transition:width .5s}
.btc-trend{font-size:14px;font-weight:700;margin-bottom:4px}
.btc-sub{font-size:11px;color:#666;line-height:1.5}
/* Recommendations */
.rec-row{display:flex;gap:8px;overflow-x:auto;padding-bottom:4px}
.rec-chip{flex-shrink:0;background:#161616;border:1px solid #222;border-radius:10px;padding:8px 12px;font-size:12px;font-weight:600;white-space:nowrap}
.rec-chip.long{border-color:#26a69a40;color:#26a69a}
.rec-chip.short{border-color:#ef535040;color:#ef5350}
/* Position cards */
.pos-card{background:#161616;border:1px solid #252525;border-radius:16px;padding:14px;margin-bottom:10px}
.pos-head{display:flex;justify-content:space-between;align-items:center;margin-bottom:12px}
.pos-sym{font-size:17px;font-weight:700}
.badge{font-size:11px;font-weight:600;padding:3px 10px;border-radius:20px}
.badge.long{background:#0d2b1f;color:#26a69a}
.badge.short{background:#2b0d0d;color:#ef5350}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.info-item{background:#1c1c1c;border-radius:10px;padding:9px 11px}
.info-item .k{font-size:10px;color:#555;margin-bottom:3px}
.info-item .v{font-size:13px;font-weight:600}
.tp-row{display:flex;gap:6px;margin-top:8px}
.tp-chip{flex:1;background:#1c1c1c;border-radius:10px;padding:7px 6px;text-align:center}
.tp-chip .tl{font-size:10px;color:#555}
.tp-chip .tv{font-size:12px;font-weight:600;margin-top:2px}
.tp-chip.hit{border:1px solid #26a69a40;background:#0d2b1f}
.tp-chip.hit .tv{color:#26a69a}
.reason-box{margin-top:8px;background:#1c1c1c;border-radius:10px;padding:9px 11px;font-size:11px;color:#999;line-height:1.55}
.chart-btn{margin-top:10px;width:100%;background:#1c1c1c;border:1px dashed #333;border-radius:10px;padding:9px;font-size:12px;color:#888;cursor:pointer;-webkit-tap-highlight-color:transparent;display:flex;align-items:center;justify-content:center;gap:6px}
.chart-btn.has-chart{border-color:#5ba4f540;color:#5ba4f5;background:#0d1f33}
.chart-upload-btn{width:100%;background:#1e3a5f;color:#5ba4f5;border:none;border-radius:10px;padding:11px;font-size:13px;font-weight:600;cursor:pointer;margin-top:8px}
/* Trade cards grid */
.trades-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:8px}
.trade-mini{background:#161616;border:1px solid #222;border-radius:12px;padding:11px 12px;cursor:pointer;-webkit-tap-highlight-color:transparent;transition:border-color .15s}
.trade-mini:active{border-color:#444}
.trade-mini .tm-sym{font-size:13px;font-weight:700;margin-bottom:4px}
.trade-mini .tm-pnl{font-size:16px;font-weight:700}
.trade-mini .tm-pnl.pos{color:#26a69a}
.trade-mini .tm-pnl.neg{color:#ef5350}
.trade-mini .tm-meta{font-size:10px;color:#555;margin-top:4px}
.empty{text-align:center;padding:36px 16px;color:#444;font-size:13px}
/* Modal */
.modal-overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.85);z-index:200;align-items:flex-end;justify-content:center}
.modal-overlay.show{display:flex}
.modal{background:#141414;border-radius:20px 20px 0 0;width:100%;max-width:560px;max-height:82vh;overflow-y:auto;padding:20px 16px 36px}
.modal-title{font-size:15px;font-weight:700;margin-bottom:16px;display:flex;justify-content:space-between;align-items:center}
.modal-close{background:none;border:none;color:#666;font-size:24px;cursor:pointer;line-height:1}
.mdet-row{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:14px}
.mdet-sym{font-size:20px;font-weight:700}
.mdet-pnl{font-size:22px;font-weight:700}
.mdet-pnl.pos{color:#26a69a}.mdet-pnl.neg{color:#ef5350}
.mdet-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:12px}
.mdet-item{background:#1c1c1c;border-radius:10px;padding:9px 11px}
.mdet-item .k{font-size:10px;color:#555;margin-bottom:3px}
.mdet-item .v{font-size:13px;font-weight:600}
.mdet-reason{background:#1c1c1c;border-radius:10px;padding:11px 13px;font-size:12px;color:#aaa;line-height:1.6}
.upd{font-size:11px;color:#333;text-align:center;padding:14px}
</style>
</head>
<body>
<div class="header">
  <h1>🧠 Sam 交易</h1>
  <button class="refresh-btn" onclick="loadAll()">↺ 刷新</button>
</div>
<div id="root"><div class="empty">載入中...</div></div>
<div class="upd" id="upd"></div>

<!-- Trade Detail Modal -->
<div class="modal-overlay" id="tradeModal" onclick="closeMod(event)">
  <div class="modal">
    <div class="modal-title">交易詳情 <button class="modal-close" onclick="closeMod()">×</button></div>
    <div id="modalBody"></div>
  </div>
</div>


<script>
let _allTrades=[], _s99={};
function closeMod(e){
  if(!e||e.target===document.getElementById('tradeModal'))
    document.getElementById('tradeModal').classList.remove('show');
}
function openTrade(idx){
  const t=_allTrades[idx];
  if(!t)return;
  const pnl=t.pnl||0,side=t.side==='long'?'多':'空';
  const entered=t.entered_at?new Date(t.entered_at).toLocaleString('zh-TW'):'--';
  const exited=t.exited_at?new Date(t.exited_at).toLocaleString('zh-TW'):'進行中';
  const outcome=t.outcome||'--';
  document.getElementById('modalBody').innerHTML=`
    <div class="mdet-row">
      <span class="mdet-sym">${t.symbol||'--'} <span style="font-size:13px;font-weight:400;color:#666">${side}</span></span>
      <span class="mdet-pnl ${pnl>=0?'pos':'neg'}">${pnl>=0?'+':''}$${Number(pnl).toFixed(2)}</span>
    </div>
    <div class="mdet-grid">
      <div class="mdet-item"><div class="k">進場價</div><div class="v">${fmt(t.entry_px,5)}</div></div>
      <div class="mdet-item"><div class="k">出場價</div><div class="v">${fmt(t.exit_px,5)}</div></div>
      <div class="mdet-item"><div class="k">止損價</div><div class="v">${fmt(t.sl_price,5)}</div></div>
      <div class="mdet-item"><div class="k">名義金額</div><div class="v">$${Number(t.notional||0).toFixed(2)}</div></div>
      <div class="mdet-item"><div class="k">進場時間</div><div class="v" style="font-size:11px">${entered}</div></div>
      <div class="mdet-item"><div class="k">出場時間</div><div class="v" style="font-size:11px">${exited}</div></div>
      <div class="mdet-item"><div class="k">出場原因</div><div class="v">${t.exit_why||'--'}</div></div>
      <div class="mdet-item"><div class="k">R倍數</div><div class="v">${t.pnl_r!=null?Number(t.pnl_r).toFixed(2)+'R':'--'}</div></div>
    </div>
    ${t.reason?`<div class="mdet-reason">📌 ${t.reason}</div>`:''}
  `;
  document.getElementById('tradeModal').classList.add('show');
}
function fmt(n,d=4){if(n==null)return'--';return Number(n).toFixed(d);}
function fgColor(v){
  if(v<=25)return'#ef5350';
  if(v<=45)return'#ff9800';
  if(v<=55)return'#ffd700';
  if(v<=75)return'#8bc34a';
  return'#26a69a';
}
function fgLabel(v){
  if(v<=25)return'極度恐懼';
  if(v<=45)return'恐懼';
  if(v<=55)return'中性';
  if(v<=75)return'貪婪';
  return'極度貪婪';
}
async function loadFG(){
  try{
    const r=await fetch('https://api.alternative.me/fng/?limit=1');
    const j=await r.json();
    const v=parseInt(j.data[0].value);
    const c=fgColor(v),lb=fgLabel(v);
    return{v,c,lb};
  }catch{return{v:null,c:'#666',lb:'--'};}
}
let _btcTrendCache={trend:'載入中',sub:'',color:'#888'};
async function loadBtcTrend(){
  try{
    const r=await fetch('/api/btc/trend');
    _btcTrendCache=await r.json();
  }catch{}
  return _btcTrendCache;
}
function coinRecs(s99){
  const sigs=s99.signals||{};
  const longs=[],shorts=[];
  Object.entries(sigs).forEach(([sym,s])=>{
    const clean=sym.replace('/USDT:USDT','');
    if(clean==='BTC')return;
    if(s.stage==='pulled_back')longs.push(clean);
    else if(s.stage==='first_broke')longs.push(clean+'*');
    if(s.stage_s==='pulled_back_s')shorts.push(clean);
    else if(s.stage_s==='first_broke_s')shorts.push(clean+'*');
  });
  return{longs,shorts};
}
async function loadAll(){
  const [samRes,fgRes,btcTrend,s99Res]=await Promise.all([
    fetch('/api/sam/dashboard').then(r=>r.json()).catch(()=>({})),
    loadFG(),
    loadBtcTrend(),
    fetch('/api/99/state').then(r=>r.json()).catch(()=>({}))
  ]);
  _s99=s99Res;
  _allTrades=(samRes.trades||[]).slice().reverse();
  renderPage(samRes,fgRes,btcTrend);
  document.getElementById('upd').textContent='更新：'+new Date().toLocaleTimeString('zh-TW');
}
function renderPage(d,fg,btc){
  const state=d.state||{};
  const trades=d.trades||[];
  const pos=state.positions||{};
  const cap=state.capital||0,equity=state.equity||0;
  const closed=trades.filter(t=>t.pnl!=null);
  const wins=closed.filter(t=>t.pnl>0);
  const wr=closed.length>0?(wins.length/closed.length*100).toFixed(1):'--';
  const totalPnl=closed.reduce((s,t)=>s+(t.pnl||0),0);
  let html='';
  // Stats
  html+=`<div class="stats-row">
    <div class="stat-card"><div class="lbl">資金</div><div class="val yellow">$${Number(cap).toFixed(2)}</div></div>
    <div class="stat-card"><div class="lbl">淨值</div><div class="val ${equity>=cap?'green':'red'}">$${Number(equity).toFixed(2)}</div></div>
    <div class="stat-card"><div class="lbl">勝率</div><div class="val ${parseFloat(wr)>=50?'green':'red'}">${wr}%</div></div>
    <div class="stat-card"><div class="lbl">累計盈虧</div><div class="val ${totalPnl>=0?'green':'red'}">${totalPnl>=0?'+':''}$${totalPnl.toFixed(2)}</div></div>
  </div>`;
  // F&G + BTC trend
  const fgVal=fg.v!=null?fg.v:'--';
  const fgC=fg.c,fgLb=fg.lb;
  const btcSub=btc.sub||(btc.ema20?`EMA20: ${btc.ema20.toLocaleString()} / EMA50: ${btc.ema50.toLocaleString()}`:'');
  html+=`<div class="info-row">
    <div class="info-box">
      <div class="ib-title">😱 恐懼貪婪指數</div>
      <div class="fg-val" style="color:${fgC}">${fgVal}</div>
      <div class="fg-label" style="color:${fgC}">${fgLb}</div>
      <div class="fg-bar"><div class="fg-fill" style="width:${fgVal}%;background:${fgC}"></div></div>
    </div>
    <div class="info-box">
      <div class="ib-title">₿ BTC 4H 走勢</div>
      <div class="btc-trend" style="color:${btc.color||'#888'}">${btc.trend||'--'}</div>
      <div class="btc-sub">${btcSub}</div>
    </div>
  </div>`;
  // Coin recommendations
  const {longs,shorts}=coinRecs(_s99||{});
  if(longs.length||shorts.length){
    html+='<div class="section"><div class="sec-title">99戰法訊號推薦</div><div class="rec-row">';
    longs.forEach(c=>{html+=`<div class="rec-chip long">📈 ${c}</div>`;});
    shorts.forEach(c=>{html+=`<div class="rec-chip short">📉 ${c}</div>`;});
    html+='</div></div>';
  }
  // Positions
  html+='<div class="section"><div class="sec-title">Sam 持倉中</div>';
  const posArr=Object.values(pos);
  if(!posArr.length){html+='<div class="empty">目前無持倉</div>';}
  else posArr.forEach(p=>{
    const isLong=p.side==='long';
    const ep=p.entry_px||0,sl=p.sl_price||0;
    html+=`<div class="pos-card">
      <div class="pos-head">
        <span class="pos-sym">${p.symbol||'--'}</span>
        <span class="badge ${p.side}">${isLong?'多':'空'} ${p.leverage||1}x</span>
      </div>
      <div class="grid2">
        <div class="info-item"><div class="k">進場價</div><div class="v">${fmt(ep,5)}</div></div>
        <div class="info-item"><div class="k">止損價</div><div class="v" style="color:${isLong?'#ef5350':'#26a69a'}">${fmt(sl,5)}</div></div>
        <div class="info-item"><div class="k">保證金</div><div class="v">$${((p.notional||0)/(p.leverage||1)).toFixed(2)}</div></div>
        <div class="info-item"><div class="k">風險金額</div><div class="v">$${Number(p.risk_amt||0).toFixed(2)}</div></div>
      </div>
      <div class="tp-row">
        <div class="tp-chip${(p.tp_stage||1)>=2?' hit':''}"><div class="tl">TP1</div><div class="tv">${fmt(p.tp1_price,5)}</div></div>
        <div class="tp-chip${(p.tp_stage||1)>=3?' hit':''}"><div class="tl">TP2</div><div class="tv">${fmt(p.tp2_price,5)}</div></div>
        <div class="tp-chip${(p.tp_stage||1)>=4?' hit':''}"><div class="tl">TP3</div><div class="tv">${fmt(p.tp3_price,5)}</div></div>
      </div>
      ${(p.partial_pnl||0)!==0?`<div class="pnl-bar ${(p.partial_pnl||0)>=0?'pos':'neg'}"><span class="pl">已實現盈虧</span><span class="pv">${(p.partial_pnl||0)>=0?'+':''}$${Number(p.partial_pnl||0).toFixed(2)}</span></div>`:''}
      ${p.reason?`<div class="reason-box">${p.reason}</div>`:''}
    </div>`;
  });
  html+='</div>';
  // Link to trades page
  const wins2=closed.filter(t=>t.pnl>0).length;
  const losses2=closed.filter(t=>t.pnl<=0).length;
  html+=`<div class="section">
    <a href="/sam-trades" style="display:block;background:#161616;border:1px solid #2a2a2a;border-radius:14px;padding:16px;text-decoration:none;color:inherit">
      <div style="display:flex;justify-content:space-between;align-items:center">
        <div>
          <div style="font-size:13px;font-weight:600;margin-bottom:4px">📋 交易紀錄</div>
          <div style="font-size:12px;color:#666">共 ${closed.length} 筆 · ✅${wins2} ❌${losses2}</div>
        </div>
        <div style="font-size:20px;color:#444">›</div>
      </div>
    </a>
  </div>`;
  document.getElementById('root').innerHTML=html;
}
loadAll();
setInterval(loadAll,15000);
</script>
</body>
</html>"""

SAM_TRADES_HTML = r"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"/>
<title>Sam 交易紀錄</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0d0d0d;color:#e8e8e8;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;padding-bottom:env(safe-area-inset-bottom,20px)}
.header{background:#111;padding:14px 16px;display:flex;align-items:center;gap:12px;border-bottom:1px solid #222;position:sticky;top:0;z-index:100}
.back-btn{background:none;border:none;color:#5ba4f5;font-size:17px;cursor:pointer;padding:2px 0;-webkit-tap-highlight-color:transparent}
.header h1{font-size:17px;font-weight:700;flex:1}
.filter-row{display:flex;gap:8px;padding:12px 14px;overflow-x:auto}
.filter-btn{flex-shrink:0;padding:6px 14px;border-radius:20px;border:1px solid #2a2a2a;background:#161616;color:#888;font-size:12px;cursor:pointer;-webkit-tap-highlight-color:transparent;transition:all .15s}
.filter-btn.active{background:#1e3a5f;border-color:#5ba4f5;color:#5ba4f5}
.summary-row{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;padding:0 14px 4px}
.sum-box{background:#161616;border:1px solid #222;border-radius:12px;padding:10px 12px;text-align:center}
.sum-box .sl{font-size:10px;color:#666;margin-bottom:3px}
.sum-box .sv{font-size:16px;font-weight:700}
.green{color:#26a69a}.red{color:#ef5350}.yellow{color:#ffd700}
.trades-list{padding:10px 14px 0}
.trade-card{background:#161616;border:1px solid #222;border-radius:14px;padding:13px 14px;margin-bottom:8px;cursor:pointer;-webkit-tap-highlight-color:transparent;transition:border-color .15s}
.trade-card:active{border-color:#444}
.trade-head{display:flex;justify-content:space-between;align-items:center;margin-bottom:6px}
.trade-sym{font-size:15px;font-weight:700}
.trade-pnl{font-size:16px;font-weight:700}
.trade-pnl.pos{color:#26a69a}.trade-pnl.neg{color:#ef5350}
.trade-meta{display:flex;gap:10px;font-size:11px;color:#555;flex-wrap:wrap}
.meta-chip{background:#1c1c1c;border-radius:6px;padding:2px 7px}
.empty{text-align:center;padding:36px 16px;color:#444;font-size:13px}
/* Detail modal */
.modal-overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.85);z-index:200;align-items:flex-end;justify-content:center}
.modal-overlay.show{display:flex}
.modal{background:#141414;border-radius:20px 20px 0 0;width:100%;max-width:560px;max-height:85vh;overflow-y:auto;padding:20px 16px 36px}
.modal-title{font-size:15px;font-weight:700;margin-bottom:16px;display:flex;justify-content:space-between;align-items:center}
.modal-close{background:none;border:none;color:#666;font-size:24px;cursor:pointer;line-height:1}
.mdet-sym{font-size:22px;font-weight:700;margin-bottom:4px}
.mdet-pnl{font-size:26px;font-weight:700;margin-bottom:14px}
.mdet-pnl.pos{color:#26a69a}.mdet-pnl.neg{color:#ef5350}
.mdet-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:12px}
.mdet-item{background:#1c1c1c;border-radius:10px;padding:9px 11px}
.mdet-item .k{font-size:10px;color:#555;margin-bottom:3px}
.mdet-item .v{font-size:13px;font-weight:600}
.mdet-reason{background:#1c1c1c;border-radius:10px;padding:12px 13px;font-size:12px;color:#aaa;line-height:1.65}
.upd{font-size:11px;color:#333;text-align:center;padding:14px}
</style>
</head>
<body>
<div class="header">
  <button class="back-btn" onclick="history.back()">‹ 返回</button>
  <h1>Sam 交易紀錄</h1>
</div>

<div class="filter-row" id="filterRow">
  <button class="filter-btn active" onclick="setFilter('all',this)">全部</button>
  <button class="filter-btn" onclick="setFilter('win',this)">獲利</button>
  <button class="filter-btn" onclick="setFilter('loss',this)">虧損</button>
  <button class="filter-btn" onclick="setFilter('long',this)">做多</button>
  <button class="filter-btn" onclick="setFilter('short',this)">做空</button>
</div>

<div id="summary" class="summary-row"></div>
<div id="list" class="trades-list"><div class="empty">載入中...</div></div>
<div class="upd" id="upd"></div>

<div class="modal-overlay" id="modal" onclick="closeMod(event)">
  <div class="modal">
    <div class="modal-title">交易詳情 <button class="modal-close" onclick="closeMod()">×</button></div>
    <div id="modalBody"></div>
  </div>
</div>

<script>
let _trades=[], _filter='all';
function closeMod(e){if(!e||e.target===document.getElementById('modal'))document.getElementById('modal').classList.remove('show');}
function setFilter(f,btn){
  _filter=f;
  document.querySelectorAll('.filter-btn').forEach(b=>b.classList.remove('active'));
  btn.classList.add('active');
  renderList();
}
function fmt(n,d=4){if(n==null)return'--';return Number(n).toFixed(d);}
function openDetail(i){
  const filtered=getFiltered();
  const t=filtered[i];
  if(!t)return;
  const pnl=t.pnl||0,side=t.side==='long'?'做多':'做空';
  const entered=t.entered_at?new Date(t.entered_at).toLocaleString('zh-TW'):'--';
  const exited=t.exited_at?new Date(t.exited_at).toLocaleString('zh-TW'):'--';
  document.getElementById('modalBody').innerHTML=`
    <div class="mdet-sym">${t.symbol||'--'} <span style="font-size:13px;font-weight:400;color:#666">${side}</span></div>
    <div class="mdet-pnl ${pnl>=0?'pos':'neg'}">${pnl>=0?'+':''}$${Number(pnl).toFixed(2)}</div>
    <div class="mdet-grid">
      <div class="mdet-item"><div class="k">進場價</div><div class="v">${fmt(t.entry_px,5)}</div></div>
      <div class="mdet-item"><div class="k">出場價</div><div class="v">${fmt(t.exit_px,5)}</div></div>
      <div class="mdet-item"><div class="k">止損價</div><div class="v">${fmt(t.sl_price,5)}</div></div>
      <div class="mdet-item"><div class="k">TP1</div><div class="v">${fmt(t.tp1_price,5)}</div></div>
      <div class="mdet-item"><div class="k">名義金額</div><div class="v">$${Number(t.notional||0).toFixed(2)}</div></div>
      <div class="mdet-item"><div class="k">風險金額</div><div class="v">$${Number(t.risk_amt||0).toFixed(2)}</div></div>
      <div class="mdet-item"><div class="k">R 倍數</div><div class="v">${t.pnl_r!=null?Number(t.pnl_r).toFixed(2)+'R':'--'}</div></div>
      <div class="mdet-item"><div class="k">結果</div><div class="v" style="color:${pnl>=0?'#26a69a':'#ef5350'}">${t.outcome||'--'}</div></div>
      <div class="mdet-item"><div class="k">進場時間</div><div class="v" style="font-size:10px">${entered}</div></div>
      <div class="mdet-item"><div class="k">出場原因</div><div class="v">${t.exit_why||'--'}</div></div>
    </div>
    ${t.reason?`<div class="mdet-reason">📌 進場邏輯：${t.reason}</div>`:''}
  `;
  document.getElementById('modal').classList.add('show');
}
function getFiltered(){
  if(_filter==='win')return _trades.filter(t=>(t.pnl||0)>0);
  if(_filter==='loss')return _trades.filter(t=>(t.pnl||0)<=0);
  if(_filter==='long')return _trades.filter(t=>t.side==='long');
  if(_filter==='short')return _trades.filter(t=>t.side==='short');
  return _trades;
}
function renderList(){
  const list=getFiltered();
  const totalPnl=list.reduce((s,t)=>s+(t.pnl||0),0);
  const wins=list.filter(t=>(t.pnl||0)>0).length;
  const wr=list.length>0?(wins/list.length*100).toFixed(0)+'%':'--';
  document.getElementById('summary').innerHTML=`
    <div class="sum-box"><div class="sl">筆數</div><div class="sv">${list.length}</div></div>
    <div class="sum-box"><div class="sl">勝率</div><div class="sv ${parseFloat(wr)>=50?'green':'red'}">${wr}</div></div>
    <div class="sum-box"><div class="sl">總盈虧</div><div class="sv ${totalPnl>=0?'green':'red'}">${totalPnl>=0?'+':''}$${totalPnl.toFixed(2)}</div></div>
  `;
  if(!list.length){document.getElementById('list').innerHTML='<div class="empty">無符合記錄</div>';return;}
  let h='';
  list.forEach((t,i)=>{
    const pnl=t.pnl||0,side=t.side==='long'?'多':'空';
    const dt=t.exited_at?new Date(t.exited_at).toLocaleDateString('zh-TW',{month:'2-digit',day:'2-digit'}):'--';
    const r=t.pnl_r!=null?Number(t.pnl_r).toFixed(1)+'R':'';
    h+=`<div class="trade-card" onclick="openDetail(${i})">
      <div class="trade-head">
        <span class="trade-sym">${t.symbol||'--'} <span style="font-size:11px;font-weight:400;color:#555">${side}</span></span>
        <span class="trade-pnl ${pnl>=0?'pos':'neg'}">${pnl>=0?'+':''}$${Number(pnl).toFixed(2)}</span>
      </div>
      <div class="trade-meta">
        <span class="meta-chip">${dt}</span>
        ${r?`<span class="meta-chip">${r}</span>`:''}
        <span class="meta-chip">${t.exit_why||'--'}</span>
      </div>
    </div>`;
  });
  document.getElementById('list').innerHTML=h;
}
async function loadTrades(){
  try{
    const r=await fetch('/api/sam/dashboard');
    const d=await r.json();
    _trades=(d.trades||[]).slice().reverse();
    renderList();
    document.getElementById('upd').textContent='更新：'+new Date().toLocaleTimeString('zh-TW');
  }catch{document.getElementById('list').innerHTML='<div class="empty">載入失敗</div>';}
}
loadTrades();
setInterval(loadTrades,30000);
</script>
</body>
</html>"""

CHARTS_DIR = BASE_DIR / "sam_99" / "position_charts"
CHARTS_DIR.mkdir(exist_ok=True)
CHARTS_META = CHARTS_DIR / "meta.json"

def _charts_meta() -> dict:
    try:
        return json.loads(CHARTS_META.read_text())
    except Exception:
        return {}

def _charts_meta_save(m: dict):
    CHARTS_META.write_text(json.dumps(m, ensure_ascii=False, indent=2))

@app.route("/api/position/chart", methods=["POST"])
def upload_position_chart():
    """上傳持倉K線截圖  form: symbol, source(99|sam), file"""
    sym    = request.form.get("symbol", "").strip().replace("/", "_").replace(":", "_")
    source = request.form.get("source", "99")
    note   = request.form.get("note", "")
    if not sym:
        return jsonify({"error": "symbol required"}), 400
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "file required"}), 400
    ext = Path(f.filename).suffix.lower() if f.filename else ".jpg"
    if ext not in (".jpg", ".jpeg", ".png", ".webp"):
        ext = ".jpg"
    ts    = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    fname = f"{sym}_{ts}{ext}"
    f.save(str(CHARTS_DIR / fname))
    meta = _charts_meta()
    key  = f"{source}:{sym}"
    meta.setdefault(key, []).append({
        "filename": fname, "uploaded_at": ts, "note": note
    })
    _charts_meta_save(meta)
    return jsonify({"ok": True, "filename": fname})

@app.route("/api/position/charts/<source>/<sym>")
def list_position_charts(source, sym):
    """列出某幣種的所有圖片"""
    meta = _charts_meta()
    sym_key = sym.replace("/", "_").replace(":", "_")
    items = meta.get(f"{source}:{sym_key}", [])
    return jsonify({"charts": items})

@app.route("/position-chart/<filename>")
def serve_position_chart(filename):
    return send_from_directory(str(CHARTS_DIR), filename)

@app.route("/api/position/chartmeta")
def api_chart_meta():
    return jsonify(_charts_meta())

@app.route("/api/99/state")
def api_99_state():
    state_file = BASE_DIR / "sam_99" / "strategy99_state.json"
    try:
        return jsonify(json.loads(state_file.read_text()))
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/sam/dashboard")
def api_sam_dashboard():
    brain_dir = BASE_DIR / "sam_99" / "brain"
    state, trades = {}, []
    try:
        state = json.loads((brain_dir / "sams_brain_state.json").read_text())
    except Exception:
        pass
    try:
        trades = json.loads((brain_dir / "sams_brain_trades.json").read_text())
    except Exception:
        pass
    return jsonify({"state": state, "trades": trades})

@app.route("/api/btc/trend")
def api_btc_trend():
    """BTC 4H 趨勢分析（SAM大方向，不依賴99戰法訊號）"""
    try:
        import ccxt, pandas as pd
        exchange = ccxt.bybit({"options": {"defaultType": "future"}})
        ohlcv = exchange.fetch_ohlcv("BTC/USDT:USDT", "4h", limit=60)
        df = pd.DataFrame(ohlcv, columns=["ts","open","high","low","close","volume"])
        closes = df["close"]
        ema20 = float(closes.ewm(span=20).mean().iloc[-1])
        ema50 = float(closes.ewm(span=50).mean().iloc[-1])
        cur   = float(closes.iloc[-1])
        prev  = float(closes.iloc[-2])
        # 方向判斷
        if cur > ema20 > ema50:
            trend = "強勢多頭"
            sub   = f"價格站上 EMA20/50 ▸ {cur:,.0f}"
            color = "#26a69a"
            icon  = "📈"
        elif cur > ema20 and cur < ema50:
            trend = "反彈中"
            sub   = f"站上 EMA20 但低於 EMA50 ▸ {cur:,.0f}"
            color = "#5ba4f5"
            icon  = "↗️"
        elif cur < ema20 < ema50:
            trend = "強勢空頭"
            sub   = f"價格低於 EMA20/50 ▸ {cur:,.0f}"
            color = "#ef5350"
            icon  = "📉"
        elif cur < ema20 and cur > ema50:
            trend = "回調中"
            sub   = f"低於 EMA20 但守住 EMA50 ▸ {cur:,.0f}"
            color = "#ef9a9a"
            icon  = "↘️"
        else:
            trend = "盤整"
            sub   = f"EMA20/50 糾纏 ▸ {cur:,.0f}"
            color = "#888"
            icon  = "↔️"
        return jsonify({
            "trend": icon + " " + trend, "sub": sub, "color": color,
            "price": round(cur, 2), "ema20": round(ema20, 2), "ema50": round(ema50, 2)
        })
    except Exception as e:
        return jsonify({"trend": "無法取得", "sub": str(e), "color": "#888", "price": 0, "ema20": 0, "ema50": 0})

@app.route("/99")
def strategy99_page():
    return Response(STRATEGY99_HTML, mimetype="text/html")

@app.route("/sam2")
def sam_dashboard_page():
    return Response(SAM_DASHBOARD_HTML, mimetype="text/html")

@app.route("/sam-trades")
def sam_trades_page():
    return Response(SAM_TRADES_HTML, mimetype="text/html")

@app.route("/99-signals")
def signals99_page():
    return Response(SIGNALS99_HTML, mimetype="text/html")

@app.route("/api/sam/thinking")
def api_sam_thinking():
    """sam 最近的思考快照"""
    thinking_file = Path(__file__).parent / "brain" / "sams_thinking_log.json"
    sam_state_file = Path(__file__).parent / "sam_99" / "brain" / "sams_brain_state.json"
    logs = []
    state = {}
    if thinking_file.exists():
        try:
            logs = json.loads(thinking_file.read_text())
        except Exception:
            pass
    if sam_state_file.exists():
        try:
            state = json.loads(sam_state_file.read_text())
        except Exception:
            pass
    return jsonify({"thinking": list(reversed(logs[-20:])), "state": state})

@app.route("/sam")
def sam_thinking_page():
    return Response(SAM_THINKING_HTML, mimetype="text/html")

@app.route("/")
def index():
    return Response(HTML, mimetype="text/html")

@app.route("/manifest.json")
def manifest():
    m = {
        "name": "投資組合",
        "short_name": "投資組合",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#0d0d0d",
        "theme_color": "#0d0d0d",
        "icons": [{"src": "/icon.png", "sizes": "192x192", "type": "image/png"}]
    }
    return jsonify(m)

def _build_crypto_data() -> dict:
    """組合 /api/crypto 的數據（供 JSONBin 同步用）"""
    import threading
    ema_state   = load_bot_state()
    nfes_state  = load_nfes_state()
    live_prices = get_live_prices()

    ema_positions = _build_positions(
        ema_state.get("positions", {}), strategy="EMA99", live_prices=live_prices)
    nfes_positions = _build_positions(
        nfes_state.get("positions", {}),
        strategy=nfes_state.get("strategy", "NFES 強化版"), live_prices=live_prices)

    all_trades = []
    for t in ema_state.get("trades", []):
        tc = dict(t); tc["strategy"] = tc.get("strategy", "EMA99"); all_trades.append(tc)
    for t in nfes_state.get("trades", []):
        tc = dict(t); tc["strategy"] = tc.get("strategy", "NFES 強化版"); all_trades.append(tc)
    recent = sorted(all_trades, key=lambda x: x.get("exit_ts", ""))

    ema_capital = ema_state.get("capital", 0)
    _record_daily_equity(ema_capital)
    equity_curve = _get_equity_curve(ema_capital)
    total_pnl = round(ema_capital - INITIAL_CAPITAL, 2)
    last_run = max(
        ema_state.get("last_run", ""), nfes_state.get("last_run", "")
    ) or ema_state.get("last_run", "")

    return {
        "positions"    : ema_positions + nfes_positions,
        "capital"      : round(ema_capital, 2),
        "last_run"     : last_run,
        "recent_trades": recent,
        "equity_curve" : equity_curve,
        "total_pnl"    : total_pnl,
        "source"       : "local",
    }


def _jsonbin_sync_loop():
    """背景執行緒：每 60 秒把最新狀態推送到 GitHub Gist，讓 Render 可以讀到"""
    import threading
    if not (GITHUB_TOKEN and GIST_ID):
        return
    def loop():
        while True:
            try:
                data = _build_crypto_data()
                ok = _gist_put(data)
                print(f"[Gist sync] {'✅ 推送成功' if ok else '❌ 推送失敗'} capital={data.get('capital')}")
            except Exception as e:
                print(f"[Gist sync] 錯誤: {e}")
            time.sleep(60)
    t = threading.Thread(target=loop, daemon=True, name="gist-sync")
    t.start()
    print("✅ GitHub Gist 同步執行緒已啟動（每 60 秒推送）")


if __name__ == "__main__":
    # ── 啟動前自動清除佔用 5050 的殘留程序 ──────────────────────
    import subprocess, signal as _signal, socket
    try:
        result = subprocess.run(
            ["lsof", "-ti", ":5050"], capture_output=True, text=True
        )
        pids = [p.strip() for p in result.stdout.strip().splitlines() if p.strip()]
        my_pid = str(os.getpid())
        for pid in pids:
            if pid != my_pid:
                try:
                    os.kill(int(pid), _signal.SIGTERM)
                    print(f"✅ 已清除佔用 port 5050 的程序 pid={pid}")
                    time.sleep(1)
                except Exception:
                    pass
    except Exception:
        pass

    print("=" * 50)
    print("📊 投資組合 App 啟動中...")
    print(f"   本機：http://localhost:5050")
    try:
        ip = socket.gethostbyname(socket.gethostname())
        print(f"   手機（同 WiFi）：http://{ip}:5050")
    except:
        pass
    print("=" * 50)
    _jsonbin_sync_loop()
    app.run(host="0.0.0.0", port=5050, debug=False)
