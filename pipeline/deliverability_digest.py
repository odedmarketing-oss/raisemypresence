#!/usr/bin/env python3
"""
deliverability_digest.py
Raise My Presence — Daily Deliverability Digest

Surfaces (per Local-Presence-Optimization/email-health-audit-runbook.md):
  3. SendGrid spam complaint rate (14d rolling)
  4. Suppression list WoW growth (SendGrid suppression endpoints vs state-file baseline)
  5. HetrixTools blacklist monitor — domain blacklist status across ~23 RBLs

Surface 6 (Google Postmaster IP reputation) deprecated RMP #50 (2026-05-26):
Postmaster Tools v1 reputation API end-of-life'd by Google with no v2 replacement
for domain/IP reputation dashboards. Pillar B-1 replaces Surface 5 with HetrixTools
blacklist monitor (free tier, IP + domain monitoring against ~23 RBLs); Pillar B-2
(future) will add Surface 7 (Postmark DMARC authentication health).

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

Exit codes:
  0  digest sent successfully (or --dry-run completed)
  1  digest send failed (SendGrid error)
  2  fatal env error (SENDGRID_API_KEY missing)

Created RMP #46, 2026-05-23.
Surface 5 migrated Postmaster v1 → HetrixTools RMP #50, 2026-05-26 (Pillar B-1).
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


def format_html(surface_3: dict, surface_4: dict, surface_5: dict, today_str: str) -> str:
    s3_status = classify_spam_rate(surface_3)
    s4_status = classify_suppression(surface_4)
    s5_status = classify_hetrixtools(surface_5)

    s3_detail = _format_surface_3_detail(surface_3)
    s4_detail = _format_surface_4_detail(surface_4)
    s5_detail = _format_surface_5_detail(surface_5)

    rows = "\n".join([
        _html_row("Surface 3 &mdash; Spam rate (14d)", s3_status, s3_detail),
        _html_row("Surface 4 &mdash; Suppression WoW", s4_status, s4_detail),
        _html_row("Surface 5 &mdash; Blacklist monitor", s5_status, s5_detail),
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
Surface 5 source: HetrixTools (Pillar B-1, RMP #50). Surface 6 (Postmaster IP rep) deprecated 2026-05-26 — see module docstring.
</div>
</body>
</html>
"""


def format_plain(surface_3: dict, surface_4: dict, surface_5: dict, today_str: str) -> str:
    """Plain-text fallback for clients that don't render HTML."""
    s3_status = classify_spam_rate(surface_3)
    s4_status = classify_suppression(surface_4)
    s5_status = classify_hetrixtools(surface_5)

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

    return f"""RMP Deliverability Digest -- {today_str}
=================================================

{s3}
{s4}
{s5}

=================================================
Sender domain: {SENDER_DOMAIN}
Auto-generated by deliverability_digest.py
Thresholds per email-health-audit-runbook.md
Surface 5: HetrixTools (Pillar B-1, RMP #50). Surface 6 deprecated.
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

    logger.info("Collecting Surface 5 (HetrixTools blacklist)...")
    surface_5 = get_hetrixtools_blacklist()
    logger.info(
        f"Surface 5: status={surface_5.get('status', 'OK')}, "
        f"total_records={surface_5.get('total_records')}, "
        f"any_listed={surface_5.get('any_listed')}"
    )

    html = format_html(surface_3, surface_4, surface_5, today_str)
    plain = format_plain(surface_3, surface_4, surface_5, today_str)

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
