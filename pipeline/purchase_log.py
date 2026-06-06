"""
purchase_log.py
Raise My Presence — Purchase Log (Stripe Webhook Idempotency + Fulfillment Tracking)

SQLite-backed log of every Stripe checkout.session.completed event received.
Implements the industry-standard idempotency pattern documented in
Stripe-Plan.md Block 4: dedup on Stripe event.id (the canonical fingerprint
that survives event-type re-delivery), tri-state response handling.

Lifecycle of a row:
    1. Webhook receives event → INSERT with fulfillment_status='processing'
       - INSERT OR IGNORE protects against double-handling on Stripe retry.
       - If the event is already in the table with status='sent', the caller
         short-circuits and returns 200 without doing the work again.
    2. PDF generated + email sent → UPDATE to fulfillment_status='sent',
       fulfilled_at=<now>.
    3. On transient failure → UPDATE to fulfillment_status='failed_transient',
       error_message=<msg>. Caller returns 500 so Stripe retries.
    4. On permanent failure → UPDATE to fulfillment_status='failed_permanent'.
       Caller returns 200 so Stripe stops retrying. Manual recovery from log.
"""

import logging
import sqlite3
from datetime import datetime, timezone
from typing import Optional

from config import DB_PATH

logger = logging.getLogger(__name__)


