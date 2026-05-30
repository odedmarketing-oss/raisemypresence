"""
email_events.py
Raise My Presence — Engagement Event Log (Telemetry Part 2)

Append-only SQLite log of ALL SendGrid Event Webhook events, sharing the same
DB file as send_log + suppression. Every event SendGrid posts (delivered, open,
click, spam_report, unsubscribe, bounce, dropped, ...) is logged here verbatim,
keyed on sg_message_id — the join key to sent_log.sendgrid_message_id (captured
in Telemetry Part 1, RMP #55) for downstream attribution (Part 4).

Design (RMP #56, Option A):
  - Raw append-only. No dedup at write time. SendGrid retries on non-2xx and may
    resend events, so duplicate rows are possible by design; the unique sg_event_id
    lives inside payload_json and dedup is a cheap query-time concern (SQLite
    json_extract). Keeping raw duplicates is the faithful-log behavior.
  - Suppression-worthy events (bounce/dropped) continue to be handled by the
    existing webhook_server.py suppression branch — this table is purely additive
    logging ALONGSIDE that, never a replacement.
  - Idempotent self-healing CREATE on every connect (mirrors suppression.py), so
    the table survives a RECOVERY.md rebuild with no separate migration step.

Written by:
  - SendGrid Event Webhook handler (webhook_server.py /webhook/sendgrid), top of
    the event loop, for every event type.
"""

import sqlite3
from datetime import datetime, timezone

from config import DB_PATH


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS email_events (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            sg_message_id TEXT,
            event_type    TEXT    NOT NULL,
            timestamp     TEXT    NOT NULL,
            payload_json  TEXT    NOT NULL
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_email_events_message_id
        ON email_events(sg_message_id)
    """)
    conn.commit()
    return conn


def insert_event(sg_message_id: str, event_type: str, timestamp: str, payload_json: str) -> None:
    """
    Append one SendGrid event to the log. Raw, no dedup.

    Args:
        sg_message_id: SendGrid X-Message-Id (join key to sent_log). May be ""
                       if absent on the event (logged as-is, not dropped).
        event_type:    SendGrid 'event' field (delivered/open/click/...).
        timestamp:     ISO-8601 string (caller converts SendGrid unix epoch).
        payload_json:  full event object serialized via json.dumps.
    """
    conn = _get_conn()
    try:
        conn.execute(
            "INSERT INTO email_events (sg_message_id, event_type, timestamp, payload_json) "
            "VALUES (?, ?, ?, ?)",
            (sg_message_id, event_type, timestamp, payload_json),
        )
        conn.commit()
    finally:
        conn.close()


def event_count() -> dict:
    """Return counts by event_type — for verification + debugging."""
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT event_type, COUNT(*) FROM email_events GROUP BY event_type"
        ).fetchall()
        return {r[0]: r[1] for r in rows}
    finally:
        conn.close()


def recent_events(limit: int = 20) -> list[dict]:
    """Return recent events for debugging."""
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT id, sg_message_id, event_type, timestamp FROM email_events "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            {"id": r[0], "sg_message_id": r[1], "event_type": r[2], "timestamp": r[3]}
            for r in rows
        ]
    finally:
        conn.close()
