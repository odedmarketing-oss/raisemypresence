#!/usr/bin/env python3
"""
deliverability_digest.py
Raise My Presence — Daily Deliverability Digest

Surfaces (per Local-Presence-Optimization/email-health-audit-runbook.md):
  3. SendGrid spam complaint rate (14d rolling)
  4. Suppression list WoW growth (SendGrid suppression endpoints vs state-file baseline)
  5. HetrixTools blacklist monitor — domain blacklist status across ~23 RBLs
  7. Postmark DMARC authentication health — Gmail-API polled weekly digest from
     dmarc@postmarkapp.com (free DMARC aggregation since 2026-04-23)

Surface 6 (Google Postmaster IP reputation) deprecated RMP #50 (2026-05-26):
Postmaster Tools v1 reputation API end-of-life'd by Google with no v2 replacement
for domain/IP reputation dashboards. Pillar B-1 replaced Surface 5 with HetrixTools
blacklist monitor (free tier, IP + domain monitoring against ~23 RBLs); Pillar B-2
(RMP #51) added Surface 7 (Postmark DMARC authentication health).

Surface 5 historical (RMP #46-#49): Postmaster v1 domain reputation, gracefully
degraded to "AUTH NOT CONFIGURED" since shipdate. Now replaced by HetrixTools.

Output: HTML + plain-text multipart email to operator via SendGrid.

Schedule (production): /etc/cron.d/deliverability-digest fires daily at 09:00
server-local (= 08:00 Bangkok = 01:00 UTC).

CLI usage:
  python3 deliverability_digest.py              # produce digest + send
  python3 deliverability_digest.py --dry-run    # produce digest, print to stdout, don't send

Dependencies (already in pipeline/requirements.txt):
  - sendgrid (SendGrid Python SDK)
  - python-dotenv
  - requests
  - beautifulsoup4 (Surface 7 HTML parsing of Postmark digest)
  - google-auth, google-auth-oauthlib, google-api-python-client (Surface 7 Gmail API)

Exit codes:
  0  digest sent successfully (or --dry-run completed)
  1  digest send failed (SendGrid error)
  2  fatal env error (SENDGRID_API_KEY missing)

Created RMP #46, 2026-05-23.
Surface 5 migrated Postmaster v1 → HetrixTools RMP #50, 2026-05-26 (Pillar B-1).
Surface 7 added RMP #51, 2026-05-27 (Pillar B-2).
"""

import argparse
import json
import logging
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Optional

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build as build_google_service
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail

# Load .env from pipeline directory (same pattern as config.py / alert_on_failure.py)
PIPELINE_DIR = Path(__file__).parent
load_dotenv(PIPELINE_DIR / ".env", override=True)

# --- Config ---
SENDGRID_API_KEY = os.environ.get("SENDGRID_API_KEY", "")
HETRIXTOOLS_API_KEY = os.environ.get("HETRIXTOOLS_API_KEY", "")

OPERATOR_EMAIL = "odedmarketing@gmail.com"
FROM_EMAIL = "hello@mail.raisemypresence.com"
FROM_NAME = "RMP Deliverability Monitor"

# HetrixTools v2 Blacklist Monitor API
# Endpoint pattern: https://api.hetrixtools.com/v2/<API_TOKEN>/blacklist/monitors/<PAGE>/<PER_PAGE>/
# Response shape (verified RMP #50 against live account):
#   [[{ID, Type, Target, Add_Date, Last_Check, Status, Label,
#       Contact_List_ID, Blacklisted_Count (str), Blacklisted_On (list|null),
#       Links: {Report_Link, Whitelabel_Report_Link}}, ...],
#    {Meta: {Total_Records: "N"}, Links: {Pages: []}}]
HETRIX_BASE_URL = "https://api.hetrixtools.com/v2"
HETRIX_PAGE_SIZE = 50  # 2 monitors today; 50 leaves headroom without pagination need

# Sender subdomain (where DKIM signs); retained for digest header rendering
SENDER_DOMAIN = "mail.raisemypresence.com"

# Surface 7 — Postmark DMARC digest via Gmail API
# Credentials + token live next to this script per Pillar B-2 deploy (RMP #51).
# Token has refresh_token=True (verified at oauth_setup.py run); Gmail API
# automatically refreshes access_token from refresh_token when expired.
GMAIL_CREDENTIALS_PATH = PIPELINE_DIR / "gmail_credentials.json"
GMAIL_TOKEN_PATH = PIPELINE_DIR / "gmail_token.json"
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
POSTMARK_DMARC_SENDER = "dmarc@postmarkapp.com"
POSTMARK_DMARC_LOOKBACK_DAYS = 14   # 14d hard cutoff (see freshness tiers below)
POSTMARK_FRESH_DAYS = 7             # 0-7d = fresh, 8-14d = stale-but-usable, 14+d = NO_DATA

# Surface 7 thresholds (per email-health-audit-runbook.md, source-aware 2b model):
#   Primary driver — "Other sources" count (unverified senders claiming domain):
#     HEALTHY = 0  ·  CONCERNING = 1-2  ·  BAD = 3+
#   Secondary driver — "Your sources" alignment % (configured sender IPs):
#     HEALTHY = 100%  ·  CONCERNING = 95-99%  ·  BAD = <95%
#   Final verdict: worst-of(primary, secondary)
# Forwarded-mail bucket is informational only; structural DKIM-break in forwarding
# chain is expected and unfixable RMP-side — does NOT drive verdict.
DMARC_OTHER_SOURCES_CONCERNING_THRESHOLD = 1   # 1-2 sources -> CONCERNING
DMARC_OTHER_SOURCES_BAD_THRESHOLD = 3          # 3+ sources -> BAD
DMARC_YOUR_SOURCES_HEALTHY_PCT = 100.0         # 100% only -> HEALTHY
DMARC_YOUR_SOURCES_BAD_PCT = 95.0              # <95% -> BAD

# State file for WoW baseline (Surface 4)
STATE_DIR = Path("/root/audit-scanner/state")
SUPPRESSION_BASELINE_FILE = STATE_DIR / "suppression-baseline.txt"

# Rolling window for spam rate
STATS_WINDOW_DAYS = 14

# Thresholds (per email-health-audit-runbook.md Surface 3 + Surface 4)
SPAM_RATE_HEALTHY_FRAC = 0.0005  # <0.05%
SPAM_RATE_BAD_FRAC = 0.001       # >0.1%
SUPPRESSION_WOW_HEALTHY_PCT = 5.0   # <5%
SUPPRESSION_WOW_BAD_PCT = 15.0      # >15%

# Surface 5 (HetrixTools) classification (binary, no CONCERNING tier):
#   Any monitor with Blacklisted_Count > 0 -> BAD (immediate operator surface per §1.21)
#   All monitors at 0/N -> HEALTHY
# Rationale: blacklist hit is operator-actionable on appearance; partial-hit handling
# is per-RBL severity, deferred until first real listing event.