# Fulfillment status enum (string values for SQLite storage)
STATUS_PROCESSING = "processing"
STATUS_SENT = "sent"
STATUS_FAILED_TRANSIENT = "failed_transient"
STATUS_FAILED_PERMANENT = "failed_permanent"


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS purchase_log (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            stripe_event_id     TEXT    NOT NULL UNIQUE,
            stripe_session_id   TEXT,
            email               TEXT,
            business_name       TEXT,
            business_city       TEXT,
            product             TEXT,
            locale              TEXT,
            amount_cents        INTEGER,
            currency            TEXT,
            payment_method      TEXT,
            purchased_at        TEXT,
            received_at         TEXT NOT NULL,
            fulfilled_at        TEXT,
            fulfillment_status  TEXT NOT NULL DEFAULT 'processing',
            error_message       TEXT,
            client_reference_id TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_purchase_log_email ON purchase_log(email)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_purchase_log_status ON purchase_log(fulfillment_status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_purchase_log_received ON purchase_log(received_at)")
    # Idempotent migration (RMP #61, Telemetry Part 4): CREATE TABLE IF NOT
    # EXISTS won't add a column to a pre-existing table, so ALTER it in if
    # missing. Mirrors send_log.py. Non-destructive (nullable). Carries the
    # per-send attribution token Stripe echoes back as client_reference_id.
    _cols = [r[1] for r in conn.execute("PRAGMA table_info(purchase_log)").fetchall()]
    if "client_reference_id" not in _cols:
        conn.execute("ALTER TABLE purchase_log ADD COLUMN client_reference_id TEXT")
    conn.commit()
    return conn


def is_already_fulfilled(stripe_event_id: str) -> bool:
    """
    True if this event.id is already in the log with fulfillment_status='sent'.
    Used by the webhook handler to short-circuit on Stripe retries.

    Returns False if event has never been seen OR if its row is still
    'processing' / 'failed_transient' (those should be retried).
    """
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT fulfillment_status FROM purchase_log WHERE stripe_event_id = ? LIMIT 1",
            (stripe_event_id,)
        ).fetchone()
        return row is not None and row[0] == STATUS_SENT
    finally:
        conn.close()


def insert_pending(
    stripe_event_id: str,
    stripe_session_id: str,
    email: str,
    business_name: str,
    business_city: str,
    product: str,
    locale: str,
    amount_cents: int,
    currency: str,
    payment_method: str,
    purchased_at: str,
    client_reference_id: str = "",
) -> bool:
    """
    Insert a new purchase row with fulfillment_status='processing'.

    Returns True if newly inserted, False if event.id already existed
    (caller should treat False as "already handling — short-circuit").

    INSERT OR IGNORE is intentional: if Stripe retries while we're mid-process,
    the second invocation gets False and bails without touching state.
    """
    conn = _get_conn()
    try:
        cur = conn.execute(
            """INSERT OR IGNORE INTO purchase_log (
                   stripe_event_id, stripe_session_id, email, business_name,
                   business_city, product, locale, amount_cents, currency,
                   payment_method, purchased_at, received_at, fulfillment_status,
                   client_reference_id
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                stripe_event_id,
                stripe_session_id,
                (email or "").lower(),
                business_name or "",
                business_city or "",
                product,
                locale,
                amount_cents,
                currency,
                payment_method or "",
                purchased_at,
                datetime.now(timezone.utc).isoformat(),
                STATUS_PROCESSING,
                client_reference_id or "",
            )
        )
        conn.commit()
        inserted = cur.rowcount > 0
        if not inserted:
            logger.info(f"purchase_log: event {stripe_event_id} already exists, skipping insert")
        return inserted
    finally:
        conn.close()


def mark_fulfilled(stripe_event_id: str) -> None:
    """Mark a purchase as successfully fulfilled."""
    conn = _get_conn()
    try:
        conn.execute(
            """UPDATE purchase_log
                  SET fulfillment_status = ?, fulfilled_at = ?, error_message = NULL
                WHERE stripe_event_id = ?""",
            (STATUS_SENT, datetime.now(timezone.utc).isoformat(), stripe_event_id)
        )
        conn.commit()
    finally:
        conn.close()


def mark_failed(stripe_event_id: str, error: str, permanent: bool = False) -> None:
    """
    Mark a purchase as failed.

    permanent=False (default): transient failure (e.g., SendGrid 5xx).
        Webhook handler should return 500 so Stripe retries.
    permanent=True: unrecoverable failure (e.g., malformed event, missing PDF).
        Webhook handler should return 200 so Stripe stops retrying.
    """
    status = STATUS_FAILED_PERMANENT if permanent else STATUS_FAILED_TRANSIENT
    conn = _get_conn()
    try:
        conn.execute(
            """UPDATE purchase_log
                  SET fulfillment_status = ?, error_message = ?
                WHERE stripe_event_id = ?""",
            (status, error[:500], stripe_event_id)
        )
        conn.commit()
    finally:
        conn.close()


def get_purchase(stripe_event_id: str) -> Optional[dict]:
    """Fetch a single purchase row by event_id. None if not found."""
    conn = _get_conn()
    try:
        row = conn.execute(
            """SELECT id, stripe_event_id, stripe_session_id, email, business_name,
                      business_city, product, locale, amount_cents, currency,
                      payment_method, purchased_at, received_at, fulfilled_at,
                      fulfillment_status, error_message
                 FROM purchase_log WHERE stripe_event_id = ? LIMIT 1""",
            (stripe_event_id,)
        ).fetchone()
        if not row:
            return None
        return {
            "id": row[0], "stripe_event_id": row[1], "stripe_session_id": row[2],
            "email": row[3], "business_name": row[4], "business_city": row[5],
            "product": row[6], "locale": row[7], "amount_cents": row[8],
            "currency": row[9], "payment_method": row[10], "purchased_at": row[11],
            "received_at": row[12], "fulfilled_at": row[13],
            "fulfillment_status": row[14], "error_message": row[15],
        }
    finally:
        conn.close()


def get_recent_purchases(limit: int = 50) -> list[dict]:
    """Return recent purchases for debugging / operator dashboard."""
    conn = _get_conn()
    try:
        rows = conn.execute(
            """SELECT stripe_event_id, email, business_name, product, locale,
                      amount_cents, currency, received_at, fulfilled_at,
                      fulfillment_status, error_message
                 FROM purchase_log ORDER BY received_at DESC LIMIT ?""",
            (limit,)
        ).fetchall()
        return [
            {
                "stripe_event_id": r[0], "email": r[1], "business_name": r[2],
                "product": r[3], "locale": r[4], "amount_cents": r[5],
                "currency": r[6], "received_at": r[7], "fulfilled_at": r[8],
                "fulfillment_status": r[9], "error_message": r[10],
            }
            for r in rows
        ]
    finally:
        conn.close()
