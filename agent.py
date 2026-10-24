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

load_dotenv()

agent = Agent(
    name="refund-hunter",
    seed=os.environ["AGENT_SEED"],  # keep fixed: it sets the agent's address
    port=8001,
    mailbox=True,
)
chat = Protocol(spec=chat_protocol_spec)

URL_RE = re.compile(r"https?://[^\s\"'<>]+")
ITEM_ID_RE = re.compile(r"\bit_[0-9a-f]{8}\b")


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


# ---- Parsing -----------------------------------------------------------------

def parse_selection(text: str) -> dict:
    """JSON for a direct @mention; prose when ASI:One's planner relays it."""
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except ValueError:
        pass
    sel: dict = {}
    lower = text.lower()
    if m := ITEM_ID_RE.search(text):
        sel["item_id"] = m.group(0)
    if m := URL_RE.search(text):
        sel["product_url"] = m.group(0).rstrip(".,)")
    if m := re.search(r"\$\s*(\d+(?:\.\d{1,2})?)|price[^0-9$]{0,20}(\d+(?:\.\d{1,2})?)", lower):
        sel["price_paid"] = m.group(1) or m.group(2)
    if "item_id" in sel and "price_paid" in sel:
        sel["action"] = "submit_item"
    elif "receipt" in lower or "photo" in lower or "upload" in lower:
        sel["action"] = "upload_receipt"
    elif "add" in lower or "track" in lower:
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
        await ctx.send(sender, reply(
            f"Tracking it: paid ${price:.2f} at {shop.title()}. "
            "I'll check the live price once the price agent is connected."))
    elif action == "add_item":
        url = sel.get("product_url", "") if valid_url(sel.get("product_url", "")) else ""
        await ctx.send(sender, card(
            "Got the link. What did you pay?" if url else "Add the item you bought.",
            "form", add_item_form(new_item_id(), url)))
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