# Status labels
STATUS_HEALTHY = "HEALTHY"
STATUS_CONCERNING = "CONCERNING"
STATUS_BAD = "BAD"
STATUS_UNKNOWN = "UNKNOWN"
STATUS_NOT_CONFIGURED = "AUTH NOT CONFIGURED"
STATUS_DEPRECATED = "DEPRECATED"

# HTML badge colors per status (match the kit/Block 3 green-accent palette)
STATUS_COLORS = {
    STATUS_HEALTHY: "#16A34A",          # green-600
    STATUS_CONCERNING: "#CA8A04",       # yellow-600
    STATUS_BAD: "#DC2626",              # red-600
    STATUS_UNKNOWN: "#6B7280",          # gray-500
    STATUS_NOT_CONFIGURED: "#6B7280",   # gray-500
}

logger = logging.getLogger("deliverability_digest")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


# ---------------------------------------------------------------------------
# Surface 3 — SendGrid spam complaint rate (14d)
# ---------------------------------------------------------------------------

def get_spam_rate_14d() -> dict:
    """
    SendGrid Stats API: /v3/stats?start_date=YYYY-MM-DD&end_date=YYYY-MM-DD

    Returns on success:
        {"spam_reports": int, "delivered": int, "spam_rate_pct": float}
    Returns on failure:
        {"status": "FAILED", "error": str}
    """
    end = date.today()
    start = end - timedelta(days=STATS_WINDOW_DAYS)
    url = "https://api.sendgrid.com/v3/stats"
    params = {"start_date": start.isoformat(), "end_date": end.isoformat()}
    headers = {"Authorization": f"Bearer {SENDGRID_API_KEY}"}

    try:
        r = requests.get(url, headers=headers, params=params, timeout=15)
        r.raise_for_status()
        days = r.json()
    except Exception as e:
        logger.error(f"Surface 3 (spam rate) — API call failed: {e}")
        return {"status": "FAILED", "error": str(e)}

    spam_reports = 0
    delivered = 0
    for day in days:
        for s in day.get("stats", []):
            m = s.get("metrics", {})
            spam_reports += m.get("spam_reports", 0)
            delivered += m.get("delivered", 0)

    rate_pct = (spam_reports / delivered * 100) if delivered else 0.0
    return {
        "spam_reports": spam_reports,
        "delivered": delivered,
        "spam_rate_pct": rate_pct,
    }


def classify_spam_rate(result: dict) -> str:
    if result.get("status") == "FAILED":
        return STATUS_UNKNOWN
    rate_frac = result["spam_rate_pct"] / 100
    if rate_frac < SPAM_RATE_HEALTHY_FRAC:
        return STATUS_HEALTHY
    if rate_frac < SPAM_RATE_BAD_FRAC:
        return STATUS_CONCERNING
    return STATUS_BAD


# ---------------------------------------------------------------------------
# Surface 4 — Suppression list WoW growth
#   Source: SendGrid suppression endpoints (canonical, not local file)
#   Comparison: state-file baseline at STATE_DIR / suppression-baseline.txt
# ---------------------------------------------------------------------------

SUPPRESSION_ENDPOINTS = [
    ("/v3/suppression/bounces", "bounces"),
    ("/v3/suppression/blocks", "blocks"),
    ("/v3/suppression/invalid_emails", "invalid_emails"),
    ("/v3/suppression/spam_reports", "spam_reports"),
    ("/v3/asm/suppressions/global", "global_unsubscribes"),
]

PAGINATION_LIMIT = 500       # SendGrid max per request
PAGINATION_SAFETY_PAGES = 100  # max pages per endpoint (50K entries cap)


def _count_endpoint(path: str) -> int:
    """Paginate one suppression endpoint, return total entry count."""
    headers = {"Authorization": f"Bearer {SENDGRID_API_KEY}"}
    total = 0
    offset = 0
    for _ in range(PAGINATION_SAFETY_PAGES):
        url = f"https://api.sendgrid.com{path}?limit={PAGINATION_LIMIT}&offset={offset}"
        r = requests.get(url, headers=headers, timeout=15)
        r.raise_for_status()
        page = r.json()
        if not page:
            break
        total += len(page)
        if len(page) < PAGINATION_LIMIT:
            break
        offset += PAGINATION_LIMIT
    else:
        logger.warning(f"Pagination safety cap hit for {path} (possible undercount)")
    return total


def _read_baseline() -> Optional[dict]:
    if not SUPPRESSION_BASELINE_FILE.exists():
        return None
    try:
        raw = SUPPRESSION_BASELINE_FILE.read_text().strip()
        if not raw:
            return None
        # JSON-first (new format: {"date","count","breakdown"}); fall back to the
        # legacy "DATE COUNT" line so the existing baseline reads clean until the
        # next 7-day refresh rewrites it as JSON (graceful migration, no gap).
        if raw.lstrip().startswith("{"):
            obj = json.loads(raw)
            return {
                "date": obj["date"],
                "count": int(obj["count"]),
                "breakdown": obj.get("breakdown"),  # None on legacy-upgraded rows
            }
        parts = raw.split()
        if len(parts) != 2:
            return None
        return {"date": parts[0], "count": int(parts[1]), "breakdown": None}
    except Exception as e:
        logger.warning(f"Failed to parse suppression baseline file: {e}")
        return None


def _write_baseline(count: int, breakdown: Optional[dict] = None) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()
    payload = {"date": today, "count": count, "breakdown": breakdown or {}}
    SUPPRESSION_BASELINE_FILE.write_text(json.dumps(payload) + "\n")


