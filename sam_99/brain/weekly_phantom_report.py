#!/usr/bin/env python3
"""
每週一 09:00 — SMA99 + NFES + 幻影深度學習 週報
─────────────────────────────────────────────────────────────
幻影學習流程：
  1. 讀取上週 SMA99 + NFES 所有進出場記錄（本地 JSON）
  2. 讀取 phantom_knowledge.json（歷史累積知識）
  3. 呼叫 Claude Haiku API 深度分析（每週一次）
  4. 儲存新學習成果至 phantom_knowledge.json（持續累積）
  5. 產生 PDF 週報（含幻影學習摘要）→ TG 傳送
"""
import json, asyncio, httpx, sys, os
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

TG_TOKEN = "8005879844:AAG8DJoaphzsweVmdvMB6SNphJdRy0osQGo"
TG_CHAT  = "1768177615"
AI_DIR   = Path(__file__).resolve().parent.parent.parent  # sam_99/brain → sam_99 → ai/
REPORT_DIR = AI_DIR / "reports"
REPORT_DIR.mkdir(exist_ok=True)

PHANTOM_KNOWLEDGE_FILE = AI_DIR / "phantom_knowledge.json"
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

# ── Telegram ──────────────────────────────────────────────────────
async def tg_send(text: str):
    async with httpx.AsyncClient(timeout=30) as c:
        await c.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML"},
        )

async def tg_pdf(pdf_path: str, caption: str):
    async with httpx.AsyncClient(timeout=30) as c:
        with open(pdf_path, "rb") as f:
            await c.post(
                f"https://api.telegram.org/bot{TG_TOKEN}/sendDocument",
                data={"chat_id": TG_CHAT, "caption": caption, "parse_mode": "HTML"},
                files={"document": (Path(pdf_path).name, f, "application/pdf")},
            )

# ── 幻影知識庫 ────────────────────────────────────────────────────
def load_knowledge() -> dict:
    """讀取累積的幻影知識庫"""
    if PHANTOM_KNOWLEDGE_FILE.exists():
        try:
            return json.loads(PHANTOM_KNOWLEDGE_FILE.read_text(encoding="utf-8"))
        except:
            pass
    return {
        "created": datetime.now(timezone.utc).isoformat(),
        "total_weeks_analyzed": 0,
        "sessions": [],           # 每週分析記錄
        "evolved_rules": "",      # 最新演化出的規則草稿
        "key_patterns": [],       # 跨週累積的關鍵規律
        "strategy_weakness": [],  # 持續記錄的弱點
    }

