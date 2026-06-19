#!/usr/bin/env python3
"""每週交易報告產生器 — 繁體中文 PDF + Telegram 發送"""

import json, os, requests
from pathlib import Path
from datetime import datetime
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable
)
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

# ── 字型註冊（Arial Unicode 支援繁中）────────────────────────────
FONT_PATH = "/Library/Fonts/Arial Unicode.ttf"
pdfmetrics.registerFont(TTFont("ZH",   FONT_PATH))
pdfmetrics.registerFont(TTFont("ZH-B", FONT_PATH))   # 無粗體字型，以同檔模擬
FONT      = "ZH"
FONT_B    = "ZH-B"

# ── 設定 ─────────────────────────────────────────────────────────
BASE       = Path(__file__).parent
TG_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "8005879844:AAG8DJoaphzsweVmdvMB6SNphJdRy0osQGo")
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID",   "1768177615")
OUT_PDF    = BASE / "weekly_report.pdf"

INITIAL_CAPITAL = 10_000.0
WEEK_START = "2026-05-05"
WEEK_END   = "2026-05-11"

# ── 顏色 ─────────────────────────────────────────────────────────
C_DARK   = colors.HexColor("#1a1a2e")
C_BLUE   = colors.HexColor("#0f3460")
C_NAVY   = colors.HexColor("#16213e")
C_GREEN  = colors.HexColor("#27ae60")
C_RED    = colors.HexColor("#e74c3c")
C_GOLD   = colors.HexColor("#f39c12")
C_GREY   = colors.HexColor("#95a5a6")
C_LIGHT  = colors.HexColor("#f4f6f8")
C_WHITE  = colors.white

# ── 工具函式 ─────────────────────────────────────────────────────
def fmt(v):
    sign = "+" if v >= 0 else ""
    return f"{sign}{v:,.2f} USDT"

def upnl(p):
    cur  = p.get("cur_px", p["entry_px"])
    side = p.get("side", "long")
    return (cur - p["entry_px"]) / p["entry_px"] * p["notional"] if side == "long" \
      else (p["entry_px"] - cur) / p["entry_px"] * p["notional"]

def week_trades(trades):
    return [t for t in trades if WEEK_START <= str(t.get("exit_ts",""))[:10] <= WEEK_END]

# ── 樣式 ─────────────────────────────────────────────────────────
def ps(name, size=9, color=C_DARK, bold=False, align=TA_LEFT,
       space_before=0, space_after=2, leading=None, bg=None):
    kw = dict(fontName=FONT_B if bold else FONT,
              fontSize=size,
              textColor=color,
              alignment=align,
              spaceBefore=space_before,
              spaceAfter=space_after,
              leading=leading or size * 1.4)
    if bg:
        kw["backColor"] = bg
    return ParagraphStyle(name, **kw)

