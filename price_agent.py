import asyncio
import os
import re
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urlencode, urlparse

import httpx
from dotenv import load_dotenv
from uagents import Agent, Context

from agentverse_profile import profile
from models import FindProduct, FindResult, PriceResult, ProductMatch, TrackItem

load_dotenv()

BD_API = "https://api.brightdata.com/datasets/v3"
# Bright Data scrapers, collect by URL (from docs.brightdata.com/scrapers.json)
DATASETS = {
    "amazon": "gd_l7q7dkf244hwjntr0",   # Amazon products
    "walmart": "gd_l95fol7l1ru6rlo116",  # Walmart - products
    "target": "gd_ltppk5mx2lp0v1k0vo",   # Target
    "bestbuy": "gd_ltre1jqe1jfr7cccf",   # Best Buy products
}
SEARCH_LIMIT = 3  # records are billed one credit each: ask for only what the carousel shows
PRICE_FIELDS = ("final_price", "price", "initial_price")
POLL_LIMIT_S = 180
SEARCH_LIMIT_S = 75  # a search slower than this gets the link form instead: pasting beats waiting
MAX_JOBS = 4  # Bright Data jobs at once
CACHE_TTL_S = 600  # free tier credits are shared: don't re-scrape a URL for 10 min

SEED = os.getenv("PRICE_AGENT_SEED", "")
if not SEED or SEED.startswith("change-me"):
    sys.exit("Set your own PRICE_AGENT_SEED in .env (see README). Same seed = same address as someone else.")

agent = Agent(
    name="refund-hunter-price",
    seed=SEED,  # keep fixed: it sets the agent's address
    port=8002,
    mailbox=True,
    **profile("price", "Refund Hunter's price agent. Gets a product's live price at Amazon, Walmart, Target or Best Buy with Bright Data's Scraper API, and finds a store's listing for a receipt item by its name. Answers TrackItem with PriceResult and FindProduct with FindResult."),
)

_cache: dict[str, tuple[float, float, str | None]] = {}  # url -> (time, price, title)


class ScrapeError(Exception):
    """A short, user-safe reason the price check failed."""


async def scrape(url: str, dataset_id: str, client: httpx.AsyncClient | None = None) -> dict:
    """Run one Bright Data Scraper API job for one URL and return its record."""
    records = await scrape_records({"url": url}, dataset_id, client=client)
    if not records:
        raise ScrapeError("no product found at that link")
    return records[0]


async def scrape_records(inp: dict, dataset_id: str, params: dict | None = None,
                         client: httpx.AsyncClient | None = None, limit_s: float | None = None) -> list[dict]:
    """Run one Bright Data Scraper API job for one input and return all its records.
    limit_s caps the whole job (request + polling); by default the request waits 90 s, then polls 180 s."""
    give_up = time.monotonic() + limit_s if limit_s else None
    token = os.getenv("BRIGHTDATA_API_TOKEN", "")
    if not token:
        raise ScrapeError("BRIGHTDATA_API_TOKEN is not set")
    own = client is None
    client = client or httpx.AsyncClient(timeout=90)
    client.headers["Authorization"] = f"Bearer {token}"
    try:
        r = await client.post(f"{BD_API}/scrape",
                              params={"dataset_id": dataset_id, "format": "json", **(params or {})},
                              json={"input": [inp]}, timeout=min(90, limit_s) if limit_s else httpx.USE_CLIENT_DEFAULT)
        if r.status_code == 202:  # took over a minute: poll the snapshot
            sid = r.json()["snapshot_id"]
            deadline = give_up or time.monotonic() + POLL_LIMIT_S
            while True:
                if time.monotonic() > deadline:
                    raise ScrapeError("Bright Data took too long")
                await asyncio.sleep(min(int(r.headers.get("retry-after", 10)), max(1, deadline - time.monotonic())))
                p = await client.get(f"{BD_API}/progress/{sid}")
                check(p)
                status = p.json().get("status")
                if status == "ready":
                    r = await client.get(f"{BD_API}/snapshot/{sid}", params={"format": "json"})
                    break
                if status in ("failed", "canceled"):
                    raise ScrapeError(f"Bright Data job {status}")
                if time.monotonic() > deadline:
                    raise ScrapeError("Bright Data took too long")
        check(r)
        records = r.json()
    except httpx.TimeoutException as e:
        raise ScrapeError("Bright Data took too long") from e
    except httpx.HTTPError as e:
        raise ScrapeError(f"network error: {type(e).__name__}") from e
    finally:
        if own:
            await client.aclose()
    if not isinstance(records, list):
        raise ScrapeError("unexpected reply from Bright Data")
    return [rec for rec in records if isinstance(rec, dict) and not rec.get("error")]


