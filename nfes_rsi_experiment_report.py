#!/usr/bin/env python3
"""
NFES RSI 實驗報告（2026-06-27 09:00）
比較 v2.1（動能斜率）vs 原條件（RSI>50/RSI<65）
產生 PDF → TG 傳送
"""
import json, asyncio, httpx
from datetime import datetime, timezone
from pathlib import Path

AI_DIR     = Path(__file__).parent
REPORT_DIR = AI_DIR / "reports"
REPORT_DIR.mkdir(exist_ok=True)

TG_TOKEN = "8005879844:AAG8DJoaphzsweVmdvMB6SNphJdRy0osQGo"
TG_CHAT  = "1768177615"

# ── 基準數據（原條件 05/19~06/06）─────────────────────────────────
BASELINE = {
    "period"      : "2026-05-19 ~ 2026-06-06",
    "total"       : 62,
    "longs"       : 50,
    "shorts"      : 12,
    "win_rate"    : 40.3,
    "long_wr"     : 46.0,
    "short_wr"    : 16.7,
    "total_pnl"   : 1001.82,
    "profit_factor": 1.38,
    "condition"   : "空單 RSI > 50 / 多單 RSI < 65",
}

ROLLBACK_CMD = (
    "cp ~/Desktop/ai/versions/nfes_signal_bot_v2_rsi_original_20260606.py "
    "~/Desktop/ai/nfes_signal_bot.py\n"
    "supervisorctl restart nfes_bot"
)

# ── TG ────────────────────────────────────────────────────────────
async def tg_pdf(pdf_path: str, caption: str):
    async with httpx.AsyncClient(timeout=60) as c:
        with open(pdf_path, "rb") as f:
            await c.post(
                f"https://api.telegram.org/bot{TG_TOKEN}/sendDocument",
                data={"chat_id": TG_CHAT, "caption": caption, "parse_mode": "HTML"},
                files={"document": (Path(pdf_path).name, f, "application/pdf")},
            )

async def tg_send(text: str):
    async with httpx.AsyncClient(timeout=30) as c:
        await c.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML"},
        )

# ── 讀取新條件期間數據（06/06 起）────────────────────────────────
def load_experiment_stats():
    with open(AI_DIR / "nfes_bot_state.json") as f:
        state = json.load(f)

    trades    = state.get("trades", [])
    exp_start = datetime(2026, 6, 6, tzinfo=timezone.utc)

    valid = []
    for t in trades:
        if "pnl" not in t or "exit_ts" not in t:
            continue
        try:
            ts = datetime.fromisoformat(t["exit_ts"])
            if ts >= exp_start:
                valid.append(t)
        except:
            pass

    total  = len(valid)
    wins   = [t for t in valid if t["pnl"] > 0]
    losses = [t for t in valid if t["pnl"] < 0]
    longs  = [t for t in valid if t.get("side") == "long"]
    shorts = [t for t in valid if t.get("side") == "short"]
    lw     = [t for t in longs  if t["pnl"] > 0]
    sw     = [t for t in shorts if t["pnl"] > 0]

    gross_win  = sum(t["pnl"] for t in wins)
    gross_loss = abs(sum(t["pnl"] for t in losses))

    return {
        "total"        : total,
        "longs"        : len(longs),
        "shorts"       : len(shorts),
        "wins"         : len(wins),
        "losses"       : len(losses),
        "win_rate"     : len(wins)/total*100 if total else 0,
        "long_wr"      : len(lw)/len(longs)*100 if longs else 0,
        "short_wr"     : len(sw)/len(shorts)*100 if shorts else 0,
        "total_pnl"    : sum(t["pnl"] for t in valid),
        "profit_factor": gross_win/gross_loss if gross_loss else 999.0,
        "avg_win"      : gross_win/len(wins) if wins else 0,
        "avg_loss"     : -gross_loss/len(losses) if losses else 0,
        "trades"       : valid,
    }

# ── 判斷結論 ─────────────────────────────────────────────────────
def verdict(new: dict) -> tuple[str, str]:
    """回傳 (結論文字, 顏色關鍵字 green/yellow/red)"""
    pf           = new["profit_factor"]
    short_incr   = new["shorts"] > BASELINE["shorts"]
    pf_ok        = pf >= BASELINE["profit_factor"]

    if pf >= 1.38 and short_incr:
        return "✅ 確認新條件（空單增加且PF維持）", "green"
    elif pf < 1.0:
        return "❌ 建議回滾原條件（PF低於1.0）", "red"
    elif pf_ok and not short_incr:
        return "⚠️ PF維持但空單未明顯增加，可繼續觀察2週", "yellow"
    else:
        return f"⚠️ PF={pf:.2f} 略低於基準1.38，建議再觀察2週", "yellow"

