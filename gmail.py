"""Gmail connect (OAuth): sign the link, talk to Google, read recent order emails.

Import on connect, keep nothing: the access token is short-lived (no refresh token),
used once by server.py and revoked.
"""
import base64
import hashlib
import hmac
import json
import os
import time
from urllib.parse import urlencode

import httpx

SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"
STATE_TTL_S = 15 * 60
MAX_EMAILS = 10
# Order emails from the last month. Shipping updates repeat the order; Amazon's emails
# no longer list items, so each would only cost an asi1 call.
QUERY = ('newer_than:30d (category:purchases OR subject:(order OR receipt)) '
         '-subject:(shipped OR delivered OR "out for delivery" OR refund OR return) -from:amazon.com')


class GmailError(Exception):
    """A short, user-safe reason the import failed."""


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(payload: str) -> str:
    secret = os.getenv("OAUTH_STATE_SECRET", "")
    return _b64(hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest())


def configured() -> bool:
    return all(os.getenv(k) for k in ("OAUTH_STATE_SECRET", "PUBLIC_URL"))


def sign_state(user_id: str) -> str:
    """The `state` in the Connect Gmail link: whose chat the receipts go to, signed and timed."""
    payload = _b64(json.dumps({"u": user_id, "t": int(time.time())}).encode())
    return f"{payload}.{_sign(payload)}"


def check_state(state: str) -> str | None:
    """The user id in a state we signed in the last 15 minutes, else None."""
    if not os.getenv("OAUTH_STATE_SECRET") or state.count(".") != 1:
        return None
    payload, sig = state.split(".")
    if not hmac.compare_digest(sig, _sign(payload)):
        return None
    try:
        data = json.loads(_unb64(payload))
    except ValueError:
        return None
    if time.time() - data.get("t", 0) > STATE_TTL_S:
        return None
    return data.get("u") or None


def connect_link(user_id: str) -> str:
    return f"{os.getenv('PUBLIC_URL', '').rstrip('/')}/oauth/start?state={sign_state(user_id)}"


def redirect_uri() -> str:
    return f"{os.getenv('PUBLIC_URL', '').rstrip('/')}/oauth/callback"


def auth_url(state: str) -> str:
    return AUTH_URL + "?" + urlencode({
        "client_id": os.getenv("GOOGLE_CLIENT_ID", ""), "redirect_uri": redirect_uri(),
        "response_type": "code", "scope": SCOPE, "access_type": "online",
        "include_granted_scopes": "true", "state": state})


def _ok(r: httpx.Response, what: str) -> dict:
    if r.status_code >= 400:
        raise GmailError(f"Google {what} failed ({r.status_code})")
    return r.json()


def exchange(client: httpx.Client, code: str) -> str:
    data = _ok(client.post(TOKEN_URL, data={
        "code": code, "client_id": os.getenv("GOOGLE_CLIENT_ID", ""),
        "client_secret": os.getenv("GOOGLE_CLIENT_SECRET", ""),
        "redirect_uri": redirect_uri(), "grant_type": "authorization_code"}), "sign-in")
    if not data.get("access_token"):
        raise GmailError("Google sign-in returned no token")
    return data["access_token"]


def profile_email(client: httpx.Client, token: str) -> str:
    data = _ok(client.get(f"{GMAIL}/profile", headers={"Authorization": f"Bearer {token}"}), "profile")
    return (data.get("emailAddress") or "").lower()


def _bodies(part: dict, out: dict):
    """Collect the first text/plain and text/html bodies from a Gmail payload tree."""
    mime, data = part.get("mimeType", ""), (part.get("body") or {}).get("data")
    if data and mime in ("text/plain", "text/html") and mime not in out:
        out[mime] = _unb64(data).decode("utf-8", errors="replace")
    for child in part.get("parts") or []:
        _bodies(child, out)


def order_emails(client: httpx.Client, token: str) -> list[dict]:
    """Up to MAX_EMAILS recent order emails as {message_id, from, to, subject, date, text, html}."""
    auth = {"Authorization": f"Bearer {token}"}
    listing = _ok(client.get(f"{GMAIL}/messages", headers=auth,
                             params={"q": QUERY, "maxResults": MAX_EMAILS}), "search")
    emails = []
    for ref in listing.get("messages") or []:
        msg = _ok(client.get(f"{GMAIL}/messages/{ref['id']}", headers=auth,
                             params={"format": "full"}), "read")
        payload = msg.get("payload") or {}
        headers = {h["name"].lower(): h["value"] for h in payload.get("headers") or []}
        bodies: dict = {}
        _bodies(payload, bodies)
        emails.append({"message_id": f"gm_{msg['id']}", "from": headers.get("from", ""),
                       "to": headers.get("to", ""), "subject": headers.get("subject", ""),
                       "date": headers.get("date", ""), "text": bodies.get("text/plain", ""),
                       "html": bodies.get("text/html", "")})
    return emails


def revoke(client: httpx.Client, token: str):
    try:
        client.post(REVOKE_URL, data={"token": token})
    except httpx.HTTPError:
        pass  # best effort: the token expires within the hour anyway
