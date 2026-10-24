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
    added_at      TEXT NOT NULL
)
"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(SCHEMA)
    return conn


def add_item(item_id: str, user_id: str, product_url: str, store: str,
             price_paid: float, name: str = "") -> bool:
    """Save a new item. Returns False if this item_id was already saved."""
    with connect() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO items (item_id, user_id, product_url, store, name,"
            " price_paid, added_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (item_id, user_id, product_url, store, name, price_paid,
             datetime.now(timezone.utc).isoformat()))
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
