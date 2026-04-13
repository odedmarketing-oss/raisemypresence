"""
suppression.py
Raise My Presence — Suppression List (Bounces + Unsubscribes)

SQLite-backed suppression list sharing the same DB file as send_log.
Every email is checked against this list before sending.
Written by:
  - SendGrid bounce webhook (reason='hard_bounce')
  - Unsubscribe webhook/handler (reason='unsubscribe')
  - Manual additions (reason='manual')
"""

import sqlite3
from datetime import datetime, timezone

from config import DB_PATH


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS suppression (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            email    TEXT    NOT NULL UNIQUE,
            reason   TEXT    NOT NULL,
            added_at TEXT    NOT NULL
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_suppression_email
        ON suppression(email)
    """)
    conn.commit()
    return conn


def is_suppressed(email: str) -> bool:
    """Check if an email is on the suppression list."""
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT 1 FROM suppression WHERE email = ? LIMIT 1",
            (email.lower(),)
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def add_suppression(email: str, reason: str = "manual") -> bool:
    """
    Add an email to suppression list.
    Returns True on success, False if already suppressed.
    Reason values: 'hard_bounce', 'unsubscribe', 'manual'
    """
    conn = _get_conn()
    try:
        conn.execute(
            "INSERT INTO suppression (email, reason, added_at) VALUES (?, ?, ?)",
            (email.lower(), reason, datetime.now(timezone.utc).isoformat())
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()


def remove_suppression(email: str) -> bool:
    """Remove an email from suppression (e.g., re-engagement). Returns True if found."""
    conn = _get_conn()
    try:
        cursor = conn.execute(
            "DELETE FROM suppression WHERE email = ?",
            (email.lower(),)
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def list_suppressions(limit: int = 100) -> list[dict]:
    """Return suppression list for debugging."""
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT email, reason, added_at FROM suppression ORDER BY added_at DESC LIMIT ?",
            (limit,)
        ).fetchall()
        return [
            {"email": r[0], "reason": r[1], "added_at": r[2]}
            for r in rows
        ]
    finally:
        conn.close()


def suppression_count() -> dict:
    """Return counts by reason."""
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT reason, COUNT(*) FROM suppression GROUP BY reason"
        ).fetchall()
        return {r[0]: r[1] for r in rows}
    finally:
        conn.close()