def get_suppression_surface() -> dict:
    """
    Aggregate suppression count across SendGrid endpoints; compute WoW delta
    against state-file baseline; update baseline post-run.

    Returns on success:
        {
          "current_total": int,
          "breakdown": dict[str, int],
          "baseline": dict | None,
          "wow_growth_pct": float | None,
          "wow_status": "OK" | "FIRST_RUN" | "BASELINE_ZERO",
        }
    Returns on hard failure:
        {"status": "FAILED", "error": str}
    """
    try:
        breakdown = {}
        total = 0
        for path, label in SUPPRESSION_ENDPOINTS:
            count = _count_endpoint(path)
            breakdown[label] = count
            total += count
    except Exception as e:
        logger.error(f"Surface 4 (suppression) — endpoint aggregation failed: {e}")
        return {"status": "FAILED", "error": str(e)}

    baseline = _read_baseline()
    result = {
        "current_total": total,
        "breakdown": breakdown,
        "baseline": baseline,
    }

    if baseline is None:
        result["wow_growth_pct"] = None
        result["wow_status"] = "FIRST_RUN"
    elif baseline["count"] == 0:
        result["wow_growth_pct"] = None
        result["wow_status"] = "BASELINE_ZERO"
    else:
        result["wow_growth_pct"] = (total - baseline["count"]) / baseline["count"] * 100
        result["wow_status"] = "OK"

    # ROLLING 7-DAY WoW: only refresh baseline if no baseline exists OR existing
    # baseline is >= 7 days old. This makes Surface 4 a true week-over-week
    # comparison aligned with runbook thresholds (<5 / 5-15 / >15% growth pct
    # calibrated for weekly drift, not daily noise). Previously the baseline was
    # overwritten every run, collapsing the comparison to day-over-day and
    # producing false-CONCERNING alarms on daily noise.
    should_write_baseline = False
    if baseline is None:
        should_write_baseline = True  # first run — establish baseline
    else:
        try:
            baseline_date = datetime.strptime(baseline["date"], "%Y-%m-%d").date()
            age_days = (date.today() - baseline_date).days
            if age_days >= 7:
                should_write_baseline = True
                logger.info(f"Baseline age {age_days} days >= 7; refreshing baseline.")
            else:
                logger.info(f"Baseline age {age_days} days < 7; keeping existing baseline.")
        except Exception as e:
            logger.warning(f"Could not parse baseline date {baseline['date']!r}: {e}; refreshing.")
            should_write_baseline = True

    if should_write_baseline:
        try:
            _write_baseline(total, breakdown)
        except Exception as e:
            logger.error(f"Failed to update suppression baseline: {e}")

    # Compute baseline age in days for detail-string rendering
    result["baseline_age_days"] = None
    if baseline:
        try:
            baseline_date = datetime.strptime(baseline["date"], "%Y-%m-%d").date()
            result["baseline_age_days"] = (date.today() - baseline_date).days
        except Exception:
            pass

    # Per-category WoW (Scope A — signal-add only; verdict logic unchanged).
    # Computable only once the baseline carries a stored breakdown; legacy-format
    # baselines (breakdown=None) render current-counts-only until the next 7-day
    # refresh writes a JSON baseline with per-category counts.
    baseline_breakdown = baseline.get("breakdown") if baseline else None
    if baseline_breakdown:
        bw = {}
        for label, curr in breakdown.items():
            prior = baseline_breakdown.get(label, 0)
            delta = curr - prior
            pct = (delta / prior * 100) if prior else None
            bw[label] = {"curr": curr, "prior": prior, "delta": delta, "pct": pct}
        result["breakdown_wow"] = bw
    else:
        result["breakdown_wow"] = None

    return result


def classify_suppression(result: dict) -> str:
    if result.get("status") == "FAILED":
        return STATUS_UNKNOWN
    if result.get("wow_status") in ("FIRST_RUN", "BASELINE_ZERO"):
        return STATUS_UNKNOWN
    growth = result.get("wow_growth_pct") or 0.0
    if growth < SUPPRESSION_WOW_HEALTHY_PCT:
        return STATUS_HEALTHY
    if growth < SUPPRESSION_WOW_BAD_PCT:
        return STATUS_CONCERNING
    return STATUS_BAD


# ---------------------------------------------------------------------------
# Surface 5 — HetrixTools blacklist monitor
#   Migrated from Google Postmaster v1 at RMP #50 (Pillar B-1).
#   Postmaster v1 EOL'd by Google; v2 has no equivalent reputation surface.
#   HetrixTools free tier monitors 2 targets (mail.raisemypresence.com,
#   raisemypresence.com) against ~23 RBLs (Spamhaus, SpamCop, SenderScore,
#   Comcast DNSBL, etc.). Polled every ~20 min by HetrixTools; we read cached
#   state via API at digest time.
#
# Graceful degradation chain:
#   1. HETRIXTOOLS_API_KEY not set      -> NOT_CONFIGURED placeholder
#   2. API call fails (network/auth)    -> FAILED placeholder with error
#   3. API returns empty monitor list   -> NO_DATA placeholder
# ---------------------------------------------------------------------------

def get_hetrixtools_blacklist() -> dict:
    """
    HetrixTools v2 List Blacklist Monitors API call.

    Returns on success:
        {
          "monitors": [
            {"target": str, "type": str, "blacklisted_count": int,
             "blacklisted_on": list, "last_check_ts": int, "report_link": str,
             "label": str},
            ...
          ],
          "total_records": int,
          "any_listed": bool,
        }
    Returns on graceful degradation or failure:
        {"status": "NOT_CONFIGURED" | "FAILED" | "NO_DATA", "error": str (optional)}
    """
    if not HETRIXTOOLS_API_KEY:
        logger.info("Surface 5 — HETRIXTOOLS_API_KEY not set; graceful degradation.")
        return {"status": "NOT_CONFIGURED"}

    url = f"{HETRIX_BASE_URL}/{HETRIXTOOLS_API_KEY}/blacklist/monitors/0/{HETRIX_PAGE_SIZE}/"
    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        payload = r.json()
    except Exception as e:
        logger.error(f"Surface 5 (HetrixTools) — API call failed: {e}")
        return {"status": "FAILED", "error": str(e)}

    # v2 shape: [[monitor_dict, ...], {"Meta": {"Total_Records": "N"}, ...}]
    if not isinstance(payload, list) or len(payload) < 1:
        logger.warning(f"Surface 5 — unexpected payload shape: {type(payload).__name__}")
        return {"status": "FAILED", "error": "unexpected response shape"}

    raw_monitors = payload[0] if isinstance(payload[0], list) else []
    meta = payload[1].get("Meta", {}) if len(payload) >= 2 and isinstance(payload[1], dict) else {}
    try:
        total_records = int(meta.get("Total_Records", len(raw_monitors)))
    except (ValueError, TypeError):
        total_records = len(raw_monitors)

    if not raw_monitors:
        logger.info("Surface 5 — HetrixTools returned 0 monitors (none configured).")
        return {"status": "NO_DATA"}

    monitors = []
    any_listed = False
    for m in raw_monitors:
        try:
            bl_count = int(m.get("Blacklisted_Count", "0"))
        except (ValueError, TypeError):
            bl_count = 0
        if bl_count > 0:
            any_listed = True
        monitors.append({
            "target": m.get("Target", "?"),
            "type": m.get("Type", "?"),
            "blacklisted_count": bl_count,
            "blacklisted_on": m.get("Blacklisted_On") or [],
            "last_check_ts": m.get("Last_Check", 0),
            "report_link": (m.get("Links") or {}).get("Report_Link", ""),
            "label": m.get("Label", ""),
        })

    return {
        "monitors": monitors,
        "total_records": total_records,
        "any_listed": any_listed,
    }


def classify_hetrixtools(result: dict) -> str:
    """Surface 5 classifier. Binary: any listing -> BAD; all clear -> HEALTHY."""
    status = result.get("status")
    if status == "NOT_CONFIGURED":
        return STATUS_NOT_CONFIGURED
    if status in ("FAILED", "NO_DATA"):
        return STATUS_UNKNOWN
    return STATUS_BAD if result.get("any_listed") else STATUS_HEALTHY


