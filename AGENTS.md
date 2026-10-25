# AGENTS.md — Refund Hunter

The rules for anyone (human or coding agent) working in this repo. Verified against current docs and releases on 2026-10-23. If code and this file disagree, fix one of them.

## What we're building

Fetch.ai uAgents that find money users are owed on past purchases. Users only talk to it inside an ASI:One chat. Fetch.ai track, Cal Hacks 13.0 (Oct 23–25, 2026).

**Phase 1 (must ship):** user adds an item in ASI:One → we fetch its live price with Bright Data → chat shows "Dropped $X" or "No change".
**Phase 2 (should ship):** receipts become tracked items without typing: a **receipt photo uploaded in the ASI:One chat** (build first: it stays inside ASI:One) and **order emails** (the user taps **Connect Gmail** and FastAPI `server.py` reads their recent orders once). See Email receipts.
**Phase 3 (if time):** refund policy check, claim draft (see Refund requests), Payment Protocol.

Build in phase order. Don't start a phase until the one before works in ASI:One.

## Agents

Each agent is one file, one job, one seed. Only the coordinator talks to users.

| Agent | File | Phase | Job | Receives | Sends |
| --- | --- | --- | --- | --- | --- |
| Coordinator | `agent.py` | 1 | Talks to the user in ASI:One, shows cards, saves items, asks the price agent for prices. The only agent that writes items | `ChatMessage` (user, incl. photos), `PriceResult`, `ParsedReceipt` | `ChatMessage` + cards, `TrackItem`, `Receipt` |
| Price agent | `price_agent.py` | 1 | Gets the live price of one item from Bright Data; searches Amazon or Walmart for receipt items without a link | `TrackItem`, `FindProduct` | `PriceResult`, `FindResult` |
| Parser agent | `parser_agent.py` | 2 | Reads a receipt (photo or email text) into store, date and line items | `Receipt` (from the coordinator or the webhook) | `ParsedReceipt` (to the coordinator) |
| Policy agent | `policy_agent.py` | 3 | Which request makes sense for this store, and the deadline (rules, no LLM) | `PolicyCheck` | `PolicyResult` |
| Claim writer | `claim_agent.py` | 3 | Drafts the refund request the user pastes into the store's chat | `ClaimRequest` | `ClaimDraft` |

Not agents: `store.py` (SQLite, shared state), `asi.py` (`asi1` JSON calls, used by the parser), `run_all.py` (starts every agent with one command), `server.py` (FastAPI: Gmail connect, plus an optional forwarding webhook), `gmail.py` (Google OAuth + Gmail API), Bright Data.

**Flow (Phase 1):** user → coordinator → saves item → `TrackItem` → price agent → Bright Data → `PriceResult` → coordinator → saves price → detail card to user.

**Flow (Phase 2):** receipt photo in chat (or email via webhook) → coordinator → `Receipt` → parser agent → `ParsedReceipt` → coordinator → review card → user confirms → saved items → Phase 1 flow. An Amazon or Walmart item without a link → `FindProduct` → price agent searches → `FindResult` → carousel → user picks one → Phase 1 flow.

**Flow (Phase 2, optional forwarding, not used in the demo):** forwarded email → Pipedream → `server.py` → `Receipt(sender=<from email>)` → coordinator looks up the linked ASI:One user → `Receipt(user_id=…)` → parser agent → same as above.

**Flow (Phase 2, Gmail connect):** menu → **Connect Gmail** → signed link → Google sign-in → `server.py` reads up to 10 recent order emails once, revokes the token → `Receipt(user_id=<from the link>)` each → coordinator → parser agent → same as above.

## Who owns what

| | Person A: ASI:One side | Person B: data side |
| --- | --- | --- |
| Owns | `agent.py`, `store.py`, cards, `price_agent.py`, `server.py`, `gmail.py`, README, demo video | `parser_agent.py` (photos and email text) |
| Phase 1 | Coordinator replies in ASI:One → form card → save item → detail card + carousel | Bright Data prints a real price (Price scraping, Path A) → price agent answers `TrackItem` with `PriceResult` |
| Phase 2 | Email path (Connect Gmail: `server.py`, `gmail.py`) + `ParsedReceipt` handling and review card in `agent.py` | Parser agent: `asi1` reads photos and email text, replies `ParsedReceipt` to its sender |
| Until the other side is ready | Fake `PriceResult` with a hardcoded price | Test with a script that sends `TrackItem` |

**Phase 3:** the policy agent (`policy_agent.py`) is MM77a's, the claim agent (`claim_agent.py`) is Jiayi's, and Vinny wires both into the coordinator. Until an agent is ready, the coordinator shows a labelled test result, and each agent can be tested alone with a script that sends its request model. See Refund requests.

`models.py` is shared: write it together first and change it only together. Work on separate branches and merge often.

## Message contract (`models.py`)

Agree on this before splitting work. Change it only together.

```python
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
```

A price counts as dropped when `price_paid - current_price > 1` and `ok` is true.

**Phase 3 (agreed, in `models.py`):**
```python
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
```

## Setup

- **Python 3.13 only.** uagents 0.26.1 crashes on 3.14 (`RuntimeError: There is no current event loop`); Fetch docs v1.0.5 support 3.10–3.13. `pyproject.toml` caps it at `<3.14`.
- `uv sync` → `cp .env.example .env` → fill in seeds and keys → `uv run agent.py`.
- Add deps with `uv add <pkg>==<version>` and commit `uv.lock`. uv only, no pip.