def check(r: httpx.Response):
    # No retries anywhere: 25 x 429 within 5 minutes gets our IP blocked.
    reasons = {400: "Bright Data rejected the link", 401: "Bright Data token is invalid",
               404: "unknown Bright Data scraper", 429: "Bright Data rate limit hit"}
    if r.status_code >= 400:
        raise ScrapeError(reasons.get(r.status_code, f"Bright Data error {r.status_code}"))


def pick_price(rec: dict) -> float | None:
    for field in PRICE_FIELDS:
        try:
            price = float(str(rec.get(field)).replace("$", "").replace(",", ""))
        except ValueError:
            continue
        if price > 0:
            return price
    return None


async def get_price(req: TrackItem, client: httpx.AsyncClient | None = None) -> PriceResult:
    now = datetime.now(timezone.utc).isoformat()
    dataset_id = DATASETS.get(req.store)
    if dataset_id is None:
        return PriceResult(item_id=req.item_id, ok=False, checked_at=now, error="store not supported yet")
    cached = _cache.get(req.product_url)
    if cached and time.time() - cached[0] < CACHE_TTL_S:
        return PriceResult(item_id=req.item_id, ok=True, current_price=cached[1], title=cached[2],
                           checked_at=datetime.fromtimestamp(cached[0], timezone.utc).isoformat())
    try:
        rec = await scrape(req.product_url, dataset_id, client)
    except ScrapeError as e:
        return PriceResult(item_id=req.item_id, ok=False, checked_at=now, error=str(e))
    price = pick_price(rec)
    if price is None:
        return PriceResult(item_id=req.item_id, ok=False, checked_at=now, error="no price on the page")
    title = rec.get("title") or rec.get("product_name")
    _cache[req.product_url] = (time.time(), price, title)
    return PriceResult(item_id=req.item_id, ok=True, current_price=price, title=title, checked_at=now)


# uagents runs an agent's handlers one at a time, so a slow scrape would hold up every request
# behind it. Handlers start the job in the background and return; MAX_JOBS run at once.
_jobs: set[asyncio.Task] = set()
_slots = asyncio.Semaphore(MAX_JOBS)


def background(coro):
    async def run():
        async with _slots:
            await coro
    task = asyncio.get_running_loop().create_task(run())
    _jobs.add(task)  # keep a reference so the task isn't garbage-collected mid-scrape
    task.add_done_callback(_jobs.discard)


@agent.on_message(model=TrackItem, replies=PriceResult)
async def on_track(ctx: Context, sender: str, req: TrackItem):
    background(answer_track(ctx, sender, req))


async def answer_track(ctx: Context, sender: str, req: TrackItem):
    start = time.monotonic()
    try:
        result = await get_price(req)
    except Exception as e:  # never leave the coordinator waiting
        ctx.logger.exception(f"price check crashed for {req.item_id}")
        result = PriceResult(item_id=req.item_id, ok=False, error=f"unexpected error: {type(e).__name__}",
                             checked_at=datetime.now(timezone.utc).isoformat())
    outcome = f"${result.current_price:.2f}" if result.ok else f"failed ({result.error})"
    ctx.logger.info(f"{req.item_id} [{req.store}] {outcome} in {time.monotonic() - start:.1f}s")
    await ctx.send(sender, result)


def _amazon_hit(rec: dict) -> tuple[str, int, bool] | None:
    asin = rec.get("asin")
    if not asin:
        return None
    return f"https://www.amazon.com/dp/{asin}", rec.get("rank_on_page") or 99, str(rec.get("sponsored")).lower() == "true"


def _walmart_hit(rec: dict) -> tuple[str, int, bool] | None:
    item = rec.get("us_item_id")
    if not item:
        return None
    rank = rec.get("product_index") or rec.get("placement_on_page") or 99
    return f"https://www.walmart.com/ip/{item}", rank, bool(rec.get("is_sponsored_flag"))


SPANISH = re.compile(r"[áéíóúñ¿¡]|\b(de|para|con|caja|paquete|unidades|juego)\b", re.IGNORECASE)


def english_name(rec: dict) -> str:
    """Walmart's search sometimes answers in Spanish; the product URL's slug is always English."""
    name = rec.get("name") or rec.get("title") or ""
    slug = urlparse(rec.get("canonical_url") or "").path.split("/")
    if SPANISH.search(name) and len(slug) > 3 and slug[1] == "ip":
        return slug[2].replace("-", " ")
    return name


