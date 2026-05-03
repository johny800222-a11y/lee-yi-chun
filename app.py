#!/usr/bin/env python3
"""
兒童點餐系統後端 — Flask + SQLite
"""
import sqlite3, os, json, requests
from datetime import datetime
from flask import Flask, send_file, request, jsonify, g

app = Flask(__name__)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH  = os.path.join(BASE_DIR, "breakfast.db")
TG_TOKEN   = "8727554075:AAFEJM-6vCxgDvCGYn22potnDH8gQ2JbI0U"
TG_CHAT_ID = "1768177615"
JSONBIN_API_KEY = os.environ.get("JSONBIN_API_KEY", "$2a$10$Paag0S516c7p2hVgS4quJ.z6QfZugOE5rHOkuDfGxfWOmCP8x5iN6")
JSONBIN_BIN_ID  = os.environ.get("JSONBIN_BIN_ID_BREAKFAST", "69f6a7cc36566621a81b4010")

# ── CORS ──────────────────────────────────────────────
def cors(resp):
    resp.headers["Access-Control-Allow-Origin"]  = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    resp.headers["Access-Control-Allow-Methods"] = "GET,POST,PUT,DELETE,OPTIONS"
    return resp

@app.after_request
def after(resp): return cors(resp)

@app.route("/", defaults={"path": ""}, methods=["OPTIONS"])
@app.route("/<path:path>", methods=["OPTIONS"])
def options(_path=""): return jsonify({}), 200

# ── DB ────────────────────────────────────────────────
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode=WAL")
    return g.db

@app.teardown_appcontext
def close_db(_):
    db = g.pop("db", None)
    if db: db.close()

def init_db():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.executescript("""
        CREATE TABLE IF NOT EXISTS menu_items (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            name     TEXT NOT NULL,
            price    INTEGER NOT NULL DEFAULT 0,
            desc     TEXT DEFAULT '',
            emoji    TEXT DEFAULT '🍽',
            image    TEXT DEFAULT '',
            category TEXT DEFAULT '飲料',
            active   INTEGER DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS inventory (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id  INTEGER UNIQUE REFERENCES menu_items(id),
            quantity INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS inventory_log (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id    INTEGER REFERENCES menu_items(id),
            action     TEXT,
            quantity   INTEGER,
            note       TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now','localtime'))
        );
        CREATE TABLE IF NOT EXISTS orders (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            items_json TEXT,
            wishes_json TEXT,
            total      INTEGER,
            created_at TEXT DEFAULT (datetime('now','localtime'))
        );
    """)
    # 種子資料
    count = cur.execute("SELECT COUNT(*) FROM menu_items").fetchone()[0]
    if count == 0:
        seeds = [
            ("紅茶",    25, "清香回甘，解渴首選", "🍵", "飲料"),
            ("奶茶",    30, "香濃奶茶，順口滑順", "🥛", "飲料"),
            ("豆漿",    25, "營養豆漿，健康滿分", "🧃", "飲料"),
            ("美式咖啡",40, "濃郁咖啡，提神醒腦", "☕️","飲料"),
            ("綠茶",    25, "清新綠茶，回甘無比", "🍃", "飲料"),
            ("珍珠奶茶",45, "Q彈珍珠，超受歡迎",  "🧋", "飲料"),
            ("荷包蛋",  15, "新鮮雞蛋，香嫩可口", "🍳", "小菜"),
            ("培根",    20, "香脆培根，鹹香下飯", "🥓", "小菜"),
            ("薯餅",    20, "外酥內軟，小孩最愛", "🥔", "小菜"),
            ("起司片",  10, "濃郁起司，越吃越香", "🧀", "小菜"),
        ]
        for s in seeds:
            cur.execute("INSERT INTO menu_items(name,price,desc,emoji,category) VALUES(?,?,?,?,?)", s)
            item_id = cur.lastrowid
            cur.execute("INSERT INTO inventory(item_id,quantity) VALUES(?,?)", (item_id, 20))
    # 舊資料庫補欄位（migration）
    try:
        cur.execute("ALTER TABLE menu_items ADD COLUMN image TEXT DEFAULT ''")
    except Exception:
        pass
    con.commit()
    con.close()

init_db()
restore_from_jsonbin()

# ── 工具 ──────────────────────────────────────────────
def row_list(rows): return [dict(r) for r in rows]

