import asyncio
import os
import re
import sys
import time
from datetime import date

from dotenv import load_dotenv
from openai import APIError, AsyncOpenAI
from uagents import Agent, Context

from models import ClaimDraft, ClaimRequest

load_dotenv()

ASI_API = "https://api.asi1.ai/v1"
MODEL = "asi1"
MESSAGE_LIMIT = 1200  # store chat boxes cut long pastes
MAX_JOBS = 4  # asi1 calls at once

ACTIONS = ("price_adjustment", "goodwill_credit", "return_rebuy")
# Used when ClaimRequest.action is empty (no policy agent yet) or "none".
DEFAULT_ACTION = {"amazon": "goodwill_credit", "walmart": "return_rebuy", "other": "price_adjustment"}
DEFAULT_WHERE = {
    "amazon": "Amazon customer service chat: https://www.amazon.com/gp/help/customer/contact-us",
    "walmart": "Walmart customer care chat: https://www.walmart.com/help",
    "other": "the store's customer service chat or email",
}
# What the message asks for, in the shopper's words.
ASK = {
    "price_adjustment": "a price adjustment: a refund of the difference to my original payment method",
    "goodwill_credit": "a one-time courtesy credit for the difference",
    "return_rebuy": "help returning it so I can buy it again at the lower price, or a refund of "
                    "the difference instead if that's easier",
}

SEED = os.getenv("CLAIM_AGENT_SEED", "")
if not SEED or SEED.startswith("change-me"):
    sys.exit("Set your own CLAIM_AGENT_SEED in .env (see README). Same seed = same address as someone else.")

agent = Agent(
    name="refund-hunter-claim",
    seed=SEED,  # keep fixed: it sets the agent's address
    port=8004,
    mailbox=True,
)

PROMPT = """You write a short message a shopper pastes into a store's customer service chat after
the price of something they bought dropped. Write as the shopper, in first person.
- Plain text only, no markdown, no subject line. Polite and direct, under 120 words.
- Ask for exactly this and nothing else: {ask}.
- Use only the facts below. Leave out anything not listed; never invent a policy, date, order
  number, promise or name, and never write a placeholder like [Your Name] or [Order #].
- If a deadline is listed, say you're asking before that date.
- End with "Thank you." and no signature.
Reply with the message only.

Facts:
{facts}"""


class ClaimError(Exception):
    """A short, user-safe reason the claim couldn't be drafted."""


def pick_action(req: ClaimRequest) -> str:
    return req.action if req.action in ACTIONS else DEFAULT_ACTION.get(req.store, DEFAULT_ACTION["other"])


def store_name(store: str) -> str:
    return store.title() if store in ("amazon", "walmart") else "your store"


def nice_date(value: str) -> str:
    """'October 10, 2026' from YYYY-MM-DD; "" if it isn't a real date."""
    try:
        d = date.fromisoformat(value[:10])
    except ValueError:
        return ""
    return f"{d:%B} {d.day}, {d.year}"


def facts(req: ClaimRequest) -> list[str]:
    """The facts the message may use; missing ones are left out."""
    diff = req.price_paid - req.current_price
    lines = [f"Store: {store_name(req.store)}", f"Item: {req.item_name[:200]}",
             f"Price paid: ${req.price_paid:.2f}", f"Price now: ${req.current_price:.2f}",
             f"Difference: ${diff:.2f}"]
    if nice_date(req.purchase_date):
        lines.append(f"Bought on: {nice_date(req.purchase_date)}")
    if req.order_number:
        lines.append(f"Order number: {req.order_number[:64]}")
    if req.product_url:
        lines.append(f"Product page: {req.product_url}")
    if nice_date(req.deadline):
        lines.append(f"Deadline: {nice_date(req.deadline)}")
    return lines


def template(req: ClaimRequest, action: str) -> str:
    """Fixed message from the same facts, for when asi1 can't write one."""
    bought = f" on {nice_date(req.purchase_date)}" if nice_date(req.purchase_date) else ""
    order = f" (order {req.order_number[:64]})" if req.order_number else ""
    deadline = f" I'm reaching out before {nice_date(req.deadline)}." if nice_date(req.deadline) else ""
    return (f"Hi, I bought {req.item_name[:200]}{bought}{order} for ${req.price_paid:.2f}. "
            f"It's now ${req.current_price:.2f}, ${req.price_paid - req.current_price:.2f} less. "
            f"Could I get {ASK[action]}?{deadline} Thank you.")


