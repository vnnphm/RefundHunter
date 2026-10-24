import asyncio
import json
import os
import re
import sys
from datetime import datetime, timezone
from urllib.parse import urlparse
from uuid import uuid4

from dotenv import load_dotenv
from uagents import Agent, Context, Protocol
from uagents_core.contrib.protocols.chat import (
    ChatAcknowledgement, ChatMessage, EndSessionContent, MetadataContent,
    ResourceContent, StartSessionContent, TextContent, chat_protocol_spec,
)

import store
from models import ParsedReceipt, PolicyRequest, PolicyResult, PriceResult, Receipt, TrackItem

load_dotenv()

# Empty until the price agent is running: the coordinator mocks PriceResult instead.
PRICE_AGENT_ADDRESS = os.getenv("PRICE_AGENT_ADDRESS", "").strip()
# Empty until the parser agent is running: the coordinator mocks ParsedReceipt instead.
PARSER_AGENT_ADDRESS = os.getenv("PARSER_AGENT_ADDRESS", "").strip()
# Empty until the policy agent is running: the coordinator mocks PolicyResult instead.
POLICY_AGENT_ADDRESS = os.getenv("POLICY_AGENT_ADDRESS", "").strip()
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
)
chat = Protocol(spec=chat_protocol_spec)

URL_RE = re.compile(r"https?://[^\s\"'<>]+")
ITEM_ID_RE = re.compile(r"\bit_[0-9a-f]{8}\b")
ACTION_RE = re.compile(r"\b(submit_item|add_item|upload_receipt|list_items|view_item|link_email"
                       r"|check_receipts|track_receipt|dismiss_receipt)\b")
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
                {"type": "button", "label": "Link my email",
                 "action": {"selection": {"action": "link_email"}}},
            ]},
            *review,
        ],
    }}


def add_item_form(item_id: str, product_url: str = "", price_paid: float | None = None,
                  name: str = "") -> dict:
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
    shop = rec["store"].title() if rec["store"] and rec["store"] != "other" else "store"
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


def price_detail(item: dict, mock: bool) -> dict:
    drop = item["price_paid"] - item["current_price"]
    rows = [
        {"label": "You paid", "value": f"${item['price_paid']:.2f}"},
        {"label": "Price now", "value": f"${item['current_price']:.2f}"},
        {"label": "Result", "value": f"Dropped ${drop:.2f}" if drop > 1 else "No change"},
        {"label": "Store", "value": item["store"].title()},
        {"label": "Checked", "value": item["checked_at"][:16].replace("T", " ") + " UTC"},
    ]
    if mock:
        rows.append({"label": "Note", "value": "Test price: price agent not connected yet"})
    return {
        "title": item["name"] or "Your item",
        "summary_rows": rows,
        "ctas": [{"label": "Track another item", "primary": True,
                  "selection": {"action": "add_item", "item_id": item["item_id"]}},
                 {"label": "See all items",
                  "selection": {"action": "list_items", "item_id": item["item_id"]}}],
    }


def policy_detail(item: dict, result: PolicyResult, mock: bool) -> dict:
    rows = [
        {"label": "Status", "value": result.status.replace("_", " ").title() or "Unknown"},
        {"label": "Store", "value": item["store"].title()},
        {"label": "Policy", "value": result.policy_type or "demo_heuristic"},
        {"label": "Summary", "value": result.explanation[:180] or "No policy explanation"},
    ]
    if result.deadline:
        rows.append({"label": "Window ends", "value": result.deadline})
    if result.source_url:
        rows.append({"label": "Verify policy", "value": result.source_url})
    if mock:
        rows.append({"label": "Note", "value": "Test policy: policy agent not connected yet"})
    return {
        "title": item["name"] or "Refund policy check",
        "summary_rows": rows,
        "ctas": [{"label": "See all items", "selection": {"action": "list_items", "item_id": item["item_id"]}}],
    }


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
            "title": it["name"] or it["store"].title() + " item",
            "subtitle": f"{it['store'].title()} · paid ${it['price_paid']:.2f}",
            "badges": [badge],
            "secondary_text": secondary,
            "primary_cta": {"label": "View",
                            "selection": {"action": "view_item", "item_id": it["item_id"]}},
        })
    return {"title": "Your tracked items",
            "subtitle": f"{len(items)} item{'s' if len(items) != 1 else ''}",
            "items": tiles}


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
    elif "item_id" in sel and "price_paid" in sel:
        sel["action"] = "submit_item"
    elif "link" in lower and ("email" in lower or "email" in sel):
        sel["action"] = "link_email"
    elif re.search(r"\brec(ei|ie)pts\b", lower):  # "any new receipts?" (or "reciepts")
        sel["action"] = "check_receipts"
    elif re.search(r"\b(approved?|confirm(ed)?|track (these|this|them|it))\b", lower) and re.search(r"rec(ei|ie)pt", lower):
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