def sync_to_jsonbin():
    """把 menu_items 同步到 JSONBin（每次寫入後呼叫）"""
    try:
        con = sqlite3.connect(DB_PATH)
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT id,name,price,desc,emoji,image,category,active FROM menu_items"
        ).fetchall()
        con.close()
        data = {"menu_items": [dict(r) for r in rows]}
        requests.put(
            f"https://api.jsonbin.io/v3/b/{JSONBIN_BIN_ID}",
            headers={"Content-Type": "application/json",
                     "X-Master-Key": JSONBIN_API_KEY},
            json=data, timeout=8
        )
    except Exception as e:
        print("JSONBin sync error:", e)

def restore_from_jsonbin():
    """啟動時從 JSONBin 拉回 menu_items 並 upsert 進 SQLite"""
    try:
        r = requests.get(
            f"https://api.jsonbin.io/v3/b/{JSONBIN_BIN_ID}/latest",
            headers={"X-Master-Key": JSONBIN_API_KEY},
            timeout=8
        )
        items = r.json().get("record", {}).get("menu_items", [])
        if not items:
            return
        con = sqlite3.connect(DB_PATH)
        cur = con.cursor()
        for it in items:
            cur.execute("""
                INSERT INTO menu_items(id,name,price,desc,emoji,image,category,active)
                VALUES(:id,:name,:price,:desc,:emoji,:image,:category,:active)
                ON CONFLICT(id) DO UPDATE SET
                    name=excluded.name, price=excluded.price,
                    desc=excluded.desc, emoji=excluded.emoji,
                    image=excluded.image, category=excluded.category,
                    active=excluded.active
            """, it)
            cur.execute("""
                INSERT INTO inventory(item_id,quantity)
                VALUES(:id, 20)
                ON CONFLICT(item_id) DO NOTHING
            """, it)
        con.commit()
        con.close()
        print(f"✅ Restored {len(items)} items from JSONBin")
    except Exception as e:
        print("JSONBin restore error:", e)

def send_telegram(text):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "Markdown"},
            timeout=5
        )
    except Exception as e:
        print("TG error:", e)

# ── 靜態頁面 ──────────────────────────────────────────
@app.route("/")
def index():
    p = os.path.join(BASE_DIR, "menu.html")
    return send_file(p) if os.path.exists(p) else ("menu.html not found", 404)

@app.route("/admin")
def admin():
    p = os.path.join(BASE_DIR, "admin.html")
    return send_file(p) if os.path.exists(p) else ("admin.html not found", 404)

@app.route("/health")
def health(): return "ok", 200

# ── 品項 API ──────────────────────────────────────────
@app.route("/api/menu", methods=["GET"])
def api_menu():
    db = get_db()
    rows = db.execute("""
        SELECT m.id, m.name, m.price, m.desc, m.emoji, m.image, m.category,
               COALESCE(i.quantity,0) AS quantity
        FROM menu_items m
        LEFT JOIN inventory i ON i.item_id = m.id
        WHERE m.active = 1
        ORDER BY m.id
    """).fetchall()
    return jsonify(row_list(rows))

@app.route("/api/menu", methods=["POST"])
def api_menu_add():
    d = request.json
    db = get_db()
    cur = db.execute(
        "INSERT INTO menu_items(name,price,desc,emoji,image,category) VALUES(?,?,?,?,?,?)",
        (d["name"], d.get("price", 0), d.get("desc",""),
         d.get("emoji","🍽"), d.get("image",""), d.get("category","飲料"))
    )
    item_id = cur.lastrowid
    init_qty = int(d.get("initial_stock", 0))
    db.execute("INSERT INTO inventory(item_id,quantity) VALUES(?,?)", (item_id, init_qty))
    if init_qty > 0:
        db.execute("INSERT INTO inventory_log(item_id,action,quantity,note) VALUES(?,?,?,?)",
                   (item_id, "in", init_qty, "初始庫存"))
    db.commit()
    sync_to_jsonbin()
    return jsonify({"ok": True, "id": item_id})

@app.route("/api/menu/<int:item_id>", methods=["PUT"])
def api_menu_update(item_id):
    d = request.json
    db = get_db()
    fields = []
    vals   = []
    for col in ("name","price","desc","emoji","image","category"):
        if col in d:
            fields.append(f"{col}=?")
            vals.append(d[col])
    if not fields:
        return jsonify({"ok": False, "msg": "no fields"}), 400
    vals.append(item_id)
    db.execute(f"UPDATE menu_items SET {','.join(fields)} WHERE id=?", vals)
    db.commit()
    sync_to_jsonbin()
    return jsonify({"ok": True})

@app.route("/api/menu/<int:item_id>", methods=["DELETE"])
def api_menu_delete(item_id):
    db = get_db()
    db.execute("UPDATE menu_items SET active=0 WHERE id=?", (item_id,))
    db.commit()
    sync_to_jsonbin()
    return jsonify({"ok": True})

