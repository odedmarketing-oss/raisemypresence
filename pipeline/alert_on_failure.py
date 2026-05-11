#!/usr/bin/env python3
"""
alert_on_failure.py
Raise My Presence — Pipeline Alerting (failure + volume-floor modes)

Modes:
  failure       — Called by run_market.sh ONLY on hard failures (exit != 0
                  OR pipeline aborted). Default mode for backwards compat.
  volume-floor  — Called by daily cron entry in /etc/cron.d/rmp-pipeline.
                  Queries sent_log; alerts if N-day send volume for a market
                  falls below a percentage of (cap × days).

CLI usage (failure mode, default — backwards-compat with run_market.sh):
  python3 alert_on_failure.py \\
    --market uk \\
    --exit-code 1 \\
    --aborted 0 \\
    --log-file /var/log/rmp-cron/uk-2026-04-22.log

CLI usage (volume-floor mode):
  python3 alert_on_failure.py \\
    --mode volume-floor \\
    --market us \\
    --cap 15

Dependencies:
  - sendgrid (declared in pipeline/requirements.txt)
  - python-dotenv (declared in pipeline/requirements.txt)
  - sqlite3 (stdlib)

Exit codes:
  0  success (alert dispatched if needed, or no alert needed)
  1  alert dispatch failed (missing API key, SendGrid error, query error, etc.)
"""

import argparse
import json
import logging
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

# Load .env from the pipeline directory (same pattern as config.py)
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env")

from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail


# --- Config ---
ALERT_RECIPIENT = "odedmarketing@gmail.com"
ALERT_SENDER = "hello@mail.raisemypresence.com"
ALERT_SENDER_NAME = "RMP Pipeline Monitor"
LOG_TAIL_LINES = 20

# Server-local hour bands per market — sent_log stores UTC ISO 8601;
# queries convert UTC→server-local (+8h) before hour extraction. Cron
# schedules in /etc/cron.d/rmp-pipeline are also server-local.
# Bug fix RMP #29 (2026-05-11): pre-fix queries used raw UTC strftime,
# which missed all sends (US 13:00 server local = 05:00 UTC = outside
# the 12-14 band). Always wrap sent_at as datetime(sent_at, '+8 hours')
# before strftime('%H', ...) extraction.
MARKET_HOUR_BANDS = {
    "uk": (7, 9),    # 08:00 server local fire → 07-09 band
    "us": (12, 14),  # 13:00 server local fire → 12-14 band
    "au": (21, 23),  # 22:00 server local fire → 21-23 band
}

# Volume-floor defaults (operator override via CLI flags)
DEFAULT_VOLUME_FLOOR_DAYS = 7
DEFAULT_VOLUME_FLOOR_PCT = 50
PIPELINE_DB_PATH = "/root/audit-scanner/pipeline/pipeline.db"

logger = logging.getLogger("alert_on_failure")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


# --- Shared helpers ---

def dispatch_alert(subject: str, body: str) -> int:
    """Send alert via SendGrid. Returns 0 on success, 1 on failure."""
    api_key = os.getenv("SENDGRID_API_KEY")
    if not api_key:
        logger.error("SENDGRID_API_KEY not set in .env — cannot dispatch alert")
        return 1
    try:
        message = Mail(
            from_email=(ALERT_SENDER, ALERT_SENDER_NAME),
            to_emails=ALERT_RECIPIENT,
            subject=subject,
            plain_text_content=body,
        )
        sg = SendGridAPIClient(api_key)
        response = sg.send(message)
        logger.info(
            f"Alert dispatched: subject={subject!r}, "
            f"sendgrid_status={response.status_code}"
        )
        return 0
    except Exception as e:
        logger.error(f"Failed to dispatch alert: {e}")
        return 1


# --- Failure mode ---

def tail_log(log_path: str, n: int = LOG_TAIL_LINES) -> str:
    """Read last N lines of the log file. Returns placeholder on error."""
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        tail = lines[-n:] if len(lines) > n else lines
        return "".join(tail)
    except FileNotFoundError:
        return f"[Log file not found: {log_path}]"
    except Exception as e:
        return f"[Error reading log: {e}]"