def store_for(url: str) -> str:
    host = urlparse(url).netloc.lower()
    if "amazon." in host or host.endswith("amzn.to"):
        return "amazon"
    if "walmart." in host:
        return "walmart"
    return "other"


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


def mock_policy_result(req: PolicyRequest) -> PolicyResult:
    """Stand-in for the policy agent: conservative demo response only."""
    return PolicyResult(
        item_id=req.item_id,
        ok=True,
        status="unknown",
        eligible=None,
        policy_type="demo_heuristic_price_protection_window",
        explanation="Policy agent not connected. This is a safe placeholder, not a real policy decision.",
    )


async def request_price(ctx: Context, req: TrackItem):
    if PRICE_AGENT_ADDRESS:
        await ctx.send(PRICE_AGENT_ADDRESS, req)
        return
    ctx.logger.info(f"no PRICE_AGENT_ADDRESS, mocking price for {req.item_id}")
    await asyncio.sleep(2)  # feel like a real lookup
    await show_price(ctx, mock_price_result(req), mock=True)


async def request_policy(ctx: Context, item: dict):
    req = PolicyRequest(
        item_id=item["item_id"],
        user_id=item["user_id"],
        store=item["store"],
        product_url=item["product_url"],
        purchase_date=item.get("purchase_date") or "",
        price_paid=item["price_paid"],
        current_price=item.get("current_price"),
    )
    if POLICY_AGENT_ADDRESS:
        await ctx.send(POLICY_AGENT_ADDRESS, req)
        return
    ctx.logger.info(f"no POLICY_AGENT_ADDRESS, mocking policy for {req.item_id}")
    await asyncio.sleep(1)
    await show_policy(ctx, mock_policy_result(req), mock=True)


async def show_price(ctx: Context, result: PriceResult, mock: bool = False):
    item = store.get_item(result.item_id)
    if item is None:
        ctx.logger.warning(f"price for unknown item {result.item_id}")
        return
    user = item["user_id"]
    if not result.ok or not result.current_price or result.current_price <= 0:
        ctx.logger.info(f"price check failed for {result.item_id}: {result.error}")
        await ctx.send(user, reply("Couldn't get the current price for that item. Try again later."))
        return
    store.save_price(result.item_id, result.current_price,
                     result.checked_at or datetime.now(timezone.utc).isoformat(), result.title or "")
    item = store.get_item(result.item_id)
    drop = item["price_paid"] - item["current_price"]
    headline = f"Price dropped ${drop:.2f}!" if drop > 1 else "No price drop yet."
    await ctx.send(user, card(headline, "detail", price_detail(item, mock)))
    if drop > 1:
        await ctx.send(user, reply("Checking whether this might still fit a refund/price-protection window…"))
        await request_policy(ctx, item)


async def show_policy(ctx: Context, result: PolicyResult, mock: bool = False):
    item = store.get_item(result.item_id)
    if item is None:
        ctx.logger.warning(f"policy for unknown item {result.item_id}")
        return
    user = item["user_id"]
    if not result.ok:
        await ctx.send(user, reply("I couldn't assess policy eligibility right now. Please try again later."))
        return
    await ctx.send(user, card("Policy check complete.", "detail", policy_detail(item, result, mock)))


@agent.on_message(model=PriceResult)
async def on_price_result(ctx: Context, sender: str, msg: PriceResult):
    if sender != PRICE_AGENT_ADDRESS:
        ctx.logger.warning(f"ignoring PriceResult from unexpected sender {sender}")
        return
    await show_price(ctx, msg)


@agent.on_message(model=PolicyResult)
async def on_policy_result(ctx: Context, sender: str, msg: PolicyResult):
    if sender != POLICY_AGENT_ADDRESS:
        ctx.logger.warning(f"ignoring PolicyResult from unexpected sender {sender}")
        return
    await show_policy(ctx, msg)


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
    shop = store_for(url) if url else ("amazon" if "amazon" in lower else "walmart" if "walmart" in lower else "other")
    return ParsedReceipt(message_id=r.message_id, user_id=r.user_id, ok=True, store=shop,
                         items=[{"name": name[:80], "price_paid": float(price.group(1).replace(",", "")),
                                 "product_url": url}])


