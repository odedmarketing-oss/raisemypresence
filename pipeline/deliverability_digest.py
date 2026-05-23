#!/usr/bin/env python3
"""
deliverability_digest.py
Raise My Presence — Daily Deliverability Digest

Surfaces (per Local-Presence-Optimization/email-health-audit-runbook.md):
  3. SendGrid spam complaint rate (14d rolling)
  4. Suppression list WoW growth (SendGrid suppression endpoints vs state-file baseline)
  5. Google Postmaster domain reputation — graceful degradation if GCP creds absent
  6. Google Postmaster IP reputation — graceful degradation if GCP creds absent

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

Optional dependency for Surfaces 5/6 (lazy import; graceful if missing):
  - google-auth (Postmaster Tools API service-account auth)

Exit codes:
  0  digest sent successfully (or --dry-run completed)
  1  digest send failed (SendGrid error)
  2  fatal env error (SENDGRID_API_KEY missing)

Created RMP #46, 2026-05-23.
"""

import argparse
import logging
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail

# Load .env from pipeline directory (same pattern as config.py / alert_on_failure.py)
PIPELINE_DIR = Path(__file__).parent
load_dotenv(PIPELINE_DIR / ".env", override=True)

# --- Config ---
SENDGRID_API_KEY = os.environ.get("SENDGRID_API_KEY", "")
GCP_POSTMASTER_KEY_PATH = os.environ.get("GCP_POSTMASTER_KEY_PATH", "")

OPERATOR_EMAIL = "odedmarketing@gmail.com"
FROM_EMAIL = "hello@mail.raisemypresence.com"
FROM_NAME = "RMP Deliverability Monitor"

# Postmaster monitors the sending domain (where DKIM signs), not the apex
POSTMASTER_DOMAIN = "mail.raisemypresence.com"

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

# Status labels
STATUS_HEALTHY = "HEALTHY"
STATUS_CONCERNING = "CONCERNING"
STATUS_BAD = "BAD"
STATUS_UNKNOWN = "UNKNOWN"
STATUS_NOT_CONFIGURED = "AUTH NOT CONFIGURED"

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
        line = SUPPRESSION_BASELINE_FILE.read_text().strip()
        parts = line.split()
        if len(parts) != 2:
            return None
        return {"date": parts[0], "count": int(parts[1])}
    except Exception as e:
        logger.warning(f"Failed to parse suppression baseline file: {e}")
        return None


def _write_baseline(count: int) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()
    SUPPRESSION_BASELINE_FILE.write_text(f"{today} {count}\n")


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
            _write_baseline(total)
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
# Surfaces 5 + 6 — Google Postmaster Tools (domain + IP reputation)
#   Graceful degradation chain:
#     1. GCP_POSTMASTER_KEY_PATH not set     -> NOT_CONFIGURED placeholder
#     2. service account JSON file missing   -> NOT_CONFIGURED placeholder
#     3. google-auth library not installed   -> AUTH_LIB_MISSING placeholder
#     4. API call fails (quota/scope/net)    -> FAILED placeholder with error
#     5. API returns no traffic stats        -> NO_DATA placeholder
# ---------------------------------------------------------------------------

