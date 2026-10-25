import json
import sqlite3
from datetime import datetime, timezone

DB_PATH = "refundhunter.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    item_id       TEXT PRIMARY KEY,
    user_id       TEXT NOT NULL,
    product_url   TEXT NOT NULL,
    store         TEXT NOT NULL,
    name          TEXT NOT NULL DEFAULT '',
    price_paid    REAL NOT NULL,
    current_price REAL,
    checked_at    TEXT NOT NULL DEFAULT '',
    added_at      TEXT NOT NULL,
    purchase_date TEXT NOT NULL DEFAULT '',  -- YYYY-MM-DD, from the receipt
    order_number  TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS links (
    email     TEXT PRIMARY KEY,   -- lowercase
    user_id   TEXT NOT NULL,      -- ASI:One address
    linked_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS receipts (
    message_id     TEXT PRIMARY KEY,
    user_id        TEXT NOT NULL,
    store          TEXT NOT NULL DEFAULT '',
    purchase_date  TEXT NOT NULL DEFAULT '',
    order_number   TEXT NOT NULL DEFAULT '',
    items_json     TEXT NOT NULL,  -- [{"name", "price_paid", "product_url"}]
    status         TEXT NOT NULL DEFAULT 'pending',  -- pending | tracked | dismissed
    created_at     TEXT NOT NULL
);
"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    have = {row["name"] for row in conn.execute("PRAGMA table_info(items)")}
    for col in ("purchase_date", "order_number"):  # added for refund requests; older databases lack them
        if col not in have:
            conn.execute(f"ALTER TABLE items ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def add_item(item_id: str, user_id: str, product_url: str, store: str,
             price_paid: float, name: str = "", purchase_date: str = "", order_number: str = "") -> bool:
    """Save a new item. Returns False if this item_id was already saved."""
    with connect() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO items (item_id, user_id, product_url, store, name,"
            " price_paid, added_at, purchase_date, order_number) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (item_id, user_id, product_url, store, name, price_paid,
             datetime.now(timezone.utc).isoformat(), purchase_date, order_number))
        return cur.rowcount == 1


def get_item(item_id: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM items WHERE item_id = ?", (item_id,)).fetchone()
        return dict(row) if row else None


def save_price(item_id: str, current_price: float, checked_at: str, title: str = ""):
    """Record the latest price. The scraped title fills in the name if the user left it blank."""
    with connect() as conn:
        conn.execute(
            "UPDATE items SET current_price = ?, checked_at = ?,"
            " name = CASE WHEN name = '' THEN ? ELSE name END WHERE item_id = ?",
            (current_price, checked_at, title or "", item_id))


def list_items(user_id: str) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM items WHERE user_id = ? ORDER BY added_at DESC", (user_id,))
        return [dict(r) for r in rows]


def items_due(checked_before: str, limit: int) -> list[dict]:
    """Items (any user) not price-checked since `checked_before` (ISO time), oldest first, for the price watch."""
    with connect() as conn:
        rows = conn.execute("SELECT * FROM items WHERE checked_at < ? ORDER BY checked_at LIMIT ?",
                            (checked_before, limit))
        return [dict(r) for r in rows]


def link_email(email: str, user_id: str):
    """Point an email address at an ASI:One user. Re-linking moves it."""
    with connect() as conn:
        conn.execute("INSERT OR REPLACE INTO links (email, user_id, linked_at) VALUES (?, ?, ?)",
                     (email.strip().lower(), user_id, _now()))


def user_for_email(email: str) -> str | None:
    with connect() as conn:
        row = conn.execute("SELECT user_id FROM links WHERE email = ?", (email.strip().lower(),)).fetchone()
        return row["user_id"] if row else None


def save_receipt(message_id: str, user_id: str, store: str, purchase_date: str,
                 order_number: str, items: list[dict]) -> bool:
    """Save a parsed receipt as pending. Returns False if this message_id was already saved."""
    with connect() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO receipts (message_id, user_id, store, purchase_date, order_number,"
            " items_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (message_id, user_id, store, purchase_date, order_number, json.dumps(items), _now()))
        return cur.rowcount == 1


def get_receipt(message_id: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM receipts WHERE message_id = ?", (message_id,)).fetchone()
    if not row:
        return None
    rec = dict(row)
    rec["items"] = json.loads(rec.pop("items_json"))
    return rec


def pending_receipts(user_id: str) -> list[dict]:
    with connect() as conn:
        rows = conn.execute("SELECT message_id FROM receipts WHERE user_id = ? AND status = 'pending'"
                            " ORDER BY created_at", (user_id,)).fetchall()
    return [get_receipt(r["message_id"]) for r in rows]


def set_receipt_status(message_id: str, status: str):
    with connect() as conn:
        conn.execute("UPDATE receipts SET status = ? WHERE message_id = ?", (status, message_id))
