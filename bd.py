"""Bright Data helpers shared by the price and policy agents. Never log the token or a URL holding it."""
import asyncio
import os
import time
from contextlib import asynccontextmanager

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

BD_API = "https://api.brightdata.com/datasets/v3"
BD_MCP = "https://mcp.brightdata.com/mcp"
# Bright Data scrapers, collect by URL (from docs.brightdata.com/scrapers.json)
DATASETS = {
    "amazon": "gd_l7q7dkf244hwjntr0",   # Amazon products
    "walmart": "gd_l95fol7l1ru6rlo116",  # Walmart - products
}
POLL_LIMIT_S = 180
MCP_TIMEOUT_S = 120


class ScrapeError(Exception):
    """A short, user-safe reason a Bright Data call failed."""


def token() -> str:
    key = os.getenv("BRIGHTDATA_API_TOKEN", "")
    if not key:
        raise ScrapeError("BRIGHTDATA_API_TOKEN is not set")
    return key


# ---- Path A: Scraper API -----------------------------------------------------

async def scrape(url: str, dataset_id: str, client: httpx.AsyncClient | None = None) -> dict:
    """Run one Bright Data Scraper API job for one URL and return its record."""
    key = token()
    own = client is None
    client = client or httpx.AsyncClient(timeout=90)
    client.headers["Authorization"] = f"Bearer {key}"
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


# ---- Path B/C: MCP -----------------------------------------------------------

@asynccontextmanager
async def mcp_session(tools: str):
    """One MCP session with only `tools` loaded (comma-separated). The URL holds the token."""
    async with streamable_http_client(f"{BD_MCP}?token={token()}&tools={tools}") as (read, write):
        async with ClientSession(read, write) as s:
            await s.initialize()
            yield s


async def call(s: ClientSession, tool: str, args: dict) -> str:
    """Run one MCP tool (1 credit) and return its text."""
    res = await s.call_tool(tool, args, read_timeout_seconds=MCP_TIMEOUT_S)
    if getattr(res, "is_error", False):
        raise ScrapeError(f"Bright Data {tool} failed")
    return "\n".join(getattr(c, "text", "") for c in getattr(res, "content", None) or [])


def mcp_error(e: BaseException) -> ScrapeError:
    """MCP errors arrive wrapped in task-group ExceptionGroups: keep ours, name the others by type."""
    while isinstance(e, BaseExceptionGroup):
        e = next((x for x in e.exceptions if isinstance(x, (ScrapeError, BaseExceptionGroup))), e.exceptions[0])
    return e if isinstance(e, ScrapeError) else ScrapeError(f"couldn't reach Bright Data: {type(e).__name__}")
