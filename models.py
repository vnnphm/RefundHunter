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


class FindProduct(Model):     # coordinator -> price agent: a receipt item with no link
    item_id: str
    user_id: str
    query: str                # item name from the receipt
    store: str = "amazon"


class ProductMatch(Model):
    name: str
    product_url: str
    price: Optional[float] = None
    image_url: str = ""


class FindResult(Model):      # price agent -> coordinator
    item_id: str
    user_id: str
    ok: bool
    matches: list[ProductMatch] = []
    error: Optional[str] = None


# Phase 3: refund requests (see "Refund requests" in AGENTS.md)
class PolicyCheck(Model):     # coordinator -> policy agent, when a drop is found
    item_id: str
    store: str                # "amazon" | "walmart" | "other" today; "target" | "bestbuy" later
    purchase_date: str = ""   # YYYY-MM-DD; "" if unknown (item added by hand)
    price_paid: float
    current_price: float


class PolicyResult(Model):    # policy agent -> coordinator
    item_id: str
    ok: bool
    action: str = "none"      # "price_adjustment" | "goodwill_credit" | "return_rebuy" | "none"
    deadline: str = ""        # YYYY-MM-DD, last day to act; "" if the purchase date is unknown
    where: str = ""           # how to reach the store, with its help/chat link
    summary: str = ""         # one line for the card, e.g. "Return & rebuy by Nov 12 to save $6.02"
    source_url: str = ""      # the policy page the rule comes from
    error: Optional[str] = None


class ClaimRequest(Model):    # coordinator -> claim agent, when the user taps "Draft a refund request"
    item_id: str
    user_id: str
    store: str
    item_name: str
    product_url: str = ""
    price_paid: float
    current_price: float
    purchase_date: str = ""   # YYYY-MM-DD, from the receipt
    order_number: str = ""
    action: str = ""          # from PolicyResult; "" = the claim agent picks its default for the store
    deadline: str = ""        # from PolicyResult; never guessed


class ClaimDraft(Model):      # claim agent -> coordinator
    item_id: str
    user_id: str
    ok: bool
    action: str = ""
    message: str = ""         # ready to paste, plain text
    where: str = ""           # how to send it (PolicyResult.where, or the claim agent's default)
    error: Optional[str] = None
