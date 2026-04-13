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
from datetime import datetime, timezone

import uvicorn
from fastapi import FastAPI, Request, Query
from fastapi.responses import HTMLResponse

from config import WEBHOOK_PORT
from suppression import add_suppression, is_suppressed

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
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    uvicorn.run(
        "webhook_server:app",
        host="0.0.0.0",
        port=WEBHOOK_PORT,
        log_level="info",
    )