| Package | Version | Released |
| --- | --- | --- |
| uagents | 0.26.1 | 2026-10-08 |
| uagents-core | 0.4.12 | 2026-10-13 |
| python-dotenv | 1.2.4 | 2026-10-01 |
| httpx (Scraper API calls) | 0.28.1 | latest on 2026-10-23 |
| mcp (MCP fallback) | 2.3.0 | 2026-10-02 |
| openai (ASI:One LLM) | 3.37.0 | latest on 2026-10-23 |
| fastapi + uvicorn (Phase 2) | 0.162.0 / 0.54.0 | latest on 2026-10-23 |

**`.env` keys:** `AGENT_SEED` (coordinator), `PRICE_AGENT_SEED`, `PARSER_AGENT_SEED`, `PRICE_AGENT_ADDRESS`, `BRIGHTDATA_API_TOKEN`, `ASI_ONE_API_KEY`, `PARSER_AGENT_ADDRESS`, and for email: `SERVER_SEED`, `SERVER_ADDRESS`, `COORDINATOR_ADDRESS`, and for Gmail connect: `PUBLIC_URL`, `OAUTH_STATE_SECRET` (both: coordinator and server), `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` (server), and for Phase 3: `POLICY_AGENT_SEED`, `POLICY_AGENT_ADDRESS`, `CLAIM_AGENT_SEED`, `CLAIM_AGENT_ADDRESS` (the claim agent also uses `ASI_ONE_API_KEY`). Optional forwarding only: `WEBHOOK_SECRET`, `RECEIPT_INBOX`. No OpenAI key: `asi1` reads receipt photos itself (see Receipt photos). `BRIGHTDATA_API_TOKEN` is one key for both the Scraper API and the MCP.
- **Every person uses their own seeds.** Same seed = same address, and two running copies fight over one mailbox.
- **Ports:** coordinator 8001, price agent 8002, parser agent 8003, claim agent 8004, policy agent 8005, email webhook (`server.py`) 8000.

## Run and connect

1. `uv run agent.py`. The terminal prints the address and a **Local Agent Inspector** URL.
2. Inspector → **Connect** → **Mailbox** → **Finish**. Keep the script running.
3. In Chrome or Brave, allow **Local Network Access**, or the inspector can't see the agent.
4. Test with a direct @mention in ASI:One. General search can be slow to pick up new agents.
5. Do the same for every agent (each gets its own mailbox).

## Rules

**Chat Protocol** (coordinator only)
- Import from `uagents_core.contrib.protocols.chat`. Use the real `chat_protocol_spec`.
- Register handlers for both `ChatMessage` and `ChatAcknowledgement` from the start.
- Acknowledge first: `ChatAcknowledgement(acknowledged_msg_id=msg.msg_id)` before any work.
- Loop over `msg.content`; never `msg.content[0]`. Log session start/end; ignore unknown types without crashing.
- Fresh `uuid4()` and `datetime.now(timezone.utc)` on every message. No `utcnow()`.
- `agent.include(chat_proto, publish_manifest=True)`.

**Agent-to-agent**
- Worker agents use `@agent.on_message(model=X, replies=Y)` with the models in `models.py`, not the chat protocol.
- The chat never waits on a scrape. The coordinator replies "Checking price…" right away and sends the result card when `PriceResult` arrives.

**Interactive Cards**
- One `MetadataContent` with string values only: `card_protocol_version: "1"`, `requires_card_interaction: "true"`, `card_kind`, and `card_payload` as `json.dumps(...)` with no wrapper. Invalid cards silently show as plain text.
- Test payloads in the Card Playground first.
- Replies arrive as JSON (direct @mention) or prose (planner): try `json.loads`, then parse text. Put `item_id` in every `selection`.
- ASI:One answers `429 rate limited` to messages sent back-to-back and drops them (no retry). Send every user message through `say()` in `agent.py`, which spaces one user's messages 1.5 s apart.

**Bright Data**: see "Price scraping" below.

**ASI:One LLM**
- `OpenAI(api_key=os.getenv("ASI_ONE_API_KEY"), base_url="https://api.asi1.ai/v1")`, `model="asi1"`. No silent fallback to another provider.

**Payment Protocol** (Phase 3)
- Send `RequestPayment` before any LLM call, or ASI:One shows plain text instead of the payment card.

**General**
- Secrets only in `.env`; document every key in `.env.example`. Never log seeds, tokens, or full emails.
- Keep only extracted receipt fields (item, price, store, date, order number). Discard the rest.
- Small flat files. No new folders until there's a reason.

## Price scraping (Bright Data)

Owner: Person B, in `price_agent.py` (move helpers to `bd.py` if it grows). Based on Bright Data's own agent guidance in [llms.txt](https://docs.brightdata.com/llms.txt): use a ready-made Scraper API when the site has one; use the MCP for page fetching; never fetch through a raw proxy and parse HTML yourself.

**Which path for which store**

