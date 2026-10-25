import asyncio
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse
from uuid import uuid4

from dotenv import load_dotenv
from uagents import Agent, Context, Protocol
from uagents_core.contrib.protocols.chat import (
    ChatAcknowledgement, ChatMessage, EndSessionContent, MetadataContent,
    ResourceContent, StartSessionContent, TextContent, chat_protocol_spec,
)

import gmail
import store
from agentverse_profile import profile
from models import (ClaimDraft, ClaimRequest, FindProduct, FindResult, ParsedReceipt, PolicyCheck, PolicyResult,
                    PriceResult, ProductMatch, Receipt, TrackItem)

load_dotenv()

# Empty until the price agent is running: the coordinator mocks PriceResult instead.
PRICE_AGENT_ADDRESS = os.getenv("PRICE_AGENT_ADDRESS", "").strip()
# Empty until the parser agent is running: the coordinator mocks ParsedReceipt instead.
PARSER_AGENT_ADDRESS = os.getenv("PARSER_AGENT_ADDRESS", "").strip()
# Empty until the policy agent is running: drop cards just leave out "what to do" and deadlines.
POLICY_AGENT_ADDRESS = os.getenv("POLICY_AGENT_ADDRESS", "").strip()
# Empty until the claim agent is running: "Draft a refund request" shows a labelled test draft instead.
CLAIM_AGENT_ADDRESS = os.getenv("CLAIM_AGENT_ADDRESS", "").strip()
# The email webhook (server.py); only it may send us Receipts.
SERVER_ADDRESS = os.getenv("SERVER_ADDRESS", "").strip()
# Shared Pipedream address users forward order emails to.
RECEIPT_INBOX = os.getenv("RECEIPT_INBOX", "").strip()

SEED = os.getenv("AGENT_SEED", "")
if not SEED or SEED.startswith("change-me"):
    sys.exit("Set your own AGENT_SEED in .env (see README). Same seed = same address as someone else.")

agent = Agent(
    name="refund-hunter",
    seed=SEED,  # keep fixed: it sets the agent's address
    port=8001,
    mailbox=True,
    **profile("coordinator", "Finds money you're owed on past purchases. Connect Gmail or upload a receipt "
                             "photo; Refund Hunter tracks prices at Amazon, Walmart, Target and Best Buy, messages you "
                             "when it drops, and drafts the refund request to paste into the store's chat."),
)
chat = Protocol(spec=chat_protocol_spec)

URL_RE = re.compile(r"https?://[^\s\"'<>]+")
ITEM_ID_RE = re.compile(r"\bit_[0-9a-f]{8}\b")
ACTION_RE = re.compile(r"\b(submit_item|add_item|upload_receipt|list_items|view_item|link_email"
                       r"|connect_gmail|check_receipts|track_receipt|dismiss_receipt|draft_claim|recheck_item|check_prices)\b")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


# ---- Messages ----------------------------------------------------------------

def reply(text: str) -> ChatMessage:
    return ChatMessage(timestamp=datetime.now(timezone.utc), msg_id=uuid4(),
                       content=[TextContent(type="text", text=text)])


def card(text: str, kind: str, payload: dict) -> ChatMessage:
    return ChatMessage(timestamp=datetime.now(timezone.utc), msg_id=uuid4(), content=[
        TextContent(type="text", text=text),
        MetadataContent(type="metadata", metadata={
            "card_protocol_version": "1",
            "requires_card_interaction": "true",
            "card_kind": kind,
            "card_payload": json.dumps(payload),
        }),
    ])


def accepts_attachments() -> ChatMessage:
    """Tells ASI:One to show the attach button for this agent."""
    return ChatMessage(timestamp=datetime.now(timezone.utc), msg_id=uuid4(), content=[
        MetadataContent(type="metadata", metadata={"attachments": "true"})])


# ---- Cards -------------------------------------------------------------------

_next_send: dict[str, float] = {}  # user -> earliest time (monotonic) for their next message
SEND_GAP_S = 1.5  # ASI:One answers 429 to messages sent back-to-back, and drops them


async def say(ctx: Context, user: str, msg: ChatMessage):
    """Send a chat message to a user, spaced out so ASI:One doesn't rate-limit it."""
    now = time.monotonic()
    slot = max(now, _next_send.get(user, 0))
    _next_send[user] = slot + SEND_GAP_S
    if slot > now:
        await asyncio.sleep(slot - now)
    await ctx.send(user, msg)


def new_item_id() -> str:
    return "it_" + uuid4().hex[:8]


def menu_card(pending: int = 0) -> dict:
    review = [{"type": "button", "label": f"Review {pending} new receipt{'s' if pending != 1 else ''}",
               "primary": True, "action": {"selection": {"action": "check_receipts"}}}] if pending else []
    return {"root": {
        "type": "section",
        "title": "Refund Hunter",
        "subtitle": "Find money you're owed on past purchases",
        "children": [
            {"type": "text", "value": "Track an item to see if its price drops after you bought it."},
            {"type": "group", "direction": "row", "gap": 8, "children": [
                {"type": "button", "label": "Add an item", "primary": True,
                 "action": {"selection": {"action": "add_item"}}},
                {"type": "button", "label": "Upload a receipt photo",
                 "action": {"selection": {"action": "upload_receipt"}}},
            ]},
            {"type": "group", "direction": "row", "gap": 8, "children": [
                {"type": "button", "label": "My items",
                 "action": {"selection": {"action": "list_items"}}},
                {"type": "button", "label": "Connect Gmail",
                 "action": {"selection": {"action": "connect_gmail"}}},
            ]},
            *review,
        ],
    }}


