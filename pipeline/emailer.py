"""
emailer.py
Raise My Presence — SendGrid Email Sender

Sends branded HTML audit reports via SendGrid.
Handles:
  - DRY_RUN mode (redirects all mail to operator inbox)
  - From address on sending subdomain (mail.raisemypresence.com)
  - Error capture with structured return
  - Subject line generation from business data
"""

import logging

from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import (
    Mail, From, To, Subject, HtmlContent,
    Header, Category,
)

from config import (
    SENDGRID_API_KEY, FROM_EMAIL, FROM_NAME,
    DRY_RUN, DRY_RUN_RECIPIENT,
)

logger = logging.getLogger(__name__)


def _build_subject(business_name: str, score: int) -> str:
    """Generate email subject line from business data."""
    return f"Your Google profile scored {score}/100 — here's what to fix"


def send_report(
    recipient_email: str,
    html_body: str,
    business_name: str,
    score: int,
    dry_run: bool | None = None,
) -> dict:
    """
    Send an HTML audit report via SendGrid.

    Args:
        recipient_email: Target email address.
        html_body: Complete HTML string from report_generator.
        business_name: For subject line and logging.
        score: Audit score for subject line.
        dry_run: Override config DRY_RUN if set. None = use config.

    Returns:
        dict with keys:
            success: bool
            status_code: int or None
            recipient: str (actual recipient after dry_run redirect)
            error: str or None
    """
    use_dry_run = dry_run if dry_run is not None else DRY_RUN
    actual_recipient = DRY_RUN_RECIPIENT if use_dry_run else recipient_email
    subject = _build_subject(business_name, score)

    if not SENDGRID_API_KEY:
        msg = "SENDGRID_API_KEY not set"
        logger.error(msg)
        return {
            "success": False,
            "status_code": None,
            "recipient": actual_recipient,
            "error": msg,
        }

    try:
        message = Mail()
        message.from_email = From(FROM_EMAIL, FROM_NAME)
        message.to = To(actual_recipient)
        message.subject = Subject(subject)
        message.content = HtmlContent(html_body)

        # Tag for SendGrid analytics
        message.category = Category("audit-report")

        # Custom header for tracking in webhook events
        message.header = Header("X-RMP-Business", business_name[:64])

        if use_dry_run:
            # Add original recipient info so operator can see who it would have gone to
            message.header = Header("X-RMP-Original-Recipient", recipient_email)
            message.header = Header("X-RMP-Dry-Run", "true")

        sg = SendGridAPIClient(SENDGRID_API_KEY)
        response = sg.send(message)

        status = response.status_code
        success = 200 <= status < 300

        if success:
            mode = "DRY-RUN" if use_dry_run else "LIVE"
            logger.info(
                f"[{mode}] Sent to {actual_recipient} "
                f"(business: {business_name}, score: {score}) — {status}"
            )
        else:
            logger.warning(
                f"SendGrid returned {status} for {actual_recipient} "
                f"(business: {business_name})"
            )

        return {
            "success": success,
            "status_code": status,
            "recipient": actual_recipient,
            "error": None if success else f"HTTP {status}",
        }

    except Exception as e:
        logger.error(f"SendGrid error for {actual_recipient}: {e}")
        return {
            "success": False,
            "status_code": None,
            "recipient": actual_recipient,
            "error": str(e),
        }
