-- 在 Supabase SQL Editor 執行此檔案

CREATE TABLE IF NOT EXISTS expenses (
  id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
  date        DATE        NOT NULL,
  person      TEXT        NOT NULL CHECK (person IN ('群', '萱')),
  category    TEXT        NOT NULL DEFAULT '其他',
  description TEXT        NOT NULL DEFAULT '',
  amount      NUMERIC     NOT NULL CHECK (amount > 0),
  source      TEXT        NOT NULL DEFAULT 'manual', -- manual | line | invoice
  invoice_no  TEXT,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 加速月份查詢
CREATE INDEX IF NOT EXISTS expenses_date_idx ON expenses (date);

-- Row Level Security（開放讀寫，部署後可依需求鎖定）
ALTER TABLE expenses ENABLE ROW LEVEL SECURITY;
CREATE POLICY "allow all" ON expenses FOR ALL USING (true) WITH CHECK (true);