def add_item_form(item_id: str, product_url: str = "", price_paid: float | None = None,
                  name: str = "", receipt: str = "") -> dict:
    """Asks only for what we don't already know: the link, the price paid, the name."""
    fields = [] if product_url else [
        {"name": "product_url", "kind": "text", "label": "Product link", "required": True}]
    if price_paid is None:
        # text, not number: ASI:One's number input is a whole-number stepper
        fields.append({"name": "price_paid", "kind": "text", "label": "Price you paid, e.g. 49.99",
                       "required": True})
    if not name:
        fields.append({"name": "name", "kind": "text", "label": "Item name (optional)"})
    selection = {"action": "submit_item", "item_id": item_id}
    if product_url:
        selection["product_url"] = product_url
    if price_paid is not None:
        selection["price_paid"] = price_paid
    if name:
        selection["name"] = name
    if receipt:  # so the item keeps the receipt's purchase date and order number
        selection["receipt"] = receipt
    return {
        "title": f"Track: {name[:60]}" if name else "Track an item",
        "fields": fields,
        "submit_cta": {"label": "Track price", "selection": selection},
    }


def link_email_form() -> dict:
    return {
        "title": "Link your email",
        "fields": [{"name": "email", "kind": "email", "label": "Email you'll forward receipts from",
                    "required": True}],
        "submit_cta": {"label": "Link email", "selection": {"action": "link_email"}},
    }


def receipt_review(rec: dict, mock: bool) -> dict:
    source = "receipt photo" if rec["message_id"].startswith("photo_") else "email"
    shop = shop_name(rec["store"]) or "store"
    rows = [{"label": it["name"][:60], "value": f"${it['price_paid']:.2f}"} for it in rec["items"]]
    if rec["purchase_date"]:
        rows.append({"label": "Bought", "value": rec["purchase_date"]})
    if rec["order_number"]:
        rows.append({"label": "Order", "value": rec["order_number"]})
    if mock:
        rows.append({"label": "Note", "value": "Test parse: parser agent not connected yet"})
    n = len(rec["items"])
    return {
        "title": f"From your {shop} {source}",
        "summary_rows": rows,
        "approve_cta": {"label": "Track this item" if n == 1 else f"Track these {n} items", "primary": True,
                        "selection": {"action": "track_receipt", "message_id": rec["message_id"]}},
        "reject_cta": {"label": "Not now",
                       "selection": {"action": "dismiss_receipt", "message_id": rec["message_id"]}},
    }


def price_detail(item: dict, mock: bool, policy: dict | None = None) -> dict:
    drop = item["price_paid"] - item["current_price"]
    rows = [
        {"label": "You paid", "value": f"${item['price_paid']:.2f}"},
        {"label": "Price now", "value": f"${item['current_price']:.2f}"},
        {"label": "Result", "value": f"Dropped ${drop:.2f}" if drop > 1 else "No change"},
        {"label": "Store", "value": shop_name(item["store"]) or "Other"},
        {"label": "Checked", "value": item["checked_at"][:16].replace("T", " ") + " UTC"},
    ]
    if drop > 1 and (policy or {}).get("summary"):  # from the policy agent
        rows.append({"label": "What to do", "value": policy["summary"][:140]})
    if mock:
        rows.append({"label": "Note", "value": "Test price: price agent not connected yet"})
    again = {"label": "Check again", "primary": drop <= 1,
             "selection": {"action": "recheck_item", "item_id": item["item_id"]}}
    see_all = {"label": "See all items", "selection": {"action": "list_items", "item_id": item["item_id"]}}
    if drop > 1:  # the money step: a message to paste into the store's chat
        ctas = [{"label": "Draft a refund request", "primary": True,
                 "selection": {"action": "draft_claim", "item_id": item["item_id"]}}, again, see_all]
    else:
        ctas = [again, {"label": "Track another item",
                        "selection": {"action": "add_item", "item_id": item["item_id"]}}, see_all]
    return {"title": item["name"] or "Your item", "summary_rows": rows, "ctas": ctas}


def items_carousel(items: list[dict]) -> dict:
    tiles = []
    for it in items[:10]:
        now = it["current_price"]
        if now is None:
            badge, secondary = {"label": "Checking…", "variant": "warning"}, "Not checked yet"
        elif it["price_paid"] - now > 1:
            badge = {"label": f"Dropped ${it['price_paid'] - now:.2f}", "variant": "success"}
            secondary = f"Now ${now:.2f}"
        else:
            badge, secondary = {"label": "No change", "variant": "info"}, f"Now ${now:.2f}"
        tiles.append({
            "id": it["item_id"],
            "title": it["name"] or (shop_name(it["store"]) or "Store") + " item",
            "subtitle": f"{shop_name(it['store']) or 'Other store'} · paid ${it['price_paid']:.2f}",
            "badges": [badge],
            "secondary_text": secondary,
            "primary_cta": {"label": "View",
                            "selection": {"action": "view_item", "item_id": it["item_id"]}},
        })
    return {"title": "Your tracked items",
            "subtitle": f"{len(items)} item{'s' if len(items) != 1 else ''}",
            "items": tiles}


SEARCHABLE = ("amazon", "walmart")  # stores the price agent can search by item name
PRICED = ("amazon", "walmart", "target", "bestbuy")  # stores the price agent can check


def match_carousel(item_id: str, name: str, price_paid: float, matches: list, shop: str = "amazon",
                   receipt: str = "") -> dict:
    """Store search results for a receipt item; tapping one tracks it like a filled-in form."""
    tiles = []
    for m in matches:
        if m.price and price_paid - m.price > 1:
            badge = {"label": f"${price_paid - m.price:.2f} less now", "variant": "success"}
        else:
            badge = {"label": shop_name(shop) or "Store", "variant": "info"}
        tiles.append({
            "id": m.product_url.rsplit("/", 1)[-1],
            "title": m.name[:80],
            "subtitle": m.product_url.rsplit("/", 1)[-1],  # ASIN / Walmart item id, to tell look-alikes apart
            "badges": [badge],
            "secondary_text": f"Now ${m.price:.2f}" if m.price else "Price unknown",
            "primary_cta": {"label": "This one", "selection": {
                "action": "submit_item", "item_id": item_id, "product_url": m.product_url,
                "price_paid": price_paid, "name": name, **({"receipt": receipt} if receipt else {})}},
        })
    return {"title": f"Which one is {name[:50]}?", "subtitle": f"You paid ${price_paid:.2f}", "items": tiles}