# ── 庫存 API ──────────────────────────────────────────
@app.route("/api/inventory", methods=["GET"])
def api_inventory():
    db = get_db()
    rows = db.execute("""
        SELECT m.id, m.name, m.price, m.desc, m.emoji, m.image, m.category,
               COALESCE(i.quantity,0) AS quantity
        FROM menu_items m
        LEFT JOIN inventory i ON i.item_id = m.id
        WHERE m.active = 1
        ORDER BY m.id
    """).fetchall()
    return jsonify(row_list(rows))

@app.route("/api/inventory/in", methods=["POST"])
def api_inv_in():
    d   = request.json
    iid = int(d["item_id"])
    qty = int(d["quantity"])
    note = d.get("note","")
    db  = get_db()
    db.execute("UPDATE inventory SET quantity = quantity + ? WHERE item_id=?", (qty, iid))
    db.execute("INSERT INTO inventory_log(item_id,action,quantity,note) VALUES(?,?,?,?)",
               (iid, "in", qty, note))
    db.commit()
    new_qty = db.execute("SELECT quantity FROM inventory WHERE item_id=?", (iid,)).fetchone()["quantity"]
    return jsonify({"ok": True, "quantity": new_qty})

@app.route("/api/inventory/out", methods=["POST"])
def api_inv_out():
    d   = request.json
    iid = int(d["item_id"])
    qty = int(d["quantity"])
    note = d.get("note","")
    db  = get_db()
    db.execute("UPDATE inventory SET quantity = MAX(0, quantity - ?) WHERE item_id=?", (qty, iid))
    db.execute("INSERT INTO inventory_log(item_id,action,quantity,note) VALUES(?,?,?,?)",
               (iid, "out", qty, note))
    db.commit()
    new_qty = db.execute("SELECT quantity FROM inventory WHERE item_id=?", (iid,)).fetchone()["quantity"]
    return jsonify({"ok": True, "quantity": new_qty})

@app.route("/api/inventory/log", methods=["GET"])
def api_inv_log():
    db = get_db()
    rows = db.execute("""
        SELECT l.id, l.action, l.quantity, l.note, l.created_at,
               m.name, m.emoji
        FROM inventory_log l
        JOIN menu_items m ON m.id = l.item_id
        ORDER BY l.id DESC LIMIT 50
    """).fetchall()
    return jsonify(row_list(rows))

# ── 訂單 API ──────────────────────────────────────────
@app.route("/api/order", methods=["POST"])
def api_order():
    d      = request.json
    items  = d.get("items", [])
    wishes = d.get("wishes", [])
    total  = d.get("total", 0)
    person = d.get("person", "")
    db     = get_db()

    # 儲存訂單
    db.execute(
        "INSERT INTO orders(items_json,wishes_json,total) VALUES(?,?,?)",
        (json.dumps(items, ensure_ascii=False),
         json.dumps(wishes, ensure_ascii=False), total)
    )

    # 扣庫存
    for it in items:
        iid = it["item_id"]
        qty = it["qty"]
        db.execute("UPDATE inventory SET quantity = MAX(0, quantity - ?) WHERE item_id=?", (qty, iid))
        db.execute("INSERT INTO inventory_log(item_id,action,quantity,note) VALUES(?,?,?,?)",
                   (iid, "order", qty, "訂單扣除"))

    db.commit()

    # Telegram 通知（只發一則）
    now = datetime.now().strftime("%m/%d %H:%M")
    person_emoji = "👧" if person == "QUEENA" else "👦"
    person_line  = f"{person_emoji} *{person}*\n" if person else ""
    msg = f"🍱 *明日餐點訂單*\n{person_line}📅 {now}\n{'─'*20}\n"
    if items:
        msg += "\n📋 *點餐清單*\n"
        for it in items:
            msg += f"{it['emoji']} {it['name']} x{it['qty']}　${it['price']*it['qty']}\n"
        msg += f"\n💰 *合計：${total}*"
    send_telegram(msg)

    return jsonify({"ok": True})

@app.route("/api/orders", methods=["GET"])
def api_orders():
    db = get_db()
    rows = db.execute("SELECT * FROM orders ORDER BY id DESC LIMIT 20").fetchall()
    result = []
    for r in rows:
        o = dict(r)
        o["items"] = json.loads(o["items_json"])
        o["wishes"] = json.loads(o["wishes_json"])
        result.append(o)
    return jsonify(result)

# ── 啟動 ──────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=True)