# ---------------------------------------------------------------------------
# Surface 7 — Postmark DMARC authentication health (Pillar B-2, RMP #51)
#   Source: weekly DMARC digest email from dmarc@postmarkapp.com (aggregating
#   since 2026-04-23, free tier). Polled via Gmail API.
#   Cadence mismatch: source weekly, digest daily — handled via tiered freshness
#   (0-7d fresh, 8-14d stale-but-rendered with downgrade-by-one if HEALTHY,
#    14+d -> NO_DATA / UNKNOWN).
#
# Graceful degradation chain:
#   1. Credentials/token files missing -> NOT_CONFIGURED placeholder
#   2. Gmail API auth fails             -> AUTH_FAILED placeholder
#   3. No matching email in lookback    -> NO_DATA placeholder (expected first week)
#   4. HTML parse fails (format drift)  -> PARSE_FAILED with HTML excerpt logged;
#                                          original email remains in inbox as fallback
# ---------------------------------------------------------------------------

def _gmail_service():
    """
    Build Gmail API service from credentials + token files. Refreshes access
    token via refresh_token if needed and writes refreshed token back to disk.
    Returns service object on success, raises Exception on failure.
    """
    if not GMAIL_CREDENTIALS_PATH.exists() or not GMAIL_TOKEN_PATH.exists():
        raise FileNotFoundError(
            f"Gmail credentials or token missing: "
            f"credentials={GMAIL_CREDENTIALS_PATH.exists()}, "
            f"token={GMAIL_TOKEN_PATH.exists()}"
        )
    creds = Credentials.from_authorized_user_file(str(GMAIL_TOKEN_PATH), GMAIL_SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(GoogleAuthRequest())
        # Persist refreshed access_token back to disk for next run
        GMAIL_TOKEN_PATH.write_text(creds.to_json())
        logger.info("Gmail access token refreshed from refresh_token.")
    if not creds.valid:
        raise RuntimeError(
            "Gmail credentials invalid even after refresh attempt; re-run oauth_setup.py."
        )
    return build_google_service("gmail", "v1", credentials=creds, cache_discovery=False)


def get_postmark_dmarc_digest() -> dict:
    """
    Find most recent Postmark DMARC weekly digest in operator inbox.

    Returns on success:
        {
          "html": str,                      # full HTML body
          "received_date": datetime,        # UTC datetime of email Date header
          "digest_age_days": int,           # today - received_date.date()
          "subject": str,
        }
    Returns on graceful degradation:
        {"status": "NOT_CONFIGURED" | "AUTH_FAILED" | "NO_DATA", "error": str (optional)}
    """
    if not GMAIL_CREDENTIALS_PATH.exists() or not GMAIL_TOKEN_PATH.exists():
        logger.info(
            f"Surface 7 — Gmail credentials/token not configured at {PIPELINE_DIR}; "
            f"graceful degradation."
        )
        return {"status": "NOT_CONFIGURED"}

    try:
        service = _gmail_service()
    except Exception as e:
        logger.error(f"Surface 7 (Postmark DMARC) — Gmail auth failed: {e}")
        return {"status": "AUTH_FAILED", "error": str(e)}

    # Search inbox for most recent Postmark DMARC digest within lookback window
    query = f"from:{POSTMARK_DMARC_SENDER} newer_than:{POSTMARK_DMARC_LOOKBACK_DAYS}d"
    try:
        # maxResults=1; Gmail returns newest-first by default for query results
        list_resp = service.users().messages().list(
            userId="me", q=query, maxResults=1
        ).execute()
        messages = list_resp.get("messages", [])
    except Exception as e:
        logger.error(f"Surface 7 — Gmail messages.list failed: {e}")
        return {"status": "AUTH_FAILED", "error": f"messages.list: {e}"}

    if not messages:
        logger.info(
            f"Surface 7 — no Postmark DMARC digest found in last "
            f"{POSTMARK_DMARC_LOOKBACK_DAYS}d (query={query})."
        )
        return {"status": "NO_DATA"}

    msg_id = messages[0]["id"]
    try:
        msg = service.users().messages().get(
            userId="me", id=msg_id, format="full"
        ).execute()
    except Exception as e:
        logger.error(f"Surface 7 — Gmail messages.get failed for {msg_id}: {e}")
        return {"status": "AUTH_FAILED", "error": f"messages.get: {e}"}

    # Extract Subject + Date headers and HTML body part
    headers = {h["name"]: h["value"] for h in msg["payload"].get("headers", [])}
    subject = headers.get("Subject", "")
    date_str = headers.get("Date", "")
    try:
        received_date = parsedate_to_datetime(date_str)
        if received_date.tzinfo is None:
            received_date = received_date.replace(tzinfo=timezone.utc)
    except Exception:
        logger.warning(f"Surface 7 — could not parse Date header {date_str!r}; using now() fallback")
        received_date = datetime.now(timezone.utc)

    html = _extract_html_body(msg["payload"])
    if not html:
        logger.warning(f"Surface 7 — no HTML body found in message {msg_id}")
        return {"status": "NO_DATA"}

    digest_age_days = (date.today() - received_date.date()).days
    return {
        "html": html,
        "received_date": received_date,
        "digest_age_days": digest_age_days,
        "subject": subject,
    }


def _extract_html_body(payload: dict) -> str:
    """
    Recursively walk Gmail message payload parts looking for text/html.
    Postmark digest is single-part text/html, but defensive walk handles
    edge cases like signed/encrypted wrappers.
    """
    import base64

    mime_type = payload.get("mimeType", "")
    if mime_type == "text/html":
        data = payload.get("body", {}).get("data", "")
        if data:
            return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
    for part in payload.get("parts", []) or []:
        html = _extract_html_body(part)
        if html:
            return html
    return ""


def parse_postmark_dmarc_html(html: str) -> dict:
    """
    Extract DMARC metrics from Postmark digest HTML via text-anchored regex.
    Uses BeautifulSoup with built-in html.parser (no lxml dep) to strip CSS/JS,
    then regex against visible-label anchors. Postmark can restructure HTML
    freely; visible labels ("Emails processed", "SPF or DKIM aligned", section
    headers, "Total / SPF Aligned / DKIM Aligned" column-header triplet) are
    stable because they're part of the human-readable contract.

    Returns on success:
        {
          "date_range": str,             # e.g. "May 18 – May 25"
          "emails_processed": int,
          "aligned_pct": float,          # "SPF or DKIM aligned" headline %
          "unaligned_pct": float,        # "SPF and DKIM not aligned"
          "your_sources": list[dict],    # [{name, ip_count, dkim_pct, spf_pct}, ...]
          "other_sources": list[dict],   # [{name, ip_count, dkim_pct, spf_pct}, ...]
          "forwarded_sources": list[str],# informational only, names only
          "parse_status": "OK",
        }
    Returns on parse failure:
        {"parse_status": "FAILED", "raw_excerpt": str (first 500 chars), "error": str}
    """
    try:
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["style", "script"]):
            tag.decompose()
        text = soup.get_text(separator="\n", strip=True)
        # Collapse blank-line runs for stable regex anchoring
        text = re.sub(r"\n{3,}", "\n\n", text)

        # Date range — typically appears near top, format "May 18 – May 25"
        # Accept various dash characters (en-dash, em-dash, hyphen)
        date_range_match = re.search(
            r"([A-Z][a-z]+\s+\d{1,2}\s*[–—-]\s*[A-Z][a-z]+\s+\d{1,2})", text
        )
        date_range = date_range_match.group(1) if date_range_match else ""

        # Headline metrics — anchored by label substrings
        emails_processed = _extract_int_before_label(text, "Emails processed")
        aligned_pct = _extract_pct_before_label(text, "SPF or DKIM aligned")
        unaligned_pct = _extract_pct_before_label(text, "SPF and DKIM not aligned")

        # Source sections — unified extractor for all three. Forwarded section
        # uses same per-source-block structure as Your/Other but we only retain
        # names downstream (forwarded sources are informational, not verdict-driving).
        your_sources = _extract_source_section(text, "Your sources")
        other_sources = _extract_source_section(text, "Other sources")
        forwarded_sources = [
            s["name"] for s in _extract_source_section(text, "Forwarded email sources")
        ]

        return {
            "date_range": date_range,
            "emails_processed": emails_processed,
            "aligned_pct": aligned_pct,
            "unaligned_pct": unaligned_pct,
            "your_sources": your_sources,
            "other_sources": other_sources,
            "forwarded_sources": forwarded_sources,
            "parse_status": "OK",
        }
    except Exception as e:
        logger.warning(f"Surface 7 — HTML parse failed: {e}")
        return {
            "parse_status": "FAILED",
            "raw_excerpt": html[:500] if html else "",
            "error": str(e),
        }