# ── 表格樣式 ─────────────────────────────────────────────────────
def base_table_style(header_bg=C_BLUE):
    return TableStyle([
        ("BACKGROUND",    (0, 0), (-1, 0),  header_bg),
        ("TEXTCOLOR",     (0, 0), (-1, 0),  C_WHITE),
        ("FONTNAME",      (0, 0), (-1, 0),  FONT_B),
        ("FONTSIZE",      (0, 0), (-1, 0),  8.5),
        ("ALIGN",         (0, 0), (-1, 0),  "CENTER"),
        ("FONTNAME",      (0, 1), (-1, -1), FONT),
        ("FONTSIZE",      (0, 1), (-1, -1), 8.5),
        ("ROWBACKGROUNDS",(0, 1), (-1, -1), [C_LIGHT, C_WHITE]),
        ("GRID",          (0, 0), (-1, -1), 0.3, C_GREY),
        ("TOPPADDING",    (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING",   (0, 0), (-1, -1), 7),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 7),
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
    ])

# ── 主報告 ───────────────────────────────────────────────────────
def build_pdf():
    e_state = json.loads((BASE / "ema99_bot_state.json").read_text(encoding="utf-8"))
    n_state = json.loads((BASE / "nfes_bot_state.json").read_text(encoding="utf-8"))

    e_trades = e_state.get("trades", [])
    e_pos    = e_state.get("positions", {})
    e_cap    = e_state.get("capital", 0)
    n_trades = n_state.get("trades", [])
    n_pos    = n_state.get("positions", {})

    we = week_trades(e_trades)
    wn = week_trades(n_trades)

    we_pnl  = sum(t["pnl"] for t in we)
    wn_pnl  = sum(t.get("pnl", 0) for t in wn)
    total_w = we_pnl + wn_pnl

    we_wins = sum(1 for t in we if t["pnl"] > 0)
    wn_wins = sum(1 for t in wn if t.get("pnl", 0) > 0)

    e_open    = sum(upnl(p) for p in e_pos.values())
    n_open    = sum(upnl(p) for p in n_pos.values())
    n_margin  = sum(p.get("margin", 0) for p in n_pos.values())
    net_worth = e_cap + e_open + n_margin + n_open
    ret_pct   = (net_worth - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100

    e_losses = sorted([t for t in we if t["pnl"] < -5], key=lambda x: x["pnl"])
    n_losses = sorted([t for t in wn if t.get("pnl", 0) < -5], key=lambda x: x.get("pnl", 0))

    # ── 文件設定 ──────────────────────────────────────────────────
    doc = SimpleDocTemplate(
        str(OUT_PDF), pagesize=A4,
        leftMargin=18*mm, rightMargin=18*mm,
        topMargin=14*mm,  bottomMargin=14*mm
    )
    W = A4[0] - 36*mm
    story = []

    # ── 標題橫幅 ─────────────────────────────────────────────────
    banner = Table(
        [[Paragraph("交易機器人 週報告", ps("t", size=20, color=C_WHITE, bold=True, align=TA_CENTER)),
          Paragraph(f"2026-05-05 ~ 2026-05-11\n產生時間：{datetime.now().strftime('%Y-%m-%d %H:%M')}",
                    ps("ts", size=9, color=C_GREY, align=TA_RIGHT))]],
        colWidths=[W * 0.55, W * 0.45]
    )
    banner.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, -1), C_DARK),
        ("TOPPADDING",    (0, 0), (-1, -1), 12),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 12),
        ("LEFTPADDING",   (0, 0), (-1, -1), 12),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 12),
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
    ]))
    story.append(banner)
    story.append(Spacer(1, 8))

    # ── 1. 資金總覽 ───────────────────────────────────────────────
    story.append(Paragraph("一、資金總覽", ps("h2", size=13, color=C_BLUE, bold=True,
                                              space_before=4, space_after=5)))

    def card(label, value, val_color=C_DARK):
        return [
            Paragraph(label, ps("cl", size=8, color=C_WHITE, align=TA_CENTER)),
            Paragraph(value, ps("cv", size=13, color=val_color, bold=True, align=TA_CENTER)),
        ]

    w_color = C_GREEN if total_w >= 0 else C_RED
    r_color = C_GREEN if ret_pct >= 0 else C_RED

    cards = Table([[
        card("本週淨損益",   fmt(total_w),          w_color),
        card("EMA99 可用資金", f"{e_cap:,.0f} USDT", C_WHITE),
        card("NFES 持倉市值", f"{n_margin+n_open:,.0f} USDT", C_WHITE),
        card("綜合淨值",    f"{net_worth:,.0f} USDT", C_GOLD),
        card("累計報酬率",  f"{ret_pct:+.1f}%",      r_color),
    ]], colWidths=[W / 5] * 5)
    cards.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, -1), C_DARK),
        ("GRID",          (0, 0), (-1, -1), 0.5, C_BLUE),
        ("TOPPADDING",    (0, 0), (-1, -1), 10),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
    ]))
    story.append(cards)
    story.append(Spacer(1, 10))

    # ── 2. 本週損益明細 ───────────────────────────────────────────
    story.append(Paragraph("二、本週損益明細", ps("h2", size=13, color=C_BLUE, bold=True,
                                                  space_before=2, space_after=5)))

    e_big_loss = min((t["pnl"] for t in e_losses), default=0)
    n_big_loss = min((t.get("pnl", 0) for t in n_losses), default=0)
    total_cnt  = len(we) + len(wn)
    total_wins = we_wins + wn_wins

    rows2 = [
        ["策略", "已實現損益", "交易次數", "勝率", "最大單筆虧損"],
        ["🔷 EMA99 Bot",
         fmt(we_pnl),
         f"{len(we)} 筆",
         f"{we_wins / len(we) * 100:.1f}%" if we else "—",
         fmt(e_big_loss)],
        ["🔶 NFES Signal Bot",
         fmt(wn_pnl),
         f"{len(wn)} 筆",
         f"{wn_wins / len(wn) * 100:.1f}%" if wn else "—",
         fmt(n_big_loss)],
        ["合計",
         fmt(total_w),
         f"{total_cnt} 筆",
         f"{total_wins / total_cnt * 100:.1f}%" if total_cnt else "—",
         ""],
    ]
    ts2 = base_table_style()
    ts2.add("BACKGROUND", (0, 3), (-1, 3), C_NAVY)
    ts2.add("TEXTCOLOR",  (0, 3), (-1, 3), C_WHITE)
    ts2.add("FONTNAME",   (0, 3), (-1, 3), FONT_B)
    for ri, row in enumerate(rows2[1:], 1):
        try:
            v = float(row[1].replace("+", "").replace(",", "").replace(" USDT", ""))
            ts2.add("TEXTCOLOR", (1, ri), (1, ri), C_GREEN if v >= 0 else C_RED)
            ts2.add("FONTNAME",  (1, ri), (1, ri), FONT_B)
        except:
            pass
    t2 = Table(rows2, colWidths=[W * 0.24, W * 0.20, W * 0.16, W * 0.16, W * 0.24])
    t2.setStyle(ts2)
    story.append(t2)
    story.append(Spacer(1, 10))

    # ── 3. 虧損原因分析 ───────────────────────────────────────────
    story.append(Paragraph("三、虧損原因分析", ps("h2", size=13, color=C_BLUE, bold=True,
                                                  space_before=2, space_after=5)))

    # EMA99 大額虧損
    story.append(Paragraph("🔷 EMA99 Bot — 重大虧損（超過 -30 USDT）",
                            ps("h3", size=10, bold=True, space_before=2, space_after=4)))
    causes_e = {
        "SIREN":    "低流動性小幣，波動劇烈，入場後市場急速崩跌",
        "HIVE":     "市場整體急跌，止損位未能有效保本，損失擴大",
        "BZ":       "低市值幣種，缺乏價格支撐，突破後快速反轉",
        "ICP":      "大幣趨勢研判失誤，高槓桿放大單次虧損",
        "UB":       "部分止盈後，剩餘倉位遭遇趨勢反轉跌破保本位",
        "TAO":      "進場時機不佳，AI板塊整體回調影響",
        "AAVE":     "進場後直接跌破止損位，無法有效保本",
        "BANANA":   "低流動性，滑點大，止損執行價格偏差",
        "CHIP":     "題材炒作退燒，快速回落至止損區間",
        "1000LUNC": "歷史高波動幣種，止損設定偏寬，損失超出預期",
        "XMR":      "隱私幣種走勢獨立，與市場相關性低，趨勢研判困難",
    }
    big_e = [t for t in e_losses if t["pnl"] < -30]
    if big_e:
        rows_e = [["幣種", "日期", "虧損金額", "原因分析"]]
        for t in big_e:
            sym   = t.get("sym", "").split("/")[0].replace(":USDT", "").replace("1000", "1000")
            key   = sym.replace("1000", "").replace("USDT", "")
            date  = str(t.get("exit_ts", ""))[:10]
            cause = causes_e.get(key, "止損觸發，波動幅度超出預期")
            rows_e.append([sym, date, fmt(t["pnl"]), cause])
        tse = base_table_style()
        for i in range(1, len(rows_e)):
            tse.add("TEXTCOLOR", (2, i), (2, i), C_RED)
            tse.add("FONTNAME",  (2, i), (2, i), FONT_B)
        te = Table(rows_e, colWidths=[W * 0.16, W * 0.13, W * 0.17, W * 0.54])
        te.setStyle(tse)
        story.append(te)

    story.append(Spacer(1, 7))

    # NFES 大額虧損
    story.append(Paragraph("🔶 NFES Signal Bot — 重大虧損（超過 -30 USDT）",
                            ps("h3b", size=10, bold=True, space_before=2, space_after=4)))
    causes_n = {
        "HIGH":  "動態倉位機制上線前，舊固定倉位（100U×5x=500U名義）過大，單筆虧損放大",
        "BIO":   "進場訊號觸發後市場急速下跌，止損位設定偏寬，損失超出預期",
        "ETH":   "大幣趨勢逆轉，NFES 4H週期訊號存在時間延遲，入場偏晚",
        "ORCA":  "止損設定合理，但市場單邊下跌速度超出預期",
    }
    big_n = [t for t in n_losses if t.get("pnl", 0) < -30]
    if big_n:
        rows_n = [["幣種", "日期", "虧損金額", "原因分析"]]
        for t in big_n:
            sym   = t.get("sym", "").replace("USDT", "")
            date  = str(t.get("exit_ts", ""))[:10]
            cause = causes_n.get(sym, "止損觸發，市場波動超出預期")
            rows_n.append([sym, date, fmt(t.get("pnl", 0)), cause])
        tsn = base_table_style()
        for i in range(1, len(rows_n)):
            tsn.add("TEXTCOLOR", (2, i), (2, i), C_RED)
            tsn.add("FONTNAME",  (2, i), (2, i), FONT_B)
        tn = Table(rows_n, colWidths=[W * 0.13, W * 0.13, W * 0.17, W * 0.57])
        tn.setStyle(tsn)
        story.append(tn)

    story.append(Spacer(1, 10))

    # ── 4. 改善建議 ───────────────────────────────────────────────
    story.append(Paragraph("四、改善建議", ps("h2", size=13, color=C_BLUE, bold=True,
                                              space_before=2, space_after=5)))

    improve = [
        ["編號", "問題描述", "建議改善方式", "優先度"],
        ["1", "EMA99 單筆虧損過大（-62~-111 USDT）",
         "低市值、高波動幣種加入黑名單，\n或限制最大倉位比例",
         "高優先"],
        ["2", "NFES HIGH 單筆 -266 USDT（舊固定倉位）",
         "已修復：動態倉位 = 總資金×10%\n此問題不再發生",
         "已修復"],
        ["3", "NFES 本週勝率偏低（30.8%，4/13）",
         "考慮僅接受強訊號 ▲+/▼+，\n過濾普通訊號以提升精準度",
         "中優先"],
        ["4", "NFES SL 方向設定錯誤（DUSDT）",
         "已修復：進場前驗證 SL 方向，\n異常訊號直接攔截不進場",
         "已修復"],
        ["5", "重啟後重複觸發同一訊號（DUSDT 兩次）",
         "已修復：bar_ts 持久化存檔，\n重啟後不重新進場",
         "已修復"],
        ["6", "電腦關機導致 Bot 停止運行",
         "待辦：搬遷至雲端 VPS\n（Oracle Free Tier / DigitalOcean）",
         "高優先"],
    ]
    ts4 = base_table_style(header_bg=C_NAVY)
    priority_colors = {
        "高優先": C_RED,
        "中優先": C_GOLD,
        "已修復": C_GREEN,
    }
    for i, row in enumerate(improve[1:], 1):
        c = priority_colors.get(row[3], C_DARK)
        ts4.add("TEXTCOLOR", (3, i), (3, i), c)
        ts4.add("FONTNAME",  (3, i), (3, i), FONT_B)
    t4 = Table(improve, colWidths=[W * 0.06, W * 0.26, W * 0.46, W * 0.22])
    t4.setStyle(ts4)
    story.append(t4)
    story.append(Spacer(1, 10))

    # ── 5. 版本迭代記錄 ──────────────────────────────────────────
    story.append(Paragraph("五、版本迭代記錄", ps("h2", size=13, color=C_BLUE, bold=True,
                                                  space_before=2, space_after=5)))

    versions = [
        ["版本", "日期", "更新內容", "Git 標籤"],
        ["v1.0", "2026-05-08 前",
         "基礎版：EMA99 + NFES 雙機器人、7x24 Supervisor 守護\nChart.js 資產曲線圖、JSONBin 雲端同步",
         "git checkout v1.0"],
        ["v1.1", "2026-05-11",
         "TG 指令合併雙策略顯示（/持倉 /盈虧 /狀態 /歷史）\nNFES 動態倉位（總資金10% + 訊號槓桿 1~5x）\nBug 修復：SL 方向驗證 + bar_ts 持久化防重入",
         "git checkout v1.1"],
        ["v1.2", "規劃中",
         "待辦：搬遷至雲端 VPS 不停機\nEMA99 高風險幣種黑名單機制\nNFES 強訊號過濾（僅 ▲+/▼+）",
         "（開發中）"],
    ]
    ts5 = base_table_style(header_bg=C_NAVY)
    ts5.add("TEXTCOLOR", (0, 3), (-1, 3), C_GREY)
    ts5.add("FONTNAME",  (3, 1), (3, 2),  FONT_B)
    ts5.add("TEXTCOLOR", (3, 1), (3, 1),  C_GREY)
    ts5.add("TEXTCOLOR", (3, 2), (3, 2),  C_GOLD)
    t5 = Table(versions, colWidths=[W * 0.10, W * 0.15, W * 0.52, W * 0.23])
    t5.setStyle(ts5)
    story.append(t5)

    story.append(Spacer(1, 6))
    # 回溯說明
    rb_text = (
        "版本回溯指令：  "
        "git checkout v1.0（還原至舊版）  |  "
        "git checkout main（回到最新版）  |  "
        "git tag -l（查看所有版本）"
    )
    story.append(Paragraph(rb_text,
        ps("rb", size=8, color=C_DARK,
           bg=colors.HexColor("#eaf0fb"), space_before=2)))

    story.append(Spacer(1, 10))

    # ── 頁尾 ─────────────────────────────────────────────────────
    story.append(HRFlowable(width=W, thickness=0.5, color=C_GREY))
    story.append(Spacer(1, 4))
    footer = Table([[
        Paragraph("EMA99 Bot  +  NFES Signal Bot  |  7x24 Supervisor 全天候守護",
                  ps("fl", size=7, color=C_GREY, align=TA_LEFT)),
        Paragraph(f"週報 v1.1  |  {datetime.now().strftime('%Y-%m-%d %H:%M')}",
                  ps("fr", size=7, color=C_GREY, align=TA_RIGHT)),
    ]], colWidths=[W * 0.6, W * 0.4])
    footer.setStyle(TableStyle([
        ("TOPPADDING",    (0,0),(-1,-1), 0),
        ("BOTTOMPADDING", (0,0),(-1,-1), 0),
    ]))
    story.append(footer)

    doc.build(story)
    print(f"PDF 已產生：{OUT_PDF}")


