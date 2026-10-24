import asyncio
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

from dotenv import load_dotenv
from mcp import ClientSession
from uagents import Agent, Context

from asi import LLMError, ask_json
from bd import DATASETS, ScrapeError, call, mcp_error, mcp_session, scrape
from models import PolicyResult, TrackItem

load_dotenv()

MCP_TOOLS = "search_engine,scrape_as_markdown"  # load only what we call
DOMAINS = {"amazon": "amazon.com", "walmart": "walmart.com"}  # amzn.to links etc. read amazon.com's policy
POLICY_LIMIT = 6000   # chars of the store's policy page sent to asi1
PRODUCT_LIMIT = 2000  # chars of return lines from the product page
CACHE_TTL_S = 24 * 3600  # policies rarely change and credits are shared: one scrape per store/link a day
RETURN_WORDS = re.compile(r"return|refund|replace", re.I)
KEYWORDS = re.compile(r"return|refund|replace|exchange|price (adjust|match|protect|guarantee)|\bdays?\b", re.I)

SEED = os.getenv("POLICY_AGENT_SEED", "")
if not SEED or SEED.startswith("change-me"):
    sys.exit("Set your own POLICY_AGENT_SEED in .env (see README). Same seed = same address as someone else.")

agent = Agent(
    name="refund-hunter-policy",
    seed=SEED,  # keep fixed: it sets the agent's address
    port=8004,
    mailbox=True,
)

PROMPT = """You check store refund policies for a shopper who bought the item below.
Use the store's policy page, and the product page lines if given (they can override the store default,
e.g. "Non-returnable" or "Returnable until Jan 31"). Return only JSON:
- returnable: false if this item can't be returned, true if it can, null if unclear
- return_days: days after purchase (or delivery) to return this item, as a whole number, or null if not stated.
  Use the window for this item's category if the policy lists one, otherwise the standard window.
- price_adjust_days: days after purchase the store refunds the difference if the price drops,
  0 if the policy says it doesn't do price adjustments, null if the page doesn't say
- summary: one or two plain sentences for the shopper: can they return it, by when, and whether
  the store refunds a price drop. Don't invent rules the pages don't state."""

SCHEMA = {
    "name": "refund_policy",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["returnable", "return_days", "price_adjust_days", "summary"],
        "properties": {
            "returnable": {"type": ["boolean", "null"]},
            "return_days": {"type": ["integer", "null"]},
            "price_adjust_days": {"type": ["integer", "null"]},
            "summary": {"type": "string"},
        },
    },
}

_policies: dict[str, tuple[float, str, str]] = {}  # domain -> (time, policy url, policy text)
_products: dict[str, tuple[float, str]] = {}       # product url -> (time, return lines)


class PolicyError(ScrapeError):
    """A short, user-safe reason the policy check failed. A ScrapeError so mcp_error() keeps it."""


# ---- Bright Data ---------------------------------------------------------

def domain_for(url: str, shop: str) -> str:
    if shop in DOMAINS:
        return DOMAINS[shop]
    host = urlparse(url).netloc.lower().split(":")[0]
    return re.sub(r"^(www|m|smile)\.", "", host)


def policy_link(results: str, domain: str) -> str | None:
    """The store's own policy page among search results: on its domain, 'return' in the link if possible."""
    links = []
    for u in re.findall(r"https?://[^\s\"'<>)\]]+", results):
        host = urlparse(u).netloc.lower().split(":")[0]
        if host == domain or host.endswith("." + domain):
            links.append(u.rstrip(".,"))
    return next((u for u in links if re.search(r"return|refund", u, re.I)), links[0] if links else None)


def relevant(markdown: str, limit: int) -> str:
    """Whole page if it fits, else only the lines about returns, refunds and windows."""
    if len(markdown) <= limit:
        return markdown
    keep = [line.strip() for line in markdown.splitlines() if KEYWORDS.search(line)]
    return "\n".join(line for line in keep if line)[:limit]


async def store_policy(s: ClientSession, domain: str) -> tuple[str, str]:
    cached = _policies.get(domain)
    if cached and time.time() - cached[0] < CACHE_TTL_S:
        return cached[1], cached[2]
    url = policy_link(await call(s, "search_engine", {"query": f"{domain} return policy"}), domain)
    if url is None:
        raise PolicyError(f"couldn't find {domain}'s return policy")
    text = relevant(await call(s, "scrape_as_markdown", {"url": url}), POLICY_LIMIT)
    if not KEYWORDS.search(text):
        raise PolicyError(f"{domain}'s policy page had no return rules")
    _policies[domain] = (time.time(), url, text)
    return url, text


def record_returns(rec, key: str = "") -> list[str]:
    """Every "field: value" in a Scraper API record that mentions returns, however deeply nested."""
    if isinstance(rec, dict):
        return [line for k, v in rec.items() for line in record_returns(v, f"{key}.{k}" if key else str(k))]
    if isinstance(rec, list):
        return [line for v in rec for line in record_returns(v, key)]
    text = f"{key}: {rec}"
    return [text] if rec not in (None, "") and RETURN_WORDS.search(text) and len(text) < 400 else []