# ---- Parsing -----------------------------------------------------------------

def parse_selection(text: str) -> dict:
    """JSON for a direct @mention; prose when ASI:One's planner relays it."""
    # Card clicks can arrive as '@agent1… {"action": …}', so parse from the first brace.
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        try:
            data = json.loads(text[start:end + 1])
            if isinstance(data, dict):
                return data.get("selection") if isinstance(data.get("selection"), dict) else data
        except ValueError:
            pass
    sel: dict = {}
    lower = text.lower()
    # The planner usually repeats our action names verbatim; trust those first.
    named = ACTION_RE.search(lower)
    if m := ITEM_ID_RE.search(text):
        sel["item_id"] = m.group(0)
    if m := URL_RE.search(text):
        sel["product_url"] = m.group(0).rstrip(".,)")
    if m := re.search(r"\$\s*(\d+(?:\.\d{1,2})?)|price[^0-9$]{0,20}(\d+(?:\.\d{1,2})?)", lower):
        sel["price_paid"] = m.group(1) or m.group(2)
    if m := EMAIL_RE.search(text):
        sel["email"] = m.group(0)
    if named:
        sel["action"] = named.group(1)
    elif re.search(r"\b(draft|claim|refund request)", lower):  # before submit_item: it may quote a price
        sel["action"] = "draft_claim"
    elif re.search(r"\b(check|update|refresh)\w* (all |my |the )*prices?\b|\b(check again|re-?check)\b", lower):
        sel["action"] = "recheck_item" if "item_id" in sel else "check_prices"
    elif "item_id" in sel and "price_paid" in sel:
        sel["action"] = "submit_item"
    elif "link" in lower and ("email" in lower or "email" in sel):
        sel["action"] = "link_email"
    elif re.search(r"\b(gmail|connect)\b", lower):
        sel["action"] = "connect_gmail"
    elif re.search(r"\brec(ei|ie)pts\b", lower):  # "any new receipts?" (or "reciepts")
        sel["action"] = "check_receipts"
    elif re.search(r"\b(approved?|confirm(ed)?|track(ing)? (these|this|them|it))\b", lower) and re.search(r"rec(ei|ie)pt", lower):
        sel["action"] = "track_receipt"
    elif re.search(r"\b(dismiss(ed)?|reject(ed)?|not now|cancel(led)?)\b", lower):
        sel["action"] = "dismiss_receipt"
    elif re.search(r"rec(ei|ie)pt", lower) or "photo" in lower or "upload" in lower:
        sel["action"] = "upload_receipt"
    elif "add" in lower or "another" in lower:
        sel["action"] = "add_item"
    elif re.search(r"\b(see all|all items|my items)\b", lower):
        sel["action"] = "list_items"
    elif "item_id" in sel:  # a carousel tile was picked
        sel["action"] = "view_item"
    elif re.search(r"\b(my items|items|tracked|list)\b", lower):
        sel["action"] = "list_items"
    elif "track" in lower:
        sel["action"] = "add_item"
    return sel


STORE_NAMES = {"amazon": "Amazon", "walmart": "Walmart", "target": "Target", "bestbuy": "Best Buy"}


def store_for(url: str) -> str:
    host = urlparse(url).netloc.lower()
    if "amazon." in host or host.endswith("amzn.to"):
        return "amazon"
    if "walmart." in host:
        return "walmart"
    if host == "target.com" or host.endswith(".target.com"):
        return "target"
    if "bestbuy." in host:
        return "bestbuy"
    return "other"


def shop_name(store: str) -> str:
    """'Best Buy' for "bestbuy"; "" for "other" or unknown."""
    return STORE_NAMES.get(store, "")


def parse_price(value) -> float | None:
    try:
        price = float(str(value).replace("$", "").replace(",", "").strip())
    except ValueError:
        return None
    return price if price > 0 else None


def valid_url(url: str) -> bool:
    u = urlparse(url or "")
    return u.scheme in ("http", "https") and bool(u.netloc)


def image_url(item: ResourceContent) -> str | None:
    resources = item.resource if isinstance(item.resource, list) else [item.resource]
    for r in resources:
        for cand in [r.uri, *((r.metadata or {}).get(k, "") for k in ("url", "uri", "source", "image_url"))]:
            if valid_url(cand):
                return cand
    return None


# ---- Prices ------------------------------------------------------------------

def mock_price_result(req: TrackItem) -> PriceResult:
    """Stand-in for the price agent: about half of items show a 10% drop, the rest no change."""
    item = store.get_item(req.item_id)
    paid = item["price_paid"]
    dropped = int(req.item_id[3:], 16) % 2 == 0
    return PriceResult(item_id=req.item_id, ok=True,
                       current_price=round(paid * 0.9, 2) if dropped else paid,
                       checked_at=datetime.now(timezone.utc).isoformat())


async def request_price(ctx: Context, req: TrackItem):
    if PRICE_AGENT_ADDRESS:
        await ctx.send(PRICE_AGENT_ADDRESS, req)
        return
    ctx.logger.info(f"no PRICE_AGENT_ADDRESS, mocking price for {req.item_id}")
    await asyncio.sleep(2)  # feel like a real lookup
    await show_price(ctx, mock_price_result(req), mock=True)


