"""
funnel_events.py
Raise My Presence — Funnel Stage Event Log (RMP #67)

Append-only SQLite log of cold-outreach funnel stages per rmp_token.
Stages written here: land, pricing-view, checkout-start.
'complete' is derived from purchase_log at query time.

Mirrors email_events.py exactly: idempotent CREATE on connect, raw append, no
write-time dedup (uniqueness enforced at query time via DISTINCT + INNER JOIN).
"""

import sqlite3

from config import DB_PATH


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS funnel_events (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            rmp_token    TEXT    NOT NULL,
            stage        TEXT    NOT NULL,
            ts           TEXT    NOT NULL,
            payload_json TEXT    NOT NULL
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_funnel_events_rmp_token
        ON funnel_events(rmp_token)
    """)
    conn.commit()
    return conn


def insert_event(rmp_token: str, stage: str, ts: str, payload_json: str) -> None:
    conn = _get_conn()
    try:
        conn.execute(
            "INSERT INTO funnel_events (rmp_token, stage, ts, payload_json) "
            "VALUES (?, ?, ?, ?)",
            (rmp_token, stage, ts, payload_json),
        )
        conn.commit()
    finally:
        conn.close()
