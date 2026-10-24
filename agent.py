import asyncio
import json
import os
import re
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
from models import PriceResult, TrackItem

load_dotenv()

# Empty until the price agent is running: the coordinator mocks PriceResult instead.
PRICE_AGENT_ADDRESS = os.getenv("PRICE_AGENT_ADDRESS", "").strip()

agent = Agent(
    name="refund-hunter",
    seed=os.environ["AGENT_SEED"],  # keep fixed: it sets the agent's address
    port=8001,
    mailbox=True,
)
chat = Protocol(spec=chat_protocol_spec)

URL_RE = re.compile(r"https?://[^\s\"'<>]+")
ITEM_ID_RE = re.compile(r"\bit_[0-9a-f]{8}\b")
ACTION_RE = re.compile(r"\b(submit_item|add_item|upload_receipt|list_items|view_item)\b")


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


def menu_card() -> dict:
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
                {"type": "button", "label": "My items",
                 "action": {"selection": {"action": "list_items"}}},
            ]},
        ],
    }}


def add_item_form(item_id: str, product_url: str = "") -> dict:
    """Asks for the link (unless we already have it) and the price paid."""
    fields = [] if product_url else [
        {"name": "product_url", "kind": "text", "label": "Product link", "required": True}]
    fields += [
        # text, not number: ASI:One's number input is a whole-number stepper
        {"name": "price_paid", "kind": "text", "label": "Price you paid, e.g. 49.99", "required": True},
        {"name": "name", "kind": "text", "label": "Item name (optional)"},
    ]
    selection = {"action": "submit_item", "item_id": item_id}
    if product_url:
        selection["product_url"] = product_url
    return {
        "title": "Track an item",
        "fields": fields,
        "submit_cta": {"label": "Track price", "selection": selection},
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
    try:
        data = json.loads(text)
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
    if named:
        sel["action"] = named.group(1)
    elif "item_id" in sel and "price_paid" in sel:
        sel["action"] = "submit_item"
    elif "receipt" in lower or "photo" in lower or "upload" in lower:
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


@agent.on_message(model=PriceResult)
async def on_price_result(ctx: Context, sender: str, msg: PriceResult):
    if sender != PRICE_AGENT_ADDRESS:
        ctx.logger.warning(f"ignoring PriceResult from unexpected sender {sender}")
        return
    await show_price(ctx, msg)


# ---- Handlers ----------------------------------------------------------------

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
        shop = store_for(url)
        if not store.add_item(item_id, sender, url, shop, price, str(sel.get("name") or "").strip()):
            await ctx.send(sender, reply("That item is already being tracked."))
            return
        ctx.logger.info(f"saved {item_id} ({shop}) for {sender}")
        await ctx.send(sender, reply(f"Tracking it: paid ${price:.2f} at {shop.title()}. Checking price…"))
        await request_price(ctx, TrackItem(item_id=item_id, user_id=sender, product_url=url, store=shop))
    elif action == "add_item":
        url = sel.get("product_url", "") if valid_url(sel.get("product_url", "")) else ""
        await ctx.send(sender, card(
            "Got the link. What did you pay?" if url else "Add the item you bought.",
            "form", add_item_form(new_item_id(), url)))
    elif action == "list_items":
        items = store.list_items(sender)
        if not items:
            await ctx.send(sender, card("You're not tracking anything yet.", "custom", menu_card()))
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
        await ctx.send(sender, card(
            "Hi! Add an item by link, or upload a receipt photo.", "custom", menu_card()))


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
            ctx.logger.info(f"text from {sender}: {item.text[:120]}")
            if not has_photo:  # a caption on a photo gets one reply, below
                await handle_text(ctx, sender, item.text)
        elif isinstance(item, ResourceContent):
            # Never log the URL: it can be a signed link.
            if image_url(item):
                ctx.logger.info(f"receipt photo from {sender}")
                await ctx.send(sender, card(
                    "Got your receipt photo. Reading receipts automatically is coming soon; "
                    "for now, add the item here.", "form", add_item_form(new_item_id())))
            else:
                await ctx.send(sender, reply("Couldn't read that attachment, please re-upload it."))
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
