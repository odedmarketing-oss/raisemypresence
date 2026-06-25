"""
audit_landing.py
Raise My Presence — Personalized Audit Landing Page Data (T-019)

Stores per-send business audit data so the landing page can display a
personalized hero (business name, top issues, outcome reframe) for
cold-email visitors arriving via ?rmp=<token>.

Written once at send time (pipeline.py), read by the /api/audit/{token}
endpoint (webhook_server.py).
"""

import json
import sqlite3
from datetime import datetime, timezone

from config import DB_PATH


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS audit_landing_data (
            rmp_token            TEXT PRIMARY KEY,
            business_name        TEXT NOT NULL,
            score                INTEGER NOT NULL,
            score_breakdown_json TEXT NOT NULL,
            locale               TEXT NOT NULL DEFAULT 'US',
            created_at           TEXT NOT NULL
        )
    """)
    conn.commit()
    return conn


def save_audit_landing_data(
    rmp_token: str,
    business_name: str,
    score: int,
    score_breakdown: dict,
    locale: str,
) -> None:
    """Persist audit data for landing-page personalization. Idempotent."""
    conn = _get_conn()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO audit_landing_data "
            "(rmp_token, business_name, score, score_breakdown_json, locale, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                rmp_token,
                business_name,
                score,
                json.dumps(score_breakdown),
                locale,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def get_audit_landing_data(rmp_token: str) -> dict | None:
    """Fetch stored audit data by rmp_token. Returns dict or None."""
    conn = _get_conn()
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT business_name, score, score_breakdown_json, locale "
            "FROM audit_landing_data WHERE rmp_token = ?",
            (rmp_token,),
        ).fetchone()
        if not row:
            return None
        return dict(row)
    finally:
        conn.close()