# store -> (dataset, input for a name, record -> (product url, rank, sponsored))
SEARCHES = {
    "amazon": ("gd_lwdb4vjm1ehb499uxs",  # Amazon products search
               lambda q: {"keyword": q, "url": "https://www.amazon.com", "pages_to_search": 1}, _amazon_hit),
    "walmart": ("gd_m7khey0wb7wviejgj",  # Walmart products search, by search-page URL
                lambda q: {"url": "https://www.walmart.com/search?" + urlencode({"q": q})}, _walmart_hit),
}


async def search_records(query: str, store: str, client: httpx.AsyncClient | None = None) -> list[dict]:
    if store not in SEARCHES:
        raise ScrapeError("search only works for Amazon and Walmart")
    dataset_id, make_input, _ = SEARCHES[store]
    query = query.strip().rstrip(".…").strip()[:80]  # a cut-off name ("Corrugated Sh...") searches badly
    return await scrape_records(make_input(query), dataset_id, {"limit_per_input": SEARCH_LIMIT}, client,
                                limit_s=SEARCH_LIMIT_S)


async def find_products(query: str, store: str = "amazon",
                        client: httpx.AsyncClient | None = None) -> list[ProductMatch]:
    """Search a store for a receipt item's name. Top matches, cached so tracking one is free."""
    hits = []
    for rec in await search_records(query, store, client):
        hit, price = SEARCHES[store][2](rec), pick_price(rec)
        if hit and price is not None and not hit[2]:
            hits.append((hit[1], hit[0], price, rec))
    matches = []
    for _, url, price, rec in sorted(hits, key=lambda h: h[0])[:SEARCH_LIMIT]:
        name = english_name(rec)
        _cache[url] = (time.time(), price, name)
        matches.append(ProductMatch(name=name, product_url=url, price=price, image_url=rec.get("image") or ""))
    return matches


@agent.on_message(model=FindProduct, replies=FindResult)
async def on_find(ctx: Context, sender: str, req: FindProduct):
    ctx.logger.info(f"search {req.item_id} [{req.store}] started")
    background(answer_find(ctx, sender, req))


async def answer_find(ctx: Context, sender: str, req: FindProduct):
    start = time.monotonic()
    try:
        result = FindResult(item_id=req.item_id, user_id=req.user_id, ok=True,
                            matches=await find_products(req.query, req.store))
    except ScrapeError as e:
        result = FindResult(item_id=req.item_id, user_id=req.user_id, ok=False, error=str(e))
    except Exception as e:  # never leave the coordinator waiting
        ctx.logger.exception(f"search crashed for {req.item_id}")
        result = FindResult(item_id=req.item_id, user_id=req.user_id, ok=False,
                            error=f"unexpected error: {type(e).__name__}")
    outcome = f"{len(result.matches)} match(es)" if result.ok else f"failed ({result.error})"
    ctx.logger.info(f"search {req.item_id}: {outcome} in {time.monotonic() - start:.1f}s")
    await ctx.send(sender, result)


async def search_once(query: str, store: str):
    """Manual test: one search, print what came back. Costs one credit per record."""
    try:
        records = await search_records(query, store)
    except ScrapeError as e:
        print(f"failed: {e}")
        return
    print(f"{len(records)} record(s) delivered (limit asked: {SEARCH_LIMIT})")
    for rec in records[:10]:
        print(f"  {SEARCHES[store][2](rec)} ${pick_price(rec)} {(rec.get('name') or '')[:70]}")
    if records:
        print("all keys:", sorted(records[0]))


async def check_once(url: str, store: str):
    """Manual test: one scrape, print what came back. Costs one credit."""
    try:
        rec = await scrape(url, DATASETS[store])
    except ScrapeError as e:
        print(f"failed: {e}")
        return
    print(f"price: {pick_price(rec)}  title: {rec.get('title') or rec.get('product_name')}")
    print("price fields:", {f: rec.get(f) for f in PRICE_FIELDS})
    print("all keys:", sorted(rec))


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--search":
        # uv run price_agent.py --search "<item name>" [amazon|walmart]
        asyncio.run(search_once(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "amazon"))
    elif len(sys.argv) > 1 and sys.argv[1] == "--check":
        # uv run price_agent.py --check <url> [amazon|walmart|target|bestbuy]
        asyncio.run(check_once(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "amazon"))
    else:
        agent.run()
