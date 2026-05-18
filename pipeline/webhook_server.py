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

import logging
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import stripe
import uvicorn
from fastapi import FastAPI, Request, Query
from fastapi.responses import HTMLResponse, JSONResponse

from config import (
    WEBHOOK_PORT,
    STRIPE_WEBHOOK_SECRET, STRIPE_API_KEY, KIT_PDF_DIR,
)
from suppression import add_suppression, is_suppressed
from purchase_log import (
    is_already_fulfilled, insert_pending, mark_fulfilled, mark_failed,
)
from pdf_personalizer import personalize_cover
from emailer import send_attachment_email, send_plain_email

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("webhooks")

app = FastAPI(title="RMP Webhooks", docs_url=None, redoc_url=None)


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
    try:
        events = await request.json()
    except Exception as e:
        logger.error(f"Failed to parse webhook payload: {e}")
        return {"status": "error", "message": "invalid json"}

    if not isinstance(events, list):
        events = [events]

    suppressed_count = 0

    for event in events:
        event_type = event.get("event", "")
        email = event.get("email", "")
        reason = event.get("reason", "")
        bounce_type = event.get("type", "")

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

    return {"status": "ok", "suppressed": suppressed_count}


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


@app.get("/webhook/unsubscribe")
async def unsubscribe(email: str = Query(default="")):
    """
    One-click unsubscribe endpoint.
    URL format: /webhook/unsubscribe?email=user@example.com
    """
    email = email.strip().lower()

    if not email or "@" not in email:
        return HTMLResponse(_ERROR_HTML, status_code=400)

    if is_suppressed(email):
        logger.debug(f"Unsubscribe: already suppressed — {email}")
        return HTMLResponse(_ALREADY_UNSUB_HTML)

    add_suppression(email, reason="unsubscribe")
    logger.info(f"Unsubscribed: {email}")
    return HTMLResponse(_UNSUB_HTML.replace("{email}", email))


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
        event = stripe.Webhook.construct_event(
            payload=payload,
            sig_header=sig_header,
            secret=STRIPE_WEBHOOK_SECRET,
        )
    except (ValueError, stripe.error.SignatureVerificationError) as e:
        logger.warning(f"Stripe signature verification failed: {e}")
        return JSONResponse({"error": "invalid_signature"}, status_code=400)

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


def _parse_session(event: dict) -> dict:
    """
    Extract fulfillment fields from a checkout.session.completed event.
    Raises _PermanentParseError on unrecoverable data issues.
    """
    session = event.get("data", {}).get("object", {})
    if not session or session.get("object") != "checkout.session":
        raise _PermanentParseError("event.data.object is not a checkout.session")

    session_id = session.get("id", "")
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
        full_session = stripe.checkout.Session.retrieve(
            session_id, expand=["line_items.data.price"]
        )
    except Exception as e:
        raise _PermanentParseError(f"failed to retrieve session line_items: {e}")

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
    return (
        '<!DOCTYPE html>'
        '<html><head><meta charset="UTF-8"></head>'
        '<body style="font-family: -apple-system, BlinkMacSystemFont, \'Segoe UI\', sans-serif; color: #111827; max-width: 560px; margin: 0 auto; padding: 24px;">'
        f'<p style="font-size: 16px; line-height: 1.6;">Hi {purchase["business_name"]},</p>'
        '<p style="font-size: 16px; line-height: 1.6;">Your Raise My Presence Kit is attached \u2014 personalized for your business. It walks you through nine milestones to maximize your Google Maps visibility, with time budgets, scorecards, and a 45-minute Fast Track if you\'re short on time.</p>'
        '<p style="font-size: 16px; line-height: 1.6;">Start with the Fast Track on page 3. Hit every milestone, and you\'ll have the strongest local Google presence in your category.</p>'
        '<p style="font-size: 16px; line-height: 1.6;">If you\'d rather we run this for you each month, page 24 has the details.</p>'
        '<p style="font-size: 16px; line-height: 1.6;">\u2014 The Raise My Presence team</p>'
        '<p style="font-size: 12px; color: #6B7280; margin-top: 32px;">Questions: <a href="mailto:hello@raisemypresence.com" style="color: #16A34A;">hello@raisemypresence.com</a></p>'
        '</body></html>'
    )


def _build_monthly_welcome_html(purchase: dict) -> str:
    return (
        '<!DOCTYPE html>'
        '<html><head><meta charset="UTF-8"></head>'
        '<body style="font-family: -apple-system, BlinkMacSystemFont, \'Segoe UI\', sans-serif; color: #111827; max-width: 560px; margin: 0 auto; padding: 24px;">'
        f'<p style="font-size: 16px; line-height: 1.6;">Hi {purchase["business_name"]},</p>'
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
            f"Business name:  {purchase['business_name']}\n"
            f"Business city:  {purchase['business_city']}\n"
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
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    uvicorn.run(
        "webhook_server:app",
        host="0.0.0.0",
        port=WEBHOOK_PORT,
        log_level="info",
    )
