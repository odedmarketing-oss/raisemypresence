"""
webhook_server.py
Raise My Presence — Webhook Server (Bounce + Unsubscribe)

Minimal FastAPI app with two endpoints:
  1. POST /webhook/sendgrid — SendGrid Event Webhook for hard bounces
  2. GET  /webhook/unsubscribe — One-click unsubscribe (link in email footer)

Deployment: pm2-managed on Tencent server
  pm2 start webhook_server.py --name rmp-webhooks --interpreter python3 -- --port 8099

SendGrid Event Webhook setup:
  1. Go to SendGrid → Settings → Mail Settings → Event Webhook
  2. Set POST URL to: http://43.134.33.213:8099/webhook/sendgrid
  3. Select events: Bounced, Dropped
  4. Enable
"""

import html
import json
import logging
import re
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import stripe
import uvicorn
from sendgrid.helpers.eventwebhook import EventWebhook, EventWebhookHeader
from fastapi import FastAPI, Request, Query
from fastapi.responses import HTMLResponse, JSONResponse, Response

from config import (
    WEBHOOK_PORT,
    STRIPE_WEBHOOK_SECRET, STRIPE_API_KEY, KIT_PDF_DIR,
    SENDGRID_WEBHOOK_VERIFY_KEY,
)
from unsubscribe import verify_unsub_token
from suppression import add_suppression, is_suppressed
from email_events import insert_event
from purchase_log import (
    is_already_fulfilled, insert_pending, mark_fulfilled, mark_failed,
)
from pdf_personalizer import personalize_cover
from emailer import send_attachment_email, send_plain_email
from pydantic import BaseModel
from funnel_events import insert_event as insert_funnel_event
from audit_landing import get_audit_landing_data
from report_generator import SCORE_FACTORS, MAX_SCORE

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("webhooks")

app = FastAPI(title="RMP Webhooks", docs_url=None, redoc_url=None)


# ---------------------------------------------------------------------------
# Funnel tracking helpers (RMP #67)
# ---------------------------------------------------------------------------

_RMP_TOKEN_RE = re.compile(r'^[0-9a-f]{16}$')
_TRACK_ORIGIN = "https://raisemypresence.com"


def _track_cors_full() -> dict:
    """Full CORS headers for OPTIONS /track preflight response."""
    return {
        "Access-Control-Allow-Origin": _TRACK_ORIGIN,
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
    }


def _track_cors_post() -> dict:
    """Minimal CORS header for POST /track response."""
    return {"Access-Control-Allow-Origin": _TRACK_ORIGIN}


class TrackPayload(BaseModel):
    token: str
    stage: str
    ts: str = ""
    payload: dict = {}


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok", "service": "rmp-webhooks"}


# ---------------------------------------------------------------------------
# SendGrid Event Webhook
# ---------------------------------------------------------------------------
# SendGrid sends POST with JSON array of event objects.
# We care about: bounce, dropped
# Event schema: https://docs.sendgrid.com/for-developers/tracking-events/event
#
# Example event:
# {
#   "email": "bounce@example.com",
#   "event": "bounce",
#   "type": "bounce",       // "bounce" or "blocked"
#   "reason": "550 No such user",
#   "timestamp": 1713000000,
#   "sg_message_id": "...",
# }

_SUPPRESS_EVENTS = {"bounce", "dropped"}


