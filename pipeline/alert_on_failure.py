#!/usr/bin/env python3
"""
alert_on_failure.py
Raise My Presence — Pipeline Failure Alerting

Called by run_market.sh ONLY on hard failures (exit != 0 OR pipeline aborted).
Sends a SendGrid alert email to the operator with triage context.

CLI usage:
  python3 alert_on_failure.py \\
    --market uk \\
    --exit-code 1 \\
    --aborted 0 \\
    --log-file /var/log/rmp-cron/uk-2026-04-22.log

Dependencies:
  - sendgrid (declared in pipeline/requirements.txt)
  - python-dotenv (declared in pipeline/requirements.txt)

Exit codes:
  0  alert successfully dispatched to SendGrid (HTTP 202)
  1  alert dispatch failed (missing API key, SendGrid error, etc.)
"""

import argparse
import logging
import os
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

logger = logging.getLogger("alert_on_failure")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


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


def build_message(market: str, exit_code: int, aborted: int, log_file: str) -> Mail:
    """Construct the alert email with triage context."""
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    # Classify failure kind for at-a-glance diagnosis
    if aborted == 1:
        failure_kind = "pipeline aborted (scan-delay or runtime abort)"
    elif exit_code != 0:
        failure_kind = f"non-zero exit code ({exit_code})"
    else:
        failure_kind = "unknown (alert triggered without clear cause)"

    # Subject — market + exit code visible for inbox search
    subject = f"[RMP ALERT] {market.upper()} pipeline failed (exit {exit_code})"

    # Last N log lines for triage
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

Dispatched by alert_on_failure.py,
called from /root/audit-scanner/pipeline/run_market.sh.
"""

    return Mail(
        from_email=(ALERT_SENDER, ALERT_SENDER_NAME),
        to_emails=ALERT_RECIPIENT,
        subject=subject,
        plain_text_content=body,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="RMP pipeline failure alerter")
    parser.add_argument("--market", required=True, help="Market code (uk/us/au)")
    parser.add_argument("--exit-code", type=int, required=True, help="Pipeline exit code")
    parser.add_argument("--aborted", type=int, default=0, help="1 if pipeline aborted")
    parser.add_argument("--log-file", required=True, help="Full log file path")
    args = parser.parse_args()

    api_key = os.getenv("SENDGRID_API_KEY")
    if not api_key:
        logger.error("SENDGRID_API_KEY not set in .env — cannot dispatch alert")
        sys.exit(1)

    try:
        message = build_message(
            market=args.market,
            exit_code=args.exit_code,
            aborted=args.aborted,
            log_file=args.log_file,
        )
        sg = SendGridAPIClient(api_key)
        response = sg.send(message)
        logger.info(
            f"Alert dispatched: market={args.market}, "
            f"exit={args.exit_code}, aborted={args.aborted}, "
            f"sendgrid_status={response.status_code}"
        )
        sys.exit(0)
    except Exception as e:
        logger.error(f"Failed to dispatch alert: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
