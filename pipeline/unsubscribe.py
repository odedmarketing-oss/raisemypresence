"""
unsubscribe.py
Raise My Presence — Shared HMAC unsubscribe-link helpers.

Single source of truth for unsubscribe-link signing + verification. Both the
sender side (report_generator builds the footer link; emailer sets the
List-Unsubscribe headers) and the webhook side (webhook_server verifies the
token) import from here, so the sign side and verify side can never drift.

The secret lives in the Tencent .env as UNSUBSCRIBE_HMAC_SECRET and is read
via config.py. A request whose token does not match is treated as forged.
"""

import hashlib
import hmac
from urllib.parse import quote

from config import UNSUBSCRIBE_HMAC_SECRET

# Production unsubscribe endpoint (public host → nginx → 127.0.0.1:8099 webhook).
# Hardcoded to the domain to match the existing footer link exactly; not derived
# from WEBHOOK_BASE_URL (whose default is the raw IP).
UNSUB_ENDPOINT = "https://webhooks.raisemypresence.com/webhook/unsubscribe"


def unsub_token(email: str) -> str:
    """HMAC-SHA256 token authenticating an unsubscribe link for `email`."""
    return hmac.new(
        UNSUBSCRIBE_HMAC_SECRET.encode(),
        email.strip().lower().encode(),
        hashlib.sha256,
    ).hexdigest()


def verify_unsub_token(email: str, token: str) -> bool:
    """Constant-time check that `token` is the valid HMAC for `email`."""
    if not token or not UNSUBSCRIBE_HMAC_SECRET:
        return False
    return hmac.compare_digest(unsub_token(email), token)


def unsubscribe_url(email: str) -> str:
    """Signed one-click unsubscribe URL for `email`."""
    e = email.strip().lower()
    return f"{UNSUB_ENDPOINT}?email={quote(e)}&t={unsub_token(e)}"