@app.post("/webhook/sendgrid")
async def sendgrid_webhook(request: Request):
    """
    Handle SendGrid Event Webhook.
    Adds hard-bounced emails to suppression list.
    """
    raw_body = (await request.body()).decode("utf-8")

    # --- Signature verification (F-01) ---
    if SENDGRID_WEBHOOK_VERIFY_KEY:
        signature = request.headers.get(EventWebhookHeader.SIGNATURE, "")
        timestamp = request.headers.get(EventWebhookHeader.TIMESTAMP, "")
        try:
            ew = EventWebhook()
            ec_key = ew.convert_public_key_to_ecdsa(SENDGRID_WEBHOOK_VERIFY_KEY)
            if not ew.verify_signature(raw_body, signature, timestamp, ec_key):
                logger.warning("SendGrid webhook: signature verification failed")
                return JSONResponse(
                    {"status": "error", "message": "invalid signature"},
                    status_code=403,
                )
        except Exception as e:
            logger.warning(f"SendGrid webhook: signature verification error: {e}")
            return JSONResponse(
                {"status": "error", "message": "invalid signature"},
                status_code=403,
            )
    else:
        logger.warning(
            "SENDGRID_WEBHOOK_VERIFY_KEY not set — skipping signature verification"
        )

    try:
        events = json.loads(raw_body)
    except Exception as e:
        logger.error(f"Failed to parse webhook payload: {e}")
        return {"status": "error", "message": "invalid json"}

    if not isinstance(events, list):
        events = [events]

    suppressed_count = 0
    logged_count = 0

    for event in events:
        event_type = event.get("event", "")
        email = event.get("email", "")
        reason = event.get("reason", "")
        bounce_type = event.get("type", "")

        # Telemetry Part 2 (RMP #56): log EVERY event type to the append-only
        # email_events table, keyed on sg_message_id (join key to sent_log).
        # This runs ABOVE the suppression filter so engagement events
        # (delivered/open/click/spam/unsubscribe) are captured too. Wrapped so a
        # logging failure can never break suppression handling or force a non-2xx
        # (which would make SendGrid retry the whole batch).
        try:
            ts_raw = event.get("timestamp")
            if ts_raw is not None:
                event_ts = datetime.fromtimestamp(int(ts_raw), tz=timezone.utc).isoformat()
            else:
                event_ts = datetime.now(timezone.utc).isoformat()
            insert_event(
                sg_message_id=event.get("sg_message_id", ""),
                event_type=event_type or "unknown",
                timestamp=event_ts,
                payload_json=json.dumps(event),
            )
            logged_count += 1
        except Exception as e:
            logger.warning(f"email_events log failed (non-fatal): {e}")

        if not email:
            continue

        if event_type not in _SUPPRESS_EVENTS:
            continue

        # Only suppress hard bounces (not soft/transient)
        if event_type == "bounce" and bounce_type not in ("bounce", "blocked"):
            logger.debug(f"Skipping soft bounce for {email}: {bounce_type}")
            continue

        added = add_suppression(email, reason=f"hard_bounce:{event_type}")
        if added:
            suppressed_count += 1
            logger.info(f"Suppressed (bounce): {email} — {reason[:80]}")
        else:
            logger.debug(f"Already suppressed: {email}")

    return {"status": "ok", "suppressed": suppressed_count, "logged": logged_count}


# ---------------------------------------------------------------------------
# One-click unsubscribe
# ---------------------------------------------------------------------------