async def show_price(ctx: Context, result: PriceResult, mock: bool = False):
    item = store.get_item(result.item_id)
    if item is None:
        ctx.logger.warning(f"price for unknown item {result.item_id}")
        return
    user = item["user_id"]
    quiet = ctx.storage.get(f"quiet:{item['item_id']}")  # a re-check from the watch or "check my prices"
    if quiet:
        ctx.storage.remove(f"quiet:{item['item_id']}")
    if not result.ok or not result.current_price or result.current_price <= 0:
        ctx.logger.info(f"price check failed for {result.item_id}: {result.error}")
        if quiet:
            await batch_done(ctx, quiet, failed=True)
        else:
            await say(ctx, user, reply("Couldn't get the current price for that item. Try again later."))
        return
    before = item["current_price"]
    store.save_price(result.item_id, result.current_price,
                     result.checked_at or datetime.now(timezone.utc).isoformat(), result.title or "")
    item = store.get_item(result.item_id)
    drop = item["price_paid"] - item["current_price"]
    if quiet:
        # The watch speaks up only when the price fell since its last check; "check my prices" shows every drop.
        fell = before is None or item["current_price"] < before - 0.005
        tell = drop > 1 and (quiet["by"] == "user" or fell)
        if tell:
            headline = (f"Good news: {item['name'] or 'an item you bought'} dropped ${drop:.2f} below what you paid!"
                        if quiet["by"] == "watch" else f"Price dropped ${drop:.2f}!")
            ctx.logger.info(f"{quiet['by']} re-check {item['item_id']}: drop ${drop:.2f}, telling {user}")
            await say(ctx, user, card(headline, "detail", price_detail(item, mock)))
            await ask_policy(ctx, item)
        await batch_done(ctx, quiet, dropped=tell)
        return
    headline = f"Price dropped ${drop:.2f}!" if drop > 1 else "No price drop yet."
    await say(ctx, user, card(headline, "detail", price_detail(item, mock)))
    if drop > 1:
        await ask_policy(ctx, item)


# ---- Re-checking prices: "Check again", "check my prices", and the price watch ----

WATCH_HOURS = float(os.getenv("PRICE_WATCH_HOURS", "6") or 0)  # re-check items older than this; 0 = off
WATCH_TICK_S = 600  # how often the watch looks for items due
WATCH_BATCH = 5  # items per tick: spreads Bright Data credits (1 per check) and our messages
CHECK_ALL_MAX = 10  # "check my prices" re-checks this many of the most recent items
QUIET_TTL_S = 1800  # a re-check with no answer after this is retried


async def recheck(ctx: Context, item: dict, by: str):
    """Ask for a fresh price without the usual card; show_price decides whether to speak up."""
    ctx.storage.set(f"quiet:{item['item_id']}", {"by": by, "user": item["user_id"], "at": time.time()})
    await request_price(ctx, TrackItem(item_id=item["item_id"], user_id=item["user_id"],
                                       product_url=item["product_url"], store=item["store"]))


async def batch_done(ctx: Context, quiet: dict, dropped: bool = False, failed: bool = False):
    """Count one answer of a "check my prices" batch; the last one sends the summary."""
    if quiet.get("by") != "user":
        return
    key = f"batch:{quiet['user']}"
    batch = ctx.storage.get(key)
    if not batch:
        return
    batch["left"] -= 1
    batch["drops"] += dropped
    batch["failed"] += failed
    if batch["left"] > 0:
        ctx.storage.set(key, batch)
        return
    ctx.storage.remove(key)
    n, drops, failed_n = batch["total"], batch["drops"], batch["failed"]
    same = n - drops - failed_n
    parts = ([f"{drops} dropped (above)"] if drops else []) + ([f"{same} no change"] if same else []) + \
        ([f"{failed_n} couldn't be checked"] if failed_n else [])
    watch = f" I'll keep checking every {WATCH_HOURS:g} hours and message you if one drops." \
        if WATCH_HOURS > 0 and PRICE_AGENT_ADDRESS else ""
    await say(ctx, quiet["user"], reply(f"Checked {n} item{'s' if n != 1 else ''}: {', '.join(parts)}.{watch}"))


@agent.on_interval(period=WATCH_TICK_S)
async def price_watch(ctx: Context):
    """Re-check prices on a schedule and tell users only about new drops."""
    if WATCH_HOURS <= 0 or not PRICE_AGENT_ADDRESS:
        return
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=WATCH_HOURS)).isoformat()
    sent = 0
    for item in store.items_due(cutoff, WATCH_BATCH * 3):
        busy = ctx.storage.get(f"quiet:{item['item_id']}")
        if busy and time.time() - busy.get("at", 0) < QUIET_TTL_S:
            continue  # already being re-checked
        await recheck(ctx, item, "watch")
        sent += 1
        if sent == WATCH_BATCH:
            break
    if sent:
        ctx.logger.info(f"price watch: re-checking {sent} item(s)")


async def ask_policy(ctx: Context, item: dict):
    """What to ask for and by when; the policy agent's answer follows the card as a message."""
    if POLICY_AGENT_ADDRESS:
        await ctx.send(POLICY_AGENT_ADDRESS, PolicyCheck(
            item_id=item["item_id"], store=item["store"], purchase_date=item.get("purchase_date") or "",
            price_paid=item["price_paid"], current_price=item["current_price"]))


@agent.on_message(model=PolicyResult)
async def on_policy_result(ctx: Context, sender: str, msg: PolicyResult):
    if not POLICY_AGENT_ADDRESS or sender != POLICY_AGENT_ADDRESS:
        ctx.logger.warning(f"ignoring PolicyResult from unexpected sender {sender}")
        return
    item = store.get_item(msg.item_id)
    if item is None or not msg.ok:
        ctx.logger.info(f"policy {msg.item_id} not used: {msg.error or 'unknown item'}")
        return
    ctx.storage.set(f"policy:{msg.item_id}", {"action": msg.action, "deadline": msg.deadline,
                                              "where": msg.where, "summary": msg.summary})
    ctx.logger.info(f"policy {msg.item_id} [{item['store']}] {msg.action} deadline={msg.deadline or '-'}")
    if msg.summary:
        more = " Tap Draft a refund request on the card above and I'll write the message." \
            if msg.action != "none" else ""
        await say(ctx, item["user_id"], reply(f"What you can do: {msg.summary}.{more}"))


@agent.on_message(model=PriceResult)
async def on_price_result(ctx: Context, sender: str, msg: PriceResult):
    if sender != PRICE_AGENT_ADDRESS:
        ctx.logger.warning(f"ignoring PriceResult from unexpected sender {sender}")
        return
    await show_price(ctx, msg)


# ---- Receipts ----------------------------------------------------------------