# ── 發送 Telegram ─────────────────────────────────────────────────
def send_telegram(we_pnl, wn_pnl, total_w):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendDocument"
    w_icon = "📈" if total_w >= 0 else "📉"
    caption = (
        f"{w_icon} <b>週報 2026-05-05 ~ 05-11</b>\n"
        f"本週淨損益：<b>{fmt(total_w)}</b>\n"
        f"🔷 EMA99：{fmt(we_pnl)}  |  🔶 NFES：{fmt(wn_pnl)}\n"
        f"版本：v1.1（本週修復 3 個 Bug）\n"
        f"詳細分析請見附件 PDF"
    )
    with open(OUT_PDF, "rb") as f:
        r = requests.post(url, data={
            "chat_id":    TG_CHAT_ID,
            "caption":    caption,
            "parse_mode": "HTML",
        }, files={"document": ("週報_2026-05-11.pdf", f, "application/pdf")}, timeout=30)
    if r.ok:
        print("Telegram 發送成功 ✅")
    else:
        print(f"Telegram 發送失敗：{r.status_code} {r.text}")


# ── 入口 ─────────────────────────────────────────────────────────
if __name__ == "__main__":
    e_state = json.loads((BASE / "ema99_bot_state.json").read_text(encoding="utf-8"))
    n_state = json.loads((BASE / "nfes_bot_state.json").read_text(encoding="utf-8"))
    we = week_trades(e_state.get("trades", []))
    wn = week_trades(n_state.get("trades", []))
    we_pnl  = sum(t["pnl"] for t in we)
    wn_pnl  = sum(t.get("pnl", 0) for t in wn)
    total_w = we_pnl + wn_pnl

    build_pdf()
    send_telegram(we_pnl, wn_pnl, total_w)
