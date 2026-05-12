# 群萱記帳本 — 部署說明

## 1. Supabase（資料庫）

1. 前往 https://supabase.com → 建立新專案
2. 進入 **SQL Editor** → 貼上 `supabase_schema.sql` 執行
3. 到 **Settings > API** 複製：
   - `Project URL` → SUPABASE_URL
   - `anon public key` → SUPABASE_ANON_KEY

---

## 2. LINE Bot

1. 前往 https://developers.line.biz → 建立 **Messaging API Channel**
2. 取得：
   - Channel Secret → LINE_CHANNEL_SECRET
   - Channel Access Token → LINE_CHANNEL_ACCESS_TOKEN
3. Webhook URL 部署後填入（見步驟 4）

---

## 3. 財政部電子發票 API

1. 前往 https://www.einvoice.nat.gov.tw
2. 登入 → 申請「手機條碼載具」（如已有請略過）
3. 至「電子發票平台」申請 API 帳號，取得 APP_ID 和 API_KEY
4. 查詢自己的手機條碼（格式 `/XXXXXXX`）

---

## 4. Render（後端部署）

1. 前往 https://render.com → New Web Service
2. 連接 GitHub repo，Root Directory 設為 `backend`
3. Build Command: `npm install`
4. Start Command: `npm start`
5. 在 Environment Variables 填入所有 env 值
6. 取得部署 URL（如 `https://expense-api.onrender.com`）
7. 回到 LINE Developers → Webhook URL 填入 `https://expense-api.onrender.com/webhook`

---

## 5. Vercel（前端部署）

1. 前往 https://vercel.com → New Project
2. 連接 GitHub repo，Root Directory 設為 `frontend`
3. 部署完成後取得 URL（如 `https://expense-tracker.vercel.app`）
4. 回到 Render → 環境變數 FRONTEND_URL 填入此 URL

---

## LINE Bot 使用格式

```
群 早餐 85
萱 超市 日用品 320
群 計程車 交通 150 2026-05-10
幫助
```

格式：`[花費人] [描述] [分類(選填)] [金額] [日期(選填)]`

花費人：群、萱  
分類：餐飲、交通、購物、日用品、娛樂、醫療、其他