async def show_parsed(ctx: Context, parsed: ParsedReceipt, mock: bool = False):
    user = parsed.user_id
    items = [{"name": it.name, "price_paid": it.price_paid, "product_url": it.product_url}
             for it in parsed.items if it.price_paid > 0] if parsed.ok else []
    if not items:
        ctx.logger.info(f"receipt {parsed.message_id} unreadable: {parsed.error or 'no priced items'}")
        await ctx.send(user, reply("I got a receipt but couldn't read the items on it. You can add them by hand."))
        return
    if not store.save_receipt(parsed.message_id, user, parsed.store, parsed.purchase_date,
                              parsed.order_number, items):
        ctx.logger.info(f"receipt {parsed.message_id} already seen")
        return
    ctx.logger.info(f"receipt {parsed.message_id}: {len(items)} item(s) for {user}")
    await ctx.send(user, card("I found a new receipt. Want me to track it?", "review",
                              receipt_review(store.get_receipt(parsed.message_id), mock)))


@agent.on_message(model=Receipt)
async def on_receipt(ctx: Context, sender: str, msg: Receipt):
    """An order email from server.py: find whose it is, then have it parsed."""
    if not SERVER_ADDRESS or sender != SERVER_ADDRESS:
        ctx.logger.warning(f"ignoring Receipt from unexpected sender {sender}")
        return
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


def find_receipt(sender: str, sel: dict, text: str) -> dict | None:
    """The receipt a review-card button refers to, even when the planner relays it as prose."""
    rec = store.get_receipt(sel.get("message_id", ""))
    if rec and rec["user_id"] == sender:
        return rec
    pending = store.pending_receipts(sender)
    return next((r for r in pending if r["message_id"] in text), pending[0] if pending else None)


# ---- Handlers ----------------------------------------------------------------

async def track_new_item(ctx: Context, sender: str, url: str, price: float, name: str,
                         item_id: str | None = None, purchase_date: str = ""):
    item_id = item_id or new_item_id()
    shop = store_for(url)
    if not store.add_item(item_id, sender, url, shop, price, name, purchase_date):
        await ctx.send(sender, reply("That item is already being tracked."))
        return
    ctx.logger.info(f"saved {item_id} ({shop}) for {sender}")
    await ctx.send(sender, reply(f"Tracking it: paid ${price:.2f} at {shop.title()}. Checking price…"))
    await request_price(ctx, TrackItem(item_id=item_id, user_id=sender, product_url=url, store=shop))


