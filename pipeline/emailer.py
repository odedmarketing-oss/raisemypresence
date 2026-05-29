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
import base64
from pathlib import Path

from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import (
    Mail, From, To, Subject, HtmlContent,
    Header, Category,
    Attachment, FileContent, FileName, FileType, Disposition,
)

from config import (
    SENDGRID_API_KEY, FROM_EMAIL, FROM_NAME,
    DRY_RUN, DRY_RUN_RECIPIENT,
)
from unsubscribe import unsubscribe_url

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
    subject: str | None = None,
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
    subject = subject or _build_subject(business_name, score)

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

        # RFC 8058 one-click unsubscribe + List-Unsubscribe (prefetch-safe, deliverability)
        unsub_link = unsubscribe_url(recipient_email)
        message.header = Header(
            "List-Unsubscribe",
            f"<{unsub_link}>, <mailto:hello@raisemypresence.com?subject=Unsubscribe>",
        )
        message.header = Header("List-Unsubscribe-Post", "List-Unsubscribe=One-Click")

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


# ---------------------------------------------------------------------------
# Generic attachment email sender (Block 4 — kit fulfillment)
# ---------------------------------------------------------------------------
# Separate from send_report() to keep concerns isolated:
#   send_report          — cold-outreach audit reports (no attachment)
#   send_attachment_email — transactional purchase delivery (PDF attachment)
# Both honor the same DRY_RUN redirect contract.


def send_attachment_email(
    recipient_email: str,
    subject: str,
    html_body: str,
    attachment_path: Path,
    attachment_filename: str,
    attachment_mime: str = "application/pdf",
    category: str = "kit-fulfillment",
    extra_headers: dict | None = None,
    dry_run: bool | None = None,
) -> dict:
    """
    Send an HTML email with a single file attachment via SendGrid.

    Args:
        recipient_email:     target email address (will be DRY_RUN-redirected
                             if dry_run is True or DRY_RUN env var is set).
        subject:             email subject line.
        html_body:           email body as HTML string.
        attachment_path:     path to the file to attach.
        attachment_filename: filename the recipient will see (e.g.
                             'raise-my-presence-kit.pdf').
        attachment_mime:     MIME type, default 'application/pdf'.
        category:            SendGrid category tag for analytics.
        extra_headers:       dict of custom X-* headers to attach.
        dry_run:             override config DRY_RUN if set; None = use config.

    Returns:
        dict with keys: success (bool), status_code (int|None),
                         recipient (str, actual after DRY_RUN redirect),
                         error (str|None).
    """
    use_dry_run = dry_run if dry_run is not None else DRY_RUN
    actual_recipient = DRY_RUN_RECIPIENT if use_dry_run else recipient_email

    if not SENDGRID_API_KEY:
        msg = "SENDGRID_API_KEY not set"
        logger.error(msg)
        return {
            "success": False,
            "status_code": None,
            "recipient": actual_recipient,
            "error": msg,
        }

    attachment_path = Path(attachment_path)
    if not attachment_path.exists():
        msg = f"Attachment not found: {attachment_path}"
        logger.error(msg)
        return {
            "success": False,
            "status_code": None,
            "recipient": actual_recipient,
            "error": msg,
        }

    try:
        # Read + base64-encode attachment
        with open(attachment_path, "rb") as f:
            encoded = base64.b64encode(f.read()).decode()

        attachment = Attachment(
            FileContent(encoded),
            FileName(attachment_filename),
            FileType(attachment_mime),
            Disposition("attachment"),
        )

        message = Mail()
        message.from_email = From(FROM_EMAIL, FROM_NAME)
        message.to = To(actual_recipient)
        message.subject = Subject(subject)
        message.content = HtmlContent(html_body)
        message.attachment = attachment
        message.category = Category(category)

        # Custom tracking headers
        if extra_headers:
            for key, val in extra_headers.items():
                message.header = Header(key, str(val)[:128])

        if use_dry_run:
            message.header = Header("X-RMP-Original-Recipient", recipient_email)
            message.header = Header("X-RMP-Dry-Run", "true")

        sg = SendGridAPIClient(SENDGRID_API_KEY)
        response = sg.send(message)

        status = response.status_code
        success = 200 <= status < 300

        if success:
            mode = "DRY-RUN" if use_dry_run else "LIVE"
            logger.info(
                f"[{mode}] Attachment sent to {actual_recipient} "
                f"(file: {attachment_filename}, category: {category}) \u2014 {status}"
            )
        else:
            logger.warning(
                f"SendGrid returned {status} for {actual_recipient} "
                f"(file: {attachment_filename})"
            )

        return {
            "success": success,
            "status_code": status,
            "recipient": actual_recipient,
            "error": None if success else f"HTTP {status}",
        }

    except Exception as e:
        logger.error(f"SendGrid attachment error for {actual_recipient}: {e}")
        return {
            "success": False,
            "status_code": None,
            "recipient": actual_recipient,
            "error": str(e),
        }


# ---------------------------------------------------------------------------
# Plain notification email (Block 4 — operator alerts)
# ---------------------------------------------------------------------------


def send_plain_email(
    recipient_email: str,
    subject: str,
    html_body: str,
    category: str = "notification",
) -> dict:
    """
    Send a simple HTML email (no attachment, no DRY_RUN redirect).
    Used for operator notifications on purchase events.
    """
    if not SENDGRID_API_KEY:
        msg = "SENDGRID_API_KEY not set"
        logger.error(msg)
        return {
            "success": False,
            "status_code": None,
            "recipient": recipient_email,
            "error": msg,
        }

    try:
        message = Mail()
        message.from_email = From(FROM_EMAIL, FROM_NAME)
        message.to = To(recipient_email)
        message.subject = Subject(subject)
        message.content = HtmlContent(html_body)
        message.category = Category(category)

        sg = SendGridAPIClient(SENDGRID_API_KEY)
        response = sg.send(message)
        status = response.status_code
        success = 200 <= status < 300

        if success:
            logger.info(f"Notification sent to {recipient_email} \u2014 {status}")
        else:
            logger.warning(f"SendGrid returned {status} for notification to {recipient_email}")

        return {
            "success": success,
            "status_code": status,
            "recipient": recipient_email,
            "error": None if success else f"HTTP {status}",
        }

    except Exception as e:
        logger.error(f"SendGrid notification error: {e}")
        return {
            "success": False,
            "status_code": None,
            "recipient": recipient_email,
            "error": str(e),
        }