def save_knowledge(kb: dict):
    PHANTOM_KNOWLEDGE_FILE.write_text(
        json.dumps(kb, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

# ── 幻影深度學習（Haiku API）────────────────────────────────────
async def phantom_deep_learn(
    week_label: str,
    ema_trades: list,
    nfes_trades: list,
    knowledge: dict
) -> dict:
    """
    呼叫 Claude Haiku 分析上週交易，輸出學習成果。
    回傳 dict 包含：insights / weakness / evolved_rules / tg_summary
    """
    if not ANTHROPIC_API_KEY:
        return {
            "insights": ["未設定 ANTHROPIC_API_KEY，幻影學習跳過。"],
            "weakness": [],
            "evolved_rules": knowledge.get("evolved_rules", ""),
            "tg_summary": "⚠️ 幻影：未設定 API Key",
        }

    all_trades = ema_trades + nfes_trades

    # ── 整理交易摘要 ─────────────────────────────────────────────
    def summarize_trades(trades, label):
        if not trades:
            return f"{label}：本週無交易"
        lines = [f"{label}：{len(trades)} 筆"]
        wins  = [t for t in trades if t.get("pnl", 0) > 0]
        loses = [t for t in trades if t.get("pnl", 0) <= 0]
        lines.append(f"  勝率 {len(wins)}/{len(trades)} = {len(wins)/len(trades):.0%}")
        lines.append(f"  合計損益 {sum(t.get('pnl',0) for t in trades):+.2f} USDT")
        for t in trades:
            sym    = t.get("sym", t.get("symbol", "?")).split("/")[0].replace("USDT","")
            side   = t.get("side","?")
            pnl    = t.get("pnl", 0)
            reason = t.get("reason", "?")
            entry  = t.get("entry_px", 0)
            exit_  = t.get("exit_px", 0)
            lines.append(f"  {sym} {side} 進={entry:.4g} 出={exit_:.4g} pnl={pnl:+.2f} 出場={reason}")
        return "\n".join(lines)

    # ── 歷史知識摘要（帶入前幾週的學習）────────────────────────
    prev_sessions = knowledge.get("sessions", [])[-4:]  # 最近4週
    prev_knowledge_text = ""
    if prev_sessions:
        prev_knowledge_text = "\n\n【前幾週累積知識】\n"
        for s in prev_sessions:
            prev_knowledge_text += f"\n● {s['week']} 週：\n"
            for ins in s.get("insights", []):
                prev_knowledge_text += f"  - {ins}\n"
        evolved = knowledge.get("evolved_rules", "")
        if evolved:
            prev_knowledge_text += f"\n【上週演化規則草稿】\n{evolved}\n"

    # ── 組裝 Prompt ──────────────────────────────────────────────
    prompt = f"""你是一個加密貨幣量化交易策略分析師，負責分析兩個自動交易策略的表現，並持續學習演化出更好的策略。

【本週分析週期】{week_label}

【本週交易記錄】
{summarize_trades(ema_trades, "SMA99 Bot（15m兩段式突破SMA99）")}

{summarize_trades(nfes_trades, "NFES Bot（4H Supertrend翻轉）")}
{prev_knowledge_text}

【任務】
請根據上週交易數據，結合前幾週累積知識，輸出以下分析（用繁體中文）：

1. **本週關鍵發現**（3~5條，每條一行）
   - 哪些進場條件有效？哪些失效？
   - 市場環境（趨勢/橫盤）對兩個策略的影響？
   - 兩策略的互補性或重疊問題？

2. **策略弱點**（1~3條，具體且可改進）
   - 不要重複前幾週已知弱點，除非還在惡化

3. **幻影演化規則草稿**（根據累積知識，寫出最新版的進場建議）
   - 格式：「當[條件A] + [條件B]時，[動作]，因為[理由]」
   - 最多5條規則
   - 這是幻影自己的規則，不是複製 SMA99 或 NFES

4. **TG 一句話摘要**（給 Telegram 通知用，20字以內）

輸出格式（JSON）：
{{
  "insights": ["發現1", "發現2", ...],
  "weakness": ["弱點1", ...],
  "evolved_rules": "規則草稿（純文字）",
  "tg_summary": "一句話摘要"
}}"""

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        message = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = message.content[0].text.strip()
        # 解析 JSON（去掉 markdown 代碼塊）
        if "```" in raw:
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        result = json.loads(raw)
        return result
    except json.JSONDecodeError as e:
        # Haiku 回傳不是 JSON，降級處理
        return {
            "insights": [raw[:500] if raw else "解析失敗"],
            "weakness": [],
            "evolved_rules": knowledge.get("evolved_rules", ""),
            "tg_summary": "幻影分析完成（格式異常）",
        }
    except Exception as e:
        return {
            "insights": [f"API 呼叫失敗：{e}"],
            "weakness": [],
            "evolved_rules": knowledge.get("evolved_rules", ""),
            "tg_summary": "⚠️ 幻影 API 錯誤",
        }

# ── 資料統計 ──────────────────────────────────────────────────────
def week_range():
    now = datetime.now(timezone.utc)
    mon = now - timedelta(days=now.weekday())
    last_mon = mon - timedelta(days=7)
    last_sun = mon - timedelta(days=1)
    return last_mon.replace(hour=0,minute=0,second=0,microsecond=0), \
           last_sun.replace(hour=23,minute=59,second=59), \
           last_mon.strftime("%Y-%m-%d"), last_sun.strftime("%Y-%m-%d")

def filter_week(trades, w_start, w_end):
    result = []
    for t in trades:
        ts = t.get('exit_ts') or t.get('exited_at') or ''
        try:
            dt = datetime.fromisoformat(ts.replace('Z','+00:00'))
            if w_start <= dt <= w_end:
                result.append(t)
        except:
            pass
    return result

def stats(trades):
    if not trades:
        return {"count":0,"wins":0,"pnl":0.0,"wr":0.0,"max_loss":0.0}
    wins = sum(1 for t in trades if t.get('pnl',0) > 0)
    pnl  = sum(t.get('pnl',0) for t in trades)
    max_loss = min((t.get('pnl',0) for t in trades), default=0.0)
    return {"count":len(trades),"wins":wins,"pnl":round(pnl,2),
            "wr":wins/len(trades),"max_loss":round(max_loss,2)}

def big_losses(trades, threshold=-30):
    return sorted([t for t in trades if t.get('pnl',0) < threshold],
                  key=lambda x: x.get('pnl',0))

def fmt_sym(sym):
    s = sym.split('/')[0]
    if s.endswith('USDT'): s = s[:-4]
    return s

# ── PDF 產生 ──────────────────────────────────────────────────────
def build_pdf(data: dict, out_path: str):
    from reportlab.lib.pagesizes import A4
    from reportlab.lib import colors
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer,
                                    Table, TableStyle, HRFlowable)
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    chinese_font = "Helvetica"
    for fp in ["/System/Library/Fonts/PingFang.ttc",
               "/Library/Fonts/Arial Unicode MS.ttf",
               "/System/Library/Fonts/STHeiti Light.ttc"]:
        if Path(fp).exists():
            try:
                pdfmetrics.registerFont(TTFont("CJK", fp))
                chinese_font = "CJK"
                break
            except: pass

    C_DARK   = colors.HexColor("#1a1a2e")
    C_BLUE   = colors.HexColor("#16213e")
    C_ACCENT = colors.HexColor("#0f3460")
    C_PHANTOM= colors.HexColor("#6c3483")   # 幻影紫
    C_RED    = colors.HexColor("#c0392b")
    C_GREEN  = colors.HexColor("#27ae60")
    C_GREY   = colors.HexColor("#888888")
    C_BG     = colors.HexColor("#f5f7fa")
    C_PH_BG  = colors.HexColor("#f5eef8")   # 幻影淡紫背景

    def ps(name, size=10, color=C_DARK, bold=False, leading=None):
        return ParagraphStyle(name, fontName=chinese_font, fontSize=size,
                              textColor=color, leading=leading or size*1.5,
                              spaceAfter=2)

    doc = SimpleDocTemplate(out_path, pagesize=A4,
                            leftMargin=1.8*cm, rightMargin=1.8*cm,
                            topMargin=1.5*cm, bottomMargin=1.5*cm)
    W = A4[0] - 3.6*cm

    s_title  = ps("title",  18, C_DARK)
    s_sub    = ps("sub",     9, C_GREY)
    s_h2     = ps("h2",     12, C_BLUE, leading=20)
    s_h2_ph  = ps("h2ph",   12, C_PHANTOM, leading=20)
    s_normal = ps("normal", 10, C_DARK)
    s_small  = ps("small",   8, C_GREY)
    s_red    = ps("red",    10, C_RED)
    s_green  = ps("green",  10, C_GREEN)

    now           = data["now"]
    w_start_s     = data["w_start_s"]
    w_end_s       = data["w_end_s"]
    capital       = data["capital"]
    ema_s         = data["ema_s"]
    nfes_s        = data["nfes_s"]
    ema_w         = data["ema_w"]
    nfes_w        = data["nfes_w"]
    phantom_w     = data["phantom_w"]
    days_done     = data["days_done"]
    days_left     = data["days_left"]
    ph_insights   = data.get("phantom_insights", [])
    ph_weakness   = data.get("phantom_weakness", [])
    ph_rules      = data.get("phantom_evolved_rules", "")
    ph_weeks      = data.get("phantom_total_weeks", 0)
    total_pnl_week = ema_w["pnl"] + nfes_w["pnl"] + phantom_w["pnl"]

    story = []

    # ── 標題列 ────────────────────────────────────────────────────
    title_data = [[
        Paragraph("交易機器人 週報告", s_title),
        Paragraph(f"{w_start_s} ~ {w_end_s}<br/>產生時間：{now.strftime('%Y-%m-%d %H:%M')}", s_sub),
    ]]
    tt = Table(title_data, colWidths=[W*0.6, W*0.4])
    tt.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,-1), C_ACCENT),
        ("TEXTCOLOR",  (0,0), (-1,-1), colors.white),
        ("VALIGN",     (0,0), (-1,-1), "MIDDLE"),
        ("TOPPADDING", (0,0), (-1,-1), 10),
        ("BOTTOMPADDING", (0,0), (-1,-1), 10),
        ("LEFTPADDING",   (0,0), (0,-1), 12),
        ("ALIGN", (1,0), (1,-1), "RIGHT"),
        ("RIGHTPADDING",  (1,0), (1,-1), 12),
    ]))
    story.append(tt)
    story.append(Spacer(1, 0.4*cm))

    # ── 一、資金總覽 ───────────────────────────────────────────────
    story.append(Paragraph("一、資金總覽", s_h2))
    pnl_color = C_GREEN if total_pnl_week >= 0 else C_RED
    pnl_s = ps("pnl_big", 22, pnl_color)
    total_return = (capital - 10000) / 10000 * 100

    overview_data = [
        [Paragraph("本週淨損益", s_small),
         Paragraph("SMA99 可用資金", s_small),
         Paragraph("NFES 持倉市值", s_small),
         Paragraph("綜合淨值", s_small),
         Paragraph("累計報酬率", s_small)],
        [Paragraph(f"<b>{total_pnl_week:+,.2f}</b>", pnl_s),
         Paragraph(f"{capital:,.0f} USDT", s_normal),
         Paragraph(f"{data['nfes_notional']:,.0f} USDT", s_normal),
         Paragraph(f"<b>{capital + data['nfes_notional']:,.0f} USDT</b>", s_normal),
         Paragraph(f"<b>{total_return:+.1f}%</b>",
                   ps("ret", 12, C_GREEN if total_return >= 0 else C_RED))],
        [Paragraph("USDT", s_small), "", "", "", ""],
    ]
    ot = Table(overview_data, colWidths=[W/5]*5)
    ot.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,0), C_BG),
        ("BACKGROUND", (0,1), (-1,2), colors.white),
        ("BOX",    (0,0), (-1,-1), 0.5, colors.HexColor("#dddddd")),
        ("INNERGRID",(0,0),(-1,-1), 0.3, colors.HexColor("#eeeeee")),
        ("ALIGN",  (0,0), (-1,-1), "CENTER"),
        ("VALIGN", (0,0), (-1,-1), "MIDDLE"),
        ("FONTNAME",(0,0),(-1,-1), chinese_font),
        ("TOPPADDING",(0,0),(-1,-1), 6),
        ("BOTTOMPADDING",(0,0),(-1,-1), 6),
        ("SPAN", (0,1), (0,2)),
    ]))
    story.append(ot)
    story.append(Spacer(1, 0.5*cm))

    # ── 二、本週損益明細 ───────────────────────────────────────────
    story.append(Paragraph("二、本週損益明細", s_h2))

    def pnl_para(val):
        c = C_GREEN if val > 0 else (C_RED if val < 0 else C_GREY)
        return Paragraph(f"{val:+,.2f} USDT", ps("pp", 10, c))

    total_count = ema_w['count']+nfes_w['count']+phantom_w['count']
    total_wins  = ema_w['wins']+nfes_w['wins']+phantom_w['wins']
    rows = [
        [Paragraph("策略", s_small),
         Paragraph("已實現損益", s_small),
         Paragraph("交易次數", s_small),
         Paragraph("勝率", s_small),
         Paragraph("最大單筆虧損", s_small)],
        [Paragraph("□ SMA99 Bot", s_normal),
         pnl_para(ema_w["pnl"]),
         Paragraph(f"{ema_w['count']} 筆", s_normal),
         Paragraph(f"{ema_w['wr']:.1%}", s_normal),
         Paragraph(f"{ema_w['max_loss']:,.2f} USDT", ps("ml",10,C_RED))],
        [Paragraph("□ NFES Signal Bot", s_normal),
         pnl_para(nfes_w["pnl"]),
         Paragraph(f"{nfes_w['count']} 筆", s_normal),
         Paragraph(f"{nfes_w['wr']:.1%}", s_normal),
         Paragraph(f"{nfes_w['max_loss']:,.2f} USDT", ps("ml2",10,C_RED))],
        [Paragraph("👻 Phantom 幻影（觀察期）", ps("ph",10,C_PHANTOM)),
         pnl_para(phantom_w["pnl"]),
         Paragraph(f"{phantom_w['count']} 筆", s_normal),
         Paragraph(f"{phantom_w['wr']:.1%}", s_normal),
         Paragraph(f"{phantom_w['max_loss']:,.2f} USDT", ps("ml3",10,C_RED))],
        [Paragraph("<b>合計</b>", ps("tot",10,C_DARK)),
         pnl_para(total_pnl_week),
         Paragraph(f"{total_count} 筆", s_normal),
         Paragraph(f"{total_wins/total_count:.1%}" if total_count else "0%", s_normal),
         Paragraph("", s_normal)],
    ]
    dt = Table(rows, colWidths=[W*0.24, W*0.22, W*0.18, W*0.18, W*0.18])
    dt.setStyle(TableStyle([
        ("BACKGROUND", (0,0),(-1,0), C_ACCENT),
        ("TEXTCOLOR",  (0,0),(-1,0), colors.white),
        ("BACKGROUND", (0,4),(-1,4), colors.HexColor("#eef2ff")),
        ("ROWBACKGROUNDS",(0,1),(-1,3),[colors.HexColor("#f9f9f9"), colors.white]),
        ("BOX",    (0,0),(-1,-1), 0.5, colors.HexColor("#cccccc")),
        ("INNERGRID",(0,0),(-1,-1), 0.3, colors.HexColor("#eeeeee")),
        ("ALIGN",  (1,0),(-1,-1), "CENTER"),
        ("FONTNAME",(0,0),(-1,-1), chinese_font),
        ("TOPPADDING",(0,0),(-1,-1),6),("BOTTOMPADDING",(0,0),(-1,-1),6),
    ]))
    story.append(dt)
    story.append(Spacer(1, 0.5*cm))

    # ── 三、虧損原因分析 ───────────────────────────────────────────
    story.append(Paragraph("三、虧損原因分析", s_h2))

    def loss_table(title, losses):
        if not losses:
            story.append(Paragraph(f"{title} — 本週無重大虧損 ✓", s_normal))
            story.append(Spacer(1, 0.2*cm))
            return
        story.append(Paragraph(f"{title} — 重大虧損（超過 -30 USDT）", s_normal))
        rows2 = [[Paragraph("幣種",s_small), Paragraph("日期",s_small),
                  Paragraph("虧損金額",s_small), Paragraph("原因分析",s_small)]]
        for t in losses:
            ts = (t.get('exit_ts') or '')[:10]
            pnl = t.get('pnl',0)
            rows2.append([
                Paragraph(fmt_sym(t.get('sym','')), s_normal),
                Paragraph(ts, s_normal),
                Paragraph(f"{pnl:,.2f} USDT", ps("lp",10,C_RED)),
                Paragraph("止損觸發，波動幅度超出預期", s_normal),
            ])
        lt = Table(rows2, colWidths=[W*0.15, W*0.18, W*0.22, W*0.45])
        lt.setStyle(TableStyle([
            ("BACKGROUND",(0,0),(-1,0), colors.HexColor("#f0f0f0")),
            ("ROWBACKGROUNDS",(0,1),(-1,-1),[colors.white, colors.HexColor("#fff8f8")]),
            ("BOX",(0,0),(-1,-1),0.5,colors.HexColor("#dddddd")),
            ("INNERGRID",(0,0),(-1,-1),0.3,colors.HexColor("#eeeeee")),
            ("FONTNAME",(0,0),(-1,-1), chinese_font),
            ("TOPPADDING",(0,0),(-1,-1),5),("BOTTOMPADDING",(0,0),(-1,-1),5),
        ]))
        story.append(lt)
        story.append(Spacer(1, 0.3*cm))

    loss_table("□ SMA99 Bot", big_losses(ema_s))
    loss_table("□ NFES Signal Bot", big_losses(nfes_s))

    # ── 四、改善建議 ───────────────────────────────────────────────
    story.append(Paragraph("四、改善建議", s_h2))
    sug_rows = [
        [Paragraph("編號",s_small), Paragraph("問題描述",s_small),
         Paragraph("建議改善方式",s_small), Paragraph("優先度",s_small)],
        ["1", Paragraph("SMA99 橫盤期無訊號", s_normal),
         Paragraph("ADX 門檻已從20降至15，觀察是否有改善", s_normal),
         Paragraph("觀察中", ps("pri",10,C_GREY))],
        ["2", Paragraph("NFES 日內模式偶有大虧", s_normal),
         Paragraph("4H強制平倉機制已上線，持續觀察", s_normal),
         Paragraph("觀察中", ps("pri2",10,C_GREY))],
        ["3", Paragraph("雙 process 造成 state 覆蓋", s_normal),
         Paragraph("改用 supervisorctl 統一管理，禁止手動啟動", s_normal),
         Paragraph("已修復", ps("pri3",10,C_GREEN))],
    ]
    if ph_weakness:
        for i, w in enumerate(ph_weakness[:2], 4):
            sug_rows.append([
                str(i),
                Paragraph(f"👻 {w[:40]}", ps("phw",10,C_PHANTOM)),
                Paragraph("幻影學習建議，下週版本迭代參考", s_normal),
                Paragraph("幻影建議", ps("php",10,C_PHANTOM)),
            ])
    st = Table(sug_rows, colWidths=[W*0.08, W*0.27, W*0.45, W*0.2])
    st.setStyle(TableStyle([
        ("BACKGROUND",(0,0),(-1,0), C_ACCENT),
        ("TEXTCOLOR", (0,0),(-1,0), colors.white),
        ("ROWBACKGROUNDS",(0,1),(-1,-1),[colors.white, C_BG]),
        ("BOX",(0,0),(-1,-1),0.5,colors.HexColor("#cccccc")),
        ("INNERGRID",(0,0),(-1,-1),0.3,colors.HexColor("#eeeeee")),
        ("ALIGN",(0,0),(0,-1),"CENTER"),
        ("FONTNAME",(0,0),(-1,-1), chinese_font),
        ("TOPPADDING",(0,0),(-1,-1),6),("BOTTOMPADDING",(0,0),(-1,-1),6),
    ]))
    story.append(st)
    story.append(Spacer(1, 0.5*cm))

    # ── 五、本週學習重點（規則式）────────────────────────────────
    story.append(Paragraph("五、本週學習重點", s_h2))
    story.append(Spacer(1, 0.2*cm))

    # 規則式學習重點（原有邏輯）
    rule_points = _rule_learning_points(ema_s, nfes_s, ema_w, nfes_w)
    lp_rows = [[Paragraph(f"{i+1}.", s_normal), Paragraph(pt, s_normal)]
               for i, pt in enumerate(rule_points)]
    if lp_rows:
        lt2 = Table(lp_rows, colWidths=[W*0.06, W*0.94])
        lt2.setStyle(TableStyle([
            ("ROWBACKGROUNDS",(0,0),(-1,-1),[colors.HexColor("#f0f8ff"), colors.white]),
            ("BOX",(0,0),(-1,-1),0.5,colors.HexColor("#b3d9ff")),
            ("INNERGRID",(0,0),(-1,-1),0.3,colors.HexColor("#ddeeff")),
            ("FONTNAME",(0,0),(-1,-1), chinese_font),
            ("TOPPADDING",(0,0),(-1,-1),7),("BOTTOMPADDING",(0,0),(-1,-1),7),
            ("VALIGN",(0,0),(-1,-1),"TOP"),
        ]))
        story.append(lt2)
    story.append(Spacer(1, 0.5*cm))

    # ── 六、👻 幻影深度學習報告 ───────────────────────────────────
    story.append(Paragraph(f"六、👻 幻影深度學習（第 {ph_weeks} 週）", s_h2_ph))
    story.append(Paragraph(
        f"觀察期：2026/05/19 ~ 2026/06/19｜已累積 {ph_weeks} 週數據｜"
        f"剩 {days_left} 天",
        ps("phsub", 8, C_GREY)
    ))
    story.append(Spacer(1, 0.2*cm))

    # 本週關鍵發現
    if ph_insights:
        story.append(Paragraph("▌ 本週關鍵發現（AI 分析）", ps("phh", 10, C_PHANTOM)))
        ins_rows = [[Paragraph(f"{i+1}.", s_normal), Paragraph(ins, s_normal)]
                    for i, ins in enumerate(ph_insights)]
        ins_t = Table(ins_rows, colWidths=[W*0.05, W*0.95])
        ins_t.setStyle(TableStyle([
            ("ROWBACKGROUNDS",(0,0),(-1,-1),[C_PH_BG, colors.white]),
            ("BOX",(0,0),(-1,-1),0.5,colors.HexColor("#c39bd3")),
            ("INNERGRID",(0,0),(-1,-1),0.3,colors.HexColor("#e8daef")),
            ("FONTNAME",(0,0),(-1,-1), chinese_font),
            ("TOPPADDING",(0,0),(-1,-1),6),("BOTTOMPADDING",(0,0),(-1,-1),6),
            ("VALIGN",(0,0),(-1,-1),"TOP"),
        ]))
        story.append(ins_t)
        story.append(Spacer(1, 0.3*cm))

    # 演化規則草稿
    if ph_rules:
        story.append(Paragraph("▌ 幻影演化規則草稿（持續更新）", ps("phh2", 10, C_PHANTOM)))
        for line in ph_rules.strip().split("\n"):
            if line.strip():
                story.append(Paragraph(f"  {line.strip()}", ps("phrule", 9, C_DARK)))
        story.append(Spacer(1, 0.3*cm))

    story.append(Spacer(1, 0.3*cm))

    # ── 七、版本迭代記錄 ───────────────────────────────────────────
    story.append(Paragraph("七、版本迭代記錄", s_h2))
    ver_rows = [
        [Paragraph("版本",s_small), Paragraph("日期",s_small),
         Paragraph("更新內容",s_small), Paragraph("Git 標籤",s_small)],
        ["v1.0", "2026-05-08",
         Paragraph("基礎版：EMA99 + NFES 雙機器人、7x24 Supervisor 守護", s_small),
         Paragraph("git checkout v1.0", ps("git",8,C_ACCENT))],
        ["v1.1", "2026-05-11",
         Paragraph("TG 指令合併雙策略 / NFES 動態倉位 / Bug 修復", s_small),
         Paragraph("git checkout v1.1", ps("git2",8,C_ACCENT))],
        ["v1.2", "2026-05-19",
         Paragraph("SMA99更名 / 市值前200 / NFES全市場 / 日內模式", s_small),
         Paragraph("git checkout v1.2", ps("git3",8,C_ACCENT))],
        ["v1.3", "2026-06-01",
         Paragraph("GitHub Gist 同步 / Render 修復", s_small),
         Paragraph("", ps("git4",8,C_ACCENT))],
        ["v5.2", "2026-06-06",
         Paragraph("SMA99空單兩段式 / SLOPE_FILTER放寬 / 幻影深度學習啟動", s_small),
         Paragraph("（最新版）", ps("git5",8,C_GREEN))],
    ]
    vt = Table(ver_rows, colWidths=[W*0.08, W*0.14, W*0.55, W*0.23])
    vt.setStyle(TableStyle([
        ("BACKGROUND",(0,0),(-1,0), C_ACCENT),
        ("TEXTCOLOR", (0,0),(-1,0), colors.white),
        ("ROWBACKGROUNDS",(0,1),(-1,-1),[colors.white, C_BG]),
        ("BOX",(0,0),(-1,-1),0.5,colors.HexColor("#cccccc")),
        ("INNERGRID",(0,0),(-1,-1),0.3,colors.HexColor("#eeeeee")),
        ("ALIGN",(0,0),(0,-1),"CENTER"),
        ("FONTNAME",(0,0),(-1,-1), chinese_font),
        ("TOPPADDING",(0,0),(-1,-1),5),("BOTTOMPADDING",(0,0),(-1,-1),5),
        ("VALIGN",(0,0),(-1,-1),"TOP"),
    ]))
    story.append(vt)
    story.append(Spacer(1, 0.4*cm))

    # 頁尾
    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#cccccc")))
    story.append(Spacer(1, 0.2*cm))
    footer  = "SMA99 Bot + NFES Signal Bot + 👻 Phantom 幻影｜7x24 Supervisor"
    footer2 = f"週報 v1.4｜{now.strftime('%Y-%m-%d %H:%M')}"
    story.append(Table([[Paragraph(footer, s_small), Paragraph(footer2, s_small)]],
                       colWidths=[W*0.7, W*0.3],
                       style=[("ALIGN",(1,0),(1,0),"RIGHT"),
                              ("FONTNAME",(0,0),(-1,-1), chinese_font)]))
    doc.build(story)