def run_failure_mode(market: str, exit_code: int, aborted: int, log_file: str) -> int:
    """Build + dispatch failure alert. Returns 0 on success, 1 on failure."""
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    if aborted == 1:
        failure_kind = "pipeline aborted (scan-delay or runtime abort)"
    elif exit_code != 0:
        failure_kind = f"non-zero exit code ({exit_code})"
    else:
        failure_kind = "unknown (alert triggered without clear cause)"

    subject = f"[RMP ALERT] {market.upper()} pipeline failed (exit {exit_code})"
    log_tail = tail_log(log_file)

    body = f"""RMP Pipeline Failure Alert
{'=' * 50}

Market:       {market.upper()}
Failure kind: {failure_kind}
Exit code:    {exit_code}
Aborted flag: {aborted}
Timestamp:    {now_utc}
Log file:     {log_file}

Last {LOG_TAIL_LINES} lines of log:
{'=' * 50}
{log_tail}
{'=' * 50}

Triage steps:
1. SSH to Tencent: ssh root@43.134.33.213
2. View full log:   cat {log_file}
3. Check pm2:       pm2 list
4. Check pipeline DB:
   sqlite3 /root/audit-scanner/pipeline/pipeline.db 'SELECT * FROM sent_log ORDER BY sent_at DESC LIMIT 10;'

Dispatched by alert_on_failure.py (failure mode),
called from /root/audit-scanner/pipeline/run_market.sh.
"""
    return dispatch_alert(subject, body)


# --- Volume-floor mode ---

def query_market_sends_window(market: str, days: int) -> tuple[int, list[tuple[str, int]]]:
    """
    Query sent_log for sends in the last N days for a market.
    Returns (total_count, daily_breakdown) where daily_breakdown is [(date, count), ...].
    Locale is inferred from sent_at hour band (sent_log has no locale column).
    """
    band = MARKET_HOUR_BANDS.get(market)
    if not band:
        raise ValueError(
            f"Unknown market: {market}. Known: {list(MARKET_HOUR_BANDS.keys())}"
        )
    h_lo, h_hi = band

    conn = sqlite3.connect(PIPELINE_DB_PATH)
    try:
        cur = conn.cursor()

        # Total count over window
        # Hour band is server-local; sent_at stored UTC → wrap with +8h
        cur.execute(
            f"""
            SELECT COUNT(*) FROM sent_log
            WHERE sent_at >= datetime('now', '-{int(days)} days')
              AND CAST(strftime('%H', datetime(sent_at, '+8 hours')) AS INT) BETWEEN ? AND ?
            """,
            (h_lo, h_hi),
        )
        total = cur.fetchone()[0]

        # Daily breakdown (most recent first)
        # DATE + hour band both server-local; sent_at stored UTC → wrap with +8h
        cur.execute(
            f"""
            SELECT DATE(datetime(sent_at, '+8 hours')) AS day, COUNT(*) AS sends
            FROM sent_log
            WHERE sent_at >= datetime('now', '-{int(days)} days')
              AND CAST(strftime('%H', datetime(sent_at, '+8 hours')) AS INT) BETWEEN ? AND ?
            GROUP BY day
            ORDER BY day DESC
            """,
            (h_lo, h_hi),
        )
        daily = cur.fetchall()
    finally:
        conn.close()

    return total, daily


def run_volume_floor_mode(market: str, cap: int, days: int, floor_pct: int) -> int:
    """
    Check rolling N-day send volume for a market against a floor.
    Returns 0 if no alert needed OR alert dispatched successfully, 1 on dispatch failure.
    """
    expected = cap * days
    floor = expected * floor_pct // 100

    try:
        actual, daily = query_market_sends_window(market, days)
    except Exception as e:
        logger.error(f"Volume-floor query failed for {market}: {e}")
        return 1

    logger.info(
        f"Volume-floor check: market={market} actual={actual} floor={floor} "
        f"expected={expected} window={days}d"
    )

    if actual >= floor:
        logger.info("Volume above floor — no alert needed.")
        return 0

    # Below floor — dispatch alert
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    daily_lines = (
        "\n".join(f"  {d}: {n}" for d, n in daily) if daily else "  (no sends in window)"
    )

    subject = (
        f"[RMP ALERT] {market.upper()} {days}-day send volume below floor "
        f"({actual} / {floor})"
    )

    body = f"""RMP Volume-Floor Alert
{'=' * 50}

Market:        {market.upper()}
Window:        {days} days (rolling)
Daily cap:     {cap}
Expected:      {expected} (cap × days)
Floor:         {floor} ({floor_pct}% of expected)
Actual:        {actual}
Shortfall:     {floor - actual} sends below floor
Timestamp:     {now_utc}

Daily breakdown ({days}-day window):
{daily_lines}

Likely causes:
- Inventory exhaustion (no fresh candidates with extractable emails)
- Discovery cascade failure (websites/emails unreachable for candidate band)
- Cron silently failing (verify /var/log/rmp-cron/{market}-*.log)
- Suppression list saturation

Triage steps:
1. SSH to Tencent: ssh root@43.134.33.213
2. Check today's cron log: cat /var/log/rmp-cron/{market}-$(date -u +%Y-%m-%d).log
3. Check recent sends:
   sqlite3 /root/audit-scanner/pipeline/pipeline.db \\
     'SELECT * FROM sent_log ORDER BY sent_at DESC LIMIT 20;'
4. Run a fresh scan if inventory is exhausted:
   cd /root/audit-scanner && python3 scanner.py
5. Reference: Local-Presence-Optimization/email-health-audit-runbook.md

Dispatched by alert_on_failure.py (volume-floor mode),
invoked daily via /etc/cron.d/rmp-pipeline.
"""
    return dispatch_alert(subject, body)


