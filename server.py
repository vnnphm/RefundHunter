"""Email receipts in: Pipedream POSTs forwarded order emails, or a user connects Gmail
(OAuth, read once, nothing kept). Either way each email goes to the coordinator as a Receipt.

Run: uv run uvicorn server:app --port 8000
"""
import hmac
import html
import logging
import os
import re
import sys
from email.utils import parseaddr
from urllib.parse import parse_qs, urlparse

import uagents_core.logger

# uagents_core opens ./uagents_core.log on import; Vercel's filesystem is read-only,
# so log to stdout only. Must run before any other uagents import.
uagents_core.logger.get_logger.__defaults__ = (None, logging.INFO, None)  # name, level, log_file

from dotenv import load_dotenv  # noqa: E402
import httpx  # noqa: E402
from fastapi import FastAPI, Header, HTTPException  # noqa: E402
from fastapi.responses import HTMLResponse, RedirectResponse  # noqa: E402
from pydantic import BaseModel, ConfigDict, Field  # noqa: E402
from uagents_core.identity import Identity  # noqa: E402
from uagents_core.types import DeliveryStatus  # noqa: E402
from uagents_core.utils.messages import send_message_to_agent  # noqa: E402

import gmail  # noqa: E402
from models import Receipt  # noqa: E402

load_dotenv()

SEED = os.getenv("SERVER_SEED", "")
if not SEED or SEED.startswith("change-me"):
    sys.exit("Set your own SERVER_SEED in .env (see README). Same seed = same address as someone else.")
identity = Identity.from_seed(SEED, 0)

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
COORDINATOR_ADDRESS = os.getenv("COORDINATOR_ADDRESS", "").strip()
MAX_BODY = 8000
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
INBOX_DOMAIN = "@upload.pipedream.net"
PRODUCT_RE = re.compile(r"https?://(?:www\.)?(?:amazon\.com/(?:[^\s\"?#]*/)?(?:dp|gp/product)/[A-Z0-9]{10}"
                        r"|walmart\.com/ip/[^\s\"?#]+)")

log = logging.getLogger("server")
logging.basicConfig(level=logging.INFO)
app = FastAPI()


