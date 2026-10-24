import asyncio
import os
import sys
import time
from datetime import datetime, timezone

import httpx
from dotenv import load_dotenv
from uagents import Agent, Context

from models import PriceResult, TrackItem

load_dotenv()

BD_API = "https://api.brightdata.com/datasets/v3"
# Bright Data scrapers, collect by URL (from docs.brightdata.com/scrapers.json)
DATASETS = {
    "amazon": "gd_l7q7dkf244hwjntr0",   # Amazon products
    "walmart": "gd_l95fol7l1ru6rlo116",  # Walmart - products
}
PRICE_FIELDS = ("final_price", "price", "initial_price")
POLL_LIMIT_S = 180
CACHE_TTL_S = 600  # free tier credits are shared: don't re-scrape a URL for 10 min

SEED = os.getenv("PRICE_AGENT_SEED", "")
if not SEED or SEED.startswith("change-me"):
    sys.exit("Set your own PRICE_AGENT_SEED in .env (see README). Same seed = same address as someone else.")

agent = Agent(
    name="refund-hunter-price",
    seed=SEED,  # keep fixed: it sets the agent's address
    port=8002,
    mailbox=True,
)

_cache: dict[str, tuple[float, float, str | None]] = {}  # url -> (time, price, title)


class ScrapeError(Exception):
    """A short, user-safe reason the price check failed."""


async def scrape(url: str, dataset_id: str, client: httpx.AsyncClient | None = None) -> dict:
    """Run one Bright Data Scraper API job for one URL and return its record."""
    token = os.getenv("BRIGHTDATA_API_TOKEN", "")
    if not token:
        raise ScrapeError("BRIGHTDATA_API_TOKEN is not set")
    own = client is None
    client = client or httpx.AsyncClient(timeout=90)
    client.headers["Authorization"] = f"Bearer {token}"
    try:
        r = await client.post(f"{BD_API}/scrape", params={"dataset_id": dataset_id, "format": "json"},
                              json={"input": [{"url": url}]})
        if r.status_code == 202:  # took over a minute: poll the snapshot
            sid = r.json()["snapshot_id"]
            deadline = time.monotonic() + POLL_LIMIT_S
            while True:
                await asyncio.sleep(int(r.headers.get("retry-after", 10)))
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
    except httpx.HTTPError as e:
        raise ScrapeError(f"network error: {type(e).__name__}") from e
    finally:
        if own:
            await client.aclose()
    if not isinstance(records, list) or not records:
        raise ScrapeError("no product found at that link")
    return records[0]


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


@agent.on_message(model=TrackItem, replies=PriceResult)
async def on_track(ctx: Context, sender: str, req: TrackItem):
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
    if len(sys.argv) > 1 and sys.argv[1] == "--check":
        # uv run price_agent.py --check <url> [amazon|walmart]
        asyncio.run(check_once(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "amazon"))
    else:
        agent.run()