# --- Scanner alert modes (RMP #29) ---

def _parse_detail(detail_json: str) -> dict:
    """Parse the --detail JSON string. Return {} on error or empty input."""
    if not detail_json:
        return {}
    try:
        d = json.loads(detail_json)
        return d if isinstance(d, dict) else {}
    except Exception as e:
        logger.warning(f"Failed to parse --detail JSON: {e}")
        return {}


def run_scan_failure_mode(market: str, detail_json: str) -> int:
    """Scanner failed (API issue, queue corruption, etc.). Dispatch alert."""
    detail = _parse_detail(detail_json)
    reason = detail.get("reason", "unknown")
    error = detail.get("error", "(no error message)")
    batch_start = detail.get("batch_start")
    batch_end = detail.get("batch_end")
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    batch_line = ""
    if batch_start is not None and batch_end is not None:
        batch_line = f"\nBatch:       positions {batch_start}-{batch_end}"

    subject = f"[RMP ALERT] Scanner failed ({market.upper()}): {reason}"
    body = f"""RMP Scanner Failure Alert
{'=' * 50}

Market:      {market.upper()}
Reason:      {reason}
Error:       {error}{batch_line}
Timestamp:   {now_utc}

Likely causes:
- Google Places API auth / quota / billing failure
- Network outage from Tencent
- Queue file corruption (queues/town-queue.{market}.json)
- State file corruption (state/state.{market}.json)
- Output write failure on /root/audit-scanner/output/

Triage:
1. SSH to Tencent: ssh root@43.134.33.213
2. tail -100 /var/log/rmp-cron/scanner-{market}.log
3. cat /root/audit-scanner/state/state.{market}.json
4. Test API: cd /root/audit-scanner && python3 scanner.py --dry-run

Cron will retry on next fire (daily). Pointer NOT advanced — same batch
will be re-attempted. Pipeline keeps draining existing inventory meanwhile.

Dispatched by alert_on_failure.py (scan-failure mode),
invoked from scanner.py queue mode.
"""
    return dispatch_alert(subject, body)


def run_queue_depth_low_mode(market: str, detail_json: str) -> int:
    """Queue has < threshold towns remaining. Dispatch warning (not urgent)."""
    detail = _parse_detail(detail_json)
    remaining = detail.get("remaining", "?")
    threshold = detail.get("threshold", "?")
    queue_size = detail.get("queue_size", "?")
    position = detail.get("position", "?")
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    subject = (
        f"[RMP ALERT] Scanner queue depth low ({market.upper()}): "
        f"{remaining} towns remaining"
    )
    body = f"""RMP Scanner Queue Depth Alert
{'=' * 50}

Market:         {market.upper()}
Remaining:      {remaining} towns
Threshold:      {threshold} towns (~30 days at current burn rate)
Queue size:     {queue_size}
Current pos:    {position}
Timestamp:      {now_utc}

NOT urgent. ~30-day window before exhaustion. Top up queue before then.

Action:
1. Generate fresh towns via build_queue.py (Mac) — see scanner/build_queue.py
   OR manually append to queues/town-queue.{market}.json
2. Deploy via Mac clone → audit-scanner.git → Tencent git pull
3. Scanner continues from current pointer automatically

This alert fires ONCE per state change. Won't re-fire until queue is topped
up + scanner runs successfully.

Dispatched by alert_on_failure.py (queue-depth-low mode),
invoked from scanner.py queue mode.
"""
    return dispatch_alert(subject, body)