def _rule_learning_points(ema_s, nfes_s, ema_w, nfes_w) -> list[str]:
    """規則式學習重點（原有邏輯，保留作為基礎分析）"""
    points = []
    all_t = ema_s + nfes_s
    if not all_t:
        return ["本週無交易記錄，持續觀察市場。"]

    wins  = [t['pnl'] for t in all_t if t.get('pnl',0) > 0]
    loses = [abs(t['pnl']) for t in all_t if t.get('pnl',0) < 0]
    avg_win  = sum(wins)/len(wins) if wins else 0
    avg_loss = sum(loses)/len(loses) if loses else 0
    rr = avg_win/avg_loss if avg_loss else 0

    if rr < 1.2:
        points.append(f"⚠️ 盈虧比偏低（{rr:.2f}x）：平均獲利 {avg_win:.1f}U vs 平均虧損 {avg_loss:.1f}U。")
    elif rr >= 1.5:
        points.append(f"✅ 盈虧比良好（{rr:.2f}x）。")

    wr = len(wins)/len(all_t) if all_t else 0
    if wr < 0.45:
        points.append(f"⚠️ 勝率偏低（{wr:.0%}，{len(wins)}/{len(all_t)}）。")
    elif wr > 0.60:
        points.append(f"✅ 勝率優秀（{wr:.0%}）。")

    if ema_w['count'] == 0:
        points.append("📊 SMA99 本週零交易：市場橫盤，等待趨勢明確。")
    elif ema_w['count'] < 3:
        points.append(f"📊 SMA99 保守進場（{ema_w['count']}筆）：篩選嚴格，合理。")

    nfes_sl = [t for t in nfes_s if t.get('reason') == 'stop_loss']
    if nfes_sl:
        sl_pnl = sum(t.get('pnl',0) for t in nfes_sl)
        points.append(f"🔮 NFES 止損 {len(nfes_sl)} 次（{sl_pnl:+.1f}U）。")

    if not points:
        points.append("✅ 本週各項指標正常，維持現有策略執行。")
    return points