def mock_parse(r: Receipt) -> ParsedReceipt:
    """Stand-in for the parser agent: one item from the subject, the first $ amount and shop link."""
    price = re.search(r"\$\s*(\d[\d,]*(?:\.\d{2})?)", r.body)
    urls = [u.rstrip(".,)>") for u in URL_RE.findall(r.body)]
    url = next((u for u in urls if store_for(u) != "other"), "")
    if not price:
        return ParsedReceipt(message_id=r.message_id, user_id=r.user_id, ok=False, error="no price found")
    name = re.sub(r"^\s*((fwd?|fw):\s*)+", "", r.subject, flags=re.I).strip() or "Item from your email"
    lower = r.body.lower()
    shop = store_for(url) if url else next((k for k in STORE_NAMES if re.search(rf"\b{k}\b", lower.replace("best buy", "bestbuy"))), "other")
    return ParsedReceipt(message_id=r.message_id, user_id=r.user_id, ok=True, store=shop,
                         items=[{"name": name[:80], "price_paid": float(price.group(1).replace(",", "")),
                                 "product_url": url}])


_gmail_card_at: dict[str, float] = {}  # user -> when a Gmail import last pushed a review card
GMAIL_BURST_S = 120  # ASI:One rate-limits (429) a burst of cards; the rest wait as pending


async def show_parsed(ctx: Context, parsed: ParsedReceipt, mock: bool = False):
    user = parsed.user_id
    items = [{"name": it.name, "price_paid": it.price_paid, "product_url": it.product_url}
             for it in parsed.items if it.price_paid > 0] if parsed.ok else []
    if not items:
        ctx.logger.info(f"receipt {parsed.message_id} unreadable: {parsed.error or 'no priced items'}")
        if parsed.message_id.startswith("photo_"):  # emails arrive via a filter: stay quiet on non-receipts
            await say(ctx, user, reply("I got a receipt but couldn't read the items on it. You can add them by hand."))
        return
    if not store.save_receipt(parsed.message_id, user, parsed.store, parsed.purchase_date,
                              parsed.order_number, items):
        ctx.logger.info(f"receipt {parsed.message_id} already seen")
        return
    ctx.logger.info(f"receipt {parsed.message_id}: {len(items)} item(s) for {user}")
    text = "I found a new receipt. Want me to track it?"
    if parsed.message_id.startswith("gm_"):  # Gmail connect imports several at once: one card, rest pending
        if time.time() - _gmail_card_at.get(user, 0) < GMAIL_BURST_S:
            ctx.logger.info(f"receipt {parsed.message_id} saved as pending (Gmail import)")
            return
        _gmail_card_at[user] = time.time()
        text = "I found a receipt in your Gmail. Want me to track it? (Ask \"any new receipts?\" for the rest.)"
    rec = store.get_receipt(parsed.message_id)
    await say(ctx, user, card(text, "review", receipt_review(rec, mock)))
    await presearch(ctx, rec)


@agent.on_message(model=Receipt)
async def on_receipt(ctx: Context, sender: str, msg: Receipt):
    """An order email from server.py: find whose it is, then have it parsed."""
    if not SERVER_ADDRESS or sender != SERVER_ADDRESS:
        ctx.logger.warning(f"ignoring Receipt from unexpected sender {sender}")
        return
    if msg.user_id:  # Gmail connect: the user signed in from our link, so we know whose it is
        user = msg.user_id
        store.link_email(msg.sender, user)  # later forwards from this address work too
    else:
        user = store.user_for_email(msg.sender)
    if user is None:
        ctx.logger.info(f"receipt {msg.message_id} from an unlinked email (@{msg.sender.split('@')[-1]})")
        return
    fwd = msg.copy(update={"user_id": user})
    if PARSER_AGENT_ADDRESS:
        await ctx.send(PARSER_AGENT_ADDRESS, fwd)
    else:
        ctx.logger.info(f"no PARSER_AGENT_ADDRESS, mocking parse for {msg.message_id}")
        await show_parsed(ctx, mock_parse(fwd), mock=True)


@agent.on_message(model=ParsedReceipt)
async def on_parsed_receipt(ctx: Context, sender: str, msg: ParsedReceipt):
    if not PARSER_AGENT_ADDRESS or sender != PARSER_AGENT_ADDRESS:
        ctx.logger.warning(f"ignoring ParsedReceipt from unexpected sender {sender}")
        return
    await show_parsed(ctx, msg)


@agent.on_message(model=FindResult)
async def on_find_result(ctx: Context, sender: str, msg: FindResult):
    """Search results for a receipt item: let the user pick, or ask for the link."""
    if not PRICE_AGENT_ADDRESS or sender != PRICE_AGENT_ADDRESS:
        ctx.logger.warning(f"ignoring FindResult from unexpected sender {sender}")
        return
    key = f"find:{msg.item_id}"
    wanted = ctx.storage.get(key)
    if wanted is None:
        ctx.logger.info(f"search {msg.item_id} already handled")
        return
    if not wanted.get("approved", True):  # searched early: hold the matches until the user approves
        wanted.update(done=True, ok=msg.ok, error=msg.error, matches=[m.dict() for m in msg.matches])
        ctx.storage.set(key, wanted)
        ctx.logger.info(f"search {msg.item_id} ready before approve: {len(msg.matches)} match(es)")
        return
    ctx.storage.remove(key)
    await show_matches(ctx, msg.user_id, msg.item_id, wanted, msg.ok, msg.matches, msg.error)


async def show_matches(ctx: Context, user: str, item_id: str, wanted: dict, ok: bool,
                       matches: list, error: str | None):
    name, paid, shop = wanted["name"], wanted["price_paid"], wanted.get("store", "amazon")
    receipt = wanted.get("receipt", "")
    form = add_item_form(item_id, "", paid, name, receipt)
    if ok and matches:
        ctx.logger.info(f"search {item_id}: {len(matches)} match(es) for {user}")
        await say(ctx, user, card(f"Which one did you buy? You paid ${paid:.2f}.", "carousel",
                                  match_carousel(item_id, name, paid, matches, shop, receipt)))
        await say(ctx, user, card("Not listed? Paste the link.", "form", form))
    else:
        ctx.logger.info(f"search {item_id} found nothing: {error or 'no matches'}")
        await say(ctx, user, card(f"I couldn't find {name[:60]} on {shop_name(shop) or 'the store'}. What's the link?",
                                  "form", form))


