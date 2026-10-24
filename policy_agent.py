import json
import os
import sys
from datetime import date, datetime, timedelta, timezone

from dotenv import load_dotenv
from uagents import Agent, Context

from models import PolicyRequest, PolicyResult

load_dotenv()

RUN_CHECK = len(sys.argv) > 1 and sys.argv[1] == "--check"
SEED = os.getenv("POLICY_AGENT_SEED", "")
if (not SEED or SEED.startswith("change-me")) and not RUN_CHECK:
    sys.exit("Set your own POLICY_AGENT_SEED in .env (see README). Same seed = same address as someone else.")
seed_for_agent = SEED if SEED and not SEED.startswith("change-me") else "policy-check-seed"

agent = Agent(
    name="refund-hunter-policy",
    seed=seed_for_agent,  # keep fixed in normal runs: it sets the agent's address
    port=8004,
    mailbox=True,
)

POLICIES = {
    "amazon": {
        "window_days": 30,
        "source_url": "https://www.amazon.com/gp/help/customer/display.html",
        "policy_type": "demo_heuristic_price_protection_window",
    },
    "walmart": {
        "window_days": 30,
        "source_url": "https://www.walmart.com/help",
        "policy_type": "demo_heuristic_price_protection_window",
    },
}


def parse_purchase_date(value: str) -> date | None:
    text = (value or "").strip()
    if len(text) != 10:
        return None
    try:
        parsed = date.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.isoformat() == text else None


def evaluate_policy(req: PolicyRequest, today: date | None = None) -> PolicyResult:
    now = today or datetime.now(timezone.utc).date()
    policy = POLICIES.get(req.store.lower())
    if policy is None:
        return PolicyResult(
            item_id=req.item_id,
            ok=True,
            status="unsupported",
            eligible=None,
            policy_type="",
            explanation="Policy checks for this store are not implemented yet.",
            source_url="",
        )
    if req.current_price is None or req.current_price <= 0:
        return PolicyResult(
            item_id=req.item_id,
            ok=True,
            status="unknown",
            eligible=None,
            policy_type=policy["policy_type"],
            explanation="Current price is missing, so policy eligibility cannot be assessed safely.",
            source_url=policy["source_url"],
        )
    if req.price_paid <= 0:
        return PolicyResult(
            item_id=req.item_id,
            ok=False,
            status="unknown",
            eligible=None,
            policy_type=policy["policy_type"],
            explanation="Paid price is invalid.",
            source_url=policy["source_url"],
            error="invalid price_paid",
        )
    purchased = parse_purchase_date(req.purchase_date)
    if purchased is None:
        return PolicyResult(
            item_id=req.item_id,
            ok=True,
            status="unknown",
            eligible=None,
            policy_type=policy["policy_type"],
            explanation="Purchase date is missing or invalid, so eligibility is uncertain.",
            source_url=policy["source_url"],
        )
    if purchased > now:
        return PolicyResult(
            item_id=req.item_id,
            ok=True,
            status="unknown",
            eligible=None,
            policy_type=policy["policy_type"],
            explanation="Purchase date is in the future, so eligibility cannot be determined.",
            source_url=policy["source_url"],
            error="purchase_date_in_future",
        )
    drop = req.price_paid - req.current_price
    if drop <= 1:
        return PolicyResult(
            item_id=req.item_id,
            ok=True,
            status="not_eligible",
            eligible=False,
            policy_type=policy["policy_type"],
            explanation="No qualifying drop found (requires more than $1 based on this demo's rule).",
            source_url=policy["source_url"],
        )
    deadline = purchased + timedelta(days=policy["window_days"])
    if now <= deadline:
        return PolicyResult(
            item_id=req.item_id,
            ok=True,
            status="eligible",
            eligible=True,
            deadline=deadline.isoformat(),
            policy_type=policy["policy_type"],
            explanation=(
                "Likely within the demo policy window. This is a conservative heuristic, "
                "not a merchant policy promise. Verify with the store policy page."
            ),
            source_url=policy["source_url"],
        )
    return PolicyResult(
        item_id=req.item_id,
        ok=True,
        status="not_eligible",
        eligible=False,
        deadline=deadline.isoformat(),
        policy_type=policy["policy_type"],
        explanation=(
            "Past the demo policy window. This is heuristic guidance only; "
            "check the official policy page for exceptions."
        ),
        source_url=policy["source_url"],
    )


@agent.on_message(model=PolicyRequest, replies=PolicyResult)
async def on_policy(ctx: Context, sender: str, req: PolicyRequest):
    try:
        result = evaluate_policy(req)
    except Exception as e:  # never leave the coordinator waiting
        ctx.logger.exception(f"policy check crashed for {req.item_id}")
        result = PolicyResult(
            item_id=req.item_id,
            ok=False,
            status="unknown",
            eligible=None,
            explanation="Policy check failed unexpectedly.",
            error=f"unexpected error: {type(e).__name__}",
        )
    ctx.logger.info(f"{req.item_id} [{req.store}] -> {result.status}")
    await ctx.send(sender, result)


def _check_cases() -> list[tuple[str, PolicyRequest, str]]:
    return [
        ("drop within window", PolicyRequest(item_id="it_a", user_id="u", store="amazon", product_url="https://a",
                                             purchase_date="2026-10-10", price_paid=100, current_price=85), "eligible"),
        ("drop over window", PolicyRequest(item_id="it_b", user_id="u", store="amazon", product_url="https://a",
                                           purchase_date="2026-09-01", price_paid=100, current_price=85), "not_eligible"),
        ("missing purchase date", PolicyRequest(item_id="it_c", user_id="u", store="amazon", product_url="https://a",
                                                purchase_date="", price_paid=100, current_price=85), "unknown"),
        ("no drop", PolicyRequest(item_id="it_d", user_id="u", store="walmart", product_url="https://w",
                                  purchase_date="2026-10-10", price_paid=100, current_price=100), "not_eligible"),
        ("future purchase date", PolicyRequest(item_id="it_e", user_id="u", store="walmart", product_url="https://w",
                                               purchase_date="2026-11-01", price_paid=100, current_price=85), "unknown"),
        ("unsupported store", PolicyRequest(item_id="it_f", user_id="u", store="other", product_url="https://x",
                                            purchase_date="2026-10-10", price_paid=100, current_price=85), "unsupported"),
        ("missing current price", PolicyRequest(item_id="it_g", user_id="u", store="amazon", product_url="https://a",
                                                purchase_date="2026-10-10", price_paid=100, current_price=None), "unknown"),
    ]


def run_check():
    fixed_today = date(2026, 10, 24)
    rows = []
    failed = 0
    for name, req, expected in _check_cases():
        out = evaluate_policy(req, today=fixed_today)
        ok = out.status == expected
        rows.append({"case": name, "expected": expected, "actual": out.status, "pass": ok,
                     "explanation": out.explanation})
        failed += 0 if ok else 1
    print(json.dumps(rows, indent=2))
    if failed:
        sys.exit(f"{failed} policy checks failed")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--check":
        # uv run policy_agent.py --check
        run_check()
    else:
        agent.run()