# ── 主流程 ────────────────────────────────────────────────────────
def save_version_snapshot():
    from shutil import copyfile
    date_str = datetime.now().strftime("%Y%m%d")
    ver_dir  = AI_DIR / "versions"
    ver_dir.mkdir(exist_ok=True)
    files = {
        AI_DIR / "ema99_bot.py"        : f"sma99_bot_v_weekly_{date_str}.py",
        AI_DIR / "nfes_signal_bot.py"  : f"nfes_signal_bot_v_weekly_{date_str}.py",
    }
    saved = []
    for src, dst_name in files.items():
        dst = ver_dir / dst_name
        if src.exists() and not dst.exists():
            copyfile(src, dst)
            saved.append(dst_name)
    if saved:
        print(f"[版本快照] 已儲存：{', '.join(saved)}")

async def main():
    now = datetime.now(timezone.utc)
    save_version_snapshot()
    w_start, w_end, w_start_s, w_end_s = week_range()
    week_label = f"{w_start_s} ~ {w_end_s}"

    print(f"[幻影週報] 開始 {week_label}")

    ema_state  = json.loads((AI_DIR / "ema99_bot_state.json").read_text())
    nfes_state = json.loads((AI_DIR / "nfes_bot_state.json").read_text())
    avail_capital  = ema_state.get("capital", 10000.0)

    nfes_positions = nfes_state.get("positions", {})
    nfes_notional  = sum(p.get("notional", p.get("margin", 0))
                         for p in (nfes_positions.values() if isinstance(nfes_positions, dict)
                                   else nfes_positions))
    # 真實總資產 = 可用資金 + 所有持倉保證金（已從資金池扣除的部分）
    nfes_locked = sum(p.get("margin", 0)
                      for p in (nfes_positions.values() if isinstance(nfes_positions, dict)
                                else nfes_positions))
    ema_locked  = sum(p.get("margin", 0) for p in ema_state.get("positions", {}).values())
    capital     = avail_capital + nfes_locked + ema_locked

    ema_all  = ema_state.get("trades", [])
    nfes_all = nfes_state.get("trades", [])

    ema_w_trades  = filter_week(ema_all,  w_start, w_end)
    nfes_w_trades = filter_week(nfes_all, w_start, w_end)

    phantom_trades = []
    phantom_file = AI_DIR / "phantom_state.json"
    if phantom_file.exists():
        ps_data = json.loads(phantom_file.read_text())
        phantom_trades = filter_week(ps_data.get("trades", []), w_start, w_end)

    ema_s  = stats(ema_w_trades)
    nfes_s = stats(nfes_w_trades)
    ph_s   = stats(phantom_trades)

    obs_start = datetime(2026, 5, 19, tzinfo=timezone.utc)
    obs_end   = datetime(2026, 6, 19, tzinfo=timezone.utc)
    days_done = max(0, (now - obs_start).days)
    days_left = max(0, (obs_end - now).days)

    # ── 幻影深度學習 ───────────────────────────────────────────────
    print("[幻影] 載入知識庫...")
    knowledge = load_knowledge()

    print("[幻影] 呼叫 Haiku 深度分析...")
    ph_learn = await phantom_deep_learn(week_label, ema_w_trades, nfes_w_trades, knowledge)

    # 儲存新學習成果至知識庫（持續累積）
    knowledge["total_weeks_analyzed"] += 1
    knowledge["sessions"].append({
        "week"          : w_start_s,
        "analyzed_at"   : now.isoformat(),
        "ema_trades"    : len(ema_w_trades),
        "nfes_trades"   : len(nfes_w_trades),
        "insights"      : ph_learn.get("insights", []),
        "weakness"      : ph_learn.get("weakness", []),
        "evolved_rules" : ph_learn.get("evolved_rules", ""),
    })
    # 更新最新演化規則
    if ph_learn.get("evolved_rules"):
        knowledge["evolved_rules"] = ph_learn["evolved_rules"]
    # 累積關鍵規律（去重）
    for ins in ph_learn.get("insights", []):
        if ins not in knowledge["key_patterns"] and len(knowledge["key_patterns"]) < 30:
            knowledge["key_patterns"].append(ins)

    save_knowledge(knowledge)
    print(f"[幻影] 知識庫已更新（第 {knowledge['total_weeks_analyzed']} 週）")

    # ── 產生 PDF ──────────────────────────────────────────────────
    week_str = w_end.strftime("%Y-%m-%d")
    pdf_path = str(REPORT_DIR / f"週報_{week_str}.pdf")

    data = dict(
        now=now, w_start_s=w_start_s, w_end_s=w_end_s,
        capital=capital, nfes_notional=nfes_notional,
        ema_s=ema_w_trades, nfes_s=nfes_w_trades,
        ema_w=ema_s, nfes_w=nfes_s, phantom_w=ph_s,
        days_done=days_done, days_left=days_left,
        phantom_insights=ph_learn.get("insights", []),
        phantom_weakness=ph_learn.get("weakness", []),
        phantom_evolved_rules=ph_learn.get("evolved_rules", ""),
        phantom_total_weeks=knowledge["total_weeks_analyzed"],
    )
    build_pdf(data, pdf_path)

    total_pnl = ema_s["pnl"] + nfes_s["pnl"] + ph_s["pnl"]
    ph_summary = ph_learn.get("tg_summary", "")

    caption = (
        f"📊 <b>交易機器人週報</b> {w_start_s} ~ {w_end_s}\n"
        f"本週損益：{total_pnl:+,.2f} USDT\n"
        f"SMA99：{ema_s['count']}筆 {ema_s['pnl']:+.1f}U｜"
        f"NFES：{nfes_s['count']}筆 {nfes_s['pnl']:+.1f}U\n"
        f"資金：{capital:,.0f} U（{(capital-10000)/10000*100:+.1f}%）\n"
        f"👻 幻影第{knowledge['total_weeks_analyzed']}週：{ph_summary}"
    )
    await tg_pdf(pdf_path, caption)
    print(f"[幻影週報] 完成 → {pdf_path}")

if __name__ == "__main__":
    asyncio.run(main())
