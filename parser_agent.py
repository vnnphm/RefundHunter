import asyncio
import base64
import json
import mimetypes
import os
import re
import sys
import time
from datetime import date

import httpx
from dotenv import load_dotenv
from uagents import Agent, Context

from asi import LLMError, ask_json
from agentverse_profile import profile
from models import ParsedReceipt, Receipt, ReceiptItem

load_dotenv()

MAX_IMAGE_BYTES = 20 * 1024 * 1024  # ASI:One's limit per image
DOWNLOAD_TIMEOUT_S = 30
BODY_LIMIT = 8000
STORES = ("amazon", "walmart", "other")

SEED = os.getenv("PARSER_AGENT_SEED", "")
if not SEED or SEED.startswith("change-me"):
    sys.exit("Set your own PARSER_AGENT_SEED in .env (see README). Same seed = same address as someone else.")

agent = Agent(
    name="refund-hunter-parser",
    seed=SEED,  # keep fixed: it sets the agent's address
    port=8003,
    mailbox=True,
    **profile("parser", "Refund Hunter's receipt reader. ASI:One (asi1) reads a receipt photo or an order email into the store, purchase date, order number and each item with its price. Answers Receipt with ParsedReceipt."),
)

PROMPT = """You read purchase receipts: a photo of a paper receipt, or the text of an order email.
Return only JSON with these fields:
- store: "amazon" or "walmart" if the purchase was from that store, otherwise "other"
- purchase_date: the purchase or order date as YYYY-MM-DD, or "" if not shown
- order_number: the order or transaction number, or "" if not shown
- items: one entry per product bought, each with
  - name: the product name as written (expand obvious abbreviations)
  - price_paid: what was paid for one unit, as a number without the currency sign
  - product_url: the product page link if the receipt has one, otherwise ""
Leave out tax, shipping, discounts, totals, gift cards and fees. Don't guess a price you can't read.
If this isn't a receipt, return an empty items list."""

SCHEMA = {
    "name": "parsed_receipt",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["store", "purchase_date", "order_number", "items"],
        "properties": {
            "store": {"type": "string", "enum": list(STORES)},
            "purchase_date": {"type": "string"},
            "order_number": {"type": "string"},
            "items": {"type": "array", "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "price_paid", "product_url"],
                "properties": {
                    "name": {"type": "string"},
                    "price_paid": {"type": "number"},
                    "product_url": {"type": "string"},
                },
            }},
        },
    },
}


class ParseError(Exception):
    """A short, user-safe reason the receipt couldn't be read."""


# ---- Photo -------------------------------------------------------------------

class NotAnImage(ParseError):
    """The link didn't give us image bytes (auth, expired, or an HTML page)."""


async def download_image(url: str, log=print) -> str:
    """GET the photo and return it as a data: URI. Logs status, type and size, never the URL."""
    try:
        async with httpx.AsyncClient(timeout=DOWNLOAD_TIMEOUT_S, follow_redirects=True) as c:
            r = await c.get(url)
    except httpx.HTTPError as e:
        raise NotAnImage(f"couldn't download the photo: {type(e).__name__}") from e
    mime = r.headers.get("content-type", "").split(";")[0].strip().lower()
    log(f"photo download: status={r.status_code} type={mime or '?'} size={len(r.content)}")
    if r.status_code != 200 or not mime.startswith("image/"):
        raise NotAnImage(f"photo link gave {r.status_code} {mime or 'no type'}")
    if len(r.content) > MAX_IMAGE_BYTES:
        raise ParseError("photo is over 20 MB")
    return data_uri(mime, r.content)


def data_uri(mime: str, data: bytes) -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"


# ---- asi1 --------------------------------------------------------------------

async def read_receipt(content: list[dict]) -> dict:
    """One asi1 call with the receipt; returns the model's JSON."""
    try:
        return await ask_json(content, SCHEMA)
    except LLMError as e:
        raise ParseError(str(e)) from e


def photo_content(image: str) -> list[dict]:
    return [{"type": "text", "text": PROMPT},
            {"type": "image_url", "image_url": {"url": image}}]


def email_content(receipt: Receipt) -> list[dict]:
    header = f"From: {receipt.sender}\nSubject: {receipt.subject}\nDate: {receipt.date}\n\n"
    return [{"type": "text", "text": PROMPT + "\n\nOrder email:\n" + header + receipt.body[:BODY_LIMIT]}]


