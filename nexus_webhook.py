#!/usr/bin/env python3
"""
Nexus Flow Elite System — TradingView Webhook Bot
───────────────────────────────────────────────────────────────
功能：
  ‧ 接收 TradingView Alert Webhook（Y.Algo 訊號）
  ‧ 支援 ▲+ / ▲ / ▼+ / ▼ 四種進場訊號
  ‧ 自動分批止盈 TP1/TP2/TP3（各 1/3 倉）+ SL 止損
  ‧ PAPER_MODE=True 時完全虛擬，不送出真實訂單
  ‧ Telegram 即時通知

環境變數（與 ema99_bot.py 共用同一個 .env）：
  BINANCE_API_KEY
  BINANCE_API_SECRET
  TELEGRAM_BOT_TOKEN
  TELEGRAM_CHAT_ID

啟動方式：
  python nexus_webhook.py
  預設監聽 http://0.0.0.0:5001/webhook

TradingView Alert Webhook URL：
  http://<你的IP或域名>:5001/webhook
  Header: X-Secret: <WEBHOOK_SECRET>

Payload 格式：
  {
    "symbol": "BTCUSDT",
    "side": "long",           // "long" or "short"
    "signal": "▲+",           // ▲+ / ▲ / ▼+ / ▼
    "entry": 79769.43,
    "tp1": 82976.68,
    "tp2": 85114.85,
    "tp3": 87253.01,
    "sl": 77631.26
  }
───────────────────────────────────────────────────────────────
"""
from __future__ import annotations
import json
import logging
import os
import time
from pathlib import Path
from datetime import datetime, timezone

# ── 自動載入 .env（與 ema99_bot.py 同目錄）─────────────────────
_env_file = Path(__file__).parent / ".env"
if _env_file.exists():
    for _line in _env_file.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

import ccxt
import requests
from flask import Flask, request, jsonify

# ═══════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════
PAPER_MODE = True       # ← 改 False 才送出真實訂單

BINANCE_KEY    = os.getenv("BINANCE_API_KEY",    "")
BINANCE_SECRET = os.getenv("BINANCE_API_SECRET", "")
TG_TOKEN       = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT_ID     = os.getenv("TELEGRAM_CHAT_ID",   "")

# Webhook 安全驗證（TradingView Alert Header 填這個）
WEBHOOK_SECRET = os.getenv("NEXUS_WEBHOOK_SECRET", "nexus_secret_2026")

# ── 資金設定 ──────────────────────────────────────────────────
# NFES 策略初始資金（USDT），用於計算每筆倉位大小
NFES_INITIAL_CAPITAL = float(os.getenv("NFES_INITIAL_CAPITAL", "10000"))

# 每筆最高保證金 = 總資金 × MAX_RISK_PCT（上限 10%）
MAX_RISK_PCT    = 0.10

# ── 訊號強度 → 槓桿對照表 ────────────────────────────────────
# ▲+ / ▼+（強訊號）→ 5x　▲ / ▼（普通訊號）→ 3x　其他 → 2x
SIGNAL_LEVERAGE: dict = {
    "▲+": 5,
    "▲" : 3,
    "▼+": 5,
    "▼" : 3,
}
DEFAULT_LEVERAGE = 2

# 向下相容：舊程式碼若 import USDT_PER_TRADE / LEVERAGE 不會報錯
USDT_PER_TRADE = 100.0   # deprecated — 請改用 calc_trade_params()
LEVERAGE       = 5        # deprecated

# TP 分批比例（三等分）
TP_RATIOS       = [1/3, 1/3, 1/3]

# ── 訊號過濾 ──────────────────────────────────────────────────
# 只接受 ▲+ / ▼+（積極訊號），若要接受全部訊號改為 False
AGGRESSIVE_ONLY = False

# ── 伺服器 ───────────────────────────────────────────────────
HOST = "0.0.0.0"
PORT = 5001

# ── 日誌 ─────────────────────────────────────────────────────
LOG_FILE = Path("nexus_webhook.log")
_log_fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
_fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
_fh.setFormatter(_log_fmt)
log = logging.getLogger("nexus")
log.setLevel(logging.INFO)
log.addHandler(_fh)
log.addHandler(logging.StreamHandler())

# ═══════════════════════════════════════════════════════════════
# TELEGRAM
# ═══════════════════════════════════════════════════════════════
def tg(msg: str):
    if not TG_TOKEN or not TG_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT_ID, "text": msg, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception as e:
        log.warning(f"Telegram 發送失敗: {e}")