# ── PDF ──────────────────────────────────────────────────────────
def build_pdf(new: dict, vtext: str, vcolor: str, out_path: str):
    from reportlab.lib.pagesizes import A4
    from reportlab.lib import colors
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer,
                                    Table, TableStyle, HRFlowable)
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    cjk = "Helvetica"
    for fp in ["/System/Library/Fonts/PingFang.ttc",
               "/Library/Fonts/Arial Unicode MS.ttf",
               "/System/Library/Fonts/STHeiti Light.ttc"]:
        if Path(fp).exists():
            try:
                pdfmetrics.registerFont(TTFont("CJK", fp))
                cjk = "CJK"
                break
            except:
                pass

    C_DARK   = colors.HexColor("#1a1a2e")
    C_BLUE   = colors.HexColor("#16213e")
    C_ACCENT = colors.HexColor("#0f3460")
    C_RED    = colors.HexColor("#c0392b")
    C_GREEN  = colors.HexColor("#27ae60")
    C_YELLOW = colors.HexColor("#d4ac0d")
    C_GREY   = colors.HexColor("#888888")
    C_CODE   = colors.HexColor("#2d2d2d")
    C_CODEBG = colors.HexColor("#f0f0f0")

    def ps(name, size=10, color=C_DARK, bold=False, leading=None):
        return ParagraphStyle(name, fontName=cjk, fontSize=size,
                              textColor=color, leading=leading or size*1.6,
                              spaceAfter=3)

    doc = SimpleDocTemplate(out_path, pagesize=A4,
                            leftMargin=1.8*cm, rightMargin=1.8*cm,
                            topMargin=1.5*cm, bottomMargin=1.5*cm)
    W = A4[0] - 3.6*cm
    story = []

    s_title  = ps("title",  18, C_DARK)
    s_sub    = ps("sub",     9, C_GREY)
    s_h2     = ps("h2",     13, C_BLUE, leading=22)
    s_normal = ps("normal", 10, C_DARK)
    s_small  = ps("small",   8, C_GREY)
    s_green  = ps("green",  11, C_GREEN)
    s_red    = ps("red",    11, C_RED)
    s_yellow = ps("yellow", 11, C_YELLOW)
    s_code   = ps("code",    9, C_CODE, leading=14)

    vc_style = {"green": s_green, "red": s_red, "yellow": s_yellow}.get(vcolor, s_normal)
    now_str  = datetime.now().strftime("%Y-%m-%d %H:%M")

    # ── 標題 ──────────────────────────────────────────────────────
    tt = Table([[
        Paragraph("NFES RSI 實驗報告", s_title),
        Paragraph(f"實驗期：2026-06-06 ~ 2026-06-27<br/>產生：{now_str}", s_sub),
    ]], colWidths=[W*0.6, W*0.4])
    tt.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,-1), C_ACCENT),
        ("TEXTCOLOR",  (0,0), (-1,-1), colors.white),
        ("VALIGN",     (0,0), (-1,-1), "MIDDLE"),
        ("LEFTPADDING", (0,0), (-1,-1), 12),
        ("RIGHTPADDING",(0,0), (-1,-1), 12),
        ("TOPPADDING",  (0,0), (-1,-1), 10),
        ("BOTTOMPADDING",(0,0),(-1,-1), 10),
        ("ROUNDEDCORNERS", [6]),
    ]))
    story += [tt, Spacer(1, 0.4*cm)]

    # ── 第一章：實驗說明 ──────────────────────────────────────────
    story.append(Paragraph("第一章　實驗說明", s_h2))
    story.append(HRFlowable(width=W, thickness=1, color=C_BLUE))
    story.append(Spacer(1, 0.2*cm))
    story.append(Paragraph("【原條件（v2）】空單 RSI > 50 ／ 多單 RSI < 65", s_normal))
    story.append(Paragraph("問題：熊市中 4H RSI 長期壓在 30~50，空單幾乎全被攔截（3週共 2843 次）", s_normal))
    story.append(Spacer(1, 0.2*cm))
    story.append(Paragraph("【新條件（v2.1）】動能斜率判斷，擇一滿足：", s_normal))
    story.append(Paragraph("空單 條件1：RSI斜率向下（最近3根下降）AND RSI < 55", s_normal))
    story.append(Paragraph("空單 條件2：RSI曾反彈至35~40區間但未站上，且當前RSI重新下彎", s_normal))
    story.append(Paragraph("多單 條件1：RSI斜率向上（最近3根上升）AND RSI > 45", s_normal))
    story.append(Paragraph("多單 條件2：RSI曾回測至60~65區間但未跌破，且當前RSI重新上揚", s_normal))
    story.append(Spacer(1, 0.4*cm))

    # ── 第二章：數據比較 ─────────────────────────────────────────
    story.append(Paragraph("第二章　數據比較", s_h2))
    story.append(HRFlowable(width=W, thickness=1, color=C_BLUE))
    story.append(Spacer(1, 0.2*cm))

    pf_new = new["profit_factor"]
    pf_new_str = f"{pf_new:.2f}" if pf_new < 900 else "N/A（無虧損）"
    short_diff = new["shorts"] - BASELINE["shorts"]
    short_diff_str = f"+{short_diff}" if short_diff >= 0 else str(short_diff)

    tbl_data = [
        ["指標", "原條件（基準）", "新條件（實驗）", "變化"],
        ["觀察期間", BASELINE["period"], "2026-06-06~06-27", "—"],
        ["總交易筆數", str(BASELINE["total"]), str(new["total"]), f"{new['total']-BASELINE['total']:+d}"],
        ["多單筆數",   str(BASELINE["longs"]),  str(new["longs"]),  f"{new['longs']-BASELINE['longs']:+d}"],
        ["空單筆數",   str(BASELINE["shorts"]), str(new["shorts"]), short_diff_str],
        ["整體勝率",   f"{BASELINE['win_rate']:.1f}%",  f"{new['win_rate']:.1f}%",  f"{new['win_rate']-BASELINE['win_rate']:+.1f}%"],
        ["多單勝率",   f"{BASELINE['long_wr']:.1f}%",   f"{new['long_wr']:.1f}%",   f"{new['long_wr']-BASELINE['long_wr']:+.1f}%"],
        ["空單勝率",   f"{BASELINE['short_wr']:.1f}%",  f"{new['short_wr']:.1f}%",  f"{new['short_wr']-BASELINE['short_wr']:+.1f}%"],
        ["總損益",     f"+{BASELINE['total_pnl']:.2f} U", f"{new['total_pnl']:+.2f} U", f"{new['total_pnl']-BASELINE['total_pnl']:+.2f} U"],
        ["Profit Factor", str(BASELINE["profit_factor"]), pf_new_str, f"{pf_new-BASELINE['profit_factor']:+.2f}" if pf_new < 900 else "—"],
    ]
    col_w = [W*0.28, W*0.26, W*0.26, W*0.20]
    tbl = Table(tbl_data, colWidths=col_w)
    style = [
        ("BACKGROUND", (0,0), (-1,0), C_ACCENT),
        ("TEXTCOLOR",  (0,0), (-1,0), colors.white),
        ("FONTNAME",   (0,0), (-1,-1), cjk),
        ("FONTSIZE",   (0,0), (-1,-1), 9),
        ("GRID",       (0,0), (-1,-1), 0.5, colors.HexColor("#cccccc")),
        ("ROWBACKGROUNDS", (0,1), (-1,-1), [colors.white, colors.HexColor("#f5f7fa")]),
        ("ALIGN",      (1,0), (-1,-1), "CENTER"),
        ("VALIGN",     (0,0), (-1,-1), "MIDDLE"),
        ("TOPPADDING", (0,0), (-1,-1), 5),
        ("BOTTOMPADDING",(0,0),(-1,-1), 5),
    ]
    # 空單筆數行（index 4）highlight
    if short_diff > 0:
        style.append(("TEXTCOLOR", (3, 4), (3, 4), C_GREEN))
    elif short_diff < 0:
        style.append(("TEXTCOLOR", (3, 4), (3, 4), C_RED))
    tbl.setStyle(TableStyle(style))
    story += [tbl, Spacer(1, 0.4*cm)]

    # ── 第三章：結論 ─────────────────────────────────────────────
    story.append(Paragraph("第三章　結論與建議", s_h2))
    story.append(HRFlowable(width=W, thickness=1, color=C_BLUE))
    story.append(Spacer(1, 0.2*cm))
    story.append(Paragraph(vtext, vc_style))
    story.append(Spacer(1, 0.3*cm))

    if vcolor == "red":
        story.append(Paragraph("建議立即執行回滾指令（見第四章）", s_red))
    elif vcolor == "yellow":
        story.append(Paragraph("建議再觀察 2 週（至 2026-07-11），週一週報持續追蹤", s_normal))
    else:
        story.append(Paragraph("新條件表現良好，可確認為正式條件，繼續使用", s_green))
    story.append(Spacer(1, 0.4*cm))

    # ── 第四章：回滾指令 ─────────────────────────────────────────
    story.append(Paragraph("第四章　回滾原條件（如需叫回）", s_h2))
    story.append(HRFlowable(width=W, thickness=1, color=C_BLUE))
    story.append(Spacer(1, 0.2*cm))
    story.append(Paragraph("原條件備份位置：", s_normal))
    story.append(Paragraph("versions/nfes_signal_bot_v2_rsi_original_20260606.py", s_code))
    story.append(Spacer(1, 0.15*cm))
    story.append(Paragraph("執行以下兩行指令即可叫回原條件：", s_normal))

    cmd_tbl = Table([
        [Paragraph(
            "cp ~/Desktop/ai/versions/nfes_signal_bot_v2_rsi_original_20260606.py "
            "~/Desktop/ai/nfes_signal_bot.py\n"
            "supervisorctl restart nfes_bot",
            s_code
        )]
    ], colWidths=[W])
    cmd_tbl.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,-1), C_CODEBG),
        ("LEFTPADDING",(0,0),(-1,-1), 10),
        ("RIGHTPADDING",(0,0),(-1,-1), 10),
        ("TOPPADDING", (0,0),(-1,-1), 8),
        ("BOTTOMPADDING",(0,0),(-1,-1), 8),
        ("BOX", (0,0),(-1,-1), 1, C_GREY),
    ]))
    story += [cmd_tbl, Spacer(1, 0.2*cm)]
    story.append(Paragraph("⚠️ 回滾後原本 v2.1 的邏輯將停用，恢復空單RSI>50 / 多單RSI<65", s_small))
    story.append(Spacer(1, 0.4*cm))

    # ── 第五章：本期空單明細 ─────────────────────────────────────
    story.append(Paragraph("第五章　本期空單明細", s_h2))
    story.append(HRFlowable(width=W, thickness=1, color=C_BLUE))
    story.append(Spacer(1, 0.2*cm))
    shorts_trades = [t for t in new["trades"] if t.get("side") == "short"]
    if shorts_trades:
        rows = [["幣種", "進場價", "損益(U)", "出場原因", "時間"]]
        for t in sorted(shorts_trades, key=lambda x: x.get("exit_ts","")):
            rows.append([
                t.get("sym","?").replace("USDT",""),
                str(t.get("entry_px","?")),
                f"{t['pnl']:+.2f}",
                t.get("reason","?"),
                t.get("exit_ts","?")[:10],
            ])
        st = Table(rows, colWidths=[W*0.15, W*0.18, W*0.15, W*0.22, W*0.30])
        st.setStyle(TableStyle([
            ("BACKGROUND", (0,0),(-1,0), C_ACCENT),
            ("TEXTCOLOR",  (0,0),(-1,0), colors.white),
            ("FONTNAME",   (0,0),(-1,-1), cjk),
            ("FONTSIZE",   (0,0),(-1,-1), 8),
            ("GRID",       (0,0),(-1,-1), 0.5, colors.HexColor("#cccccc")),
            ("ROWBACKGROUNDS",(0,1),(-1,-1),[colors.white, colors.HexColor("#f5f7fa")]),
            ("ALIGN",      (0,0),(-1,-1), "CENTER"),
            ("VALIGN",     (0,0),(-1,-1), "MIDDLE"),
            ("TOPPADDING", (0,0),(-1,-1), 4),
            ("BOTTOMPADDING",(0,0),(-1,-1), 4),
        ]))
        story.append(st)
    else:
        story.append(Paragraph("本期無空單交易", s_small))

    doc.build(story)

# ── 主流程 ────────────────────────────────────────────────────────
async def main():
    print("[RSI實驗] 開始產生報告...")
    new    = load_experiment_stats()
    vtext, vcolor = verdict(new)

    pdf_path = str(REPORT_DIR / "NFES_RSI實驗報告_20260627.pdf")
    build_pdf(new, vtext, vcolor, pdf_path)
    print(f"[RSI實驗] PDF 產生完成 → {pdf_path}")

    caption = (
        f"📊 <b>NFES RSI 實驗報告（3週結果）</b>\n"
        f"新條件：2026-06-06 ~ 2026-06-27\n"
        f"空單筆數：{new['shorts']} 筆 ／ 勝率：{new['short_wr']:.1f}%\n"
        f"PF：{new['profit_factor']:.2f}（基準 1.38）\n"
        f"{vtext}"
    )
    await tg_pdf(pdf_path, caption)
    print("[RSI實驗] TG 傳送完成")

if __name__ == "__main__":
    asyncio.run(main())
