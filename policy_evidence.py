"""Conservative policy evidence handling for RefundHunter.
Never assert customer eligibility just from a generic page or time window.
"""
from datetime import datetime, timezone
from urllib.parse import urlparse
import re

POLICY_URLS = {
    'walmart': 'https://corporate.walmart.com/askwalmart/does-walmart-price-match',
    'amazon': 'https://www.amazon.com/gp/help/customer/display.html',
}

def trusted_source(store: str, url: str) -> bool:
    host = (urlparse(url).hostname or '').lower()
    domains = {'walmart': ('walmart.com',), 'amazon': ('amazon.com',)}
    return any(host == domain or host.endswith('.' + domain) for domain in domains.get(store.lower(), ())) and urlparse(url).scheme == 'https'

def classify_policy(store: str, markdown: str, source_url: str) -> dict:
    """Classify *policy evidence*, not overall refund/return eligibility.

    Output unknown whenever textual evidence is ambiguous, unavailable or mismatched.
    Walmart explicit denial applies only to Walmart.com after-purchase price matching.
    """
    output = {'status': 'unknown', 'eligible': None, 'evidence': '', 'explanation':
        'The scraped page does not provide sufficient, product-specific evidence to establish a price adjustment.',
        'source_url': source_url, 'checked_at': datetime.now(timezone.utc).isoformat()}
    if not trusted_source(store, source_url) or not markdown.strip():
        output['explanation'] = 'Official policy evidence could not be verified.'
        return output
    compact = re.sub(r'\s+', ' ', markdown)
    # Match explicit policy sentence rather than generic promotional material.
    if store.lower() == 'walmart':
        denial = re.search(r'we do not offer any price matching.{0,450}?items previously purchased on Walmart\.com that have since decreased in price', compact, re.I)
        if denial:
            output.update(status='not_eligible', eligible=False,
                evidence=denial.group(0)[:480],
                explanation='Walmart explicitly excludes post-purchase Walmart.com price matching. This does not decide separate return/refund rights.')
    return output