# ═══════════════════════════════════════════════════════════════
# EXCHANGE
# ═══════════════════════════════════════════════════════════════
def get_exchange() -> ccxt.binanceusdm:
    exch = ccxt.binanceusdm({
        "apiKey": BINANCE_KEY,
        "secret": BINANCE_SECRET,
        "options": {"defaultType": "future"},
    })
    exch.load_markets()
    return exch

# ═══════════════════════════════════════════════════════════════
# 動態倉位計算
# ═══════════════════════════════════════════════════════════════
def _get_nfes_capital() -> float:
    """讀取 nfes_bot_state.json，計算當前可用資金 = 初始資金 + 已實現盈虧"""
    try:
        state_file = Path(__file__).parent / "nfes_bot_state.json"
        if not state_file.exists():
            return NFES_INITIAL_CAPITAL
        d = json.loads(state_file.read_text(encoding="utf-8"))
        realized = sum(t.get("pnl", 0) for t in d.get("trades", []))
        capital  = NFES_INITIAL_CAPITAL + realized
        return max(capital, 0.0)
    except Exception as e:
        log.warning(f"_get_nfes_capital 讀取失敗: {e}")
        return NFES_INITIAL_CAPITAL


def calc_trade_params(signal: str) -> tuple[float, int]:
    """
    依訊號強度與當前資金計算 (margin_usdt, leverage)。
    margin = min(total_capital × MAX_RISK_PCT, total_capital × MAX_RISK_PCT) 取整數
    leverage = 依 SIGNAL_LEVERAGE 對照表
    """
    capital  = _get_nfes_capital()
    margin   = round(capital * MAX_RISK_PCT, 2)   # 總資金 10%
    margin   = max(margin, 10.0)                   # 最低 10 USDT
    lev      = SIGNAL_LEVERAGE.get(signal, DEFAULT_LEVERAGE)
    log.info(f"calc_trade_params  signal={signal!r}  capital={capital:.2f}  "
             f"margin={margin:.2f}  lev={lev}x")
    return margin, lev