def _extract_int_before_label(text: str, label: str) -> int:
    """Postmark layout is 'NUMBER\nLabel'; find integer immediately before label."""
    pattern = rf"(\d+)\s*\n+\s*{re.escape(label)}"
    m = re.search(pattern, text)
    return int(m.group(1)) if m else 0


def _extract_pct_before_label(text: str, label: str) -> float:
    """Postmark layout is 'NN%\nLabel'; find percent immediately before label."""
    pattern = rf"(\d+(?:\.\d+)?)%\s*\n+\s*{re.escape(label)}"
    m = re.search(pattern, text)
    return float(m.group(1)) if m else 0.0


def _extract_source_section(text: str, header: str) -> list:
    """
    Extract source blocks under a section header. Postmark visible-text layout
    per source (verified against real .eml at RMP #51 parser validation):

        <source-domain>          <- block start anchor
        Total                    <- column header (constant, distinguishes
        SPF Aligned                 actual source blocks from prose mentions)
        DKIM Aligned
        <IP-or-aggregator>       <- first IP row (literal IPv4 or "N more IPs")
        <count>
        <spf-pct>%
        <dkim-pct>%
        [... more IP rows ...]

    Per-source dkim_pct/spf_pct returned = minimum across all IPs in the block
    (conservative — a single misaligned IP surfaces in the source-level metric).
    ip_count = total emails summed across IP rows.

    Section boundary: starts after section header line; ends at next section
    header or end of block.
    """
    section_headers = ["Your sources", "Other sources", "Forwarded email sources"]
    other_headers = [h for h in section_headers if h != header]

    start_match = re.search(rf"^{re.escape(header)}\s*$", text, re.MULTILINE)
    if not start_match:
        return []
    block_start = start_match.end()
    block_end = len(text)
    for h in other_headers:
        m = re.search(rf"^{re.escape(h)}\s*$", text[block_start:], re.MULTILINE)
        if m:
            candidate_end = block_start + m.start()
            if candidate_end < block_end:
                block_end = candidate_end
    block = text[block_start:block_end]

    # Anchor: <name>\nTotal\nSPF Aligned\nDKIM Aligned — uniquely marks source
    # block start. Prose mentions of domain names (e.g. "raisemypresence.com"
    # mid-paragraph) won't match because they aren't followed by this triplet.
    anchor_pattern = re.compile(
        r"^([^\n]+)\nTotal\n+SPF Aligned\n+DKIM Aligned\n",
        re.MULTILINE,
    )
    anchors = list(anchor_pattern.finditer(block))
    if not anchors:
        return []

    rows = []
    for i, anchor_match in enumerate(anchors):
        name = anchor_match.group(1).strip()
        if not name:
            continue
        # IP-data range: from end of this anchor to start of next anchor (or EOB)
        data_start = anchor_match.end()
        data_end = anchors[i + 1].start() if i + 1 < len(anchors) else len(block)
        ip_data = block[data_start:data_end]
        # Per-IP row: <IPv4-or-aggregator>\n<count>\n<spf>%\n<dkim>%
        # Column order per Postmark headers: Total, SPF Aligned, DKIM Aligned
        ip_rows = re.findall(
            r"(?:\d+\.\d+\.\d+\.\d+|\d+\s+more\s+IPs?)\n+(\d+)\n+(\d+(?:\.\d+)?)%\n+(\d+(?:\.\d+)?)%",
            ip_data,
            re.IGNORECASE,
        )
        if not ip_rows:
            continue
        total_count = sum(int(c) for c, _, _ in ip_rows)
        min_spf = min(float(s) for _, s, _ in ip_rows)
        min_dkim = min(float(d) for _, _, d in ip_rows)
        rows.append({
            "name": name,
            "ip_count": total_count,
            "dkim_pct": min_dkim,
            "spf_pct": min_spf,
        })
    return rows


def classify_postmark_dmarc(s7: dict) -> str:
    """
    Surface 7 classifier. Source-aware 2b model:
      Primary — "Other sources" count (unverified senders claiming domain)
      Secondary — "Your sources" alignment %
      Final verdict = worst-of(primary, secondary)
      Tiered freshness applied: HEALTHY downgraded to CONCERNING if digest 8-14d old.
    """
    status = s7.get("status")
    if status == "NOT_CONFIGURED":
        return STATUS_NOT_CONFIGURED
    if status in ("AUTH_FAILED", "NO_DATA"):
        return STATUS_UNKNOWN

    parsed = s7.get("parsed", {})
    if parsed.get("parse_status") == "FAILED":
        return STATUS_UNKNOWN

    # Primary driver — "Other sources" count
    other_count = len(parsed.get("other_sources", []))
    if other_count >= DMARC_OTHER_SOURCES_BAD_THRESHOLD:
        primary = STATUS_BAD
    elif other_count >= DMARC_OTHER_SOURCES_CONCERNING_THRESHOLD:
        primary = STATUS_CONCERNING
    else:
        primary = STATUS_HEALTHY

    # Secondary driver — "Your sources" alignment %
    your_sources = parsed.get("your_sources", [])
    if your_sources:
        # Conservative: take minimum DKIM% across owned IPs as the alignment metric
        # (a single misaligned owned IP is the signal worth surfacing).
        min_dkim = min(s.get("dkim_pct", 100.0) for s in your_sources)
        min_spf = min(s.get("spf_pct", 100.0) for s in your_sources)
        your_alignment_pct = min(min_dkim, min_spf)
        if your_alignment_pct < DMARC_YOUR_SOURCES_BAD_PCT:
            secondary = STATUS_BAD
        elif your_alignment_pct < DMARC_YOUR_SOURCES_HEALTHY_PCT:
            secondary = STATUS_CONCERNING
        else:
            secondary = STATUS_HEALTHY
    else:
        # No owned sources visible in digest — informational only, don't drive verdict
        secondary = STATUS_HEALTHY

    # worst-of(primary, secondary) via status ordering
    order = {STATUS_HEALTHY: 0, STATUS_CONCERNING: 1, STATUS_BAD: 2}
    verdict = max([primary, secondary], key=lambda s: order.get(s, 0))

    # Tiered freshness — downgrade HEALTHY by one tier if digest is 8-14d old
    digest_age = s7.get("digest_age_days", 0)
    if digest_age > POSTMARK_FRESH_DAYS and verdict == STATUS_HEALTHY:
        verdict = STATUS_CONCERNING

    return verdict


