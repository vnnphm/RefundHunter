"""Policy agent: which refund request makes sense for a price drop, and the last day to make it.

A rule table per store, no LLM and no scraping: answers instantly and costs no credits.
See "Refund requests" in AGENTS.md for the contract and where each rule comes from.
"""
import asyncio
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone

from dotenv import load_dotenv
from uagents import Agent, Context
from uagents.communication import send_sync_message
from uagents.resolver import RulesBasedResolver

from models import PolicyCheck, PolicyResult

load_dotenv()

SEED = os.getenv("POLICY_AGENT_SEED", "")
if not SEED or SEED.startswith("change-me"):
    sys.exit("Set your own POLICY_AGENT_SEED in .env (see README). Same seed = same address as someone else.")

agent = Agent(
    name="refund-hunter-policy",
    seed=SEED,  # keep fixed: it sets the agent's address
    port=8005,
    mailbox=True,
)

# One row per store. `steps` are tried in order: (action, days after purchase it's open).
# `checked` is the day the source page was read. `notes` adds a caveat to the summary for that action.
RULES = {
    "amazon": {
        "name": "Amazon",
        # No refunds for price drops; a goodwill credit is a request, the return window is the sure option.
        "steps": [("goodwill_credit", 30)],
        "where": "Amazon customer service chat: https://www.amazon.com/gp/help/customer/contact-us",
        "source_url": "https://www.amazon.com/gp/help/customer/display.html?nodeId=GKM69DUUYKQWKWX7",
        "checked": "2026-10-24",
        "notes": {},
    },
    "walmart": {
        "name": "Walmart",
        "steps": [("return_rebuy", 90)],
        "where": "Walmart orders page, Start a return: https://www.walmart.com/orders",
        "source_url": "https://www.walmart.com/help/article/walmart-standard-return-policy/adc0dfb692954e67a4de206fb8d9e03a",
        "checked": "2026-10-24",
        "notes": {"return_rebuy": "most electronics: 30 days", "none": "most electronics: 30 days"},
    },
    "target": {
        "name": "Target",
        "steps": [("price_adjustment", 14), ("return_rebuy", 90)],
        "where": "Target guest services chat: https://help.target.com",
        "source_url": "https://www.target.com/returns",
        "checked": "2026-10-24",  # the 90-day return window; the 14-day price match isn't read live yet
        "notes": {"return_rebuy": "Target-brand items: 365 days", "none": "Target-brand items: 365 days"},
    },
    "bestbuy": {
        "name": "Best Buy",
        # Best Buy matches its own lower price during the return and exchange period.
        "steps": [("price_adjustment", 15)],
        "where": "Best Buy chat, or call 1-888-237-8289: https://www.bestbuy.com/site/customer-service",
        "source_url": "https://www.bestbuy.com/site/help-topics/price-match-guarantee/pcmcat290300050002.c",
        "checked": "2026-10-25",  # the price match rule; the 15-day return period isn't read live yet
        "notes": {"price_adjustment": "longer for My Best Buy members", "none": "longer for My Best Buy members"},
    },
}


def day(d: date) -> str:
    return f"{d:%b} {d.day}"


def describe(action: str, rule: dict, drop: str, by: str) -> str:
    """One line for the card, e.g. "Return & rebuy by Nov 12 to save $6.02". `drop` is "$6.02" or ""."""
    amount = f"{drop} " if drop else ""
    if action == "price_adjustment":
        text = f"Ask {rule['name']} for a {amount}price adjustment {by}"
    elif action == "goodwill_credit":
        text = f"Ask {rule['name']} for a {amount}goodwill credit {by} (not guaranteed), or return & rebuy in that time"
    else:
        text = f"Return & rebuy {by}" + (f" to save {drop}" if drop else "")
    note = rule["notes"].get(action)
    return f"{text} ({note})" if note else text


def decide(req: PolicyCheck, today: date | None = None) -> PolicyResult:
    today = today or datetime.now(timezone.utc).date()
    rule = RULES.get(req.store)
    if rule is None:
        return PolicyResult(item_id=req.item_id, ok=True, action="none",
                            summary="Check the store's return and price-adjustment policy: we don't have its rules yet.")
    gap = round(req.price_paid - req.current_price, 2)
    drop = f"${gap:.2f}" if gap > 0 else ""
    common = {"item_id": req.item_id, "ok": True, "where": rule["where"], "source_url": rule["source_url"]}
    try:
        bought = date.fromisoformat(req.purchase_date) if req.purchase_date else None
    except ValueError:
        bought = None
    if bought is None:  # no deadline without a purchase date: say how long the window is instead
        action, days = rule["steps"][0]
        return PolicyResult(**common, action=action,
                            summary=describe(action, rule, drop, f"within {days} days of purchase"))
    for action, days in rule["steps"]:
        deadline = bought + timedelta(days=days)
        if deadline >= today:
            return PolicyResult(**common, action=action, deadline=deadline.isoformat(),
                                summary=describe(action, rule, drop, f"by {day(deadline)}"))
    note = f" ({rule['notes']['none']})" if "none" in rule["notes"] else ""
    return PolicyResult(**common, action="none", deadline=deadline.isoformat(),
                        summary=f"{rule['name']}'s return window closed {day(deadline)}{note}")


@agent.on_message(model=PolicyCheck, replies=PolicyResult)
async def on_check(ctx: Context, sender: str, req: PolicyCheck):
    try:
        result = decide(req)
    except Exception as e:  # never leave the coordinator waiting
        ctx.logger.error(f"policy check crashed for {req.item_id}: {type(e).__name__}")
        result = PolicyResult(item_id=req.item_id, ok=False, error=f"unexpected error: {type(e).__name__}")
    ctx.logger.info(f"{req.item_id} [{req.store}] {result.action} deadline={result.deadline or '-'}")
    await ctx.send(sender, result)


async def send_once(req: PolicyCheck):
    """Manual test of the running agent: send it one PolicyCheck as a message and print its reply.
    Goes straight to this machine's port 8005 (no mailbox), so start the agent first."""
    local = RulesBasedResolver({agent.address: "http://127.0.0.1:8005/submit"})
    reply = await send_sync_message(agent.address, req, response_type=PolicyResult, resolver=local, timeout=15)
    if isinstance(reply, PolicyResult):
        print(json.dumps(json.loads(reply.model_dump_json()), indent=2))
    else:
        sys.exit(f"No reply ({reply}). Is the policy agent running? Start it with uv run policy_agent.py")


if __name__ == "__main__":
    if len(sys.argv) > 4 and sys.argv[1] in ("--check", "--send"):
        # uv run policy_agent.py --check|--send <store> <price_paid> <current_price> [YYYY-MM-DD]
        req = PolicyCheck(item_id="it_check000", store=sys.argv[2], price_paid=float(sys.argv[3]),
                          current_price=float(sys.argv[4]), purchase_date=sys.argv[5] if len(sys.argv) > 5 else "")
        if sys.argv[1] == "--send":
            asyncio.run(send_once(req))
        else:
            print(json.dumps(json.loads(decide(req).model_dump_json()), indent=2))
    else:
        agent.run()