async def handle_text(ctx: Context, sender: str, text: str):
    sel = parse_selection(text)
    action = sel.get("action")

    if action == "submit_item":
        url = sel.get("product_url", "").strip()
        price = parse_price(sel.get("price_paid"))
        item_id = sel.get("item_id") or new_item_id()
        if not valid_url(url) or price is None:
            await ctx.send(sender, card(
                "I need a full product link (starting with https://) and the price you paid.",
                "form", add_item_form(item_id, url if valid_url(url) else "")))
            return
        await track_new_item(ctx, sender, url, price, str(sel.get("name") or "").strip(), item_id)
    elif action == "link_email":
        email = (sel.get("email") or "").strip()
        if not EMAIL_RE.fullmatch(email):
            await ctx.send(sender, card("Which email do you get order confirmations at?", "form",
                                        link_email_form()))
            return
        store.link_email(email, sender)
        ctx.logger.info(f"linked an email (@{email.split('@')[-1]}) to {sender}")
        where = (f"Forward order emails from {email} to {RECEIPT_INBOX} and I'll pick them up."
                 if RECEIPT_INBOX else "Email forwarding isn't set up yet; check back soon.")
        await ctx.send(sender, reply(f"Linked {email}. {where}"))
    elif action == "check_receipts":
        pending = store.pending_receipts(sender)
        if not pending:
            await ctx.send(sender, reply("No new receipts right now."))
            return
        for rec in pending[:3]:
            await ctx.send(sender, card("Want me to track this?", "review",
                                        receipt_review(rec, mock=not PARSER_AGENT_ADDRESS)))
    elif action == "track_receipt":
        rec = find_receipt(sender, sel, text)
        if rec is None or rec["status"] != "pending":
            await ctx.send(sender, reply("I don't have a receipt waiting for you to confirm."))
            return
        store.set_receipt_status(rec["message_id"], "tracked")
        for it in rec["items"]:
            if valid_url(it["product_url"]):
                await track_new_item(
                    ctx, sender, it["product_url"], it["price_paid"], it["name"],
                    purchase_date=rec.get("purchase_date") or "")
            else:  # paper receipts and some emails have no product link
                await ctx.send(sender, card(f"What's the product link for {it['name'][:60]}?", "form",
                                            add_item_form(new_item_id(), "", it["price_paid"], it["name"])))
    elif action == "dismiss_receipt":
        rec = find_receipt(sender, sel, text)
        if rec and rec["status"] == "pending":
            store.set_receipt_status(rec["message_id"], "dismissed")
        await ctx.send(sender, reply("Okay, I won't track that receipt."))
    elif action == "add_item":
        url = sel.get("product_url", "") if valid_url(sel.get("product_url", "")) else ""
        await ctx.send(sender, card(
            "Got the link. What did you pay?" if url else "Add the item you bought.",
            "form", add_item_form(new_item_id(), url)))
    elif action == "list_items":
        items = store.list_items(sender)
        if not items:
            await ctx.send(sender, card("You're not tracking anything yet.", "custom",
                                        menu_card(len(store.pending_receipts(sender)))))
            return
        await ctx.send(sender, card("Here's everything you're tracking.", "carousel", items_carousel(items)))
    elif action == "view_item":
        item = store.get_item(sel.get("item_id", ""))
        if item is None or item["user_id"] != sender:
            await ctx.send(sender, reply("I couldn't find that item."))
        elif item["current_price"] is None:
            await ctx.send(sender, reply("Still checking the price on that one. Try again in a moment."))
        else:
            await ctx.send(sender, card(item["name"] or "Your item", "detail",
                                        price_detail(item, mock=not PRICE_AGENT_ADDRESS)))
    elif action == "upload_receipt":
        await ctx.send(sender, reply(
            "Attach a photo of your receipt with the paperclip button and send it."))
    elif URL_RE.search(text):
        # A pasted link: skip straight to the price question.
        url = URL_RE.search(text).group(0).rstrip(".,)")
        await ctx.send(sender, card("Got the link. What did you pay?", "form",
                                    add_item_form(new_item_id(), url)))
    else:
        n = len(store.pending_receipts(sender))
        nudge = f" You have {n} new receipt{'s' if n != 1 else ''} to review." if n else ""
        await ctx.send(sender, card(
            "Hi! Add an item by link, or upload a receipt photo." + nudge, "custom", menu_card(n)))


@chat.on_message(ChatMessage)
async def on_message(ctx: Context, sender: str, msg: ChatMessage):
    await ctx.send(sender, ChatAcknowledgement(
        timestamp=datetime.now(timezone.utc), acknowledged_msg_id=msg.msg_id))
    has_photo = any(isinstance(item, ResourceContent) for item in msg.content)
    for item in msg.content:
        if isinstance(item, StartSessionContent):
            ctx.logger.info(f"session started by {sender}")
            await ctx.send(sender, accepts_attachments())
        elif isinstance(item, TextContent):
            ctx.logger.info(f"text from {sender}: {EMAIL_RE.sub('<email>', item.text[:120])}")
            if not has_photo:  # a caption on a photo gets one reply, below
                await handle_text(ctx, sender, item.text)
        elif isinstance(item, ResourceContent):
            # Never log the URL: it can be a signed link.
            url = image_url(item)
            if not url:
                await ctx.send(sender, reply("Couldn't read that attachment, please re-upload it."))
            elif PARSER_AGENT_ADDRESS:
                message_id = "photo_" + uuid4().hex[:12]
                ctx.logger.info(f"receipt photo {message_id} from {sender} -> parser")
                await ctx.send(sender, reply("Reading your receipt…"))
                await ctx.send(PARSER_AGENT_ADDRESS, Receipt(message_id=message_id, user_id=sender,
                                                             image_url=url))
            else:  # the test parse reads email text, not photos
                ctx.logger.info(f"receipt photo from {sender}, no PARSER_AGENT_ADDRESS")
                await ctx.send(sender, card(
                    "Got your receipt photo, but the receipt reader isn't connected yet. "
                    "Add the item here for now.", "form", add_item_form(new_item_id())))
        elif isinstance(item, EndSessionContent):
            ctx.logger.info(f"session ended by {sender}")
        else:
            ctx.logger.info(f"ignoring {type(item).__name__} from {sender}")


@chat.on_message(ChatAcknowledgement)
async def on_ack(ctx: Context, sender: str, msg: ChatAcknowledgement):
    ctx.logger.info(f"ack from {sender} for {msg.acknowledged_msg_id}")


agent.include(chat, publish_manifest=True)

if __name__ == "__main__":
    agent.run()
