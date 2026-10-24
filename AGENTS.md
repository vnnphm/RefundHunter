# AGENTS.md — Refund Hunter

The rules for anyone (human or coding agent) working in this repo. Verified against current docs and releases on 2026-10-23. If code and this file disagree, fix one of them.

## What we're building

Fetch.ai uAgents that find money users are owed on past purchases. Users only talk to it inside an ASI:One chat. Fetch.ai track, Cal Hacks 13.0 (Oct 23–25, 2026).

**Phase 1 (must ship):** user adds an item in ASI:One → we fetch its live price with Bright Data → chat shows "Dropped $X" or "No change".
**Phase 2 (should ship):** new order emails arrive automatically (Gmail → Pipedream → FastAPI) and become tracked items.
**Phase 3 (if time):** refund policy check, claim draft, Payment Protocol.

Build in phase order. Don't start a phase until the one before works in ASI:One.

## Agents

Each agent is one file, one job, one seed. Only the coordinator talks to users.

| Agent | File | Phase | Job | Receives | Sends |
| --- | --- | --- | --- | --- | --- |
| Coordinator | `agent.py` | 1 | Talks to the user in ASI:One, shows cards, saves items, asks the price agent for prices | `ChatMessage` (user), `PriceResult` | `ChatMessage` + cards, `TrackItem` |
| Price agent | `price_agent.py` | 1 | Gets the live price of one item from Bright Data | `TrackItem` | `PriceResult` |
| Parser agent | `parser_agent.py` | 2 | Turns an order email into a tracked item | `Receipt` (from the webhook) | `TrackItem` |
| Policy agent | `policy_agent.py` | 3 | Is the item still refundable, and until when | `TrackItem` | `PolicyResult` |
| Claim writer | `claim_agent.py` | 3 | Drafts the refund request | item + policy | claim text |

Not agents: `store.py` (SQLite, shared state), `server.py` (FastAPI webhook), Pipedream, Bright Data.

**Flow (Phase 1):** user → coordinator → saves item → `TrackItem` → price agent → Bright Data → `PriceResult` → coordinator → saves price → detail card to user.

## Who owns what (2 people)

| | Person A: ASI:One side | Person B: data side |
| --- | --- | --- |
| Owns | `agent.py`, `store.py`, cards, README, demo video | `price_agent.py`, `parser_agent.py`, `server.py`, Pipedream |
| Phase 1 | Coordinator replies in ASI:One → form card → save item → detail card + carousel | Bright Data prints a real price (Price scraping, Path 1) → price agent answers `TrackItem` with `PriceResult` |
| Phase 2 | Show email-sourced items in the carousel | FastAPI on Railway + Pipedream → parser agent |
| Until the other side is ready | Fake `PriceResult` with a hardcoded price | Test with a script that sends `TrackItem` |

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

class Receipt(Model):         # webhook -> parser agent (Phase 2)
    message_id: str
    user_id: str
    sender: str = ""
    subject: str = ""
    date: str = ""
    body: str = ""            # plain text, max 8,000 chars
```

A price counts as dropped when `price_paid - current_price > 1` and `ok` is true.

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

**`.env` keys:** `AGENT_SEED` (coordinator), `PRICE_AGENT_SEED`, `PARSER_AGENT_SEED`, `PRICE_AGENT_ADDRESS`, `BRIGHTDATA_API_TOKEN`, `ASI_ONE_API_KEY`, `WEBHOOK_SECRET`. `BRIGHTDATA_API_TOKEN` is one key for both the Scraper API and the MCP.
- **Every person uses their own seeds.** Same seed = same address, and two running copies fight over one mailbox.
- **Ports:** coordinator 8001, price agent 8002, parser agent 8003.

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
| Anything else (Walmart, Target, …) | **B: MCP** `search_scrapers` → if a scraper exists, run it via Path A with its `dataset_id` | Bright Data says don't build a scraper for a site that has one |
| No scraper found | **C: MCP** `scrape_as_markdown` → ASI:One reads the price | Works on any page |

Build Path A first; it covers the demo. Add C as the general fallback, then B if there's time.

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
- Only the price agent calls Bright Data. The coordinator says "Checking price…" and sends the card when `PriceResult` arrives.
- Every failure (timeout, 4xx/5xx, empty record, no price) becomes `PriceResult(ok=False, error=...)`. Never let the handler raise.
- Accept only a positive number as a price.
- Don't re-scrape a URL within a few minutes; reuse the stored price and `checked_at`. Credits are shared.
- Skip the MCP `extract` tool: it relies on MCP sampling, which our client doesn't implement. Path C does the same job.
- Never log the API key or a URL containing `?token=`.

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
- [Fetch.ai CalHacks 13.0 hackpack](https://innovationlab.fetch.ai/events/hackathons/calhacks-13-0/hackpack)
- PyPI release data; sandbox tests of uagents 0.26.1 on Python 3.13 vs 3.14 and the mcp 2.3.0 client API
