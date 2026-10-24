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


class Receipt(Model):         # coordinator or webhook -> parser agent (Phase 2)
    message_id: str           # Gmail id, or "photo_" + uuid for uploads
    user_id: str
    sender: str = ""
    subject: str = ""
    date: str = ""
    body: str = ""            # email plain text, max 8,000 chars
    image_url: str = ""       # receipt photo URL from ASI:One (photo uploads)


class ReceiptItem(Model):
    name: str
    price_paid: float
    product_url: str = ""     # emails often have one; paper receipts don't


class ParsedReceipt(Model):   # parser agent -> coordinator
    message_id: str
    user_id: str
    ok: bool
    store: str = ""           # "amazon" | "walmart" | "other"
    purchase_date: str = ""   # YYYY-MM-DD
    order_number: str = ""
    items: list[ReceiptItem] = []
    error: Optional[str] = None


class PolicyRequest(Model):    # coordinator -> policy agent (Phase 3)
    item_id: str
    user_id: str
    store: str                 # "amazon" | "walmart" | "other"
    product_url: str
    purchase_date: str = ""    # YYYY-MM-DD
    price_paid: float
    current_price: Optional[float] = None


class PolicyResult(Model):     # policy agent -> coordinator
    item_id: str
    ok: bool
    status: str = ""           # "eligible" | "not_eligible" | "unknown" | "unsupported"
    eligible: Optional[bool] = None
    deadline: str = ""         # YYYY-MM-DD
    policy_type: str = ""      # demo heuristic name
    explanation: str = ""      # user-safe reason
    source_url: str = ""       # where to verify policy manually
    error: Optional[str] = None
