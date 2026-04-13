"""
pipeline.py
Raise My Presence — Outreach Pipeline Orchestrator

Full automated pipeline:
  scan JSON → filter (<threshold) → dedup → suppression check →
  website discovery → email scrape → validate → generate report →
  send via SendGrid → log

CLI usage:
  python pipeline.py --scan-file /root/audit-scanner/scan_results_2026-04-12.json
  python pipeline.py --scan-file scan_results.json --dry-run
  python pipeline.py --scan-file scan_results.json --no-dry-run --cap 5

All safety controls enforced:
  - 24h scan-to-send delay (configurable)
  - Daily send cap (default 20)
  - Deduplication by place_id + email
  - Suppression list (bounces + unsubscribes)
  - DRY_RUN default (must explicitly disable)
  - MX validation before send
"""

import os
import sys
import json
import time
import logging
import argparse
from datetime import datetime, timezone
from pathlib import Path

from config import (
    SCORE_THRESHOLD, DAILY_SEND_CAP, DRY_RUN,
    SCAN_TO_SEND_DELAY_HOURS, DISCOVERY_RATE_LIMIT,
)
from send_log import already_sent, today_send_count, log_send
from suppression import is_suppressed
from website_discoverer import discover_website
from email_extractor import extract_emails
from email_validator import validate_email
from emailer import send_report
from report_generator import generate_report, recompute_score

logger = logging.getLogger("pipeline")


# ---------------------------------------------------------------------------
# Scan file loader
# ---------------------------------------------------------------------------

def load_scan(scan_path: str) -> list[dict]:
    """Load and parse scan JSON file."""
    path = Path(scan_path)
    if not path.exists():
        logger.error(f"Scan file not found: {scan_path}")
        sys.exit(1)

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    # Scanner outputs either a list or a dict with a "results" key
    if isinstance(data, list):
        return data
    elif isinstance(data, dict) and "results" in data:
        return data["results"]
    else:
        logger.error(f"Unexpected scan file format: {type(data)}")
        sys.exit(1)


def check_scan_delay(scan_path: str, delay_hours: int) -> bool:
    """
    Enforce minimum delay between scan file creation and pipeline execution.
    Returns True if enough time has passed.
    """
    path = Path(scan_path)
    mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    now = datetime.now(timezone.utc)
    elapsed_hours = (now - mtime).total_seconds() / 3600

    if elapsed_hours < delay_hours:
        remaining = delay_hours - elapsed_hours
        logger.warning(
            f"Scan-to-send delay not met. "
            f"File age: {elapsed_hours:.1f}h, required: {delay_hours}h, "
            f"remaining: {remaining:.1f}h"
        )
        return False
    return True


# ---------------------------------------------------------------------------
# Filter
# ---------------------------------------------------------------------------

def filter_businesses(businesses: list[dict], threshold: int) -> list[dict]:
    """Filter to businesses below score threshold."""
    filtered = []
    for biz in businesses:
        breakdown = biz.get("score_breakdown", {})
        score = recompute_score(breakdown)
        if score < threshold:
            filtered.append(biz)
    logger.info(
        f"Filtered: {len(filtered)}/{len(businesses)} businesses "
        f"below score threshold {threshold}"
    )
    return filtered


# ---------------------------------------------------------------------------
# Per-business processing
# ---------------------------------------------------------------------------

