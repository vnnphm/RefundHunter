"""Apply reviewed RefundHunter PR #10 fixes on copilot/add-policy-agent.
Run at repository root: python apply_refundhunter_pr10_fix.py
Idempotent; refuses to run on main/master. No network access required.
"""
from pathlib import Path
import subprocess


def replace_once(s, old, new, label):
    if new in s:
        return s
    if s.count(old) != 1:
        raise RuntimeError(f'{label}: expected exactly one match, found {s.count(old)}')
    return s.replace(old, new, 1)


def patch():
    branch = subprocess.check_output(['git', 'branch', '--show-current'], text=True).strip()
    if branch != 'copilot/add-policy-agent':
        raise RuntimeError(f'Refusing to edit branch {branch!r}; switch to copilot/add-policy-agent first')
    edits = {}
    p = Path('models.py'); s = p.read_text()
    s = replace_once(s, 'class PolicyRequest(Model):    # coordinator -> policy agent (Phase 3)\n    item_id: str', 'class PolicyRequest(Model):    # coordinator -> policy agent (Phase 3)\n    item_id: str\n    request_id: str = ""', 'model request')
    s = replace_once(s, 'class PolicyResult(Model):     # policy agent -> coordinator\n    item_id: str', 'class PolicyResult(Model):     # policy agent -> coordinator\n    item_id: str\n    request_id: str = ""', 'model result')
    edits[p] = s

    p = Path('store.py'); s = p.read_text()
    s = replace_once(s, "    purchase_date TEXT NOT NULL DEFAULT ''\n);", "    purchase_date TEXT NOT NULL DEFAULT '',\n    policy_request_id TEXT NOT NULL DEFAULT ''\n);", 'table')
    s = replace_once(s, '    if "purchase_date" not in cols:\n        conn.execute("ALTER TABLE items ADD COLUMN purchase_date TEXT NOT NULL DEFAULT \'\'")', '    if "purchase_date" not in cols:\n        conn.execute("ALTER TABLE items ADD COLUMN purchase_date TEXT NOT NULL DEFAULT \'\'")\n    if "policy_request_id" not in cols:\n        conn.execute("ALTER TABLE items ADD COLUMN policy_request_id TEXT NOT NULL DEFAULT \'\'")', 'migration')
    s += '''\n\ndef set_policy_request(item_id: str, request_id: str):\n    with connect() as conn:\n        conn.execute("UPDATE items SET policy_request_id = ? WHERE item_id = ?", (request_id, item_id))\n\n\ndef is_current_policy_request(item_id: str, request_id: str) -> bool:\n    item = get_item(item_id)\n    return bool(item and request_id and item["policy_request_id"] == request_id)\n''' if 'def set_policy_request(' not in s else ''
    edits[p] = s

    p = Path('agent.py'); s = p.read_text()
    s = replace_once(s, 'from datetime import datetime, timezone', 'from datetime import date, datetime, timezone', 'date import')
    s = replace_once(s, 'def add_item_form(item_id: str, product_url: str = "", price_paid: float | None = None,\n                  name: str = "") -> dict:', 'def add_item_form(item_id: str, product_url: str = "", price_paid: float | None = None,\n                  name: str = "", purchase_date: str = "") -> dict:', 'form signature')
    s = replace_once(s, '    if not name:\n        fields.append({"name": "name", "kind": "text", "label": "Item name (optional)"})', '    if not name:\n        fields.append({"name": "name", "kind": "text", "label": "Item name (optional)"})\n    if not purchase_date:\n        fields.append({"name": "purchase_date", "kind": "text",\n                       "label": "Purchase date YYYY-MM-DD (optional)"})', 'form field')
    s = replace_once(s, '    if name:\n        selection["name"] = name\n    return {', '    if name:\n        selection["name"] = name\n    if purchase_date:\n        selection["purchase_date"] = purchase_date\n    return {', 'form carry')
    s = replace_once(s, 'def parse_price(value) -> float | None:', '''def valid_purchase_date(value: str) -> bool:\n    if not value:\n        return True\n    try:\n        return date.fromisoformat(value).isoformat() == value and value <= date.today().isoformat()\n    except (TypeError, ValueError):\n        return False\n\n\ndef parse_price(value) -> float | None:''', 'date validator')
    s = replace_once(s, '    req = PolicyRequest(\n        item_id=item["item_id"],', '    req = PolicyRequest(\n        request_id=uuid4().hex,\n        item_id=item["item_id"],', 'request ID')
    s = replace_once(s, '    if POLICY_AGENT_ADDRESS:\n        await ctx.send(POLICY_AGENT_ADDRESS, req)', '    store.set_policy_request(item["item_id"], req.request_id)\n    if POLICY_AGENT_ADDRESS:\n        await ctx.send(POLICY_AGENT_ADDRESS, req)', 'request saved')
    s = replace_once(s, '        item_id=req.item_id,\n        ok=True,\n        status="unknown",', '        item_id=req.item_id,\n        request_id=req.request_id,\n        ok=True,\n        status="unknown",', 'mock id')
    s = replace_once(s, '    user = item["user_id"]\n    if not result.ok:\n        await ctx.send(user, reply("I couldn\'t assess policy eligibility right now. Please try again later."))', '    user = item["user_id"]\n    if not store.is_current_policy_request(result.item_id, result.request_id):\n        ctx.logger.info(f"ignored stale policy reply for {result.item_id}")\n        return\n    if not result.ok:\n        await ctx.send(user, reply("I couldn\'t assess policy eligibility right now. Please try again later."))', 'reply guard')
    s = replace_once(s, '        await track_new_item(ctx, sender, url, price, str(sel.get("name") or "").strip(), item_id)', '        purchase_date = str(sel.get("purchase_date") or "").strip()\n        if not valid_purchase_date(purchase_date):\n            await ctx.send(sender, reply("Purchase date must be a valid, non-future YYYY-MM-DD date."))\n            return\n        await track_new_item(ctx, sender, url, price, str(sel.get("name") or "").strip(),\n                             item_id, purchase_date=purchase_date)', 'submit date')
    s = replace_once(s, 'add_item_form(new_item_id(), "", it["price_paid"], it["name"])', 'add_item_form(new_item_id(), "", it["price_paid"], it["name"],\n                                                          rec.get("purchase_date") or "")', 'receipt date')
    s = replace_once(s, '    if m := EMAIL_RE.search(text):', '    if m := re.search(r"\\b\\d{4}-\\d{2}-\\d{2}\\b", text):\n        sel["purchase_date"] = m.group(0)\n    if m := EMAIL_RE.search(text):', 'prose date')
    edits[p] = s

    p = Path('policy_agent.py'); s = p.read_text()
    # Policy eligibility must not be asserted on a date heuristic alone.
    s = replace_once(s, '    if now <= deadline:\n        return PolicyResult(\n            item_id=req.item_id,\n            ok=True,\n            status="eligible",\n            eligible=True,', '    if now <= deadline:\n        return PolicyResult(\n            item_id=req.item_id,\n            ok=True,\n            status="unknown",\n            eligible=None,', 'unsafe eligible')
    s = replace_once(s, '"Likely within the demo policy window. This is a conservative heuristic, "\n                "not a merchant policy promise. Verify with the store policy page."', '"A price drop was found within a 30-day DEMO review window. "\n                "This does NOT establish price-adjustment or refund eligibility. "\n                "Confirm the merchant policy and order conditions yourself."', 'review description')
    s = replace_once(s, '"Past the demo policy window. This is heuristic guidance only; "\n            "check the official policy page for exceptions."', '"Outside the 30-day DEMO review window; the store may have other remedies. "\n            "This does NOT establish refund ineligibility. Check actual merchant terms."', 'expired description')
    s = replace_once(s, '        status="not_eligible",\n        eligible=False,\n        deadline=deadline.isoformat(),', '        status="unknown",\n        eligible=None,\n        deadline=deadline.isoformat(),', 'expired status')
    s = replace_once(s, '"drop within window", PolicyRequest(', '"drop within window", PolicyRequest(', 'check anchor')
    s = replace_once(s, 'price_paid=100, current_price=85), "eligible"', 'price_paid=100, current_price=85), "unknown"', 'test expectation recent')
    s = replace_once(s, 'price_paid=100, current_price=85), "not_eligible"', 'price_paid=100, current_price=85), "unknown"', 'test expectation expired')
    s = replace_once(s, '    ctx.logger.info(f"{req.item_id} [{req.store}] -> {result.status}")', '    result.request_id = req.request_id\n    ctx.logger.info(f"{req.item_id} [{req.store}] -> {result.status}")', 'response ID')
    edits[p] = s

    for p, content in edits.items():
        compile(content, str(p), 'exec')
    for p, content in edits.items():
        p.write_text(content)
        print(f'patched {p}')
    print('PATCH COMPLETE. Run python test_refundhunter_pr10_fix.py, review git diff, then commit/push.')

if __name__ == '__main__':
    patch()