def _format_surface_7_detail(s7: dict) -> str:
    """Render Surface 7 detail string for HTML."""
    status = s7.get("status")
    if status == "NOT_CONFIGURED":
        return (
            "Gmail credentials/token not configured on Tencent. "
            f"Place <code>gmail_credentials.json</code> + <code>gmail_token.json</code> "
            f"at <code>{PIPELINE_DIR}</code> via <code>oauth_setup.py</code> to enable."
        )
    if status == "AUTH_FAILED":
        return f"FAILED &mdash; Gmail API auth: {s7.get('error', 'unknown')}"
    if status == "NO_DATA":
        return (
            f"No Postmark DMARC digest in last {POSTMARK_DMARC_LOOKBACK_DAYS}d. "
            "Expected weekly from <code>dmarc@postmarkapp.com</code> — investigate "
            "Postmark account or domain DMARC <code>rua=</code> record."
        )

    parsed = s7.get("parsed", {})
    if parsed.get("parse_status") == "FAILED":
        return (
            f"PARSE FAILED &mdash; {parsed.get('error', 'unknown')}. "
            "Original email in <code>odedmarketing@gmail.com</code> inbox as manual fallback."
        )

    age = s7.get("digest_age_days", 0)
    age_label = f"{age}d ago" if age != 0 else "today"
    stale_warn = ""
    if age > POSTMARK_FRESH_DAYS:
        stale_warn = (
            f' <span style="color:#CA8A04;">&middot; ⚠ digest {age}d old; '
            f'Postmark may have skipped a cycle</span>'
        )

    your_count = len(parsed.get("your_sources", []))
    other_count = len(parsed.get("other_sources", []))
    forwarded_count = len(parsed.get("forwarded_sources", []))
    aligned_pct = parsed.get("aligned_pct", 0.0)
    date_range = parsed.get("date_range", "?")

    # Owned-source min alignment (the secondary-driver metric)
    your_sources = parsed.get("your_sources", [])
    if your_sources:
        min_dkim = min(s.get("dkim_pct", 100.0) for s in your_sources)
        min_spf = min(s.get("spf_pct", 100.0) for s in your_sources)
        owned_alignment = min(min_dkim, min_spf)
        owned_str = f"{owned_alignment:.0f}% owned-IP alignment ({your_count} src)"
    else:
        owned_str = "no owned sources visible"

    # Other sources — list names if 1-3, count-only if more
    if other_count == 0:
        other_str = "0 unverified sources"
    elif other_count <= 3:
        other_names = ", ".join(s["name"] for s in parsed["other_sources"])
        other_str = f"{other_count} unverified ({other_names})"
    else:
        other_str = f"{other_count} unverified sources (investigate)"

    forwarded_str = (
        f"{forwarded_count} forwarded (informational)" if forwarded_count else ""
    )

    parts = [
        f"{aligned_pct:.0f}% aligned headline",
        owned_str,
        other_str,
    ]
    if forwarded_str:
        parts.append(forwarded_str)

    return (
        f"{' &middot; '.join(parts)}<br>"
        f'<span style="color:#9ca3af;font-size:11px;">'
        f'Based on Postmark digest {date_range} &middot; received {age_label}{stale_warn}</span>'
    )


# ---------------------------------------------------------------------------
# Formatting — HTML + plain-text multipart
# ---------------------------------------------------------------------------

def _html_row(label: str, status: str, detail: str) -> str:
    color = STATUS_COLORS.get(status, "#6B7280")
    return (
        f'<tr><td class="label">{label}</td>'
        f'<td class="status"><span class="badge" style="background:{color};">{status}</span></td>'
        f'<td class="detail">{detail}</td></tr>'
    )


def _format_surface_3_detail(s3: dict) -> str:
    if s3.get("status") == "FAILED":
        return f"FAILED &mdash; {s3.get('error', 'unknown')}"
    return (
        f"{s3['spam_reports']} spam reports / "
        f"{s3['delivered']} delivered "
        f"= {s3['spam_rate_pct']:.4f}%"
    )


def _format_suppression_breakdown_html(s4: dict) -> str:
    """
    Scope A signal-add: render per-category suppression composition
    (bounces / blocks / invalid_emails / spam_reports / global_unsubscribes)
    with per-category WoW when the baseline carries a breakdown. All five
    categories always shown so spam_reports=0 is explicit (the key disposition
    input). Returns an HTML fragment appended to the Surface 4 detail cell.
    """
    breakdown = s4.get("breakdown") or {}
    if not breakdown:
        return ""
    bw = s4.get("breakdown_wow")
    label_order = [label for _, label in SUPPRESSION_ENDPOINTS]
    parts = []
    for label in label_order:
        curr = breakdown.get(label, 0)
        disp = label.replace("_", " ")
        if bw and label in bw:
            d = bw[label]
            if d["pct"] is not None:
                parts.append(f"{disp} {curr} ({d['delta']:+d}, {d['pct']:+.1f}%)")
            else:
                parts.append(f"{disp} {curr} ({d['delta']:+d})")
        else:
            parts.append(f"{disp} {curr}")
    note = "" if bw else " &middot; <em>per-category WoW from next 7-day baseline</em>"
    return (
        f'<br><span style="color:#6b7280;font-size:12px;">'
        f'{" &middot; ".join(parts)}{note}</span>'
    )


def _format_surface_4_detail(s4: dict) -> str:
    if s4.get("status") == "FAILED":
        return f"FAILED &mdash; {s4.get('error', 'unknown')}"
    breakdown_html = _format_suppression_breakdown_html(s4)
    wow = s4.get("wow_status")
    if wow == "FIRST_RUN":
        return (
            f"First run &mdash; baseline established at {s4['current_total']} suppressions. "
            f"Comparison delta builds daily; full WoW delta at day 7."
            f"{breakdown_html}"
        )
    if wow == "BASELINE_ZERO":
        return f"Current = {s4['current_total']}; prior baseline was 0.{breakdown_html}"
    prior = s4["baseline"]["count"]
    curr = s4["current_total"]
    growth = s4["wow_growth_pct"]
    age_days = s4.get("baseline_age_days")
    age_str = f" ({age_days}d ago)" if age_days is not None else ""
    return (
        f"{curr} today vs {prior} on {s4['baseline']['date']}{age_str} "
        f"({curr - prior:+d}, {growth:+.1f}%)"
        f"{breakdown_html}"
    )


