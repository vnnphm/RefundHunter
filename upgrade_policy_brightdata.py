"""Run AFTER apply_refundhunter_pr10_fix.py, on copilot/add-policy-agent.
Adds real Bright Data MCP scraping and safe evidence-based policy decisions.
"""
from pathlib import Path
import subprocess
import shutil

def sub_once(text, old, new):
    if text.count(old) != 1:
        raise RuntimeError(f'Expected one anchor, found {text.count(old)}: {old[:75]}')
    return text.replace(old, new, 1)

def main():
    branch = subprocess.check_output(['git','branch','--show-current'], text=True).strip()
    if branch != 'copilot/add-policy-agent':
        raise SystemExit(f'Refusing to modify branch {branch!r}')
    shutil.copyfile(Path(__file__).with_name('policy_evidence.py'), Path('policy_evidence.py'))
    p=Path('policy_agent.py'); s=p.read_text()
    s=sub_once(s, 'from models import PolicyRequest, PolicyResult',
        'from models import PolicyRequest, PolicyResult\nfrom policy_evidence import POLICY_URLS, classify_policy')
    start=s.index('def evaluate_policy(')
    end=s.index('@agent.on_message(model=PolicyRequest', start)
    s=s[:start]+'''async def scrape_official_policy(store: str) -> tuple[str, str]:
    """Scrape a allowlisted official merchant policy page via Bright Data MCP.

    Fail closed. Never pass an untrusted user URL to the policy scraper.
    """
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    token = os.getenv("BRIGHTDATA_API_TOKEN", "").strip()
    if not token:
        raise RuntimeError("BRIGHTDATA_API_TOKEN is missing")
    url = POLICY_URLS[store]
    endpoint = f"https://mcp.brightdata.com/mcp?token={token}&tools=scrape_as_markdown"
    async with streamable_http_client(endpoint) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(
                "scrape_as_markdown", {"url": url}, read_timeout_seconds=120)
    if result.is_error:
        raise RuntimeError("Bright Data policy scraper returned an error")
    text = "\\n".join(getattr(c, "text", "") for c in result.content)
    if len(text.strip()) < 80:
        raise RuntimeError("Insufficient policy page content")
    return url, text[:80000]


async def evaluate_policy(req: PolicyRequest) -> PolicyResult:
    """Check actual scraped evidence; never substitute a made-up 30-day policy."""
    store = (req.store or "").lower()
    fields = dict(item_id=req.item_id, request_id=req.request_id)
    if store not in POLICY_URLS:
        return PolicyResult(**fields, ok=True, status="unsupported", eligible=None,
                            explanation="Merchant policy integration is not available.")
    if not req.purchase_date or parse_purchase_date(req.purchase_date) is None:
        return PolicyResult(**fields, ok=True, status="unknown", eligible=None,
                            explanation="Purchase date is missing or invalid.")
    if parse_purchase_date(req.purchase_date) > datetime.now(timezone.utc).date():
        return PolicyResult(**fields, ok=True, status="unknown", eligible=None,
                            explanation="Purchase date is in the future.")
    if req.current_price is None or req.price_paid <= 0 or req.current_price <= 0:
        return PolicyResult(**fields, ok=True, status="unknown", eligible=None,
                            explanation="Valid purchase and current prices are required.")
    if req.price_paid - req.current_price <= 1:
        return PolicyResult(**fields, ok=True, status="not_eligible", eligible=False,
                            explanation="No price drop greater than $1 was detected (demo trigger threshold).")
    try:
        url, content = await scrape_official_policy(store)
        evidence = classify_policy(store, content, url)
        return PolicyResult(**fields, ok=True, status=evidence["status"],
                            eligible=evidence["eligible"], source_url=url,
                            policy_type="live_brightdata_policy_evidence",
                            explanation=evidence["explanation"],
                            evidence=evidence["evidence"], checked_at=evidence["checked_at"])
    except Exception as exc:
        # Do not log URL/token; avoid leaking credentials through an exception message.
        return PolicyResult(**fields, ok=True, status="unknown", eligible=None,
                            policy_type="live_brightdata_policy_evidence",
                            explanation="Official merchant policy could not be retrieved. Eligibility is unverified.",
                            error=type(exc).__name__)


''' + s[end:]
    s=sub_once(s, 'result = evaluate_policy(req)', 'result = await evaluate_policy(req)')
    # Legacy hardcoded check cases are obsolete; explicit unit tests live separately.
    a=s.index('def _check_cases()'); b=s.index('if __name__ == "__main__":',a)
    s=s[:a]+'''def run_check():
    from policy_evidence import classify_policy
    example = ('We do not offer any price matching for items previously '
               'purchased on Walmart.com that have since decreased in price')
    result = classify_policy('walmart', example,
        'https://corporate.walmart.com/askwalmart/does-walmart-price-match')
    assert result['status'] == 'not_eligible', result
    print('Offline policy evidence smoke check passed (no live scrape).')


'''+s[b:]
    compile(s,str(p),'exec');p.write_text(s)
    p=Path('models.py');s=p.read_text()
    s=sub_once(s, '    source_url: str = ""       # where to verify policy manually',
        '    source_url: str = ""       # where to verify policy manually\n    evidence: str = ""\n    checked_at: str = ""')
    compile(s,str(p),'exec');p.write_text(s)
    p=Path('agent.py');s=p.read_text()
    s=sub_once(s, '    if result.source_url:\n        rows.append({"label": "Verify policy", "value": result.source_url})',
      '    if result.source_url:\n        rows.append({"label": "Verify policy", "value": result.source_url})\n    if result.checked_at:\n        rows.append({"label": "Checked UTC", "value": result.checked_at[:19]})\n    if result.evidence:\n        rows.append({"label": "Official evidence", "value": result.evidence[:350]})')
    compile(s,str(p),'exec');p.write_text(s)
    p=Path('pyproject.toml');s=p.read_text()
    if '"mcp==' not in s: s=s.replace('dependencies = [', 'dependencies = [\n    "mcp==2.3.0",')
    p.write_text(s)
    print('Policy Bright Data integration patched. Run offline unit tests and live smoke tests separately.')
if __name__=='__main__':main()
