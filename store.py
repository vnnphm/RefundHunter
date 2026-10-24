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
    purchase_date TEXT NOT NULL DEFAULT '',  -- YYYY-MM-DD from a receipt, else ''
    returnable    INTEGER,                   -- policy agent (Phase 3): 1 / 0 / NULL = unknown
    return_days   INTEGER,
    price_adjust_days INTEGER,
    policy_summary TEXT NOT NULL DEFAULT ''
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


# Columns added after the first release: CREATE TABLE IF NOT EXISTS won't add them to an old DB.
ITEM_COLUMNS = {
    "purchase_date": "TEXT NOT NULL DEFAULT ''",
    "returnable": "INTEGER",
    "return_days": "INTEGER",
    "price_adjust_days": "INTEGER",
    "policy_summary": "TEXT NOT NULL DEFAULT ''",
}


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    have = {r["name"] for r in conn.execute("PRAGMA table_info(items)")}
    for col, kind in ITEM_COLUMNS.items():
        if col not in have:
            conn.execute(f"ALTER TABLE items ADD COLUMN {col} {kind}")
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def add_item(item_id: str, user_id: str, product_url: str, store: str,
             price_paid: float, name: str = "", purchase_date: str = "") -> bool:
    """Save a new item. Returns False if this item_id was already saved."""
    with connect() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO items (item_id, user_id, product_url, store, name,"
            " price_paid, added_at, purchase_date) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (item_id, user_id, product_url, store, name, price_paid,
             datetime.now(timezone.utc).isoformat(), purchase_date))
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


def save_policy(item_id: str, returnable: bool | None, return_days: int | None,
                price_adjust_days: int | None, summary: str):
    with connect() as conn:
        conn.execute(
            "UPDATE items SET returnable = ?, return_days = ?, price_adjust_days = ?, policy_summary = ?"
            " WHERE item_id = ?",
            (None if returnable is None else int(returnable), return_days, price_adjust_days, summary, item_id))


def list_items(user_id: str) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM items WHERE user_id = ? ORDER BY added_at DESC", (user_id,))
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