async def product_returns(s: ClientSession, url: str, shop: str, log=print) -> str:
    """Return lines from the product ("Non-returnable", "Returnable until …"). Optional: "" on failure.

    Amazon and Walmart: the same Scraper API record the price agent reads (structured, 1 credit).
    Other stores: the product page as markdown through the MCP (1 credit)."""
    cached = _products.get(url)
    if cached and time.time() - cached[0] < CACHE_TTL_S:
        return cached[1]
    try:
        if shop in DATASETS:
            page = "\n".join(record_returns(await scrape(url, DATASETS[shop])))
        else:
            page = await call(s, "scrape_as_markdown", {"url": url})
    except Exception as e:  # the store policy alone still answers the question
        log(f"product page skipped: {type(e).__name__}")
        page = ""  # cached too: don't spend a credit on the same broken link again
    lines = [line.strip() for line in page.splitlines() if RETURN_WORDS.search(line) and len(line) < 400]
    text = "\n".join(lines)[:PRODUCT_LIMIT]
    _products[url] = (time.time(), text)
    return text


# ---- asi1 --------------------------------------------------------------------

async def read_policy(domain: str, policy: str, product: str) -> dict:
    text = f"{PROMPT}\n\nStore: {domain}\n\nStore policy page:\n{policy}"
    if product:
        text += f"\n\nProduct page lines about returns:\n{product}"
    try:
        return await ask_json(text, SCHEMA)
    except LLMError as e:
        raise PolicyError(str(e)) from e


def to_days(value) -> int | None:
    """A whole number of days in 0..365, else None (we don't trust it)."""
    if isinstance(value, bool):
        return None
    try:
        days = int(value)
    except (TypeError, ValueError):
        return None
    return days if 0 <= days <= 365 else None


async def get_policy(req: TrackItem, log=print) -> PolicyResult:
    now = datetime.now(timezone.utc).isoformat()
    domain = domain_for(req.product_url, req.store)
    if not domain:
        return PolicyResult(item_id=req.item_id, ok=False, checked_at=now, error="no store in that link")
    try:
        try:
            async with mcp_session(MCP_TOOLS) as s:
                url, policy = await store_policy(s, domain)
                product = await product_returns(s, req.product_url, req.store, log)
        except Exception as e:  # from None: the cause can carry the token URL
            raise PolicyError(str(mcp_error(e))) from None
        data = await read_policy(domain, policy, product)
    except PolicyError as e:
        return PolicyResult(item_id=req.item_id, ok=False, checked_at=now, error=str(e))
    returnable = data.get("returnable")
    return PolicyResult(item_id=req.item_id, ok=True, checked_at=now, policy_url=url,
                        returnable=returnable if isinstance(returnable, bool) else None,
                        return_days=to_days(data.get("return_days")),
                        price_adjust_days=to_days(data.get("price_adjust_days")),
                        summary=str(data.get("summary") or "").strip()[:400])


@agent.on_message(model=TrackItem, replies=PolicyResult)
async def on_track(ctx: Context, sender: str, req: TrackItem):
    start = time.monotonic()
    try:
        result = await get_policy(req, ctx.logger.info)
    except Exception as e:  # never leave the coordinator waiting
        # Type only: MCP errors can carry the URL, which holds the token.
        ctx.logger.error(f"policy check crashed for {req.item_id}: {type(e).__name__}")
        result = PolicyResult(item_id=req.item_id, ok=False, error=f"unexpected error: {type(e).__name__}",
                              checked_at=datetime.now(timezone.utc).isoformat())
    outcome = (f"returnable={result.returnable} days={result.return_days} adjust={result.price_adjust_days}"
               if result.ok else f"failed ({result.error})")
    ctx.logger.info(f"{req.item_id} [{req.store}] {outcome} in {time.monotonic() - start:.1f}s")
    await ctx.send(sender, result)


async def list_tools():
    """Step 0 in AGENTS.md: print the MCP tools we load and their inputs. Costs no credits."""
    async with mcp_session(MCP_TOOLS) as s:
        for t in (await s.list_tools()).tools:
            print(t.name, json.dumps(t.inputSchema))


async def check_once(url: str, shop: str):
    """Manual test: one policy check. Costs 2-3 Bright Data credits and one ASI:One call."""
    req = TrackItem(item_id="it_check000", user_id="check", product_url=url, store=shop)
    print(json.dumps(json.loads((await get_policy(req)).model_dump_json()), indent=2))


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--tools":
        asyncio.run(list_tools())
    elif len(sys.argv) > 2 and sys.argv[1] == "--check":
        # uv run policy_agent.py --check <url> [amazon|walmart|other]
        asyncio.run(check_once(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "amazon"))
    else:
        agent.run()
