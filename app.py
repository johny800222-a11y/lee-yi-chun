#!/usr/bin/env python3
import threading
import os
import logging
from flask import Flask

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

app = Flask(__name__)

@app.route("/")
def index():
    return "EMA99 Bot is running ✅"

@app.route("/health")
def health():
    return "ok", 200

def start_bot():
    try:
        log.info("Bot thread starting...")
        import ema99_bot
        ema99_bot.main()
    except Exception as e:
        log.exception(f"Bot crashed: {e}")

# 用 os.getpid() 確保只在主 worker 啟動一次
# gunicorn --preload 模式下 fork 前執行，避免多次啟動
_started = False

def _launch():
    global _started
    if not _started:
        _started = True
        t = threading.Thread(target=start_bot, daemon=True, name="ema99-bot")
        t.start()
        log.info(f"Bot thread launched (pid={os.getpid()})")

_launch()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