def _format_surface_5_detail(s5: dict) -> str:
    """Render HetrixTools Surface 5 detail string for HTML."""
    status = s5.get("status")
    if status == "NOT_CONFIGURED":
        return (
            "HetrixTools API key not configured on Tencent. "
            "Set <code>HETRIXTOOLS_API_KEY</code> in pipeline <code>.env</code> to enable."
        )
    if status == "FAILED":
        return f"FAILED &mdash; {s5.get('error', 'unknown')}"
    if status == "NO_DATA":
        return (
            "HetrixTools returned zero monitors. Add at least one Blacklist Monitor "
            "at <a href=\"https://hetrixtools.com/blacklist-monitors\">hetrixtools.com</a>."
        )

    monitors = s5.get("monitors", [])
    if s5.get("any_listed"):
        # BAD path — list every monitor with its hit count + report link.
        # Per §1.21, this is the high-signal path; surface enough for triage.
        parts = []
        for m in monitors:
            label = m["target"]
            count = m["blacklisted_count"]
            link = m["report_link"]
            if count > 0:
                hit_rbls = ", ".join(m["blacklisted_on"][:5]) or "—"
                more = (
                    f" +{len(m['blacklisted_on']) - 5} more"
                    if len(m["blacklisted_on"]) > 5 else ""
                )
                parts.append(
                    f"<strong>{label}</strong> listed on {count} RBL(s): "
                    f"{hit_rbls}{more} — <a href=\"{link}\">report</a>"
                )
            else:
                parts.append(f"{label} 0/23 clear")
        return "<br>".join(parts)
    else:
        # HEALTHY path — single-line summary across all monitors.
        clean_summaries = [f"{m['target']} 0/23" for m in monitors]
        return (
            f"All {len(monitors)} monitor(s) clear of all RBLs: " +
            ", ".join(clean_summaries)
        )


def format_html(surface_3: dict, surface_4: dict, surface_5: dict, surface_7: dict, today_str: str) -> str:
    s3_status = classify_spam_rate(surface_3)
    s4_status = classify_suppression(surface_4)
    s5_status = classify_hetrixtools(surface_5)
    s7_status = classify_postmark_dmarc(surface_7)

    s3_detail = _format_surface_3_detail(surface_3)
    s4_detail = _format_surface_4_detail(surface_4)
    s5_detail = _format_surface_5_detail(surface_5)
    s7_detail = _format_surface_7_detail(surface_7)

    rows = "\n".join([
        _html_row("Surface 3 &mdash; Spam rate (14d)", s3_status, s3_detail),
        _html_row("Surface 4 &mdash; Suppression WoW", s4_status, s4_detail),
        _html_row("Surface 5 &mdash; Blacklist monitor", s5_status, s5_detail),
        _html_row("Surface 7 &mdash; DMARC auth health", s7_status, s7_detail),
    ])

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
       color: #1a1a1a; background: #ffffff; max-width: 640px; margin: 24px auto; padding: 0 16px; }}