| Store | Path | Why |
| --- | --- | --- |
| Amazon | **A: Scraper API** (REST), dataset `gd_l7q7dkf244hwjntr0` | Ready-made scraper; returns `final_price` directly |
| Walmart | **A: Scraper API** (REST), dataset `gd_l95fol7l1ru6rlo116` | Ready-made scraper; price field not yet confirmed (`--check` prints it) |
| Anything else (Target, …) | **B: MCP** `search_scrapers` → if a scraper exists, run it via Path A with its `dataset_id` | Bright Data says don't build a scraper for a site that has one |
| No scraper found | **C: MCP** `scrape_as_markdown` → ASI:One reads the price | Works on any page |

**Built:** Path A for Amazon and Walmart in `price_agent.py`. `store: "other"` replies `ok=False, error="store not supported yet"` until C is added. Dataset ids come from [scrapers.json](https://docs.brightdata.com/scrapers.json).
Test one link without starting the agent (costs one credit): `uv run price_agent.py --check <url> [amazon|walmart]`.

**Search (receipt items without a link):** Amazon products search `gd_lwdb4vjm1ehb499uxs`, input `{"keyword", "url": "https://www.amazon.com", "pages_to_search": 1}` with `limit_per_input=3` (honoured: 3 records = 3 credits, ~7 s). Fields: `asin`, `name`, `final_price`, `image`, `sponsored`, `rank_on_page`. Sponsored and priceless results are skipped; URLs become `https://www.amazon.com/dp/<asin>`, and their prices go into the 10-minute cache, so tracking a pick costs nothing. Walmart: products search `gd_m7khey0wb7wviejgj`, input `{"url": "https://www.walmart.com/search?q=<name>"}`, same limit (50–80 s; names sometimes come back in Spanish, so `english_name` rebuilds them from the English `canonical_url` slug). Fields: `us_item_id`, `name`, `price`, `is_sponsored_flag`, `product_index`; links become `https://www.walmart.com/ip/<us_item_id>`, which the Walmart product scraper accepts (`final_price` confirmed). Walmart's emails have no product links at all (the item picture is a bare `<img>` with the name as alt text), so search is how Walmart items get one. Other stores get the link form. Test: `uv run price_agent.py --search "<item name>" [amazon|walmart]` (≤3 credits).

**Auth and budget**
- One API key. REST: `Authorization: Bearer <BRIGHTDATA_API_TOKEN>`. MCP: `https://mcp.brightdata.com/mcp?token=<BRIGHTDATA_API_TOKEN>`.
- Free tier: 5,000 credits/month, **one pool shared by both of us and every product**. Scraper API = 1 credit per delivered record; MCP / Web Unlocker = 1 credit per request. No rollover.
- **25 HTTP 429s within 5 minutes gets our IP blocked** from all Bright Data APIs until support clears it. Never retry in a tight loop; honor `retry-after`.

**Path A: Scraper API, synchronous** (`uv add httpx==0.28.1`)
- `POST https://api.brightdata.com/datasets/v3/scrape?dataset_id=gd_l7q7dkf244hwjntr0&format=json`, body `{"input": [{"url": product_url}]}`. One input per request.
- `200` → JSON array of records. Use `final_price` (fallback `initial_price`), `currency`, `availability`, `title`. Empty array = bad input URL.
- Waits up to 1 minute. If longer: `202` with `snapshot_id` and a `retry-after` header. Poll `GET /datasets/v3/progress/{snapshot_id}` every `retry-after` seconds, then `GET /datasets/v3/snapshot/{snapshot_id}?format=json`.
- `400` bad input, `401` bad key, `404` bad dataset_id, `429` rate limited → `PriceResult(ok=False)`.

```python
import asyncio, os
import httpx

BD_KEY = os.environ["BRIGHTDATA_API_TOKEN"]
BD_API = "https://api.brightdata.com/datasets/v3"
AMAZON_PRODUCTS = "gd_l7q7dkf244hwjntr0"  # Amazon products, collect by URL

async def amazon_price(url: str) -> dict:
    headers = {"Authorization": f"Bearer {BD_KEY}"}
    async with httpx.AsyncClient(timeout=90, headers=headers) as c:
        r = await c.post(f"{BD_API}/scrape",
                         params={"dataset_id": AMAZON_PRODUCTS, "format": "json"},
                         json={"input": [{"url": url}]})
        if r.status_code == 202:  # still running: poll the snapshot
            sid = r.json()["snapshot_id"]
            for _ in range(12):
                await asyncio.sleep(int(r.headers.get("retry-after", 10)))
                status = (await c.get(f"{BD_API}/progress/{sid}")).json().get("status")
                if status == "failed":
                    raise RuntimeError("Bright Data job failed")
                if status == "ready":
                    r = await c.get(f"{BD_API}/snapshot/{sid}", params={"format": "json"})
                    break
            else:
                raise TimeoutError("Bright Data snapshot not ready")
        r.raise_for_status()
        records = r.json()
    if not records:
        raise ValueError("no record for this URL")
    rec = records[0]
    return {"price": rec.get("final_price") or rec.get("initial_price"),
            "currency": rec.get("currency"), "title": rec.get("title"),
            "availability": rec.get("availability")}
```

**Path B/C: MCP** (`uv add mcp==2.3.0 openai==3.37.0`)
- All MCP tools are on by default; load fewer with `&groups=` (`scrapers`, `browser`, `advanced_scraping`, `geo`) or `&tools=a,b`.
- mcp 2.x: `streamable_http_client(url)` yields **two** streams; results have `.content` and `.is_error` (not `isError`). Always pass `read_timeout_seconds`.
- **Step 0:** list your tools and their inputs before writing calls:
  ```python
  res = await s.list_tools()
  for t in res.tools:
      print(t.name, t.inputSchema)
  ```
- **B:** `search_scrapers` with the store's domain → `dataset_id` → `get_scraper_details` (input fields, price field) → call Path A's REST endpoint with that `dataset_id`. Look the id up once and save it as a constant.
- **C:** `scrape_as_markdown` → first ~8,000 chars → ASI:One (`asi1`, temperature 0) returns strict JSON `{"title", "price", "currency", "in_stock"}`.

```python
import json, os
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from openai import AsyncOpenAI

BD_MCP = f"https://mcp.brightdata.com/mcp?token={os.environ['BRIGHTDATA_API_TOKEN']}"
llm = AsyncOpenAI(api_key=os.environ["ASI_ONE_API_KEY"], base_url="https://api.asi1.ai/v1")
PROMPT = ('From this product page, return only JSON: {"title": str|null, "price": number|null, '
          '"currency": "USD", "in_stock": bool|null}. price = current selling price.\n\n')

async def page_markdown(url: str) -> str:
    async with streamable_http_client(BD_MCP) as (read, write):
        async with ClientSession(read, write) as s:
            await s.initialize()
            res = await s.call_tool("scrape_as_markdown", {"url": url}, read_timeout_seconds=120)
    if res.is_error:
        raise RuntimeError("scrape failed")
    return "\n".join(getattr(c, "text", "") for c in res.content)

async def read_price(markdown: str) -> dict:
    r = await llm.chat.completions.create(
        model="asi1", temperature=0,
        messages=[{"role": "user", "content": PROMPT + markdown[:8000]}])
    text = (r.choices[0].message.content or "").strip()
    text = text.removeprefix("`" * 3 + "json").strip("`\n ")  # drop a markdown fence if present
    return json.loads(text)
```
Confirm `{"url": ...}` against Step 0's `inputSchema`.

**Rules**
- uagents runs an agent's handlers **one at a time**: a handler that awaits a 60 s scrape holds up every message behind it. `price_agent.py` starts each job with `background()` (at most `MAX_JOBS` at once) and returns. Searches give up after `SEARCH_LIMIT_S` (75 s) and the user gets the link form; price checks keep the longer limit.
- Only the price agent calls Bright Data. The coordinator says "Checking price…" and sends the card when `PriceResult` arrives.
- Every failure (timeout, 4xx/5xx, empty record, no price) becomes `PriceResult(ok=False, error=...)`. Never let the handler raise.
- Accept only a positive number as a price.
- Don't re-scrape a URL within a few minutes; reuse the stored price and `checked_at`. Credits are shared.
- Skip the MCP `extract` tool: it relies on MCP sampling, which our client doesn't implement. Path C does the same job.
- Never log the API key or a URL containing `?token=`.

## Receipt photos (Phase 2)

Owner: Person A (receiving the photo, review card) and Person B (parser agent). Based on Fetch's [image analysis agent example](https://innovationlab.fetch.ai/resources/docs/examples/chat-protocol/image-analysis-agent) (docs v1.0.5) and the ASI:One API docs on image input.

**Status:** steps 1–2 are built in `agent.py` (`accepts_attachments()`, `image_url()`). Step 3 is built in `parser_agent.py` (photos and email text; test with `uv run parser_agent.py --check <photo | link | email.txt>`). Steps 4–5 are built in `agent.py` (review card, link form, Amazon search carousel).

**1. Tell ASI:One we accept attachments.** When a `StartSessionContent` arrives, reply with:
```python
MetadataContent(type="metadata", metadata={"attachments": "true"})
```
Without this, users can't attach a photo to our agent.

**2. Get the image URL** (coordinator). The photo arrives as `ResourceContent`; its `.resource` is one `Resource` or a list. Take the first http(s) URL from `resource.uri`, else from `resource.metadata` keys `url`, `uri`, `source`, `image_url`. The agent can't download from ASI:One's private storage, only these URLs. No URL → reply "Couldn't read that attachment, please re-upload".
```python
from urllib.parse import urlparse

def image_url(item) -> str | None:
    res = item.resource if isinstance(item.resource, list) else [item.resource]
    for r in res:
        for cand in [r.uri, *(r.metadata.get(k, "") for k in ("url", "uri", "source", "image_url"))]:
            u = urlparse(cand or "")
            if u.scheme in ("http", "https") and u.netloc:
                return cand
    return None
```
Then: reply "Reading your receipt…" right away, and send `Receipt(message_id="photo_"+uuid, user_id=sender, image_url=url)` to the parser agent.

**3. Read the receipt** (parser agent). `asi1` reads images itself, so there's no second model or key. All ASI:One models take image input, on both `/v1/chat/completions` and `/v1/responses`.
- **Download the photo, send the bytes.** Images go in as a `data:` URI, not a link: `data:<mime>;base64,<bytes>`. The parser `GET`s `Receipt.image_url` (httpx, follow redirects, ~30 s timeout), checks `content-type` is `image/*` and the size is ≤ 20 MB, then base64-encodes it. Up to 10 images per request; we send one.
- **Ask for structured output.** `/v1/chat/completions` with `model="asi1"`, `temperature=0` and `response_format` set to a JSON schema of `ParsedReceipt`'s fields (store, purchase_date, order_number, items with name and price_paid). On `/v1/responses` the same schema goes in `text.format`, and the image part is `{"type": "input_image", "image_url": "data:…"}`.
  ```python
  {"role": "user", "content": [
      {"type": "text", "text": PROMPT},
      {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}]}
  ```
- **Errors are explicit.** An unreadable image is a `400` with `code: "unsupported_content_part"` (no URL, bad base64, over 20 MB, too many images, a host ASI:One doesn't fetch from). A `200` means the image was used.
- Same prompt and output for email text (`Receipt.body`), so photos and emails share one parser.
- Download failed, unreadable photo, or no items found → `ParsedReceipt(ok=False, error=...)`.

**Open question: can the parser download ASI:One's attachment URL?** Fetch's example only forwarded the URL to OpenAI; it never downloaded it, and ASI:One's storage may need auth or expire. Find out with the first real photo:
- The parser logs the download's status code, `content-type` and size (never the URL).
- `200` + `image/*` → done, use the steps above.
- `401`/`403`/`404`, or HTML instead of an image → try sending the URL itself as `image_url` (no download). `parser_agent.py` does this automatically and logs "sending the link to asi1 instead". If ASI:One answers `400 unsupported_content_part` for that host too, we need another way to get the bytes; ask in the Fetch.ai hackathon channel before building more.

**4. Confirm before tracking** (coordinator). Show a `review` card: store, date, and each item with its price. Approve → save items and send `TrackItem`s. Never track items the user hasn't confirmed; OCR misreads prices.

**5. Missing product links.** Paper receipts have names, not links, and the price agent needs a link. For each item without `product_url`: on an Amazon or Walmart receipt, the coordinator sends `FindProduct` and shows the top 3 matches from that store in a `carousel` (each tile's button is a filled-in `submit_item`), then a link form for "not listed". The user picks, because one name matches several variants (sizes, kits). Other stores, or no matches, get the link form. The search starts when the review card is shown, not on Approve (Walmart's takes 50–80 s), and the matches wait in `ctx.storage` until the user approves; dismissing forgets them. Only shown cards search, so a Gmail import spends credits on one receipt at a time.

**Rules**
- Only use the photo to read the receipt; don't store the image or its bytes. Keep only the parsed fields.
- Never log image URLs (they can be signed links) or full receipt text.

## Email receipts (Phase 2)

Owner: Person A. Users tap **Connect Gmail**, sign in with Google, and their recent order emails become review cards. No forwarding, no filters. The Pipedream forwarding path further down is built and tested but **not used**: it's optional, and nothing needs to run for it.

**Gmail connect (OAuth).** Import on connect, keep nothing. Menu → **Connect Gmail** → the coordinator replies with `{PUBLIC_URL}/oauth/start?state=…` (cards can't open links, so it's in the text). The `state` is the user's ASI:One address + time, HMAC-signed with `OAUTH_STATE_SECRET`, valid 15 minutes (`gmail.sign_state`/`check_state`). `/oauth/start` → Google consent (`gmail.readonly`, `access_type=online`: no refresh token) → `/oauth/callback` swaps the code for a token, reads the Gmail address and up to 10 messages matching `gmail.QUERY` (last 30 days, purchases/order/receipt, no shipping updates, no Amazon since its emails don't list items), revokes the token, and sends each as `Receipt(message_id="gm_"+id, user_id=<from state>, sender=<gmail>)`. The coordinator trusts `user_id` only from `SERVER_ADDRESS` and links that Gmail address, so later forwards work too.
- Google Cloud Console: enable the **Gmail API**; Google Auth Platform → Branding (app name, support email), Audience **External, left in Testing** with test users added (max 100; they see "Google hasn't verified this app" → Advanced → Continue), Data access: `…/auth/gmail.readonly`; Clients → **Web application** with redirect URIs `https://<server URL>/oauth/callback` and `http://localhost:8000/oauth/callback`.
- Only test users can sign in, so add judges' Gmail addresses before they try it.
- Never log the token, the code, bodies or full addresses (count and domain only).

**`server.py`** (`uv run uvicorn server:app --port 8000`, or deploy to Vercel: zero-config FastAPI). It turns each email into text (the HTML part when there is one, since stores put items there; image alt text kept, as stores cut long names short; only product links kept, unwrapped from tracking redirects; 8,000 chars max) and sends it as a `Receipt` to `COORDINATOR_ADDRESS` with `send_message_to_agent` (`uagents_core.utils.messages`), signed with its own `SERVER_SEED` identity. Put the address `/health` reports in `.env` as `SERVER_ADDRESS`; the coordinator only accepts `Receipt`s from it. On Vercel, set `SERVER_SEED`, `COORDINATOR_ADDRESS`, `PUBLIC_URL`, `OAUTH_STATE_SECRET`, `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, and turn off Vercel Authentication (Deployment Protection) so Google's redirect can reach it.

**Coordinator.** A `Receipt` from `SERVER_ADDRESS` with `user_id` set (Gmail connect) is used as is, and its address gets linked; one without (forwarding) is matched by linked address, and an unlinked sender is dropped (logged by domain only). Then → forwarded to `PARSER_AGENT_ADDRESS` with `user_id` filled in; if that's empty, a test parse (subject + first `$` amount + first shop link) stands in, labelled on the card. A `ParsedReceipt` with no priced items is dropped quietly for emails (a broad search lets some non-receipts through), but photos get "couldn't read the items". Otherwise → saved as pending in `receipts` (the `message_id` key drops duplicates) → `review` card. Approve → each item with a shop link goes into the Phase 1 flow; items without one get a form asking only for the link. "Any new receipts?" re-sends pending review cards, and the menu mentions them, in case ASI:One doesn't show messages the agent sends first.

**Answered in testing (2026-10-24)**
- Agentverse delivers `server.py`'s messages even though its identity isn't registered as an agent.
- ASI:One shows cards the agent sends first, but answers `429 rate limited` to a burst (see `say()`), so a Gmail import shows one card and keeps the rest pending.

**Rules**
- Keep only parsed fields. Never log email bodies or full addresses (message id and domain only).

### Optional: forwarding through Pipedream (not used)

Kept for later; skip it for setup and the demo. With `WEBHOOK_SECRET` unset (as in our Vercel), `/email` refuses every request (401), so leaving it in is safe. Why we dropped it: each user had to confirm a forwarding address and make a Gmail filter, and Amazon's emails no longer list items anyway.

**Linking** (for forwarding). Type "link my email you@x.com" (Gmail connect links automatically) → `store.link_email(email, user)`. One row per email (lowercase); re-linking moves it. No ownership check for the demo: anyone can claim any address. Production fix: a one-time code the user forwards to prove the address.

**Pipedream** (only the code step below lives outside the repo)
1. New workflow, trigger **Email** ("New Email Received"; instant, not polled). Its unique `…@upload.pipedream.net` address goes in `.env` as `RECEIPT_INBOX`.
2. Workflow **Settings → Environment Variables**: `WEBHOOK_SECRET` (same as `.env` and Vercel) and `REFUND_SERVER_URL` (Vercel URL, no trailing slash).
3. Step **Node → Run Node code**. The event is `{headers, body: {text, html, …}}`; header values can be lists:
   ```js
   export default defineComponent({
     async run({ steps }) {
       const e = steps.trigger.event;
       const addr = v => typeof v === "string" ? v : Array.isArray(v) ? v.map(addr).join(", ")
         : (v?.value || []).map(a => a.address).filter(Boolean).join(", ") || v?.text || v?.address || "";
       const one = v => (Array.isArray(v) ? v[0] : v) ?? "";
       const hdr = n => Object.entries(e.headers || {}).find(([k]) => k.toLowerCase() === n)?.[1];
       const res = await fetch(`${process.env.REFUND_SERVER_URL}/email`, {
         method: "POST",
         headers: { "Content-Type": "application/json", "X-Webhook-Secret": process.env.WEBHOOK_SECRET },
         body: JSON.stringify({
           message_id: e.messageId || hdr("message-id") || `pd_${Date.now()}`,
           from: [e.from, hdr("from"), e.mail_from, e.envelope?.from].map(addr).find(Boolean) || "",
           forwarded_for: addr(hdr("x-forwarded-for")),
           to: [e.to, hdr("to")].map(addr).find(Boolean) || "",
           subject: String(e.subject || one(hdr("subject"))), date: String(e.date || one(hdr("date"))),
           text: e.body?.text || e.text || "", html: e.body?.html || e.html || "",  // body sits under event.body
         }),
       });
       const body = await res.text();
       if (!res.ok) throw new Error(`server ${res.status}: ${body}`);
       return body;
     },
   });
       const body = await res.text();
       if (!res.ok) throw new Error(`server ${res.status}: ${body}`);
       return body;
     },
   });
   ```
4. Each user, once in Gmail: Settings → **Forwarding and POP/IMAP** → add the inbox address (Gmail mails a confirm link to it: open it from the trigger's event list). Then a filter by keywords, so any store works. Has the words: `subject:(order OR receipt OR purchase) ("order total" OR "order number" OR "order #" OR subtotal)`; Doesn't have: `shipped OR delivered OR "out for delivery" OR refund OR return` (shipping updates would repeat the order) → **Forward to** the inbox.
5. Who it's from: a hand-forwarded email's `from` is the user. A filter auto-forward keeps the store as `from`, but the original `To` is still the user (and Gmail may add `X-Forwarded-For: user@gmail.com inbox@…`). `server.py` uses `X-Forwarded-For`, else the first `To` when the `@upload.pipedream.net` inbox isn't among them (auto-forward), else `from` (hand forward).

Step errors: `401` secret mismatch (redeploy Vercel after changing env vars), `422` no sender found, `502` coordinator unreachable, `{"ok":true}` but no card → address not linked or a repeated `message_id`.

**`server.py` `/email`** (forwarding only): checks the `X-Webhook-Secret` header, takes the user's address from `forwarded_for`, else `to` on an auto-forward, else `from`, and sends `Receipt(user_id="", sender=<email>)` the same way as above. `cloudflared tunnel --url http://localhost:8000` works for testing it locally.


