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
BOT_STATE_FILE  = BASE_DIR / "ema99_bot_state.json"
MANUAL_FILE     = BASE_DIR / "portfolio_manual.json"

# JSONBin — 讓 Render 雲端也能讀到 bot 狀態
JSONBIN_API_KEY = os.getenv("JSONBIN_API_KEY", "")
JSONBIN_BIN_ID  = os.getenv("JSONBIN_BIN_ID",  "")
# 手動持倉存在第二個 bin（可選，沒設就存本機 JSON）
JSONBIN_MANUAL_BIN_ID = os.getenv("JSONBIN_MANUAL_BIN_ID", "")

app = Flask(__name__, static_folder=str(BASE_DIR))

# ─────────────────────────────────────────────────────────────────
# JSONBin 工具
# ─────────────────────────────────────────────────────────────────
_jb_cache: dict = {}
_jb_cache_ts: float = 0.0

def _jsonbin_get(bin_id: str) -> dict | None:
    """讀取 JSONBin，cache 20 秒避免過度請求"""
    global _jb_cache, _jb_cache_ts
    cache_key = bin_id
    now = time.time()
    if cache_key in _jb_cache and now - _jb_cache_ts < 20:
        return _jb_cache[cache_key]
    try:
        r = _requests.get(
            f"https://api.jsonbin.io/v3/b/{bin_id}/latest",
            headers={"X-Master-Key": JSONBIN_API_KEY},
            timeout=8,
        )
        if r.ok:
            data = r.json().get("record", {})
            _jb_cache[cache_key] = data
            _jb_cache_ts = now
            return data
    except Exception:
        pass
    return None

def _jsonbin_put(bin_id: str, data: dict) -> bool:
    try:
        r = _requests.put(
            f"https://api.jsonbin.io/v3/b/{bin_id}",
            headers={"X-Master-Key": JSONBIN_API_KEY,
                     "Content-Type": "application/json"},
            json=data, timeout=8,
        )
        if r.ok:
            _jb_cache[bin_id] = data
            _jb_cache_ts = time.time()
        return r.ok
    except Exception:
        return False

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
    """把 'BTC/USDT:USDT' 變成 'BTC'"""
    return sym.split("/")[0]

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
    """'1000PEPE/USDT:USDT' → '1000PEPEUSDT'"""
    base = ccxt_sym.split("/")[0]
    return base + "USDT"


# ─────────────────────────────────────────────────────────────────
# API 路由
# ─────────────────────────────────────────────────────────────────

def load_bot_state() -> dict:
    """優先從 JSONBin 讀（雲端 Render 用），否則讀本機 JSON"""
    if JSONBIN_API_KEY and JSONBIN_BIN_ID:
        data = _jsonbin_get(JSONBIN_BIN_ID)
        if data:
            return data
    if BOT_STATE_FILE.exists():
        return json.loads(BOT_STATE_FILE.read_text())
    return {}