_UNSUB_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Unsubscribed — Raise My Presence</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            background: #F9FAFB; color: #111827;
            display: flex; justify-content: center; align-items: center;
            min-height: 100vh; padding: 20px;
        }
        .card {
            background: #FFFFFF; border-radius: 16px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.1);
            padding: 48px 40px; max-width: 480px; text-align: center;
        }
        .icon { font-size: 48px; margin-bottom: 16px; }
        h1 { font-size: 22px; font-weight: 700; margin-bottom: 12px; }
        p { font-size: 15px; color: #6B7280; line-height: 1.6; }
        .email { font-weight: 600; color: #111827; }
    </style>
</head>
<body>
    <div class="card">
        <div class="icon">✓</div>
        <h1>You've been unsubscribed</h1>
        <p>
            <span class="email">{email}</span> has been removed from our mailing list.
            You won't receive any more audit reports from Raise My Presence.
        </p>
    </div>
</body>
</html>"""

_ALREADY_UNSUB_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Already Unsubscribed — Raise My Presence</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            background: #F9FAFB; color: #111827;
            display: flex; justify-content: center; align-items: center;
            min-height: 100vh; padding: 20px;
        }
        .card {
            background: #FFFFFF; border-radius: 16px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.1);
            padding: 48px 40px; max-width: 480px; text-align: center;
        }
        .icon { font-size: 48px; margin-bottom: 16px; }
        h1 { font-size: 22px; font-weight: 700; margin-bottom: 12px; }
        p { font-size: 15px; color: #6B7280; line-height: 1.6; }
    </style>
</head>
<body>
    <div class="card">
        <div class="icon">👍</div>
        <h1>Already unsubscribed</h1>
        <p>This email address was already removed from our list. No further action needed.</p>
    </div>
</body>
</html>"""

_ERROR_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Error — Raise My Presence</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            background: #F9FAFB; color: #111827;
            display: flex; justify-content: center; align-items: center;
            min-height: 100vh; padding: 20px;
        }
        .card {
            background: #FFFFFF; border-radius: 16px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.1);
            padding: 48px 40px; max-width: 480px; text-align: center;
        }
        h1 { font-size: 22px; font-weight: 700; margin-bottom: 12px; }
        p { font-size: 15px; color: #6B7280; line-height: 1.6; }
        a { color: #16A34A; }
    </style>
</head>
<body>
    <div class="card">
        <h1>Something went wrong</h1>
        <p>To unsubscribe, please email
        <a href="mailto:hello@raisemypresence.com?subject=Unsubscribe">hello@raisemypresence.com</a>
        with the subject "Unsubscribe".</p>
    </div>
</body>
</html>"""


_CONFIRM_UNSUB_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Confirm Unsubscribe — Raise My Presence</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            background: #F9FAFB; color: #111827;
            display: flex; justify-content: center; align-items: center;
            min-height: 100vh; padding: 20px;
        }
        .card {
            background: #FFFFFF; border-radius: 16px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.1);
            padding: 48px 40px; max-width: 480px; text-align: center;
        }
        h1 { font-size: 22px; font-weight: 700; margin-bottom: 12px; }
        p { font-size: 15px; color: #6B7280; line-height: 1.6; margin-bottom: 24px; }
        .email { font-weight: 600; color: #111827; }
        button {
            font: inherit; font-weight: 600; font-size: 15px;
            color: #FFFFFF; background: #16A34A; border: none;
            border-radius: 10px; padding: 14px 28px; cursor: pointer;
        }
        button:hover { background: #15803D; }
    </style>
</head>
<body>
    <div class="card">
        <h1>Unsubscribe?</h1>
        <p>
            Click below to remove <span class="email">{email}</span> from our
            mailing list. You won't receive any more audit reports from
            Raise My Presence.
        </p>
        <form method="POST" action="{action}">
            <button type="submit">Unsubscribe</button>
        </form>
    </div>
</body>
</html>"""


@app.get("/webhook/unsubscribe")
async def unsubscribe_get(email: str = Query(default=""), t: str = Query(default="")):
    """
    Render the unsubscribe CONFIRM page. NEVER mutates state — prefetch-safe
    (mail-scanner GETs cannot unsubscribe anyone). The actual opt-out happens
    on POST to this same URL (confirm button or RFC 8058 one-click).

    `t` is the HMAC token from new sends. Legacy links without a token are
    still honored via the confirm -> POST flow (CAN-SPAM backward-compat).
    A token that is present but invalid is treated as tampering and rejected.
    """
    email = email.strip().lower()

    if not email or "@" not in email:
        return HTMLResponse(_ERROR_HTML, status_code=400)

    if t and not verify_unsub_token(email, t):
        logger.warning(f"Unsubscribe GET: invalid token for {email}")
        return HTMLResponse(_ERROR_HTML, status_code=400)

    if is_suppressed(email):
        return HTMLResponse(_ALREADY_UNSUB_HTML)

    action = f"/webhook/unsubscribe?email={quote(email)}"
    if t:
        action += f"&t={quote(t)}"
    page = _CONFIRM_UNSUB_HTML.replace("{email}", html.escape(email)).replace("{action}", action)
    return HTMLResponse(page)


@app.post("/webhook/unsubscribe")
async def unsubscribe_post(email: str = Query(default=""), t: str = Query(default="")):
    """
    Perform the unsubscribe — the ONLY mutating path. Reached two ways:
      - the confirm-page button (form POST carrying email + token in the URL)
      - RFC 8058 List-Unsubscribe-Post one-click (provider POSTs the header URL)
    Both carry email + token as query params, so we read them the same way.
    """
    email = email.strip().lower()

    if not email or "@" not in email:
        return HTMLResponse(_ERROR_HTML, status_code=400)

    if not t:
        logger.warning(f"Unsubscribe POST: missing token for {email}")
        return HTMLResponse(_ERROR_HTML, status_code=400)

    if not verify_unsub_token(email, t):
        logger.warning(f"Unsubscribe POST: invalid token for {email}")
        return HTMLResponse(_ERROR_HTML, status_code=400)

    if is_suppressed(email):
        return HTMLResponse(_ALREADY_UNSUB_HTML)

    add_suppression(email, reason="unsubscribe")
    logger.info(f"Unsubscribed: {email}")
    return HTMLResponse(_UNSUB_HTML.replace("{email}", html.escape(email)))


# ---------------------------------------------------------------------------
# Stripe Checkout Webhook (Block 4 — purchase fulfillment)
# ---------------------------------------------------------------------------
# Receives checkout.session.completed events. Fulfills kit purchases with a
# personalized PDF; sends welcome confirmation for subscription purchases.
# Idempotent on Stripe event.id per industry standard.
#
# Response contract (Decision 2):
#   400 — bad signature                 (don't retry; not really from Stripe)
#   200 — already processed, success, or known-permanent failure
#   500 — transient failure              (let Stripe retry up to 3 days)

OPERATOR_NOTIFICATION_EMAIL = "odedmarketing@gmail.com"

# Stripe lookup_key → (product, locale, kit_pdf_filename).
# Matches Stripe-Plan.md Created Products Tracker.
_LOOKUP_KEY_MAP = {
    "kit_us":     ("kit",     "US", "kit_us.pdf"),
    "kit_uk":     ("kit",     "UK", "kit_uk.pdf"),
    "kit_au":     ("kit",     "AU", "kit_au.pdf"),
    "kit_nz":     ("kit",     "NZ", "kit_nz.pdf"),
    "monthly_us": ("monthly", "US", None),
    "monthly_uk": ("monthly", "UK", None),
    "monthly_au": ("monthly", "AU", None),
    "monthly_nz": ("monthly", "NZ", None),
}


class _PermanentParseError(Exception):
    """Raised when an event cannot be processed AND a Stripe retry won't fix it."""
    pass


@app.post("/webhook/stripe")
async def stripe_webhook(request: Request):
    """Stripe Checkout webhook handler. Tri-state response per contract above."""
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    if not STRIPE_WEBHOOK_SECRET:
        logger.error("STRIPE_WEBHOOK_SECRET not configured")
        return JSONResponse({"error": "webhook_secret_missing"}, status_code=500)

    # 1. Signature verification (400 on failure — no retry)
    try:
        stripe.Webhook.construct_event(
            payload=payload,
            sig_header=sig_header,
            secret=STRIPE_WEBHOOK_SECRET,
        )
    except (ValueError, stripe.error.SignatureVerificationError) as e:
        logger.warning(f"Stripe signature verification failed: {e}")
        return JSONResponse({"error": "invalid_signature"}, status_code=400)

    # stripe-python v15 StripeObject (Pydantic-based) no longer supports dict-style
    # .get() access. Signature is already verified above; parse the raw JSON payload
    # as a plain dict so all downstream .get() access works natively.
    event = json.loads(payload)

    event_id = event.get("id", "")
    event_type = event.get("type", "")
    logger.info(f"Stripe event: {event_type} ({event_id})")

    # 2. Filter — only checkout.session.completed is handled
    if event_type != "checkout.session.completed":
        logger.debug(f"Ignoring event type {event_type}")
        return JSONResponse({"received": True, "handled": False})

    # 3. Idempotency short-circuit on already-fulfilled events
    if is_already_fulfilled(event_id):
        logger.info(f"Event {event_id} already fulfilled \u2014 short-circuit 200")
        return JSONResponse({"received": True, "already_fulfilled": True})

    # 4. Parse session (raises _PermanentParseError if unrecoverable)
    try:
        purchase = _parse_session(event)
    except _PermanentParseError as e:
        logger.error(f"Permanent parse failure on {event_id}: {e}")
        _record_permanent_parse_failure(event_id, str(e))
        return JSONResponse({"received": True, "error": str(e)}, status_code=200)

    # 5. Insert pending row (INSERT OR IGNORE handles race-with-retry)
    inserted = insert_pending(
        stripe_event_id=event_id,
        stripe_session_id=purchase["session_id"],
        email=purchase["email"],
        business_name=purchase["business_name"],
        business_city=purchase["business_city"],
        product=purchase["product"],
        locale=purchase["locale"],
        amount_cents=purchase["amount_cents"],
        currency=purchase["currency"],
        payment_method=purchase["payment_method"],
        purchased_at=purchase["purchased_at"],
        client_reference_id=purchase["client_reference_id"],
    )
    if not inserted:
        if is_already_fulfilled(event_id):
            return JSONResponse({"received": True, "already_fulfilled": True})
        logger.info(f"Event {event_id} concurrent in-flight \u2014 200")
        return JSONResponse({"received": True, "in_flight": True})

    # 6. Fulfill
    try:
        if purchase["product"] == "kit":
            send_result = _fulfill_kit(purchase)
        elif purchase["product"] == "monthly":
            send_result = _fulfill_monthly(purchase)
        else:
            raise _PermanentParseError(f"unknown product: {purchase['product']}")
    except _PermanentParseError as e:
        mark_failed(event_id, str(e), permanent=True)
        logger.error(f"Permanent fulfillment failure on {event_id}: {e}")
        return JSONResponse({"received": True, "error": str(e)}, status_code=200)
    except Exception as e:
        mark_failed(event_id, f"unhandled: {e}", permanent=False)
        logger.exception(f"Transient fulfillment failure on {event_id}")
        return JSONResponse({"received": False, "error": str(e)}, status_code=500)

    # 7. Tri-state response based on SendGrid result
    if send_result["success"]:
        mark_fulfilled(event_id)
        _notify_operator(purchase, send_result)
        logger.info(
            f"Fulfilled {purchase['product']}_{purchase['locale']} for "
            f"{purchase['email']} (event {event_id})"
        )
        return JSONResponse({"received": True, "fulfilled": True})

    status_code = send_result.get("status_code") or 0
    is_permanent = 400 <= status_code < 500
    mark_failed(event_id, send_result.get("error", ""), permanent=is_permanent)

    if is_permanent:
        logger.error(f"Permanent send failure on {event_id}: {send_result.get('error')}")
        return JSONResponse(
            {"received": True, "error": send_result.get("error")},
            status_code=200,
        )
    logger.warning(
        f"Transient send failure on {event_id} \u2014 Stripe will retry: "
        f"{send_result.get('error')}"
    )
    return JSONResponse(
        {"received": False, "error": send_result.get("error")},
        status_code=500,
    )


# ---------------------------------------------------------------------------
# Stripe webhook helpers
# ---------------------------------------------------------------------------


def _stripe_obj_to_dict(obj) -> dict:
    """
    Convert a stripe-python StripeObject to a plain dict supporting .get().
    stripe-python v15+ uses Pydantic models (model_dump); older versions used
    to_dict_recursive / to_dict. Try in order; fall back to JSON-string parse.
    """
    if hasattr(obj, "model_dump"):           # stripe-python v15+ (Pydantic)
        return obj.model_dump()
    if hasattr(obj, "to_dict_recursive"):    # older stripe-python
        return obj.to_dict_recursive()
    if hasattr(obj, "to_dict"):              # very old stripe-python
        return obj.to_dict()
    return json.loads(str(obj))              # last-resort fallback


def _compute_order_reference(session_id: str) -> str:
    """Deterministic order reference for receipts.

    Format: RMP-YYYY-<last 8 chars of session_id, uppercased>.
    Called from both /api/session/{id} (thank-you page) and
    _build_kit_email_html (kit delivery email) so page and email
    show identical references without coordination via DB write.

    UTC year keeps reference stable across server timezones.
    """
    return f"RMP-{datetime.now(timezone.utc).year}-{session_id[-8:].upper()}"


def _parse_session(event: dict) -> dict:
    """
    Extract fulfillment fields from a checkout.session.completed event.
    Raises _PermanentParseError on unrecoverable data issues.
    """
    session = event.get("data", {}).get("object", {})
    if not session or session.get("object") != "checkout.session":
        raise _PermanentParseError("event.data.object is not a checkout.session")

    session_id = session.get("id", "")
    client_reference_id = (session.get("client_reference_id") or "").strip()
    customer_details = session.get("customer_details") or {}
    collected_info = session.get("collected_information") or {}
    address = customer_details.get("address") or {}

    email = (
        customer_details.get("email") or session.get("customer_email") or ""
    ).strip().lower()
    if not email or "@" not in email:
        raise _PermanentParseError("no valid email on session")

    business_name = (collected_info.get("business_name") or "").strip()
    if not business_name:
        # Fallback chain if name_collection wasn't enabled or buyer left blank.
        business_name = (
            collected_info.get("individual_name")
            or customer_details.get("name")
            or "Your Business"
        ).strip()
        logger.warning(
            f"Session {session_id}: business_name missing; fell back to {business_name!r}. "
            "Verify Payment Link name_collection config."
        )

    business_city = (address.get("city") or "").strip()

    # Resolve product + locale via line_items.lookup_key (requires API call)
    if not STRIPE_API_KEY:
        raise _PermanentParseError(
            "STRIPE_API_KEY not configured \u2014 cannot expand line_items"
        )
    stripe.api_key = STRIPE_API_KEY
    try:
        full_session_obj = stripe.checkout.Session.retrieve(
            session_id, expand=["line_items.data.price"]
        )
    except Exception as e:
        raise _PermanentParseError(f"failed to retrieve session line_items: {e}")

    # Convert StripeObject to plain dict for .get()-based access below.
    full_session = _stripe_obj_to_dict(full_session_obj)

    line_items = (full_session.get("line_items") or {}).get("data") or []
    if not line_items:
        raise _PermanentParseError("session has no line_items")

    price = line_items[0].get("price") or {}
    lookup_key = price.get("lookup_key") or ""
    if lookup_key not in _LOOKUP_KEY_MAP:
        raise _PermanentParseError(f"unknown lookup_key {lookup_key!r}")

    product, locale, kit_pdf_filename = _LOOKUP_KEY_MAP[lookup_key]

    amount_cents = int(session.get("amount_total") or 0)
    currency = (session.get("currency") or "").upper()
    payment_method_types = session.get("payment_method_types") or []
    payment_method = payment_method_types[0] if payment_method_types else ""

    created_unix = session.get("created")
    if created_unix:
        purchased_at = datetime.fromtimestamp(created_unix, tz=timezone.utc).isoformat()
    else:
        purchased_at = datetime.now(timezone.utc).isoformat()

    return {
        "session_id": session_id,
        "client_reference_id": client_reference_id,
        "email": email,
        "business_name": business_name,
        "business_city": business_city,
        "product": product,
        "locale": locale,
        "kit_pdf_filename": kit_pdf_filename,
        "amount_cents": amount_cents,
        "currency": currency,
        "payment_method": payment_method,
        "purchased_at": purchased_at,
    }


def _fulfill_kit(purchase: dict) -> dict:
    """Personalize the locale-keyed kit PDF and deliver via SendGrid."""
    source_pdf = KIT_PDF_DIR / purchase["kit_pdf_filename"]
    if not source_pdf.exists():
        raise _PermanentParseError(f"master kit PDF missing: {source_pdf}")

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        output_pdf = Path(tmp.name)

    try:
        personalize_cover(
            source_pdf=source_pdf,
            output_pdf=output_pdf,
            business_name=purchase["business_name"],
            business_city=purchase["business_city"] or "Your City",
            issue_date=datetime.now(timezone.utc),
        )
        subject = f"Your Raise My Presence Kit is ready, {purchase['business_name']}"
        html_body = _build_kit_email_html(purchase)
        recipient_filename = f"raise-my-presence-kit-{purchase['locale'].lower()}.pdf"

        return send_attachment_email(
            recipient_email=purchase["email"],
            subject=subject,
            html_body=html_body,
            attachment_path=output_pdf,
            attachment_filename=recipient_filename,
            category=f"kit-fulfillment-{purchase['locale'].lower()}",
            extra_headers={
                "X-RMP-Business": purchase["business_name"][:64],
                "X-RMP-Locale": purchase["locale"],
                "X-RMP-Session": purchase["session_id"][:64],
            },
        )
    finally:
        try:
            output_pdf.unlink(missing_ok=True)
        except Exception:
            pass


def _fulfill_monthly(purchase: dict) -> dict:
    """Subscription welcome email (v1 confirmation; full sequence post-launch)."""
    subject = f"Welcome to Raise My Presence Management, {purchase['business_name']}"
    html_body = _build_monthly_welcome_html(purchase)
    return send_plain_email(
        recipient_email=purchase["email"],
        subject=subject,
        html_body=html_body,
        category=f"monthly-welcome-{purchase['locale'].lower()}",
    )


def _build_kit_email_html(purchase: dict) -> str:
    order_ref = _compute_order_reference(purchase["session_id"])
    return (
        '<!DOCTYPE html>'
        '<html><head><meta charset="UTF-8"></head>'
        '<body style="font-family: -apple-system, BlinkMacSystemFont, \'Segoe UI\', sans-serif; color: #111827; max-width: 560px; margin: 0 auto; padding: 24px;">'
        f'<p style="font-size: 16px; line-height: 1.6;">Hi {html.escape(purchase["business_name"])},</p>'
        '<p style="font-size: 16px; line-height: 1.6;">Your Raise My Presence Kit is attached \u2014 personalized for your business. It walks you through nine milestones to maximize your Google Maps visibility, with time budgets, scorecards, and a 45-minute Fast Track if you\'re short on time.</p>'
        '<p style="font-size: 16px; line-height: 1.6;">Start with the Fast Track on page 3. Hit every milestone, and you\'ll have the strongest local Google presence in your category.</p>'
        '<p style="font-size: 16px; line-height: 1.6;">If you\'d rather we run this for you each month, page 24 has the details.</p>'
        '<p style="font-size: 16px; line-height: 1.6;">\u2014 The Raise My Presence team</p>'
        f'<p style="font-size: 11px; color: #9CA3AF; margin-top: 24px; letter-spacing: 0.04em;">Order reference: {order_ref}</p>'
        '<p style="font-size: 12px; color: #6B7280; margin-top: 8px;">Questions: <a href="mailto:hello@raisemypresence.com" style="color: #16A34A;">hello@raisemypresence.com</a></p>'
        '</body></html>'
    )


def _build_monthly_welcome_html(purchase: dict) -> str:
    return (
        '<!DOCTYPE html>'
        '<html><head><meta charset="UTF-8"></head>'
        '<body style="font-family: -apple-system, BlinkMacSystemFont, \'Segoe UI\', sans-serif; color: #111827; max-width: 560px; margin: 0 auto; padding: 24px;">'
        f'<p style="font-size: 16px; line-height: 1.6;">Hi {html.escape(purchase["business_name"])},</p>'
        '<p style="font-size: 16px; line-height: 1.6;">Welcome to Raise My Presence Management. Your subscription is active.</p>'
        '<p style="font-size: 16px; line-height: 1.6;">Here\'s what happens next: we\'ll audit your Google Business Profile within 2 business days, then begin the monthly optimization cycle \u2014 reviews, posts, photos, citation maintenance, performance monitoring. No calls, no meetings, no reports. You\'ll see the results in your profile.</p>'
        '<p style="font-size: 16px; line-height: 1.6;">Manage your subscription anytime at <a href="https://billing.stripe.com/p/login/dRmeV51MU4gE6ur7SWdAk00" style="color: #16A34A;">billing.stripe.com</a>.</p>'
        '<p style="font-size: 16px; line-height: 1.6;">\u2014 The Raise My Presence team</p>'
        '<p style="font-size: 12px; color: #6B7280; margin-top: 32px;">Questions: <a href="mailto:hello@raisemypresence.com" style="color: #16A34A;">hello@raisemypresence.com</a></p>'
        '</body></html>'
    )


def _notify_operator(purchase: dict, send_result: dict) -> None:
    """Best-effort operator notification. Errors logged, never propagated."""
    try:
        amount_display = f"{purchase['amount_cents'] / 100:.2f} {purchase['currency']}"
        subject = (
            f"New {purchase['product']} purchase \u2014 {purchase['business_name']} "
            f"({purchase['locale']}, {amount_display})"
        )
        body = (
            '<!DOCTYPE html><html><body style="font-family: monospace; font-size: 13px;">'
            '<pre>'
            f"Product:        {purchase['product']}\n"
            f"Locale:         {purchase['locale']}\n"
            f"Amount:         {amount_display}\n"
            f"Business name:  {html.escape(purchase['business_name'])}\n"
            f"Business city:  {html.escape(purchase['business_city'])}\n"
            f"Customer email: {purchase['email']}\n"
            f"Session ID:     {purchase['session_id']}\n"
            f"Delivered to:   {send_result.get('recipient')}\n"
            f"SendGrid HTTP:  {send_result.get('status_code')}\n"
            '</pre></body></html>'
        )
        send_plain_email(
            recipient_email=OPERATOR_NOTIFICATION_EMAIL,
            subject=subject,
            html_body=body,
            category="operator-purchase-notification",
        )
    except Exception as e:
        logger.warning(f"Operator notification failed (non-fatal): {e}")


def _record_permanent_parse_failure(event_id: str, reason: str) -> None:
    """Insert a permanent-failed row so retries of bad events short-circuit."""
    try:
        insert_pending(
            stripe_event_id=event_id,
            stripe_session_id="",
            email="",
            business_name="",
            business_city="",
            product="unknown",
            locale="unknown",
            amount_cents=0,
            currency="",
            payment_method="",
            purchased_at=datetime.now(timezone.utc).isoformat(),
        )
        mark_failed(event_id, f"parse: {reason}", permanent=True)
    except Exception as e:
        logger.warning(f"Could not record parse-failure row for {event_id}: {e}")


# ---------------------------------------------------------------------------
# Block 3 — thank-you.html session lookup API
# ---------------------------------------------------------------------------
# Frontend (raisemypresence.com/thank-you) fetches session details for
# personalization rendering. session_id is in the Stripe Checkout redirect URL.
# Backend uses STRIPE_API_KEY (secret, server-side) to retrieve sanitized
# session data with CORS header for the Vercel-hosted origin.
#
# Tri-state response:
#   200 — success                  (sanitized JSON with personalization fields)
#   400 — invalid session_id format
#   404 — session not found
#   410 — session older than 30 min  (prevents enumeration replay)
#   502 — Stripe API retrieve failure

_THANKYOU_ALLOWED_ORIGIN = "https://raisemypresence.com"
_SESSION_AGE_CAP_SECONDS = 30 * 60  # 30 minutes


def _thankyou_cors() -> dict:
    return {"Access-Control-Allow-Origin": _THANKYOU_ALLOWED_ORIGIN}


@app.get("/api/session/{session_id}")
async def get_session_for_thankyou(session_id: str):
    """Return sanitized session data for thank-you page personalization."""
    if not session_id.startswith("cs_") or len(session_id) < 20:
        return JSONResponse(
            {"error": "invalid_session_id"},
            status_code=400,
            headers=_thankyou_cors(),
        )

    if not STRIPE_API_KEY:
        logger.error("STRIPE_API_KEY not configured for /api/session")
        return JSONResponse(
            {"error": "server_misconfigured"},
            status_code=500,
            headers=_thankyou_cors(),
        )

    stripe.api_key = STRIPE_API_KEY

    try:
        full_session_obj = stripe.checkout.Session.retrieve(
            session_id, expand=["line_items.data.price"]
        )
    except stripe.error.InvalidRequestError:
        return JSONResponse(
            {"error": "not_found"},
            status_code=404,
            headers=_thankyou_cors(),
        )
    except Exception as e:
        logger.warning(f"/api/session retrieve failed for {session_id}: {e}")
        return JSONResponse(
            {"error": "retrieve_failed"},
            status_code=502,
            headers=_thankyou_cors(),
        )

    session = _stripe_obj_to_dict(full_session_obj)

    # Age cap — only return data for sessions <30 min old.
    created_unix = session.get("created", 0)
    if created_unix and (time.time() - created_unix) > _SESSION_AGE_CAP_SECONDS:
        return JSONResponse(
            {"error": "expired"},
            status_code=410,
            headers=_thankyou_cors(),
        )

    customer_details = session.get("customer_details") or {}
    collected_info = session.get("collected_information") or {}
    address = customer_details.get("address") or {}

    email = (
        customer_details.get("email") or session.get("customer_email") or ""
    ).strip().lower()
    business_name = (collected_info.get("business_name") or "").strip()
    business_city = (address.get("city") or "").strip()

    # Derive tier + locale from lookup_key (option b — _LOOKUP_KEY_MAP is canonical).
    line_items = (session.get("line_items") or {}).get("data") or []
    tier = ""
    locale = ""
    if line_items:
        lookup_key = (line_items[0].get("price") or {}).get("lookup_key") or ""
        mapping = _LOOKUP_KEY_MAP.get(lookup_key)
        if mapping:
            tier, locale, _ = mapping

    return JSONResponse(
        {
            "email": email,
            "business_name": business_name,
            "business_city": business_city,
            "order_reference": _compute_order_reference(session_id),
            "tier": tier,
            "locale": locale,
        },
        headers=_thankyou_cors(),
    )


# ---------------------------------------------------------------------------
# Funnel tracking — /track (RMP #67)
# ---------------------------------------------------------------------------

@app.options("/track")
async def track_preflight():
    return Response(status_code=204, headers=_track_cors_full())


@app.post("/track")
async def track_funnel(body: TrackPayload):
    # Silently discard tokens that don't match secrets.token_hex(8) format
    if not _RMP_TOKEN_RE.match(body.token):
        return Response(status_code=204, headers=_track_cors_post())

    # B3 fix (RMP #82): reject tokens not minted by the pipeline
    if not get_audit_landing_data(body.token):
        return Response(status_code=204, headers=_track_cors_post())

    # Validate caller-supplied ts; fall back to UTC now if missing/unparseable
    ts = body.ts
    try:
        if not ts:
            raise ValueError
        datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        ts = datetime.now(timezone.utc).isoformat()

    try:
        insert_funnel_event(
            rmp_token=body.token,
            stage=body.stage,
            ts=ts,
            payload_json=json.dumps(body.payload),
        )
    except Exception as e:
        logger.warning("funnel_events insert failed (non-fatal): %s", e)

    return Response(status_code=204, headers=_track_cors_post())


# ---------------------------------------------------------------------------
# Audit landing page — /api/audit/{rmp_token} (T-019)
# ---------------------------------------------------------------------------

_AUDIT_CORS_ORIGIN = "https://raisemypresence.com"


def _audit_cors() -> dict:
    return {"Access-Control-Allow-Origin": _AUDIT_CORS_ORIGIN}


def _audit_cors_full() -> dict:
    return {
        "Access-Control-Allow-Origin": _AUDIT_CORS_ORIGIN,
        "Access-Control-Allow-Methods": "GET, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
    }


def _outcome_reframe(score: int, top_issues: list) -> str:
    """Generate a score-tiered outcome sentence referencing top issue areas."""
    areas = " and ".join(i["label"].lower() for i in top_issues[:2])
    if score < 40:
        return (
            f"Your listing is missing critical elements in {areas} "
            f"that drive the majority of local search rankings. "
            f"Fixing these could put you in front of customers already searching for your services."
        )
    if score < 60:
        return (
            f"Your profile has gaps in {areas} that are "
            f"limiting your visibility to local customers. "
            f"A few targeted improvements could significantly increase your reach."
        )
    return (
        f"Your profile is on the right track but has room to grow in {areas}. "
        f"Closing these gaps could help you stand out from nearby competitors."
    )


@app.options("/api/audit/{rmp_token}")
async def audit_preflight(rmp_token: str):
    return Response(status_code=204, headers=_audit_cors_full())


@app.get("/api/audit/{rmp_token}")
async def get_audit_for_landing(rmp_token: str):
    """Return personalized audit data for the landing-page hero."""
    if not _RMP_TOKEN_RE.match(rmp_token):
        return JSONResponse(
            {"error": "invalid_token"},
            status_code=400,
            headers=_audit_cors(),
        )

    data = get_audit_landing_data(rmp_token)
    if not data:
        return JSONResponse(
            {"error": "not_found"},
            status_code=404,
            headers=_audit_cors(),
        )

    # Compute top 3 issues from score_breakdown
    breakdown = json.loads(data["score_breakdown_json"])
    issues = []
    for key, label, maximum in SCORE_FACTORS:
        earned = min(breakdown.get(key, 0), maximum)
        gap = maximum - earned
        if gap > 0:
            if earned == 0:
                status = "Missing"
            elif (earned / maximum) < 0.5:
                status = "Low"
            else:
                status = "Incomplete"
            issues.append({
                "key": key, "label": label,
                "earned": earned, "max": maximum,
                "status": status, "gap": gap,
            })
    issues.sort(key=lambda x: x["gap"], reverse=True)
    top_issues = [
        {"key": i["key"], "label": i["label"], "earned": i["earned"],
         "max": i["max"], "status": i["status"]}
        for i in issues[:3]
    ]

    display_score = data["score"]
    outcome = _outcome_reframe(display_score, top_issues)

    return JSONResponse(
        {
            "business_name": data["business_name"],
            "score": display_score,
            "top_issues": top_issues,
            "outcome": outcome,
            "locale": data["locale"],
        },
        headers=_audit_cors(),
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    uvicorn.run(
        "webhook_server:app",
        host="127.0.0.1",
        port=WEBHOOK_PORT,
        log_level="info",
    )