# ---- Cleanup -----------------------------------------------------------------

def iso_date(value: str) -> str:
    """YYYY-MM-DD if value starts with a real date, else ""."""
    m = re.match(r"\s*(\d{4}-\d{2}-\d{2})", value or "")
    if not m:
        return ""
    try:
        return date.fromisoformat(m.group(1)).isoformat()
    except ValueError:
        return ""


def to_price(value) -> float | None:
    try:
        price = round(float(str(value).replace("$", "").replace(",", "").strip()), 2)
    except ValueError:
        return None
    return price if price > 0 else None


def to_parsed(receipt: Receipt, data: dict) -> ParsedReceipt:
    """Turn asi1's JSON into a ParsedReceipt, dropping anything we can't trust."""
    items = []
    for it in data.get("items") or []:
        if not isinstance(it, dict):
            continue
        name = str(it.get("name") or "").strip()[:200]
        price = to_price(it.get("price_paid"))
        if not name or price is None:
            continue
        url = str(it.get("product_url") or "").strip()
        items.append(ReceiptItem(name=name, price_paid=price,
                                 product_url=url if re.match(r"https?://\S+$", url) else ""))
    store = str(data.get("store") or "").lower()
    base = dict(message_id=receipt.message_id, user_id=receipt.user_id)
    if not items:
        return ParsedReceipt(**base, ok=False, error="no items found on the receipt")
    return ParsedReceipt(**base, ok=True, items=items,
                         store=store if store in STORES else "other",
                         purchase_date=iso_date(str(data.get("purchase_date") or "")) or iso_date(receipt.date),
                         order_number=str(data.get("order_number") or "").strip()[:64])


async def parse(receipt: Receipt, log=print) -> ParsedReceipt:
    if receipt.image_url:
        try:
            image = await download_image(receipt.image_url, log)
        except NotAnImage as e:
            # Open question in AGENTS.md: ASI:One's attachment links may need auth.
            # Fall back to letting asi1 fetch the link itself.
            log(f"{e}; sending the link to asi1 instead")
            image = receipt.image_url
        content = photo_content(image)
    elif receipt.body.strip():
        content = email_content(receipt)
    else:
        raise ParseError("receipt has no photo or text")
    return to_parsed(receipt, await read_receipt(content))


@agent.on_message(model=Receipt, replies=ParsedReceipt)
async def on_receipt(ctx: Context, sender: str, receipt: Receipt):
    start = time.monotonic()
    kind = "photo" if receipt.image_url else "email"
    try:
        result = await parse(receipt, ctx.logger.info)
    except ParseError as e:
        result = ParsedReceipt(message_id=receipt.message_id, user_id=receipt.user_id, ok=False, error=str(e))
    except Exception as e:  # never leave the coordinator waiting
        ctx.logger.exception(f"parse crashed for {receipt.message_id}")
        result = ParsedReceipt(message_id=receipt.message_id, user_id=receipt.user_id, ok=False,
                               error=f"unexpected error: {type(e).__name__}")
    outcome = f"{len(result.items)} item(s) [{result.store}]" if result.ok else f"failed ({result.error})"
    ctx.logger.info(f"{receipt.message_id} ({kind}) {outcome} in {time.monotonic() - start:.1f}s")
    await ctx.send(sender, result)


async def check_once(source: str):
    """Manual test: parse one photo URL, local image, or .txt email body. Uses ASI:One, no Bright Data."""
    ids = dict(message_id="check", user_id="check")
    mime = mimetypes.guess_type(source)[0] or ""
    try:
        if re.match(r"https?://", source):
            result = await parse(Receipt(**ids, image_url=source))
        elif source.lower().endswith(".txt"):
            with open(source, encoding="utf-8") as f:
                result = await parse(Receipt(**ids, body=f.read()))
        elif mime.startswith("image/"):
            with open(source, "rb") as f:
                image = data_uri(mime, f.read())
            result = to_parsed(Receipt(**ids), await read_receipt(photo_content(image)))
        else:
            sys.exit("--check takes an http(s) image link, an image file, or a .txt email body")
    except ParseError as e:
        print(f"failed: {e}")
        return
    print(json.dumps(json.loads(result.model_dump_json()), indent=2))


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--check":
        # uv run parser_agent.py --check <image url | image file | email.txt>
        asyncio.run(check_once(sys.argv[2]))
    else:
        agent.run()
