from typing import Optional

from uagents import Model


class TrackItem(Model):       # coordinator/parser -> price agent
    item_id: str              # "it_" + 8 hex chars
    user_id: str              # ASI:One sender address
    product_url: str
    store: str                # "amazon" | "walmart" | "other"


class PriceResult(Model):     # price agent -> coordinator
    item_id: str
    ok: bool
    current_price: Optional[float] = None
    title: Optional[str] = None
    checked_at: str = ""      # ISO 8601 UTC
    error: Optional[str] = None


class Receipt(Model):         # webhook -> parser agent (Phase 2)
    message_id: str
    user_id: str
    sender: str = ""
    subject: str = ""
    date: str = ""
    body: str = ""            # plain text, max 8,000 chars