@app.route("/api/crypto")
def api_crypto():
    """讀取 bot state（本機或 JSONBin），回傳加密幣持倉"""
    state = load_bot_state()
    if not state:
        return jsonify({"positions": [], "capital": 0, "trades": [], "source": "none"})
    positions = state.get("positions", {})
    trades    = state.get("trades", [])
    capital   = state.get("capital", 0)
    last_run  = state.get("last_run", "")

    # 取得即時價格
    live_prices = get_live_prices()

    result = []
    for sym, p in positions.items():
        entry    = p.get("entry_px", 0)
        sl       = p.get("trail_sl") or p.get("sl", 0)
        margin   = p.get("margin", 0)
        lev      = p.get("lev", 1)
        notional = p.get("notional", margin)
        partial  = p.get("partial", False)
        entry_ts = p.get("entry_ts", "")

        # 即時價 > 存檔價，優先用即時
        bsym      = to_binance_sym(sym)
        live_cur  = live_prices.get(bsym)
        cur       = live_cur if live_cur else p.get("cur_px", entry)
        using_live = live_cur is not None

        pnl_pct = (cur - entry) / entry * 100 if entry else 0
        pnl_usd = (cur - entry) * p.get("qty", 0)
        sl_triggered = cur < sl  # 即時價跌破止損

        result.append({
            "sym"         : fmt_sym(sym),
            "full_sym"    : sym,
            "entry_px"    : round(entry, 8),
            "cur_px"      : round(cur, 8),
            "sl"          : round(sl, 8),
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

    # 近期交易（最新 10 筆）
    recent = sorted(trades, key=lambda x: x.get("exit_ts",""), reverse=True)[:10]
    for t in recent:
        t["sym"] = fmt_sym(t.get("sym",""))

    source = "jsonbin" if (JSONBIN_API_KEY and JSONBIN_BIN_ID) else "local"
    return jsonify({
        "positions"    : result,
        "capital"      : round(capital, 2),
        "last_run"     : last_run,
        "recent_trades": recent,
        "source"       : source,
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

</style>
</head>
<body>

<div class="header">
  <h1>📊 投資組合</h1>
  <button class="refresh-btn" onclick="loadAll()">↻ 更新</button>
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
let cryptoData  = { positions: [], capital: 0, recent_trades: [] };
let manualData  = { positions: [] };
let editingId   = null;

async function loadAll() {
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
  if (tab === 'trades') { el.innerHTML = renderTrades(); return; }

  let html = '';

  // 加密幣區塊
  if (tab === 'all' || tab === 'CRYPTO') {
    const positions = cryptoData.positions;
    html += `<div class="section">
      <div class="section-title"><span>🔷 加密幣持倉</span></div>`;
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

function renderCryptoCard(p) {
  const pnlColor   = p.pnl_pct >= 0 ? 'green' : 'red';
  const pnlSign    = p.pnl_pct >= 0 ? '▲' : '▼';
  const slPct      = p.entry_px ? ((p.sl - p.entry_px) / p.entry_px * 100).toFixed(1) : 0;
  const liveTag    = p.using_live ? '🟢 即時' : '🟡 存檔';
  const cardBorder = p.sl_triggered ? 'border:1px solid rgba(255,69,58,.5);' : '';
  return `<div class="pos-card" style="${cardBorder}">
    <div class="pos-top">
      <div class="pos-sym">${p.sym} ${p.sl_triggered ? '⚠️' : ''}</div>
      <span class="pos-badge badge-crypto">加密 ${p.lev}×</span>
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

function renderTrades() {
  const trades = cryptoData.recent_trades || [];
  let html = `<div class="section"><div class="section-title">📋 近期交易（加密）</div>`;
  if (!trades.length) {
    html += `<div class="empty"><div class="icon">📭</div><div>尚無交易記錄</div></div>`;
  } else {
    html += `<div class="pos-card">`;
    trades.forEach(t => {
      const sign = t.pnl >= 0 ? '+' : '';
      const col  = t.pnl >= 0 ? 'var(--green)' : 'var(--red)';
      const ts   = t.exit_ts ? new Date(t.exit_ts).toLocaleString('zh-TW', {month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}) : '';
      const reasonLabel = {
        'partial_tp':'部分止盈', 'stop_loss':'止損', 'trail_stop':'移動止損', 'breakeven':'保本出場'
      }[t.reason] || t.reason;
      html += `<div class="trade-row">
        <div>
          <div class="trade-sym">${t.sym}</div>
          <div class="trade-reason">${reasonLabel}</div>
        </div>
        <div>
          <div class="trade-pnl" style="color:${col}">${sign}$${fmt(t.pnl)}</div>
          <div class="trade-ts">${ts}</div>
        </div>
      </div>`;
    });
    html += `</div>`;
  }
  html += `</div>`;
  return html;
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

// 啟動
loadAll();
// 每 30 秒自動更新
setInterval(loadAll, 30000);
</script>
</body>
</html>"""

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

if __name__ == "__main__":
    print("=" * 50)
    print("📊 投資組合 App 啟動中...")
    print(f"   本機：http://localhost:5050")
    import socket
    try:
        ip = socket.gethostbyname(socket.gethostname())
        print(f"   手機（同 WiFi）：http://{ip}:5050")
    except:
        pass
    print("=" * 50)
    app.run(host="0.0.0.0", port=5050, debug=False)