def get_postmaster_reputation() -> dict:
    """
    Postmaster Tools API: /v1/domains/{domain}/trafficStats

    Returns on success:
        {"domain_rep": str, "ip_rep": str, "raw_latest": dict}
    Returns on graceful degradation or failure:
        {"status": "NOT_CONFIGURED" | "AUTH_LIB_MISSING" | "FAILED" | "NO_DATA",
         "domain_rep": placeholder, "ip_rep": placeholder, ...}
    """
    if not GCP_POSTMASTER_KEY_PATH:
        logger.info("Surfaces 5/6 — GCP_POSTMASTER_KEY_PATH not set; graceful degradation.")
        return {
            "status": "NOT_CONFIGURED",
            "domain_rep": STATUS_NOT_CONFIGURED,
            "ip_rep": STATUS_NOT_CONFIGURED,
        }

    if not Path(GCP_POSTMASTER_KEY_PATH).exists():
        logger.warning(f"Surfaces 5/6 — service account file not found: {GCP_POSTMASTER_KEY_PATH}")
        return {
            "status": "NOT_CONFIGURED",
            "domain_rep": STATUS_NOT_CONFIGURED,
            "ip_rep": STATUS_NOT_CONFIGURED,
        }

    try:
        from google.oauth2 import service_account
        from google.auth.transport.requests import Request as GAuthRequest
    except ImportError:
        logger.warning(
            "Surfaces 5/6 — google-auth library not installed. "
            "Install on Tencent: pip install google-auth"
        )
        return {
            "status": "AUTH_LIB_MISSING",
            "domain_rep": STATUS_NOT_CONFIGURED,
            "ip_rep": STATUS_NOT_CONFIGURED,
        }

    try:
        scopes = ["https://www.googleapis.com/auth/postmaster.readonly"]
        creds = service_account.Credentials.from_service_account_file(
            GCP_POSTMASTER_KEY_PATH, scopes=scopes
        )
        creds.refresh(GAuthRequest())
        token = creds.token

        # Last 7 days; latest entry is the most relevant signal
        url = f"https://gmailpostmastertools.googleapis.com/v1/domains/{POSTMASTER_DOMAIN}/trafficStats"
        headers = {"Authorization": f"Bearer {token}"}
        end_d = date.today() - timedelta(days=1)
        start_d = end_d - timedelta(days=7)
        params = {
            "startDate.year": start_d.year,
            "startDate.month": start_d.month,
            "startDate.day": start_d.day,
            "endDate.year": end_d.year,
            "endDate.month": end_d.month,
            "endDate.day": end_d.day,
        }
        r = requests.get(url, headers=headers, params=params, timeout=15)
        r.raise_for_status()
        stats = r.json().get("trafficStats", [])
        if not stats:
            logger.info("Postmaster API returned no traffic stats (volume may be too low).")
            return {
                "status": "NO_DATA",
                "domain_rep": "NO_DATA",
                "ip_rep": "NO_DATA",
            }

        latest = stats[-1]
        domain_rep = latest.get("domainReputation", "UNKNOWN")

        # ipReputations is a list of buckets per reputation level + IP count.
        # Take the bucket with the highest ipCount as the headline reputation.
        ip_reps = latest.get("ipReputations", [])
        if ip_reps:
            ip_reps_sorted = sorted(ip_reps, key=lambda x: x.get("ipCount", 0), reverse=True)
            ip_rep = ip_reps_sorted[0].get("reputation", "UNKNOWN")
        else:
            ip_rep = "UNKNOWN"

        return {
            "domain_rep": domain_rep,
            "ip_rep": ip_rep,
            "raw_latest": latest,
        }
    except Exception as e:
        logger.error(f"Surfaces 5/6 — Postmaster API call failed: {e}")
        return {
            "status": "FAILED",
            "error": str(e),
            "domain_rep": STATUS_UNKNOWN,
            "ip_rep": STATUS_UNKNOWN,
        }


def classify_postmaster_rep(rep_value: str) -> str:
    if rep_value == STATUS_NOT_CONFIGURED:
        return STATUS_NOT_CONFIGURED
    if rep_value in (STATUS_UNKNOWN, "UNKNOWN", "NO_DATA"):
        return STATUS_UNKNOWN
    if rep_value in ("HIGH", "MEDIUM"):
        return STATUS_HEALTHY
    if rep_value == "LOW":
        return STATUS_CONCERNING
    if rep_value == "BAD":
        return STATUS_BAD
    return STATUS_UNKNOWN


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


def _format_surface_4_detail(s4: dict) -> str:
    if s4.get("status") == "FAILED":
        return f"FAILED &mdash; {s4.get('error', 'unknown')}"
    wow = s4.get("wow_status")
    if wow == "FIRST_RUN":
        return (
            f"First run &mdash; baseline established at {s4['current_total']} suppressions. "
            f"Comparison delta builds daily; full WoW delta at day 7."
        )
    if wow == "BASELINE_ZERO":
        return f"Current = {s4['current_total']}; prior baseline was 0."
    prior = s4["baseline"]["count"]
    curr = s4["current_total"]
    growth = s4["wow_growth_pct"]
    age_days = s4.get("baseline_age_days")
    age_str = f" ({age_days}d ago)" if age_days is not None else ""
    return (
        f"{curr} today vs {prior} on {s4['baseline']['date']}{age_str} "
        f"({curr - prior:+d}, {growth:+.1f}%)"
    )