def searchable(rec: dict, it: dict) -> bool:
    return bool(PRICE_AGENT_ADDRESS) and rec["store"] in SEARCHABLE and not valid_url(it["product_url"])


async def start_search(ctx: Context, user: str, it: dict, shop: str, approved: bool, receipt: str = "") -> str:
    item_id = new_item_id()
    ctx.storage.set(f"find:{item_id}", {"name": it["name"], "price_paid": it["price_paid"],
                                        "store": shop, "approved": approved, "receipt": receipt})
    await ctx.send(PRICE_AGENT_ADDRESS, FindProduct(item_id=item_id, user_id=user, query=it["name"], store=shop))
    return item_id


async def presearch(ctx: Context, rec: dict):
    """Search while the user reads the review card (Walmart takes about a minute), so Approve is quick."""
    key = f"pre:{rec['message_id']}"
    todo = [(i, it) for i, it in enumerate(rec["items"]) if searchable(rec, it)]
    if not todo or ctx.storage.has(key):
        return
    ctx.storage.set(key, {str(i): await start_search(ctx, rec["user_id"], it, rec["store"], approved=False,
                                                     receipt=rec["message_id"])
                          for i, it in todo})


def forget_presearch(ctx: Context, message_id: str):
    for item_id in (ctx.storage.get(f"pre:{message_id}") or {}).values():
        ctx.storage.remove(f"find:{item_id}")
    ctx.storage.remove(f"pre:{message_id}")


# ---- Refund requests (claim agent) -------------------------------------------

def claim_request(item: dict, policy: dict) -> ClaimRequest:
    return ClaimRequest(
        item_id=item["item_id"], user_id=item["user_id"], store=item["store"],
        item_name=item["name"] or "this item", product_url=item["product_url"],
        price_paid=item["price_paid"], current_price=item["current_price"],
        purchase_date=item.get("purchase_date") or "", order_number=item.get("order_number") or "",
        action=policy.get("action", ""), deadline=policy.get("deadline", ""))


def test_draft(req: ClaimRequest) -> ClaimDraft:
    """Stands in until the claim agent is connected; the reply says it's a test."""
    order = f" (order {req.order_number})" if req.order_number else ""
    return ClaimDraft(item_id=req.item_id, user_id=req.user_id, ok=True, action="price_adjustment",
                      where="the store's customer service chat", message=(
                          f"Hi, I bought {req.item_name}{order} for ${req.price_paid:.2f}. It's now "
                          f"${req.current_price:.2f}. Could I get a refund of the "
                          f"${req.price_paid - req.current_price:.2f} difference? Thank you."))


def short_date(value: str) -> str:
    """'Jan 18' from YYYY-MM-DD (the year too when it isn't this year); the input if it isn't a date."""
    try:
        d = datetime.strptime(value[:10], "%Y-%m-%d")
    except ValueError:
        return value
    year = f", {d.year}" if d.year != datetime.now(timezone.utc).year else ""
    return f"{d:%b} {d.day}{year}"


async def show_claim(ctx: Context, draft: ClaimDraft, mock: bool = False):
    """Where to send it, then the message alone so it copies cleanly."""
    if not draft.ok:
        ctx.logger.info(f"claim {draft.item_id} failed: {draft.error}")
        await say(ctx, draft.user_id, reply("I couldn't write that request right now. Try again in a minute."))
        return
    policy = ctx.storage.get(f"policy:{draft.item_id}") or {}  # from the policy agent, once it exists
    where = policy.get("where") or draft.where
    by = f" by {short_date(policy['deadline'])}" if policy.get("deadline") else ""
    note = " (test draft: the claim agent isn't connected yet)" if mock else ""
    ctx.logger.info(f"claim {draft.item_id}: {draft.action} draft for {draft.user_id}")  # never the text
    # The link goes last with nothing after it, so chat apps don't fold punctuation into the URL.
    await say(ctx, draft.user_id, reply(f"Here's your refund request{note}. Paste the next message{by} into {where}"))
    await say(ctx, draft.user_id, reply(draft.message))


@agent.on_message(model=ClaimDraft)
async def on_claim_draft(ctx: Context, sender: str, msg: ClaimDraft):
    if not CLAIM_AGENT_ADDRESS or sender != CLAIM_AGENT_ADDRESS:
        ctx.logger.warning(f"ignoring ClaimDraft from unexpected sender {sender}")
        return
    await show_claim(ctx, msg)


def find_receipt(sender: str, sel: dict, text: str) -> dict | None:
    """The receipt a review-card button refers to, even when the planner relays it as prose."""
    rec = store.get_receipt(sel.get("message_id", ""))
    if rec and rec["user_id"] == sender:
        return rec
    pending = store.pending_receipts(sender)
    named = next((r for r in pending if r["message_id"] in text), None)
    # Planner prose often drops the id: guess only when one receipt is waiting, never pick among several.
    return named or (pending[0] if len(pending) == 1 else None)


async def show_pending(ctx: Context, sender: str, intro: str = "Want me to track this?"):
    pending = store.pending_receipts(sender)
    if not pending:
        await say(ctx, sender, reply("No new receipts right now."))
    for rec in pending[:3]:
        await say(ctx, sender, card(intro, "review", receipt_review(rec, mock=not PARSER_AGENT_ADDRESS)))
        await presearch(ctx, rec)


# ---- Handlers ----------------------------------------------------------------