# ═══════════════════════════════════════════════════════════════
# 核心下單邏輯
# ═══════════════════════════════════════════════════════════════
def execute_signal(payload: dict):
    """
    收到 webhook payload 後執行進場 + 掛 TP/SL 單
    """
    symbol   = payload.get("symbol", "BTCUSDT").upper()
    side     = payload.get("side", "").lower()       # "long" or "short"
    signal   = payload.get("signal", "")
    entry    = float(payload.get("entry", 0))
    tp1      = float(payload.get("tp1", 0))
    tp2      = float(payload.get("tp2", 0))
    tp3      = float(payload.get("tp3", 0))
    sl       = float(payload.get("sl", 0))

    # ── 基本驗證 ─────────────────────────────────────────────
    if side not in ("long", "short"):
        log.warning(f"未知方向: {side}，忽略")
        return
    if entry <= 0 or sl <= 0:
        log.warning(f"無效價格: entry={entry}, sl={sl}，忽略")
        return

    # ── 訊號過濾 ─────────────────────────────────────────────
    if AGGRESSIVE_ONLY and "+" not in signal:
        log.info(f"非積極訊號 {signal}，略過（AGGRESSIVE_ONLY=True）")
        return

    tps = [tp1, tp2, tp3]
    ts  = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    tag = "🟢 LONG" if side == "long" else "🔴 SHORT"

    log.info(f"[{ts}] 收到訊號 {tag} {symbol} | signal={signal} entry={entry} TP1={tp1} TP2={tp2} TP3={tp3} SL={sl}")

    # ── 動態計算倉位：保證金 = 總資金×10%，槓桿依訊號強度 ────
    margin_usdt, lev = calc_trade_params(signal)
    notional  = margin_usdt * lev
    qty_total = round(notional / entry, 6)

    if PAPER_MODE:
        # ── 模擬模式 ────────────────────────────────────────
        paper_msg = (
            f"📋 <b>[PAPER] Nexus Webhook</b>\n"
            f"{tag} {symbol}  <code>{signal}</code>\n"
            f"Entry : <b>{entry:,.2f}</b>\n"
            f"TP1   : {tp1:,.2f}  TP2: {tp2:,.2f}  TP3: {tp3:,.2f}\n"
            f"SL    : {sl:,.2f}\n"
            f"Qty   : {qty_total}  保證金: {margin_usdt:.0f} USDT × {lev}x = {notional:.0f} USDT\n"
            f"時間  : {ts}"
        )
        log.info(f"[PAPER] {paper_msg}")
        tg(paper_msg)
        return

    # ── 真實下單 ─────────────────────────────────────────────
    try:
        exch = get_exchange()

        # 設定槓桿（動態）
        try:
            exch.set_leverage(lev, symbol)
        except Exception:
            pass  # 已設定或不支援時忽略

        # 1. 市價進場
        order_side = "buy" if side == "long" else "sell"
        entry_order = exch.create_market_order(symbol, order_side, qty_total)
        log.info(f"進場單: {entry_order}")

        # 稍等確認成交
        time.sleep(1)

        # 2. TP 分批 Limit 單（反向）
        tp_side = "sell" if side == "long" else "buy"
        tp_orders = []
        for i, (tp_price, ratio) in enumerate(zip(tps, TP_RATIOS), 1):
            if tp_price <= 0:
                continue
            tp_qty = round(qty_total * ratio, 6)
            o = exch.create_limit_order(
                symbol, tp_side, tp_qty, tp_price,
                params={"reduceOnly": True}
            )
            tp_orders.append(o)
            log.info(f"TP{i} 單: price={tp_price} qty={tp_qty} id={o['id']}")

        # 3. SL Stop-Market 單（反向）
        sl_side = "sell" if side == "long" else "buy"
        sl_order = exch.create_order(
            symbol, "stop_market", sl_side, qty_total, sl,
            params={"stopPrice": sl, "reduceOnly": True, "closePosition": True}
        )
        log.info(f"SL 單: stopPrice={sl} id={sl_order['id']}")

        # Telegram 通知
        msg = (
            f"✅ <b>Nexus 進場成功</b>\n"
            f"{tag} {symbol}  <code>{signal}</code>\n"
            f"Entry : <b>{entry:,.2f}</b>\n"
            f"TP1/2/3: {tp1:,.2f} / {tp2:,.2f} / {tp3:,.2f}\n"
            f"SL    : {sl:,.2f}\n"
            f"Qty   : {qty_total}  保證金: {margin_usdt:.0f} USDT × {lev}x = {notional:.0f} USDT\n"
            f"時間  : {ts}"
        )
        tg(msg)

    except Exception as e:
        err_msg = f"❌ Nexus 下單失敗: {e}"
        log.error(err_msg)
        tg(err_msg)


# ═══════════════════════════════════════════════════════════════
# FLASK WEBHOOK SERVER
# ═══════════════════════════════════════════════════════════════
app = Flask(__name__)

@app.route("/webhook", methods=["POST"])
def webhook():
    # ── 安全驗證 ─────────────────────────────────────────────
    secret = request.headers.get("X-Secret", "")
    if secret != WEBHOOK_SECRET:
        log.warning(f"Webhook 驗證失敗，IP: {request.remote_addr}")
        return jsonify({"error": "unauthorized"}), 401

    # ── 解析 payload ─────────────────────────────────────────
    try:
        payload = request.get_json(force=True)
    except Exception:
        return jsonify({"error": "invalid json"}), 400

    if not payload:
        return jsonify({"error": "empty payload"}), 400

    log.info(f"收到 Webhook: {json.dumps(payload, ensure_ascii=False)}")

    # 非同步執行（避免 TradingView 等 timeout）
    try:
        execute_signal(payload)
    except Exception as e:
        log.error(f"execute_signal 例外: {e}")
        return jsonify({"error": str(e)}), 500

    return jsonify({"status": "ok"}), 200


@app.route("/health", methods=["GET"])
def health():
    mode = "PAPER" if PAPER_MODE else "LIVE"
    return jsonify({
        "status": "running",
        "mode": mode,
        "time": datetime.now(timezone.utc).isoformat(),
    })


# ═══════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════
if __name__ == "__main__":
    mode_str = "🟡 PAPER MODE（模擬）" if PAPER_MODE else "🔴 LIVE MODE（真實下單）"
    log.info("=" * 60)
    log.info(f"Nexus Webhook Bot 啟動 {mode_str}")
    log.info(f"監聽: http://{HOST}:{PORT}/webhook")
    log.info(f"安全金鑰: {WEBHOOK_SECRET}")
    log.info("=" * 60)
    tg(f"🚀 <b>Nexus Webhook Bot 啟動</b>\n{mode_str}\nPort: {PORT}")
    app.run(host=HOST, port=PORT, debug=False)