def clean(text: str) -> str:
    """asi1's answer as a pasteable message, or "" if it isn't usable."""
    text = text.strip().strip("`").strip()
    text = re.sub(r"^(subject|message):.*?\n+", "", text, flags=re.I)  # a stray subject line
    text = text.strip('"\n ')
    if not text or re.search(r"\[[^\]]*\]", text) or len(text) > MESSAGE_LIMIT:
        return ""  # empty, a [placeholder], or rambling
    return text


async def write_message(req: ClaimRequest, action: str) -> str:
    """One asi1 call; returns the message, or "" if the answer wasn't usable."""
    key = os.getenv("ASI_ONE_API_KEY", "")
    if not key:
        raise ClaimError("ASI_ONE_API_KEY is not set")
    llm = AsyncOpenAI(api_key=key, base_url=ASI_API)
    prompt = PROMPT.format(ask=ASK[action], facts="\n".join(facts(req)))
    try:
        r = await llm.chat.completions.create(
            model=MODEL, temperature=0.3, messages=[{"role": "user", "content": prompt}])
    except APIError as e:
        raise ClaimError(f"ASI:One error: {type(e).__name__}") from e
    finally:
        await llm.close()
    return clean(r.choices[0].message.content or "")


async def draft(req: ClaimRequest, log=print) -> ClaimDraft:
    base = dict(item_id=req.item_id, user_id=req.user_id)
    if req.price_paid - req.current_price <= 0:
        return ClaimDraft(**base, ok=False, error="the price hasn't dropped")
    action = pick_action(req)
    try:
        message = await write_message(req, action)
        if not message:
            log(f"{req.item_id}: asi1 answer unusable, using the template")
    except ClaimError as e:
        log(f"{req.item_id}: {e}, using the template")
        message = ""
    return ClaimDraft(**base, ok=True, action=action, message=message or template(req, action),
                      where=DEFAULT_WHERE.get(req.store, DEFAULT_WHERE["other"]))


_jobs: set[asyncio.Task] = set()
_slots = asyncio.Semaphore(MAX_JOBS)


def background(coro):
    async def run():
        async with _slots:
            await coro
    task = asyncio.get_running_loop().create_task(run())
    _jobs.add(task)  # keep a reference so the task isn't garbage-collected mid-call
    task.add_done_callback(_jobs.discard)


@agent.on_message(model=ClaimRequest, replies=ClaimDraft)
async def on_claim_request(ctx: Context, sender: str, req: ClaimRequest):
    background(answer_claim(ctx, sender, req))


async def answer_claim(ctx: Context, sender: str, req: ClaimRequest):
    start = time.monotonic()
    try:
        result = await draft(req, ctx.logger.info)
    except Exception as e:  # never leave the coordinator waiting
        ctx.logger.exception(f"claim crashed for {req.item_id}")
        result = ClaimDraft(item_id=req.item_id, user_id=req.user_id, ok=False,
                            error=f"unexpected error: {type(e).__name__}")
    outcome = result.action if result.ok else f"failed ({result.error})"
    ctx.logger.info(f"{req.item_id} [{req.store}] {outcome} in {time.monotonic() - start:.1f}s")
    await ctx.send(sender, result)


async def check_once(store: str, name: str, paid: str, now: str, action: str = "", deadline: str = ""):
    """Manual test: draft one message. Uses ASI:One only (template without a key)."""
    req = ClaimRequest(item_id="check", user_id="check", store=store, item_name=name,
                       price_paid=float(paid), current_price=float(now), action=action, deadline=deadline)
    result = await draft(req)
    if not result.ok:
        print(f"failed: {result.error}")
        return
    print(f"Action: {result.action}\nWhere: {result.where}\n\n{result.message}")


if __name__ == "__main__":
    if len(sys.argv) > 5 and sys.argv[1] == "--check":
        # uv run claim_agent.py --check <amazon|walmart|other> "<item name>" <paid> <now> [action] [deadline]
        asyncio.run(check_once(*sys.argv[2:8]))
    else:
        agent.run()