async def track_new_item(ctx: Context, sender: str, url: str, price: float, name: str,
                         item_id: str | None = None, receipt: dict | None = None):
    item_id = item_id or new_item_id()
    shop = store_for(url)
    rec = receipt or {}
    if not store.add_item(item_id, sender, url, shop, price, name,
                          rec.get("purchase_date", ""), rec.get("order_number", "")):
        await say(ctx, sender, reply("That item is already being tracked."))
        return
    ctx.logger.info(f"saved {item_id} ({shop}) for {sender}")
    where = f" at {shop_name(shop)}" if shop_name(shop) else ""
    note = "" if shop in PRICED else " (I can only check Amazon, Walmart, Target and Best Buy prices so far.)"
    await say(ctx, sender, reply(f"Tracking it: paid ${price:.2f}{where}. Checking price…{note}"))
    await request_price(ctx, TrackItem(item_id=item_id, user_id=sender, product_url=url, store=shop))


async def handle_text(ctx: Context, sender: str, text: str):
    sel = parse_selection(text)
    action = sel.get("action")

    if action == "submit_item":
        url = sel.get("product_url", "").strip()
        price = parse_price(sel.get("price_paid"))
        item_id = sel.get("item_id") or new_item_id()
        if not valid_url(url) or price is None:
            await say(ctx, sender, card(
                "I need a full product link (starting with https://) and the price you paid.",
                "form", add_item_form(item_id, url if valid_url(url) else "")))
            return
        rec = store.get_receipt(str(sel.get("receipt") or ""))
        await track_new_item(ctx, sender, url, price, str(sel.get("name") or "").strip(), item_id,
                             rec if rec and rec["user_id"] == sender else None)
    elif action == "link_email":
        email = (sel.get("email") or "").strip()
        if not EMAIL_RE.fullmatch(email):
            await say(ctx, sender, card("Which email do you get order confirmations at?", "form",
                                        link_email_form()))
            return
        store.link_email(email, sender)
        ctx.logger.info(f"linked an email (@{email.split('@')[-1]}) to {sender}")
        where = (f"Forward order emails from {email} to {RECEIPT_INBOX} and I'll pick them up."
                 if RECEIPT_INBOX else "Email forwarding isn't set up yet; check back soon.")
        await say(ctx, sender, reply(f"Linked {email}. {where}"))
    elif action == "connect_gmail":
        if not gmail.configured():
            await say(ctx, sender, reply("Gmail connect isn't set up yet. You can upload a receipt photo instead."))
            return
        ctx.logger.info(f"sent a Gmail connect link to {sender}")
        await say(ctx, sender, reply(
            f"Connect Gmail here: {gmail.connect_link(sender)}\n\n"
            "I'll read order emails from the last 30 days once (read-only), show each receipt here for you "
            "to review, and keep nothing else from your inbox. The link works for 15 minutes."))
    elif action == "check_receipts":
        await show_pending(ctx, sender)
    elif action == "track_receipt":
        rec = find_receipt(sender, sel, text)
        if rec is None and store.pending_receipts(sender):
            await show_pending(ctx, sender, "Which receipt? Tap Approve on the one you mean.")
            return
        if rec and rec["status"] == "tracked":  # a second tap, or the planner resending the click
            await say(ctx, sender, reply("Already on it: I'm tracking that receipt. If I'm still looking "
                                         "up an item, its matches will show up here in a minute."))
            return
        if rec is None or rec["status"] != "pending":
            await say(ctx, sender, reply("I don't have a receipt waiting for you to confirm."))
            return
        store.set_receipt_status(rec["message_id"], "tracked")
        early = ctx.storage.get(f"pre:{rec['message_id']}") or {}
        ctx.storage.remove(f"pre:{rec['message_id']}")
        for i, it in enumerate(rec["items"]):
            if valid_url(it["product_url"]):
                await track_new_item(ctx, sender, it["product_url"], it["price_paid"], it["name"], receipt=rec)
            elif searchable(rec, it):  # search for it, user picks
                item_id = early.get(str(i))
                wanted = ctx.storage.get(f"find:{item_id}") if item_id else None
                shop = shop_name(rec["store"])
                if wanted and wanted.get("done"):  # the early search already finished
                    ctx.storage.remove(f"find:{item_id}")
                    await show_matches(ctx, sender, item_id, wanted, wanted["ok"],
                                       [ProductMatch(**m) for m in wanted["matches"]], wanted.get("error"))
                elif wanted:  # still running: show the matches when they arrive
                    wanted["approved"] = True
                    ctx.storage.set(f"find:{item_id}", wanted)
                    await say(ctx, sender, reply(f"Still looking up {it['name'][:60]} on {shop}…"))
                else:
                    await say(ctx, sender, reply(f"Looking up {it['name'][:60]} on {shop}…"))
                    await start_search(ctx, sender, it, rec["store"], approved=True, receipt=rec["message_id"])
            else:  # paper receipts and some emails have no product link
                await say(ctx, sender, card(f"What's the product link for {it['name'][:60]}?", "form",
                                            add_item_form(new_item_id(), "", it["price_paid"], it["name"],
                                                          rec["message_id"])))
    elif action == "dismiss_receipt":
        rec = find_receipt(sender, sel, text)
        if rec is None and store.pending_receipts(sender):
            await show_pending(ctx, sender, "Which receipt? Tap Not now on the one you mean.")
            return
        if rec and rec["status"] == "pending":
            store.set_receipt_status(rec["message_id"], "dismissed")
            forget_presearch(ctx, rec["message_id"])
        await say(ctx, sender, reply("Okay, I won't track that receipt."))
    elif action == "add_item":
        url = sel.get("product_url", "") if valid_url(sel.get("product_url", "")) else ""
        await say(ctx, sender, card(
            "Got the link. What did you pay?" if url else "Add the item you bought.",
            "form", add_item_form(new_item_id(), url)))
    elif action == "list_items":
        items = store.list_items(sender)
        if not items:
            await say(ctx, sender, card("You're not tracking anything yet.", "custom",
                                        menu_card(len(store.pending_receipts(sender)))))
            return
        await say(ctx, sender, card("Here's everything you're tracking.", "carousel", items_carousel(items)))
    elif action == "recheck_item":
        item = store.get_item(sel.get("item_id", ""))
        if item is None or item["user_id"] != sender:
            await say(ctx, sender, reply("I couldn't find that item. Say \"check my prices\" to re-check them all."))
            return
        ctx.storage.remove(f"quiet:{item['item_id']}")  # this one gets the full card
        await say(ctx, sender, reply("Checking the price again…"))
        await request_price(ctx, TrackItem(item_id=item["item_id"], user_id=sender,
                                           product_url=item["product_url"], store=item["store"]))
    elif action == "check_prices":
        items = store.list_items(sender)[:CHECK_ALL_MAX]
        if not items:
            await say(ctx, sender, reply("You're not tracking anything yet. Add an item or upload a receipt."))
            return
        ctx.storage.set(f"batch:{sender}", {"total": len(items), "left": len(items), "drops": 0, "failed": 0})
        await say(ctx, sender, reply(f"Checking {len(items)} item{'s' if len(items) != 1 else ''}… "
                                     "I'll show any drops and then a summary."))
        for item in items:
            await recheck(ctx, item, "user")
    elif action == "draft_claim":
        item = store.get_item(sel.get("item_id", ""))
        if item is None or item["user_id"] != sender:
            drops = [it for it in store.list_items(sender)
                     if it["current_price"] is not None and it["price_paid"] - it["current_price"] > 1]
            if len(drops) != 1:  # prose without an item id: don't guess between items
                await say(ctx, sender, reply("Which item? Open it from My items and tap Draft a refund request."
                                             if drops else "None of your items has dropped in price yet."))
                return
            item = drops[0]
        if item["current_price"] is None or item["price_paid"] - item["current_price"] <= 1:
            await say(ctx, sender, reply("That one hasn't dropped in price, so there's nothing to ask for yet."))
            return
        req = claim_request(item, ctx.storage.get(f"policy:{item['item_id']}") or {})
        if CLAIM_AGENT_ADDRESS:
            ctx.logger.info(f"claim {item['item_id']} [{item['store']}] -> claim agent")
            await say(ctx, sender, reply("Writing your refund request…"))
            await ctx.send(CLAIM_AGENT_ADDRESS, req)
        else:
            await show_claim(ctx, test_draft(req), mock=True)
    elif action == "view_item":
        item = store.get_item(sel.get("item_id", ""))
        if item is None or item["user_id"] != sender:
            await say(ctx, sender, reply("I couldn't find that item."))
        elif item["current_price"] is None:
            await say(ctx, sender, reply("Still checking the price on that one. Try again in a moment."))
        else:
            policy = ctx.storage.get(f"policy:{item['item_id']}")
            await say(ctx, sender, card(item["name"] or "Your item", "detail", price_detail(
                item, mock=not PRICE_AGENT_ADDRESS, policy=policy)))
            if policy is None and item["price_paid"] - item["current_price"] > 1:
                await ask_policy(ctx, item)  # dropped before the policy agent was connected
    elif action == "upload_receipt":
        await say(ctx, sender, reply(
            "Attach a photo of your receipt with the paperclip button and send it."))
    elif URL_RE.search(text):
        # A pasted link: skip straight to the price question.
        url = URL_RE.search(text).group(0).rstrip(".,)")
        await say(ctx, sender, card("Got the link. What did you pay?", "form",
                                    add_item_form(new_item_id(), url)))
    else:
        n = len(store.pending_receipts(sender))
        nudge = f" You have {n} new receipt{'s' if n != 1 else ''} to review." if n else ""
        await say(ctx, sender, card(
            "Hi! Add an item by link, or upload a receipt photo." + nudge, "custom", menu_card(n)))