def _format_postmaster_detail(pm: dict) -> tuple:
    """Return (domain_detail_html, ip_detail_html)."""
    status = pm.get("status")
    if status == "NOT_CONFIGURED":
        msg = (
            "GCP service account for Postmaster Tools not configured on Tencent. "
            "Drop service-account JSON to <code>/root/audit-scanner/credentials/</code> "
            "and set <code>GCP_POSTMASTER_KEY_PATH</code> in pipeline <code>.env</code> to enable."
        )
        return msg, msg
    if status == "AUTH_LIB_MISSING":
        msg = (
            "<code>google-auth</code> library missing on Tencent. "
            "Install: <code>pip install google-auth</code>"
        )
        return msg, msg
    if status == "FAILED":
        msg = f"FAILED &mdash; {pm.get('error', 'unknown')}"
        return msg, msg
    if status == "NO_DATA":
        msg = "Postmaster API returned no traffic stats (volume may be too low for reputation scoring yet)."
        return msg, msg
    return (
        f"Domain reputation: <strong>{pm.get('domain_rep', 'UNKNOWN')}</strong>",
        f"IP reputation: <strong>{pm.get('ip_rep', 'UNKNOWN')}</strong>",
    )


def format_html(surface_3: dict, surface_4: dict, postmaster: dict, today_str: str) -> str:
    s3_status = classify_spam_rate(surface_3)
    s4_status = classify_suppression(surface_4)
    s5_status = classify_postmaster_rep(postmaster.get("domain_rep", STATUS_UNKNOWN))
    s6_status = classify_postmaster_rep(postmaster.get("ip_rep", STATUS_UNKNOWN))

    s3_detail = _format_surface_3_detail(surface_3)
    s4_detail = _format_surface_4_detail(surface_4)
    s5_detail, s6_detail = _format_postmaster_detail(postmaster)

    rows = "\n".join([
        _html_row("Surface 3 &mdash; Spam rate (14d)", s3_status, s3_detail),
        _html_row("Surface 4 &mdash; Suppression WoW", s4_status, s4_detail),
        _html_row("Surface 5 &mdash; Domain reputation", s5_status, s5_detail),
        _html_row("Surface 6 &mdash; IP reputation", s6_status, s6_detail),
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
</style>
</head>
<body>
<h1>RMP Deliverability Digest</h1>
<div class="dateline">{today_str} &middot; raisemypresence.com &middot; sender: {POSTMASTER_DOMAIN}</div>
<table>
{rows}
</table>
<div class="footer">
Auto-generated by <code>deliverability_digest.py</code> &middot; cron <code>0 9 * * *</code> server-local (08:00 Bangkok)<br>
Thresholds per <code>email-health-audit-runbook.md</code><br>
Surfaces 1/2/8/9 not included &mdash; SQLite-derived, covered by <code>alert_on_failure.py</code> volume-floor + cron log structure
</div>
</body>
</html>
"""


def format_plain(surface_3: dict, surface_4: dict, postmaster: dict, today_str: str) -> str:
    """Plain-text fallback for clients that don't render HTML."""
    s3_status = classify_spam_rate(surface_3)
    s4_status = classify_suppression(surface_4)
    s5_status = classify_postmaster_rep(postmaster.get("domain_rep", STATUS_UNKNOWN))
    s6_status = classify_postmaster_rep(postmaster.get("ip_rep", STATUS_UNKNOWN))

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

    s5 = f"Surface 5 -- Domain reputation      : {s5_status}    ({postmaster.get('domain_rep', '?')})"
    s6 = f"Surface 6 -- IP reputation          : {s6_status}    ({postmaster.get('ip_rep', '?')})"

    return f"""RMP Deliverability Digest -- {today_str}
=================================================

{s3}
{s4}
{s5}
{s6}

=================================================
Sender domain: {POSTMASTER_DOMAIN}
Auto-generated by deliverability_digest.py
Thresholds per email-health-audit-runbook.md
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
    logger.info(f"Surface 3: {surface_3}")

    logger.info("Collecting Surface 4 (suppression WoW)...")
    surface_4 = get_suppression_surface()
    logger.info(
        f"Surface 4: current_total={surface_4.get('current_total')}, "
        f"baseline={surface_4.get('baseline')}, "
        f"wow_status={surface_4.get('wow_status')}, "
        f"wow_growth_pct={surface_4.get('wow_growth_pct')}"
    )

    logger.info("Collecting Surfaces 5/6 (Postmaster)...")
    postmaster = get_postmaster_reputation()
    logger.info(f"Postmaster: status={postmaster.get('status', 'OK')}")

    html = format_html(surface_3, surface_4, postmaster, today_str)
    plain = format_plain(surface_3, surface_4, postmaster, today_str)

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
