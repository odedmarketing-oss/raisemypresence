"""
email_verifier.py
Raise My Presence — Mailbox-Level Email Verification (RMP #68)

Pre-send verification via MailValid API. Called after syntax/MX validation,
suppression check, and dedup — only addresses that would actually trigger a
send are verified (protects the 100/day free quota).

Decision matrix:
  status "valid"                 → allow
  status "catch_all" / "unknown" → allow (flagged)
  status "invalid"               → skip
  status "do_not_mail"           → skip
  status "do_not_mail" + status_reason "role_based" → allow (flagged)
  is_disposable == true          → skip
  API error / timeout / quota    → allow (fail-open)

Results are cached per email in SQLite so no address is re-verified.
api_error results are NOT cached — retried on the next run.
"""

import logging
import sqlite3
from datetime import datetime, timezone

import requests

from config import DB_PATH, EMAIL_VERIFY_API_KEY, EMAIL_VERIFY_ENABLED

logger = logging.getLogger("pipeline")

_MAILVALID_URL = "https://mailvalid.io/api/v1/verify/single"
_TIMEOUT = 10  # seconds

# Verdicts that block sending
_BLOCK_VERDICTS = {"invalid", "do_not_mail", "disposable"}

_no_key_warned = False


# ---------------------------------------------------------------------------
# SQLite cache
# ---------------------------------------------------------------------------

def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS email_verification_cache (
            email       TEXT PRIMARY KEY,
            verdict     TEXT NOT NULL,
            raw_status  TEXT,
            verified_at TEXT NOT NULL
        )
    """)
    conn.commit()
    return conn


def _get_cached(email: str) -> str | None:
    """Return cached verdict for email, or None if not cached."""
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT verdict FROM email_verification_cache WHERE email = ? LIMIT 1",
            (email,)
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def _set_cached(email: str, verdict: str, raw_status: str) -> None:
    """Write verification result to cache."""
    conn = _get_conn()
    try:
        conn.execute(
            """INSERT OR REPLACE INTO email_verification_cache
               (email, verdict, raw_status, verified_at)
               VALUES (?, ?, ?, ?)""",
            (email, verdict, raw_status, datetime.now(timezone.utc).isoformat())
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def verify_email(email: str) -> tuple[bool, str]:
    """
    Mailbox-level verification via MailValid API.

    Returns (should_send, verdict_label).
    Fail-open: any API/network error defaults to (True, "api_error").
    """
    global _no_key_warned

    email = email.strip().lower()

    # Kill-switch
    if not EMAIL_VERIFY_ENABLED:
        return True, "disabled"

    # No API key configured
    if not EMAIL_VERIFY_API_KEY:
        if not _no_key_warned:
            logger.warning("EMAIL_VERIFY_API_KEY not set — skipping verification")
            _no_key_warned = True
        return True, "no_api_key"

    # Check cache first
    cached = _get_cached(email)
    if cached is not None:
        should_send = cached not in _BLOCK_VERDICTS
        return should_send, cached

    # Call MailValid API
    try:
        resp = requests.post(
            _MAILVALID_URL,
            headers={
                "X-API-Key": EMAIL_VERIFY_API_KEY,
                "Content-Type": "application/json",
            },
            json={"email": email},
            timeout=_TIMEOUT,
        )

        if resp.status_code == 429:
            logger.warning("MailValid quota exhausted (429) — fail-open")
            return True, "quota_exhausted"

        if resp.status_code != 200:
            logger.warning(f"MailValid HTTP {resp.status_code} — fail-open")
            return True, "api_error"

        data = resp.json()

    except (requests.Timeout, requests.ConnectionError) as exc:
        logger.warning(f"MailValid request failed ({type(exc).__name__}) — fail-open")
        return True, "api_error"
    except (requests.RequestException, ValueError) as exc:
        logger.warning(f"MailValid error ({type(exc).__name__}) — fail-open")
        return True, "api_error"

    # Parse response — MailValid nests fields under "result"
    result = data.get("result")
    if not isinstance(result, dict):
        logger.warning("MailValid response missing 'result' object — fail-open")
        return True, "api_error"

    raw_status = result.get("status", "")
    is_disposable = result.get("is_disposable", False)
    status_reason = result.get("status_reason", "")

    if is_disposable:
        verdict = "disposable"
    elif raw_status in ("invalid",):
        verdict = "invalid"
    elif raw_status == "do_not_mail":
        # RMP #69 (B): role addresses come back do_not_mail with
        # status_reason "role_based" — allow them; non-role do_not_mail
        # (traps/complainers) stays blocked.
        verdict = "role_based" if status_reason == "role_based" else "do_not_mail"
    elif raw_status in ("catch_all",):
        verdict = "catch_all"
    elif raw_status in ("unknown",):
        verdict = "unknown"
    elif raw_status in ("valid",):
        verdict = "valid"
    else:
        # Unrecognised status — fail-open, but cache it
        verdict = raw_status or "unknown"

    # Cache result (never cache api_error / quota_exhausted)
    _set_cached(email, verdict, raw_status)

    should_send = verdict not in _BLOCK_VERDICTS
    return should_send, verdict