@chat.on_message(ChatMessage)
async def on_message(ctx: Context, sender: str, msg: ChatMessage):
    await ctx.send(sender, ChatAcknowledgement(
        timestamp=datetime.now(timezone.utc), acknowledged_msg_id=msg.msg_id))
    has_photo = any(isinstance(item, ResourceContent) for item in msg.content)
    for item in msg.content:
        if isinstance(item, StartSessionContent):
            ctx.logger.info(f"session started by {sender}")
            await say(ctx, sender, accepts_attachments())
        elif isinstance(item, TextContent):
            ctx.logger.info(f"text from {sender}: {EMAIL_RE.sub('<email>', item.text[:120])}")
            if not has_photo:  # a caption on a photo gets one reply, below
                await handle_text(ctx, sender, item.text)
        elif isinstance(item, ResourceContent):
            # Never log the URL: it can be a signed link.
            url = image_url(item)
            if not url:
                await say(ctx, sender, reply("Couldn't read that attachment, please re-upload it."))
            elif PARSER_AGENT_ADDRESS:
                message_id = "photo_" + uuid4().hex[:12]
                ctx.logger.info(f"receipt photo {message_id} from {sender} -> parser")
                await say(ctx, sender, reply("Reading your receipt…"))
                await ctx.send(PARSER_AGENT_ADDRESS, Receipt(message_id=message_id, user_id=sender,
                                                             image_url=url))
            else:  # the test parse reads email text, not photos
                ctx.logger.info(f"receipt photo from {sender}, no PARSER_AGENT_ADDRESS")
                await say(ctx, sender, card(
                    "Got your receipt photo, but the receipt reader isn't connected yet. "
                    "Add the item here for now.", "form", add_item_form(new_item_id())))
        elif isinstance(item, EndSessionContent):
            ctx.logger.info(f"session ended by {sender}")
        elif isinstance(item, MetadataContent):  # keys only: find out if card clicks arrive here
            ctx.logger.info(f"metadata from {sender}: keys={sorted(item.metadata)}")
        else:
            ctx.logger.info(f"ignoring {type(item).__name__} from {sender}")


@chat.on_message(ChatAcknowledgement)
async def on_ack(ctx: Context, sender: str, msg: ChatAcknowledgement):
    ctx.logger.info(f"ack from {sender} for {msg.acknowledged_msg_id}")


agent.include(chat, publish_manifest=True)

if __name__ == "__main__":
    agent.run()