## Refund requests (Phase 3)

Owners: policy agent MM77a, claim agent Jiayi, coordinator wiring Vinny. **Built:** `policy_agent.py`, `claim_agent.py`, the Phase 3 models in `models.py`, and the coordinator wiring for both (below).

**Why.** Finding a drop isn't the money. Stores don't refund drops on their own, and returns can't be automated: they sit behind the user's login and change often. But store chats (Target, Best Buy, Amazon customer service) take a pasted message. So the last step is: **"Dropped $6.02" → tap "Draft a refund request" → where to send it, and a ready-to-paste message.** The user sends it; we never contact a store.

**Flow.** `PriceResult` shows a drop → coordinator sends `PolicyCheck` → `PolicyResult` saved in the coordinator's `ctx.storage` under `policy:<item_id>`, its `summary` added to the price card → user taps **Draft a refund request** → `ClaimRequest` (item facts + the policy's `action`/`deadline`) → `ClaimDraft` → coordinator sends "Paste this into {where} by {deadline}:" and then the message alone (so it copies cleanly). Each agent works without the other: no policy → the claim agent picks its default action and states no deadline; no claim agent → the card still shows the policy summary.

**Policy agent (`policy_agent.py`, MM77a; port 8005, `POLICY_AGENT_SEED`, mailbox)**
- A rule table, no LLM: store → `action`, window in days, `where` (help/chat link), `source_url`. `deadline` = purchase date + window; empty when the purchase date is unknown; `action="none"` with a `summary` like "Return window closed Oct 30" when it's passed.
- What we know so far (**verify each against the store's current policy page, and put that URL and the date checked in the table**): Amazon and Walmart don't refund price drops after purchase, so the options are return & rebuy within the return window, or (Amazon) asking customer service for a goodwill credit, which isn't guaranteed. Target and Best Buy offer price adjustments within a set window. Many items have different windows (electronics, holiday season); start with the default window and say so in `summary`.
- `store` is "amazon" | "walmart" | "other" today (`store_for` in `agent.py`); add "target"/"bestbuy" rows now so they work once we track those stores.
- Always reply with a `PolicyResult` (unknown store → `ok=True, action="none"`, a `summary` saying to check the store's policy).
- **Built** (`RULES` in `policy_agent.py`). Steps are tried in order; the first one still open gives `action` and `deadline` (purchase date + days). No purchase date → the first step, no deadline, and the window in days in `summary`. All closed → `action="none"`, "Return window closed …". Test: `uv run policy_agent.py --check walmart 98 80 2026-10-20` → `return_rebuy`, deadline 2027-01-18, "Return & rebuy by Jan 18 to save $18.00 (most electronics: 30 days)". `--send` (same arguments) sends that `PolicyCheck` to the agent running on this machine (straight to port 8005, no mailbox) and prints the `PolicyResult` it replies with.

  | Store | Steps | Source | Checked |
  | --- | --- | --- | --- |
  | amazon | goodwill credit, 30 days (no price-drop refunds; return & rebuy in the same window) | `amazon.com/gp/help/customer/display.html?nodeId=GKM69DUUYKQWKWX7` | 2026-10-24: 30 days |
  | walmart | return & rebuy, 90 days (most electronics 30, said in `summary`) | `walmart.com/help/article/walmart-standard-return-policy/…` | 2026-10-24: 90 days, electronics 30 |
  | target | price adjustment 14 days, then return & rebuy 90 days | `target.com/returns` | 2026-10-24: 90 days (Target brand 365). 2026-10-25: price match `target.com/help/articles/policies-guidelines/price-match-guarantee`, Target's own lower price within 14 days, original receipt required |
  | bestbuy | price adjustment, 15 days | `bestbuy.com/site/help-topics/price-match-guarantee/…` | 2026-10-25: matches its own lower price "during the return and exchange period"; return period `bestbuy.com/site/help-topics/return-exchange-policy/…`: 15 days, My Best Buy Plus/Total 60, activatable devices (phones) 14 |

  Pages were read with Bright Data (MCP `search_engine` + `scrape_as_markdown`); the 2026-10-25 Target price match and Best Buy return period rows were confirmed from the official pages through a web search (the sandbox couldn't open the store sites directly). The `where` links (store help/chat pages) haven't been opened yet.

**Claim agent (`claim_agent.py`, Jiayi; port 8004, `CLAIM_AGENT_SEED`, mailbox)**
- `asi1` writes the message: `AsyncOpenAI(api_key=ASI_ONE_API_KEY, base_url="https://api.asi1.ai/v1")`, `model="asi1"`, like `parser_agent.py`. Plain text, polite, under ~120 words, asks for the one thing in `action`, and uses **only the facts given** (item, prices, the difference, order number, purchase date, deadline). Never invent a policy, date, order number or promise.
- Default when `action` is empty: Amazon → goodwill credit, Walmart → return & rebuy, other → price adjustment; with a default `where` per store (help/chat link).
- Template fallback (no key, asi1 error, empty answer): a fixed message filled from the same facts, so the button always gives something usable.
- uagents runs an agent's handlers one at a time: start the asi1 call in the background and return (copy `background()` from `price_agent.py`). Always reply with a `ClaimDraft`.
- A `--check` CLI, e.g. `uv run claim_agent.py --check amazon "Ticonderoga pencils" 4.50 2.97`, prints one draft Optional `[action] [deadline]` after the prices. Without `ASI_ONE_API_KEY` it prints the template.
- Built as above. `action="none"` or unknown also gets the store default. `where` is always the default per store (`ClaimRequest` has no `where`), so the coordinator should prefer the saved `PolicyResult.where`. A price that hasn't dropped → `ok=False`. asi1 calls time out after 30 s (`LLM_TIMEOUT_S`, one retry) and fall back to the template. With `COORDINATOR_ADDRESS` set, only the coordinator may ask for drafts.

**Coordinator side (Vinny)**
- Built: `store.py` has `purchase_date` and `order_number` on `items` (an `ALTER TABLE` migration in `connect()` for older databases), filled from the receipt when an item is tracked from one: direct link, search pick or link form all carry the receipt id (`"receipt"` in the selection; only the sender's own receipt is used).
- Built: `price_detail` shows **Draft a refund request** (`{"action": "draft_claim", "item_id"}`) on drops, first and primary. `draft_claim` checks the item is the sender's and has dropped (prose without an item id drafts only when exactly one item has dropped), builds the `ClaimRequest` (plus `policy:<item_id>` from storage if present) and sends it to `CLAIM_AGENT_ADDRESS`; without that address a labelled test draft stands in. `on_claim_draft` (sender checked) sends "Paste the next message into {where}", then the message alone, through `say()`.
- Built: on a drop, `show_price` sends the card, then `PolicyCheck` (with the item's purchase date) if `POLICY_AGENT_ADDRESS` is set. `on_policy_result` (sender checked) saves it under `policy:<item_id>` and sends "What you can do: {summary}" (plus a nudge to tap Draft a refund request unless the window has closed). Opening the item from My items shows the saved summary as a "What to do" row; `draft_claim` passes its `action`/`deadline` on, and `show_claim` uses its `where` and deadline ("by Jan 18").

**Rules**
- Never log the draft text or order numbers (item id, store, action and timing only).
- The user sends every message; no agent contacts a store or logs into anything.
- Don't state a deadline or policy the policy agent didn't return.

## Submission checklist

- [ ] README lists every agent's name and address, plus both badges:
  `![tag:innovationlab](https://img.shields.io/badge/innovationlab-3D8BD3)`
  `![tag:hackathon](https://img.shields.io/badge/hackathon-5F43F1)`
- [ ] Each agent's Agentverse profile: clear name, description under 300 characters, example prompts
- [ ] Public ASI:One shared chat link showing the full flow
- [ ] 3–5 minute demo video, Devpost, CalHacks ASI:One Submission Agent

## Sources (checked 2026-10-23)

- [uAgent creation, docs v1.0.5](https://innovationlab.fetch.ai/resources/docs/agent-creation/uagent-creation)
- [Agent Chat Protocol, docs v1.0.5](https://innovationlab.fetch.ai/resources/docs/agent-communication/agent-chat-protocol)
- [ASI:One Interactive Cards, docs v1.0.5](https://innovationlab.fetch.ai/resources/docs/interactive-cards/asi-interactive-cards)
- [ASI:One OpenAI compatibility, docs v1.0.5](https://innovationlab.fetch.ai/resources/docs/asione/build/openai-compatibility)
- [fetch-skills chat-protocol skill v2.0.3 (2026-05-04)](https://cdn.jsdelivr.net/npm/fetch-skills@2.0.3/skills/chat-protocol/SKILL.md), older than the docs; docs win
- [Bright Data llms.txt](https://docs.brightdata.com/llms.txt): agent guidance (Scraper API first, MCP for page fetching, Bearer auth)
- [Scraper API synchronous requests](https://docs.brightdata.com/api-reference/scrapers/synchronous-requests.md): `/datasets/v3/scrape`, 1-minute limit, 202 + `snapshot_id`
- [Amazon Scraper API](https://docs.brightdata.com/products/scrapers/amazon/introduction.md): dataset `gd_l7q7dkf244hwjntr0`, `final_price` / `initial_price` / `currency` / `availability`
- [Scraper API limits](https://docs.brightdata.com/products/scrapers/limits.md): one input per sync request, 25 × 429 in 5 min blocks the IP
- [Free tier](https://docs.brightdata.com/general/account/billing-and-pricing/free-tier.md): 5,000 shared credits/month
- [Bright Data MCP overview](https://docs.brightdata.com/products/mcp-server/overview.md), [remote quickstart](https://docs.brightdata.com/products/mcp-server/remote/quickstart), [tools](https://docs.brightdata.com/products/mcp-server/tools), [advanced config](https://docs.brightdata.com/products/mcp-server/remote/advanced)
- [Image analysis agent example, docs v1.0.5](https://innovationlab.fetch.ai/resources/docs/examples/chat-protocol/image-analysis-agent): `{"attachments": "true"}` on session start, image URL from `ResourceContent` (it uses OpenAI; we use `asi1` instead)
- ASI:One API docs, image input (checked 2026-10-24): every model takes images as a `data:` URI, ≤ 20 MB, ≤ 10 per request, same rules on `/v1/chat/completions` and `/v1/responses`; unreadable image → `400 unsupported_content_part`
- [Fetch.ai CalHacks 13.0 hackpack](https://innovationlab.fetch.ai/events/hackathons/calhacks-13-0/hackpack)
- PyPI release data; sandbox tests of uagents 0.26.1 on Python 3.13 vs 3.14 and the mcp 2.3.0 client API
