"""
shared_capital.py — 雙 Bot 共用資金池
  - 資金來源：ema99_bot_state.json 的 capital 欄位（EMA99 是主帳）
  - NFES 開/平倉時透過此模組同步扣/還保證金
  - file lock 防止兩個 process 同時寫入
"""
from __future__ import annotations
import json, fcntl, time, logging
from pathlib import Path
from datetime import datetime, timezone

BASE             = Path(__file__).parent
EMA99_STATE_FILE = BASE / "ema99_bot_state.json"
NFES_STATE_FILE  = BASE / "nfes_bot_state.json"
LOCK_FILE        = BASE / ".shared_capital.lock"

INITIAL_CAPITAL   = 10_000.0
MAX_POS_TOTAL     = 10          # 兩個 bot 合計最多持倉數
MAX_POS_PER_BOT   = 5           # 每個 bot 軟上限
MAX_MARGIN_PCT    = 0.10        # 每筆保證金 = 資金 10%
STRONG_MARGIN_CAP = 500.0       # 強訊號突破 10 倉時的保證金上限

log = logging.getLogger("shared_capital")


# ── 基礎讀寫（不加鎖，供內部使用）──────────────────────────────

def _read_capital() -> float:
    try:
        d = json.loads(EMA99_STATE_FILE.read_text(encoding="utf-8"))
        return float(d.get("capital", INITIAL_CAPITAL))
    except Exception:
        return INITIAL_CAPITAL


def _write_capital(value: float) -> None:
    try:
        d = json.loads(EMA99_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        d = {}
    d["capital"] = round(value, 4)
    EMA99_STATE_FILE.write_text(
        json.dumps(d, indent=2, ensure_ascii=False, default=str)
    )


# ── 公開 API ─────────────────────────────────────────────────────

def get_capital() -> float:
    """讀取共用可用資金"""
    return _read_capital()


def adjust_capital(delta: float, note: str = "") -> float:
    """原子性修改資金（加鎖）。回傳修改後金額。"""
    for _attempt in range(10):
        lf = open(str(LOCK_FILE), "w")
        try:
            fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
            current = _read_capital()
            new_cap = current + delta
            _write_capital(new_cap)
            log.debug(f"adjust_capital {delta:+.2f} → {new_cap:.2f}  [{note}]")
            return new_cap
        except BlockingIOError:
            time.sleep(0.05)
        finally:
            try:
                fcntl.flock(lf, fcntl.LOCK_UN)
            except Exception:
                pass
            lf.close()
    # 降級：無鎖直接寫
    current = _read_capital()
    new_cap = current + delta
    _write_capital(new_cap)
    return new_cap


def get_total_positions() -> tuple[int, int, int]:
    """回傳 (ema99持倉數, nfes持倉數, 合計)"""
    e_cnt = n_cnt = 0
    try:
        e_cnt = len(json.loads(EMA99_STATE_FILE.read_text()).get("positions", {}))
    except Exception:
        pass
    try:
        n_cnt = len(json.loads(NFES_STATE_FILE.read_text()).get("positions", {}))
    except Exception:
        pass
    return e_cnt, n_cnt, e_cnt + n_cnt


def calc_margin(signal: str = "") -> tuple[float, int]:
    """
    根據訊號強度與當前資金計算 (margin_usdt, leverage)。
    強訊號（含 +）突破 10 倉上限時，保證金上限 500 USDT。
    回傳 (0, 0) 表示不可開倉。
    """
    from nexus_webhook import SIGNAL_LEVERAGE, DEFAULT_LEVERAGE
    capital   = get_capital()
    _, _, total = get_total_positions()
    is_strong   = "+" in signal
    lev         = SIGNAL_LEVERAGE.get(signal, DEFAULT_LEVERAGE)

    if total >= MAX_POS_TOTAL:
        if is_strong:
            margin = min(round(capital * MAX_MARGIN_PCT, 2), STRONG_MARGIN_CAP)
            log.info(f"calc_margin [overflow] signal={signal!r} total={total} → {margin:.0f}U × {lev}x")
            return max(margin, 10.0), lev
        log.info(f"calc_margin 已達上限 {total}/{MAX_POS_TOTAL}，非強訊號不開倉")
        return 0.0, 0

    margin = round(capital * MAX_MARGIN_PCT, 2)
    margin = max(margin, 10.0)
    log.info(f"calc_margin signal={signal!r} capital={capital:.0f} total={total} → {margin:.0f}U × {lev}x")
    return margin, lev