class Email(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    message_id: str
    sender: str = Field("", alias="from")  # "Name <a@b.com>" or "a@b.com"
    forwarded_for: str = ""  # Gmail filter forwards: X-Forwarded-For "user@gmail.com inbox@…"
    to: str = ""  # an auto-forward keeps the original To (the user); a hand forward has our inbox
    subject: str = ""
    date: str = ""
    text: str = ""
    html: str = ""


def user_address(email: Email) -> str:
    """Whose receipt this is. A filter auto-forward keeps the store as `from`, so look at
    X-Forwarded-For, then the original To, before falling back to `from` (a hand forward)."""
    if fwd := EMAIL_RE.search(email.forwarded_for):
        return fwd.group(0).lower()
    to = [a.lower() for a in EMAIL_RE.findall(email.to)]
    if to and not any(a.endswith(INBOX_DOMAIN) for a in to):  # not sent to our inbox: auto-forwarded
        return to[0]
    return parseaddr(email.sender)[1].lower()


def product_link(href: str) -> str:
    """The product page behind a link, unwrapping tracking redirects (Amazon's gp/r.html?U=…)."""
    href = html.unescape(href)
    query = parse_qs(urlparse(href).query)
    for key in ("U", "u", "url"):
        if query.get(key):
            href = query[key][0]
            break
    m = PRODUCT_RE.search(href)
    return m.group(0) if m else ""


def plain_text(email: Email) -> str:
    if not email.html.strip():
        body = email.text
    else:  # stores put item names, prices and product links in the HTML; the text part is often thin
        body = re.sub(r"(?is)<(script|style).*?</\1>", " ", email.html)
        body = re.sub(r'(?i)<a\s[^>]*href="([^"]+)"[^>]*>', lambda m: f" {product_link(m.group(1))} ", body)
        # Stores cut long item names short in the text; the full name is often the picture's alt text.
        body = re.sub(r'(?i)<img\s[^>]*alt="([^"]{12,})"[^>]*>', r" [\1] ", body)
        body = re.sub(r"(?i)<(br|/p|/div|/tr|/li|/h\d)\b[^>]*>", "\n", body)
        body = html.unescape(re.sub(r"<[^>]+>", " ", body))
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n\n", body)).strip()[:MAX_BODY]


@app.get("/health")
def health():
    return {"ok": True, "address": identity.address}


def send_receipt(email: Email, sender: str, user_id: str = "") -> bool:
    """Pass one email to the coordinator. False if it couldn't be delivered."""
    receipt = Receipt(message_id=email.message_id, user_id=user_id, sender=sender,
                      subject=email.subject[:300], date=email.date, body=plain_text(email))
    # Never log bodies or full addresses: message id and domain only.
    log.info(f"email {email.message_id[:40]} from @{sender.split('@')[-1]} -> coordinator")
    statuses = send_message_to_agent(COORDINATOR_ADDRESS, receipt, identity)
    if statuses and all(s.status == DeliveryStatus.FAILED for s in statuses):
        log.warning(f"email {email.message_id[:40]} not delivered: {statuses[0].detail}")
        return False
    return True


@app.post("/email")
def receive_email(email: Email, x_webhook_secret: str = Header(default="")):
    if not WEBHOOK_SECRET or not hmac.compare_digest(x_webhook_secret, WEBHOOK_SECRET):
        raise HTTPException(status_code=401, detail="bad secret")
    if not COORDINATOR_ADDRESS:
        raise HTTPException(status_code=503, detail="COORDINATOR_ADDRESS is not set")
    from_addr = user_address(email)
    if "@" not in from_addr:
        raise HTTPException(status_code=422, detail="no sender address")
    if not send_receipt(email, from_addr):
        raise HTTPException(status_code=502, detail="couldn't reach the coordinator")  # Pipedream retries
    return {"ok": True}


def page(title: str, text: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(status_code=status, content=(
        "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
        f"<title>Refund Hunter</title><body style='font-family:system-ui;max-width:32rem;margin:4rem auto;"
        f"padding:0 1rem'><h2>{html.escape(title)}</h2><p>{html.escape(text)}</p></body>"))


EXPIRED = "This link expired or isn't valid. Ask Refund Hunter to connect Gmail again for a new one."


@app.get("/oauth/start")
def oauth_start(state: str = ""):
    if not (gmail.configured() and os.getenv("GOOGLE_CLIENT_ID") and COORDINATOR_ADDRESS):
        return page("Not set up yet", "Gmail connect isn't configured on this server.", 503)
    if gmail.check_state(state) is None:
        return page("Link expired", EXPIRED, 400)
    return RedirectResponse(gmail.auth_url(state))


@app.get("/oauth/callback")
def oauth_callback(state: str = "", code: str = "", error: str = ""):
    user_id = gmail.check_state(state)
    if user_id is None:
        return page("Link expired", EXPIRED, 400)
    if error or not code:
        return page("Not connected", "Gmail wasn't connected. You can try again from ASI:One.")
    token = ""
    try:
        with httpx.Client(timeout=20) as client:
            try:
                token = gmail.exchange(client, code)
                address = gmail.profile_email(client, token)
                emails = gmail.order_emails(client, token)
            finally:
                if token:
                    gmail.revoke(client, token)
    except (gmail.GmailError, httpx.HTTPError) as e:
        reason = str(e) if isinstance(e, gmail.GmailError) else "couldn't reach Google"
        log.warning(f"gmail import failed: {reason}")
        return page("Something went wrong", f"{reason[:1].upper()}{reason[1:]}. Please try again.", 502)
    sent = sum(send_receipt(Email(**e), address, user_id) for e in emails)
    log.info(f"gmail import @{address.split('@')[-1]}: {len(emails)} order email(s), {sent} delivered")
    if not emails:
        return page("No orders found", "No order emails in the last 30 days (Amazon's are skipped: "
                    "they no longer list items, so upload a screenshot of the order instead).")
    if not sent:
        return page("Something went wrong", "Couldn't reach Refund Hunter. Please try again in a minute.", 502)
    return page(f"Found {len(emails)} order email{'s' if len(emails) != 1 else ''}",
                "Head back to ASI:One and send \"@refundhunter any new receipts?\" to review them. "
                "Nothing from your inbox was stored.")
