"""Email webhook: Pipedream POSTs forwarded order emails here; we pass them to the coordinator.

Run: uv run uvicorn server:app --port 8000
"""
import hmac
import html
import logging
import os
import re
import sys
from email.utils import parseaddr

import uagents_core.logger

# uagents_core opens ./uagents_core.log on import; Vercel's filesystem is read-only,
# so log to stdout only. Must run before any other uagents import.
uagents_core.logger.get_logger.__defaults__ = (None, logging.INFO, None)  # name, level, log_file

from dotenv import load_dotenv  # noqa: E402
from fastapi import FastAPI, Header, HTTPException  # noqa: E402
from pydantic import BaseModel, ConfigDict, Field  # noqa: E402
from uagents_core.identity import Identity  # noqa: E402
from uagents_core.utils.messages import send_message_to_agent  # noqa: E402

from models import Receipt  # noqa: E402

load_dotenv()

SEED = os.getenv("SERVER_SEED", "")
if not SEED or SEED.startswith("change-me"):
    sys.exit("Set your own SERVER_SEED in .env (see README). Same seed = same address as someone else.")
identity = Identity.from_seed(SEED, 0)

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
COORDINATOR_ADDRESS = os.getenv("COORDINATOR_ADDRESS", "").strip()
MAX_BODY = 8000

log = logging.getLogger("server")
logging.basicConfig(level=logging.INFO)
app = FastAPI()


class Email(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    message_id: str
    sender: str = Field("", alias="from")  # "Name <a@b.com>" or "a@b.com"
    subject: str = ""
    date: str = ""
    text: str = ""
    html: str = ""


def plain_text(email: Email) -> str:
    if email.text.strip():
        body = email.text
    else:  # HTML-only email: drop tags, keep link targets so the parser can find product URLs
        body = re.sub(r"(?is)<(script|style).*?</\1>", " ", email.html)
        body = re.sub(r'(?i)<a\s[^>]*href="([^"]+)"[^>]*>', r" \1 ", body)
        body = html.unescape(re.sub(r"<[^>]+>", " ", body))
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n\n", body)).strip()[:MAX_BODY]


@app.get("/health")
def health():
    return {"ok": True, "address": identity.address}


@app.post("/email")
def receive_email(email: Email, x_webhook_secret: str = Header(default="")):
    if not WEBHOOK_SECRET or not hmac.compare_digest(x_webhook_secret, WEBHOOK_SECRET):
        raise HTTPException(status_code=401, detail="bad secret")
    if not COORDINATOR_ADDRESS:
        raise HTTPException(status_code=503, detail="COORDINATOR_ADDRESS is not set")
    from_addr = parseaddr(email.sender)[1].lower()
    if "@" not in from_addr:
        raise HTTPException(status_code=422, detail="no sender address")
    receipt = Receipt(message_id=email.message_id, user_id="", sender=from_addr,
                      subject=email.subject[:300], date=email.date, body=plain_text(email))
    # Never log bodies or full addresses: message id and domain only.
    log.info(f"email {email.message_id[:40]} from @{from_addr.split('@')[-1]} -> coordinator")
    send_message_to_agent(COORDINATOR_ADDRESS, receipt, identity)
    return {"ok": True}