def run_queue_exhausted_mode(market: str, detail_json: str) -> int:
    """Queue pointer >= queue length. Dispatch URGENT alert."""
    detail = _parse_detail(detail_json)
    queue_size = detail.get("queue_size", "?")
    position = detail.get("position", "?")
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    subject = f"[RMP ALERT] Scanner queue EXHAUSTED ({market.upper()})"
    body = f"""RMP Scanner Queue Exhaustion Alert
{'=' * 50}

Market:         {market.upper()}
Queue size:     {queue_size}
Final position: {position}
Timestamp:      {now_utc}

URGENT: scanner will produce no fresh inventory until queue is topped up.
Existing pipeline inventory will drain over the next ~4-9 days, then 0 sends.

This means the depth-low warning at threshold 75 was missed or ignored.
Investigate why the safety-net warning was bypassed.

Action:
1. URGENT: Top up queues/town-queue.{market}.json with fresh towns
2. Generate via build_queue.py (Mac) — see scanner/build_queue.py
3. Deploy via Mac clone → audit-scanner.git → Tencent git pull
4. Scanner will reset last_alerted_for_exhaust on next successful scan

Dispatched by alert_on_failure.py (queue-exhausted mode),
invoked from scanner.py queue mode.
"""
    return dispatch_alert(subject, body)


# --- Main ---

def main() -> None:
    parser = argparse.ArgumentParser(
        description="RMP pipeline alerter (failure + volume-floor modes)"
    )
    parser.add_argument(
        "--mode",
        choices=["failure", "volume-floor", "scan-failure", "queue-depth-low", "queue-exhausted"],
        default="failure",
        help="Alert mode (default: failure, for backwards compat with run_market.sh)",
    )
    parser.add_argument("--market", required=True, help="Market code (uk/us/au)")

    # failure mode args (required in failure mode, ignored in volume-floor mode)
    parser.add_argument(
        "--exit-code", type=int, default=0,
        help="(failure mode) Pipeline exit code",
    )
    parser.add_argument(
        "--aborted", type=int, default=0,
        help="(failure mode) 1 if pipeline aborted",
    )
    parser.add_argument(
        "--log-file", default="",
        help="(failure mode) Full log file path",
    )

    # volume-floor mode args (ignored in failure mode)
    parser.add_argument(
        "--cap", type=int, default=15,
        help="(volume-floor mode) Daily send cap (default: 15 for US)",
    )
    parser.add_argument(
        "--days", type=int, default=DEFAULT_VOLUME_FLOOR_DAYS,
        help=f"(volume-floor mode) Rolling window in days (default: {DEFAULT_VOLUME_FLOOR_DAYS})",
    )
    parser.add_argument(
        "--floor-pct", type=int, default=DEFAULT_VOLUME_FLOOR_PCT,
        help=f"(volume-floor mode) Floor as %% of cap × days (default: {DEFAULT_VOLUME_FLOOR_PCT})",
    )

    # scanner alert mode args (RMP #29; ignored in failure / volume-floor modes)
    parser.add_argument(
        "--detail", default="",
        help="(scan-failure / queue-depth-low / queue-exhausted modes) JSON string with mode-specific context",
    )

    args = parser.parse_args()

    if args.mode == "failure":
        if not args.log_file:
            logger.error("--log-file is required in failure mode")
            sys.exit(1)
        sys.exit(run_failure_mode(
            market=args.market,
            exit_code=args.exit_code,
            aborted=args.aborted,
            log_file=args.log_file,
        ))
    elif args.mode == "volume-floor":
        sys.exit(run_volume_floor_mode(
            market=args.market,
            cap=args.cap,
            days=args.days,
            floor_pct=args.floor_pct,
        ))
    elif args.mode == "scan-failure":
        sys.exit(run_scan_failure_mode(
            market=args.market,
            detail_json=args.detail,
        ))
    elif args.mode == "queue-depth-low":
        sys.exit(run_queue_depth_low_mode(
            market=args.market,
            detail_json=args.detail,
        ))
    elif args.mode == "queue-exhausted":
        sys.exit(run_queue_exhausted_mode(
            market=args.market,
            detail_json=args.detail,
        ))


if __name__ == "__main__":
    main()