h1 {{ font-size: 18px; margin: 0 0 4px 0; }}
.dateline {{ color: #6b7280; font-size: 12px; margin-bottom: 24px; }}
table {{ width: 100%; border-collapse: collapse; }}
td {{ padding: 12px 8px; border-bottom: 1px solid #e5e7eb; vertical-align: top; font-size: 14px; }}
td.label {{ width: 38%; font-weight: 600; color: #111827; }}
td.status {{ width: 24%; }}
td.detail {{ color: #4b5563; font-size: 13px; line-height: 1.4; }}
.badge {{ display: inline-block; padding: 3px 8px; border-radius: 4px; color: #fff;
         font-size: 11px; font-weight: 600; }}
.footer {{ margin-top: 32px; color: #9ca3af; font-size: 11px; line-height: 1.5; }}
code {{ font-size: 11px; background: #f3f4f6; padding: 1px 4px; border-radius: 3px; }}
a {{ color: #2563eb; text-decoration: none; }}
a:hover {{ text-decoration: underline; }}
</style>
</head>
<body>
<h1>RMP Deliverability Digest</h1>
<div class="dateline">{today_str} &middot; raisemypresence.com &middot; sender: {SENDER_DOMAIN}</div>
<table>
{rows}
</table>
<div class="footer">
Auto-generated by <code>deliverability_digest.py</code> &middot; cron <code>0 9 * * *</code> server-local (08:00 Bangkok)<br>
Thresholds per <code>email-health-audit-runbook.md</code><br>
Surfaces 1/2/8/9 not included &mdash; SQLite-derived, covered by <code>alert_on_failure.py</code> volume-floor + cron log structure<br>
Surface 5 source: HetrixTools (Pillar B-1, RMP #50). Surface 7 source: Postmark DMARC weekly digest (Pillar B-2, RMP #51). Surface 6 (Postmaster IP rep) deprecated 2026-05-26 — see module docstring.
</div>
</body>
</html>
"""


def format_plain(surface_3: dict, surface_4: dict, surface_5: dict, surface_7: dict, today_str: str) -> str:
    """Plain-text fallback for clients that don't render HTML."""
    s3_status = classify_spam_rate(surface_3)
    s4_status = classify_suppression(surface_4)
    s5_status = classify_hetrixtools(surface_5)
    s7_status = classify_postmark_dmarc(surface_7)

    s3 = f"Surface 3 -- Spam rate (14d)        : {s3_status}"
    if "spam_rate_pct" in surface_3:
        s3 += (
            f"    ({surface_3['spam_reports']} / {surface_3['delivered']} "
            f"= {surface_3['spam_rate_pct']:.4f}%)"
        )

    s4 = f"Surface 4 -- Suppression WoW growth : {s4_status}"
    if surface_4.get("wow_growth_pct") is not None:
        s4 += (
            f"    ({surface_4['wow_growth_pct']:+.1f}%, "
            f"total={surface_4.get('current_total')})"
        )
    elif surface_4.get("wow_status") == "FIRST_RUN":
        s4 += f"    (first run, baseline={surface_4.get('current_total')})"
    # Per-category composition (Scope A signal-add; verdict unchanged)
    _bd = surface_4.get("breakdown") or {}
    if _bd:
        _bw = surface_4.get("breakdown_wow")
        _order = [label for _, label in SUPPRESSION_ENDPOINTS]
        _parts = []
        for _lbl in _order:
            _curr = _bd.get(_lbl, 0)
            if _bw and _lbl in _bw:
                _d = _bw[_lbl]
                if _d["pct"] is not None:
                    _parts.append(f"{_lbl}={_curr}({_d['delta']:+d},{_d['pct']:+.1f}%)")
                else:
                    _parts.append(f"{_lbl}={_curr}({_d['delta']:+d})")
            else:
                _parts.append(f"{_lbl}={_curr}")
        s4 += "\n  composition: " + ", ".join(_parts)
        if not _bw:
            s4 += "  (per-category WoW from next 7-day baseline)"

    # Surface 5 plain-text
    s5 = f"Surface 5 -- Blacklist monitor      : {s5_status}"
    s5_st = surface_5.get("status")
    if s5_st == "NOT_CONFIGURED":
        s5 += "    (HETRIXTOOLS_API_KEY not set)"
    elif s5_st == "FAILED":
        s5 += f"    (FAILED: {surface_5.get('error', 'unknown')})"
    elif s5_st == "NO_DATA":
        s5 += "    (no monitors configured)"
    else:
        monitors = surface_5.get("monitors", [])
        if surface_5.get("any_listed"):
            s5 += "    (listings detected)"
            for m in monitors:
                if m["blacklisted_count"] > 0:
                    s5 += (
                        f"\n  - {m['target']}: {m['blacklisted_count']} RBL(s) "
                        f"[{', '.join(m['blacklisted_on'][:3])}]"
                    )
        else:
            s5 += f"    (all {len(monitors)} monitor(s) clear)"

    # Surface 7 plain-text
    s7 = f"Surface 7 -- DMARC auth health      : {s7_status}"
    s7_st = surface_7.get("status")
    if s7_st == "NOT_CONFIGURED":
        s7 += "    (Gmail credentials/token not set)"
    elif s7_st == "AUTH_FAILED":
        s7 += f"    (AUTH_FAILED: {surface_7.get('error', 'unknown')})"
    elif s7_st == "NO_DATA":
        s7 += f"    (no Postmark digest in last {POSTMARK_DMARC_LOOKBACK_DAYS}d)"
    else:
        parsed = surface_7.get("parsed", {})
        if parsed.get("parse_status") == "FAILED":
            s7 += f"    (PARSE_FAILED: {parsed.get('error', 'unknown')})"
        else:
            other_count = len(parsed.get("other_sources", []))
            your_count = len(parsed.get("your_sources", []))
            aligned_pct = parsed.get("aligned_pct", 0.0)
            age = surface_7.get("digest_age_days", 0)
            s7 += (
                f"    ({aligned_pct:.0f}% headline, {your_count} owned/{other_count} unverified, "
                f"{age}d ago)"
            )

    return f"""RMP Deliverability Digest -- {today_str}
=================================================

{s3}
{s4}
{s5}
{s7}

=================================================
Sender domain: {SENDER_DOMAIN}
Auto-generated by deliverability_digest.py
Thresholds per email-health-audit-runbook.md
Surface 5: HetrixTools (Pillar B-1, RMP #50). Surface 7: Postmark DMARC (Pillar B-2, RMP #51). Surface 6 deprecated.
"""


# ---------------------------------------------------------------------------
# Send
# ---------------------------------------------------------------------------

def send_digest(html: str, plain: str, today_str: str) -> bool:
    """Send digest via SendGrid SDK. Returns True on success."""
    try:
        message = Mail(
            from_email=(FROM_EMAIL, FROM_NAME),
            to_emails=OPERATOR_EMAIL,
            subject=f"[RMP DIGEST] {today_str}",
            plain_text_content=plain,
            html_content=html,
        )
        sg = SendGridAPIClient(SENDGRID_API_KEY)
        response = sg.send(message)
        logger.info(
            f"Digest sent to {OPERATOR_EMAIL}: sendgrid_status={response.status_code}"
        )
        return response.status_code in (200, 202)
    except Exception as e:
        logger.error(f"SendGrid send failed: {e}")
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="RMP daily deliverability digest")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Format digest and print to stdout; do not send email or update baseline",
    )
    args = parser.parse_args()

    if not SENDGRID_API_KEY:
        logger.error("SENDGRID_API_KEY not loaded from .env -- cannot proceed.")
        sys.exit(2)

    today_str = date.today().isoformat()
    logger.info(f"=== Deliverability Digest run -- {today_str} ===")

    logger.info("Collecting Surface 3 (spam rate, 14d)...")
    surface_3 = get_spam_rate_14d()
    s3_verdict = classify_spam_rate(surface_3)
    logger.info(
        f"Surface 3: verdict={s3_verdict}, "
        f"comparison_state={surface_3.get('status', 'OK')}, "
        f"spam_reports={surface_3.get('spam_reports')}, "
        f"delivered={surface_3.get('delivered')}, "
        f"spam_rate_pct={surface_3.get('spam_rate_pct')}"
    )

    logger.info("Collecting Surface 4 (suppression WoW)...")
    surface_4 = get_suppression_surface()
    s4_verdict = classify_suppression(surface_4)
    logger.info(
        f"Surface 4: verdict={s4_verdict}, "
        f"comparison_state={surface_4.get('wow_status', 'OK')}, "
        f"current_total={surface_4.get('current_total')}, "
        f"baseline={surface_4.get('baseline')}, "
        f"wow_growth_pct={surface_4.get('wow_growth_pct')}"
    )

    logger.info("Collecting Surface 5 (HetrixTools blacklist)...")
    surface_5 = get_hetrixtools_blacklist()
    s5_verdict = classify_hetrixtools(surface_5)
    logger.info(
        f"Surface 5: verdict={s5_verdict}, "
        f"comparison_state={surface_5.get('status', 'OK')}, "
        f"total_records={surface_5.get('total_records')}, "
        f"any_listed={surface_5.get('any_listed')}"
    )

    logger.info("Collecting Surface 7 (Postmark DMARC digest)...")
    surface_7 = get_postmark_dmarc_digest()
    # Parse HTML body if digest was retrieved successfully
    if "html" in surface_7:
        surface_7["parsed"] = parse_postmark_dmarc_html(surface_7["html"])
    s7_verdict = classify_postmark_dmarc(surface_7)
    parsed = surface_7.get("parsed", {})
    logger.info(
        f"Surface 7: verdict={s7_verdict}, "
        f"comparison_state={surface_7.get('status', 'OK')}, "
        f"aligned_pct={parsed.get('aligned_pct')}, "
        f"other_sources_count={len(parsed.get('other_sources', []))}, "
        f"your_sources_count={len(parsed.get('your_sources', []))}, "
        f"digest_age_days={surface_7.get('digest_age_days')}"
    )

    html = format_html(surface_3, surface_4, surface_5, surface_7, today_str)
    plain = format_plain(surface_3, surface_4, surface_5, surface_7, today_str)

    if args.dry_run:
        logger.info("--dry-run mode: printing digest to stdout, not sending.")
        print("\n--- PLAIN TEXT ---\n")
        print(plain)
        print("\n--- HTML ---\n")
        print(html)
        sys.exit(0)

    success = send_digest(html, plain, today_str)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
