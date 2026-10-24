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
| mcp (Phase 1, price agent) | 2.3.0 | 2026-10-02 |
| openai (ASI:One LLM) | 3.37.0 | latest at time of writing |
| fastapi + uvicorn (Phase 2) | 0.162.0 / 0.54.0 | latest at time of writing |

**`.env` keys:** `AGENT_SEED` (coordinator), `PRICE_AGENT_SEED`, `PARSER_AGENT_SEED`, `PRICE_AGENT_ADDRESS`, `BRIGHTDATA_API_TOKEN`, `ASI_ONE_API_KEY`, `WEBHOOK_SECRET`.
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

**Bright Data MCP** (price agent)
- URL: `https://mcp.brightdata.com/mcp?token=<BRIGHTDATA_API_TOKEN>`
- mcp 2.x: `streamable_http_client(url)` yields **two** streams, `(read, write)`.
  ```python
  from mcp import ClientSession
  from mcp.client.streamable_http import streamable_http_client

  async with streamable_http_client(url) as (read, write):
      async with ClientSession(read, write) as s:
          await s.initialize()
          res = await s.call_tool("web_data_amazon_product", {"url": url_to_check},
                                  read_timeout_seconds=180)
  ```
- Tools: `web_data_amazon_product`, `web_data_walmart_product`; fallback `scrape_as_markdown`. Run `list_tools()` first to see what your token has.
- `groups=` / `pro=1` URL params are unofficial (Smithery listing). Verify before relying on them.

**ASI:One LLM**
- `OpenAI(api_key=os.getenv("ASI_ONE_API_KEY"), base_url="https://api.asi1.ai/v1")`, `model="asi1"`. No silent fallback to another provider.

**Payment Protocol** (Phase 3)
- Send `RequestPayment` before any LLM call, or ASI:One shows plain text instead of the payment card.

**General**
- Secrets only in `.env`; document every key in `.env.example`. Never log seeds, tokens, or full emails.
- Keep only extracted receipt fields (item, price, store, date, order number). Discard the rest.
- Small flat files. No new folders until there's a reason.

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
- [Bright Data remote MCP quickstart](https://docs.brightdata.com/products/mcp-server/remote/quickstart)
- [Bright Data Web MCP on Smithery](https://smithery.ai/skills/sitammeur/brightdata-web-mcp) (unofficial)
- [Fetch.ai CalHacks 13.0 hackpack](https://innovationlab.fetch.ai/events/hackathons/calhacks-13-0/hackpack)
- PyPI release data; sandbox tests of uagents 0.26.1 on Python 3.13 vs 3.14 and the mcp 2.3.0 client API