def process_business(business: dict, dry_run: bool) -> dict:
    """
    Process a single business through the full pipeline.

    Returns a result dict with status and details.
    """
    name = business.get("name", "unknown")
    place_id = business.get("place_id", "")
    breakdown = business.get("score_breakdown", {})
    score = recompute_score(breakdown)

    result = {
        "name": name,
        "place_id": place_id,
        "score": score,
        "status": "pending",
        "email": None,
        "website": None,
        "skip_reason": None,
    }

    # --- Step 1: Website discovery ---
    website = discover_website(business)
    if not website:
        result["status"] = "skipped"
        result["skip_reason"] = "no_website"
        return result
    result["website"] = website

    # Rate limit between discovery and extraction
    time.sleep(DISCOVERY_RATE_LIMIT)

    # --- Step 2: Email extraction ---
    emails = extract_emails(website)
    if not emails:
        result["status"] = "skipped"
        result["skip_reason"] = "no_email_found"
        return result

    # --- Step 3: Find first valid, non-suppressed, non-duplicate email ---
    target_email = None
    for email in emails:
        # Validate syntax + MX
        is_valid, reason = validate_email(email)
        if not is_valid:
            logger.debug(f"  Email invalid ({reason}): {email}")
            continue

        # Check suppression
        if is_suppressed(email):
            logger.debug(f"  Email suppressed: {email}")
            continue

        # Check dedup
        if already_sent(place_id, email):
            logger.debug(f"  Already sent to: {email}")
            continue

        target_email = email
        break

    if not target_email:
        result["status"] = "skipped"
        result["skip_reason"] = "no_valid_email"
        return result

    result["email"] = target_email

    # --- Step 4: Generate report ---
    html_report = generate_report(business, recipient_email=target_email)

    # --- Step 5: Send ---
    send_result = send_report(
        recipient_email=target_email,
        html_body=html_report,
        business_name=name,
        score=score,
        dry_run=dry_run,
    )

    if send_result["success"]:
        # --- Step 6: Log ---
        log_send(
            place_id=place_id,
            email=target_email,
            subject=f"Score {score}/100",
            score=score,
            dry_run=dry_run,
            status="sent",
        )
        result["status"] = "sent"
    else:
        result["status"] = "send_failed"
        result["skip_reason"] = send_result.get("error", "unknown")

    return result


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def run_pipeline(
    scan_path: str,
    dry_run: bool = True,
    cap: int = None,
    skip_delay_check: bool = False,
    threshold: int = None,
) -> dict:
    """
    Execute the full outreach pipeline.

    Args:
        scan_path: Path to scan JSON file.
        dry_run: If True, redirect all emails to operator inbox.
        cap: Override daily send cap. None = use config.
        skip_delay_check: Bypass 24h scan-to-send delay.
        threshold: Override score threshold. None = use config.

    Returns:
        Summary dict with counts and per-business results.
    """
    send_cap = cap if cap is not None else DAILY_SEND_CAP
    score_threshold = threshold if threshold is not None else SCORE_THRESHOLD

    logger.info("=" * 60)
    logger.info("RAISE MY PRESENCE — OUTREACH PIPELINE")
    logger.info(f"  Mode:      {'DRY RUN' if dry_run else 'LIVE'}")
    logger.info(f"  Scan file: {scan_path}")
    logger.info(f"  Threshold: <{score_threshold}")
    logger.info(f"  Daily cap: {send_cap}")
    logger.info("=" * 60)

    # --- Delay check ---
    if not skip_delay_check:
        if not check_scan_delay(scan_path, SCAN_TO_SEND_DELAY_HOURS):
            logger.error("Pipeline aborted — scan-to-send delay not met.")
            return {"aborted": True, "reason": "scan_delay"}

    # --- Load and filter ---
    businesses = load_scan(scan_path)
    targets = filter_businesses(businesses, score_threshold)

    if not targets:
        logger.info("No businesses below threshold. Nothing to do.")
        return {"aborted": False, "total": len(businesses), "targets": 0, "results": []}

    # --- Check daily cap headroom ---
    already_sent_today = today_send_count(dry_run=dry_run)
    remaining_cap = max(0, send_cap - already_sent_today)
    logger.info(f"Daily cap: {already_sent_today}/{send_cap} used, {remaining_cap} remaining")

    if remaining_cap == 0:
        logger.warning("Daily send cap reached. Pipeline complete.")
        return {
            "aborted": False,
            "total": len(businesses),
            "targets": len(targets),
            "cap_reached": True,
            "results": [],
        }

    # --- Process businesses ---
    results = []
    sent_count = 0

    for i, biz in enumerate(targets):
        if sent_count >= remaining_cap:
            logger.info(f"Daily cap reached after {sent_count} sends. Stopping.")
            break

        name = biz.get("name", "unknown")
        logger.info(f"\n[{i+1}/{len(targets)}] Processing: {name}")

        result = process_business(biz, dry_run=dry_run)
        results.append(result)

        if result["status"] == "sent":
            sent_count += 1
            logger.info(f"  ✓ Sent to {result['email']} (score: {result['score']})")
        elif result["status"] == "skipped":
            logger.info(f"  ✗ Skipped: {result['skip_reason']}")
        elif result["status"] == "send_failed":
            logger.warning(f"  ✗ Send failed: {result['skip_reason']}")

    # --- Summary ---
    summary = {
        "aborted": False,
        "dry_run": dry_run,
        "total_scanned": len(businesses),
        "targets_below_threshold": len(targets),
        "processed": len(results),
        "sent": sent_count,
        "skipped_no_website": sum(1 for r in results if r.get("skip_reason") == "no_website"),
        "skipped_no_email": sum(1 for r in results if r.get("skip_reason") == "no_email_found"),
        "skipped_no_valid_email": sum(1 for r in results if r.get("skip_reason") == "no_valid_email"),
        "send_failed": sum(1 for r in results if r["status"] == "send_failed"),
        "results": results,
    }

    logger.info("\n" + "=" * 60)
    logger.info("PIPELINE COMPLETE")
    logger.info(f"  Total scanned:    {summary['total_scanned']}")
    logger.info(f"  Below threshold:  {summary['targets_below_threshold']}")
    logger.info(f"  Processed:        {summary['processed']}")
    logger.info(f"  Sent:             {summary['sent']}")
    logger.info(f"  No website:       {summary['skipped_no_website']}")
    logger.info(f"  No email:         {summary['skipped_no_email']}")
    logger.info(f"  No valid email:   {summary['skipped_no_valid_email']}")
    logger.info(f"  Send failed:      {summary['send_failed']}")
    logger.info("=" * 60)

    return summary


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Raise My Presence — Outreach Pipeline"
    )
    parser.add_argument(
        "--scan-file", required=True,
        help="Path to scan JSON file"
    )
    parser.add_argument(
        "--dry-run", action="store_true", default=None,
        help="Force dry-run mode (send to operator inbox)"
    )
    parser.add_argument(
        "--no-dry-run", action="store_true",
        help="Disable dry-run mode (send to real businesses)"
    )
    parser.add_argument(
        "--cap", type=int, default=None,
        help=f"Override daily send cap (default: {DAILY_SEND_CAP})"
    )
    parser.add_argument(
        "--threshold", type=int, default=None,
        help=f"Override score threshold (default: {SCORE_THRESHOLD})"
    )
    parser.add_argument(
        "--skip-delay", action="store_true",
        help="Skip 24h scan-to-send delay check"
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Enable debug logging"
    )

    args = parser.parse_args()

    # --- Logging setup ---
    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # --- Resolve dry-run ---
    if args.no_dry_run:
        dry_run = False
    elif args.dry_run:
        dry_run = True
    else:
        dry_run = DRY_RUN  # from config / env

    if not dry_run:
        logger.warning("⚠️  LIVE MODE — emails will be sent to real businesses")
        response = input("Type 'confirm' to proceed: ")
        if response.strip().lower() != "confirm":
            logger.info("Aborted by user.")
            sys.exit(0)

    # --- Run ---
    summary = run_pipeline(
        scan_path=args.scan_file,
        dry_run=dry_run,
        cap=args.cap,
        skip_delay_check=args.skip_delay,
        threshold=args.threshold,
    )

    # Write summary to JSON
    summary_path = Path(args.scan_file).parent / "pipeline_run.json"
    # Remove per-business results for the summary file (too large)
    summary_export = {k: v for k, v in summary.items() if k != "results"}
    summary_export["run_at"] = datetime.now(timezone.utc).isoformat()
    summary_export["scan_file"] = args.scan_file

    with open(summary_path, "w") as f:
        json.dump(summary_export, f, indent=2)
    logger.info(f"Summary written to {summary_path}")


if __name__ == "__main__":
    main()
