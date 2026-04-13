"""
send_log.py
Raise My Presence — Send Log (Deduplication + Tracking)

SQLite-backed log of every email sent (or dry-run simulated).
Deduplication key: place_id + email (never contact same business twice).
Daily count query enforces DAILY_SEND_CAP.
"""

import sqlite3
from datetime import datetime, timezone

from config import DB_PATH


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sent_log (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            place_id   TEXT    NOT NULL,
            email      TEXT    NOT NULL,
            subject    TEXT,
            score      INTEGER,
            sent_at    TEXT    NOT NULL,
            dry_run    INTEGER NOT NULL DEFAULT 0,
            status     TEXT    DEFAULT 'sent',
            UNIQUE(place_id, email)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_sent_log_date
        ON sent_log(sent_at)
    """)
    conn.commit()
    return conn


def already_sent(place_id: str, email: str) -> bool:
    """Check if this place_id + email combo was already sent."""
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT 1 FROM sent_log WHERE place_id = ? AND email = ? LIMIT 1",
            (place_id, email.lower())
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def today_send_count(dry_run: bool = False) -> int:
    """Count emails sent today (real sends only by default)."""
    conn = _get_conn()
    try:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        row = conn.execute(
            "SELECT COUNT(*) FROM sent_log WHERE sent_at LIKE ? AND dry_run = ?",
            (f"{today}%", int(dry_run))
        ).fetchone()
        return row[0] if row else 0
    finally:
        conn.close()


def log_send(
    place_id: str,
    email: str,
    subject: str = "",
    score: int = 0,
    dry_run: bool = False,
    status: str = "sent"
) -> bool:
    """
    Record a send. Returns True on success, False if duplicate.
    """
    conn = _get_conn()
    try:
        conn.execute(
            """INSERT INTO sent_log (place_id, email, subject, score, sent_at, dry_run, status)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                place_id,
                email.lower(),
                subject,
                score,
                datetime.now(timezone.utc).isoformat(),
                int(dry_run),
                status,
            )
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()


def get_send_history(limit: int = 50) -> list[dict]:
    """Return recent send history for debugging."""
    conn = _get_conn()
    try:
        rows = conn.execute(
            """SELECT place_id, email, score, sent_at, dry_run, status
               FROM sent_log ORDER BY sent_at DESC LIMIT ?""",
            (limit,)
        ).fetchall()
        return [
            {
                "place_id": r[0], "email": r[1], "score": r[2],
                "sent_at": r[3], "dry_run": bool(r[4]), "status": r[5]
            }
            for r in rows
        ]
    finally:
        conn.close()
