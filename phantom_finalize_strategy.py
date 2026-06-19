#!/usr/bin/env python3
"""
2026-06-19 — 幻影觀察期結束，自動統整最終策略
─────────────────────────────────────────────────────────────
不問使用者任何問題，自動完成：
  1. 讀取 30 天累積的 phantom_knowledge.json（每週學習成果）
  2. 讀取 SMA99 + NFES 整個觀察期（5/19~6/19）的全部交易
  3. 帶入原始「人性流動性」三條件設計（資金費率/假突破/多空比）
  4. 呼叫 Claude API 自主演算、收斂出最終版幻影策略
  5. 產生策略文件 + 摘要訊息，直接送 TG，不等待確認
"""
import json, asyncio, httpx, sys, os
from datetime import datetime, timezone
from pathlib import Path

AI_DIR = Path(__file__).resolve().parent
TG_TOKEN = "8005879844:AAG8DJoaphzsweVmdvMB6SNphJdRy0osQGo"
TG_CHAT  = "1768177615"
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

PHANTOM_KNOWLEDGE_FILE = AI_DIR / "phantom_knowledge.json"
EMA_STATE_FILE  = AI_DIR / "ema99_bot_state.json"
NFES_STATE_FILE = AI_DIR / "nfes_bot_state.json"
OUT_MD = AI_DIR / "versions" / f"幻影_最終策略_{datetime.now().strftime('%Y%m%d')}.md"

OBS_START = datetime(2026, 5, 19, tzinfo=timezone.utc)
OBS_END   = datetime(2026, 6, 19, 23, 59, 59, tzinfo=timezone.utc)

ORIGINAL_DESIGN = """
【幻影原始構想：人性流動性逆勢策略】
三個人性指標同時共鳴才進場：
① 資金費率極端（群眾情緒過熱）：多頭 Funding > +0.08%(8H) / 空頭 < -0.08%(8H)
② False Breakout（流動性掃蕩）：突破近20根1H高低點，3根K內收回原區間
③ 多空比驗證：做空 多空比>1.5／做多 多空比<0.7
④ 量能確認：突破根成交量 > 均量 × 1.5
風控草案：SL=掃蕩極值再外0.5×ATR；TP1=1.5R(出50%)；TP2=3R(全出)；最長持倉8小時強制平倉
只做主流幣前20，避免低流動性誤判
"""

async def tg_send(text: str):
    async with httpx.AsyncClient(timeout=30) as c:
        await c.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML"},
        )

async def tg_doc(path: Path, caption: str):
    async with httpx.AsyncClient(timeout=30) as c:
        with open(path, "rb") as f:
            await c.post(
                f"https://api.telegram.org/bot{TG_TOKEN}/sendDocument",
                data={"chat_id": TG_CHAT, "caption": caption, "parse_mode": "HTML"},
                files={"document": (path.name, f, "text/markdown")},
            )

def load_json(p: Path) -> dict:
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}

def trades_in_window(trades: list) -> list:
    out = []
    for t in trades:
        ts = t.get("exit_ts") or t.get("exited_at") or ""
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if OBS_START <= dt <= OBS_END:
                out.append(t)
        except Exception:
            pass
    return out

def summarize(trades: list, label: str) -> str:
    if not trades:
        return f"{label}：觀察期內無交易"
    wins = [t for t in trades if t.get("pnl", 0) > 0]
    pnl  = sum(t.get("pnl", 0) for t in trades)
    lines = [f"{label}：{len(trades)} 筆，勝率 {len(wins)}/{len(trades)}={len(wins)/len(trades):.0%}，合計損益 {pnl:+.2f}"]
    return "\n".join(lines)

async def main():
    kb = load_json(PHANTOM_KNOWLEDGE_FILE)
    ema_trades  = trades_in_window(load_json(EMA_STATE_FILE).get("trades", []))
    nfes_trades = trades_in_window(load_json(NFES_STATE_FILE).get("trades", []))

    history_text = ""
    for s in kb.get("sessions", []):
        history_text += f"\n● {s.get('week')} 週：\n"
        for ins in s.get("insights", []):
            history_text += f"  - {ins}\n"

    prompt = f"""你是一個獨立的加密貨幣量化策略研究員，代號「幻影」。
過去30天（2026-05-19 ~ 2026-06-19）你持續觀察兩個既有自動交易策略（SMA99 順勢、NFES 順勢中段）的實戰表現，
目的是自主演算、收斂出第三套屬於你自己的逆勢策略。現在觀察期結束，你要交出最終版本。

【觀察期內 SMA99 + NFES 實戰數據】
{summarize(ema_trades, "SMA99 Bot")}
{summarize(nfes_trades, "NFES Bot")}

【30天每週學習累積】
{history_text}

{ORIGINAL_DESIGN}

【任務】
不要再問任何問題、不要保留模糊地帶，直接給出可以拿去寫程式實作的最終版本：

1. **最終進場規則**（具體數值、條件全部寫死，不留「視情況」字眼）
2. **最終風控規則**（SL/TP1/TP2/TP3/最長持倉時間，全部給出明確數字與理由）
3. **與 SMA99、NFES 的互補定位**（一句話講清楚幻影補的是哪個缺口）
4. **時間框架**（明確：用哪個K線做訊號、哪個K線做方向過濾）
5. **為什麼這版本比原始構想更好**（根據觀察期數據說明你做了什麼調整、為什麼）
6. **建議虛擬盤驗證週期與停止/轉實盤的判斷標準**（具體勝率/PF門檻數字）

用繁體中文，輸出結構化 Markdown，給工程師直接照著寫程式。不要輸出JSON，直接輸出最終 Markdown 文件全文。"""

    final_md = "# 幻影最終策略生成失敗\n\n未取得 API 回應。"
    tg_summary = "⚠️ 幻影最終策略生成失敗"

    if not ANTHROPIC_API_KEY:
        final_md = "# 幻影最終策略\n\n未設定 ANTHROPIC_API_KEY，無法自動生成，請設定後重跑此腳本。"
        tg_summary = "⚠️ 缺少 ANTHROPIC_API_KEY，幻影最終策略未生成"
    else:
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
            message = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=4096,
                messages=[{"role": "user", "content": prompt}],
            )
            final_md = message.content[0].text.strip()
            tg_summary = "👻 幻影觀察期結束，最終策略已生成"
        except Exception as e:
            final_md = f"# 幻影最終策略生成失敗\n\n錯誤：{e}"
            tg_summary = f"⚠️ 幻影最終策略生成錯誤：{e}"

    OUT_MD.parent.mkdir(exist_ok=True)
    OUT_MD.write_text(final_md, encoding="utf-8")

    kb["final_strategy_md"] = str(OUT_MD)
    kb["final_strategy_generated_at"] = datetime.now(timezone.utc).isoformat()
    PHANTOM_KNOWLEDGE_FILE.write_text(json.dumps(kb, ensure_ascii=False, indent=2), encoding="utf-8")

    await tg_send(f"{tg_summary}\n\n觀察期：2026/05/19 ~ 2026/06/19（30天）\n檔案：{OUT_MD.name}")
    if OUT_MD.exists() and ANTHROPIC_API_KEY:
        await tg_doc(OUT_MD, "👻 幻影最終策略（觀察期結束自動生成）")

if __name__ == "__main__":
    asyncio.run(main())
