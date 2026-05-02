#!/usr/bin/env python3
import threading
import os
from flask import Flask

app = Flask(__name__)

@app.route("/")
def index():
    return "EMA99 Bot is running ✅"

@app.route("/health")
def health():
    return "ok", 200

def start_bot():
    import ema99_bot
    ema99_bot.main()

# 無論 gunicorn 還是直接執行都會啟動 bot
_bot_thread = threading.Thread(target=start_bot, daemon=True)
_bot_thread.start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
