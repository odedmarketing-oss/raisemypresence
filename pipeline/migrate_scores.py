"""
migrate_scores.py
Raise My Presence — One-time score migration (RMP #82, B2 fix)

Converts existing sent_log.score and audit_landing_data.score values
from the legacy raw /88 scale to the normalized /100 scale.

Run ONCE on the Tencent server BEFORE deploying the code changes:
    cd /root/audit-scanner/pipeline
    python3 migrate_scores.py

Idempotent: a schema_migrations marker table records whether this
migration has already run. Re-running is a safe no-op.
"""

import sqlite3
import sys
from pathlib import Path

# Import DB_PATH from config so we hit the same database the pipeline uses.
sys.path.insert(0, str(Path(__file__).parent))
from config import DB_PATH

MIGRATION_NAME = "b2_normalize_scores_88_to_100"
MAX_SCORE_RAW = 88  # historical raw ceiling (sum of SCORE_FACTORS maximums)


def migrate():
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")

    # --- One-shot guard ---
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            name       TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL
        )
    """)
    already = conn.execute(
        "SELECT 1 FROM schema_migrations WHERE name = ?", (MIGRATION_NAME,)
    ).fetchone()
    if already:
        print(f"Migration '{MIGRATION_NAME}' already applied — skipping.")
        conn.close()
        return

    # --- sent_log ---
    total_sl = conn.execute("SELECT COUNT(*) FROM sent_log").fetchone()[0]

    sample_before = conn.execute(
        "SELECT place_id, score FROM sent_log ORDER BY sent_at DESC LIMIT 5"
    ).fetchall()

    conn.execute(
        "UPDATE sent_log SET score = ROUND(score * 100.0 / ?)",
        (MAX_SCORE_RAW,),
    )

    sample_after = conn.execute(
        "SELECT place_id, score FROM sent_log ORDER BY sent_at DESC LIMIT 5"
    ).fetchall()

    print(f"sent_log: {total_sl} rows migrated")
    print(f"  before (sample): {sample_before}")
    print(f"  after  (sample): {sample_after}")

    # --- audit_landing_data ---
    total_ald = conn.execute("SELECT COUNT(*) FROM audit_landing_data").fetchone()[0]

    sample_before_ald = conn.execute(
        "SELECT rmp_token, score FROM audit_landing_data ORDER BY created_at DESC LIMIT 5"
    ).fetchall()

    conn.execute(
        "UPDATE audit_landing_data SET score = ROUND(score * 100.0 / ?)",
        (MAX_SCORE_RAW,),
    )

    sample_after_ald = conn.execute(
        "SELECT rmp_token, score FROM audit_landing_data ORDER BY created_at DESC LIMIT 5"
    ).fetchall()

    print(f"audit_landing_data: {total_ald} rows migrated")
    print(f"  before (sample): {sample_before_ald}")
    print(f"  after  (sample): {sample_after_ald}")

    # --- Record migration ---
    conn.execute(
        "INSERT INTO schema_migrations (name, applied_at) VALUES (?, datetime('now'))",
        (MIGRATION_NAME,),
    )

    conn.commit()
    conn.close()
    print("Done.")


if __name__ == "__main__":
    migrate()
