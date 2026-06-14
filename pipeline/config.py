"""
config.py
Raise My Presence — Pipeline Configuration

All environment variables and constants in one place.
Loaded by every other pipeline module.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
PIPELINE_DIR = Path(__file__).parent
DB_PATH = PIPELINE_DIR / "pipeline.db"
SCAN_DIR = Path("/root/audit-scanner")

# ---------------------------------------------------------------------------
# Auto-load .env from pipeline directory
# Replaces manual `source .env && export SENDGRID_API_KEY` step
#
# override=True is critical: pm2 may have cached stale env vars from earlier
# process launches (e.g., a rotated SENDGRID_API_KEY). Without override, the
# stale pm2-injected values win over the current .env file. With override,
# the .env file is always the source of truth.
# ---------------------------------------------------------------------------
load_dotenv(PIPELINE_DIR / ".env", override=True)

# ---------------------------------------------------------------------------
# SendGrid
# ---------------------------------------------------------------------------
SENDGRID_API_KEY = os.environ.get("SENDGRID_API_KEY", "")
FROM_EMAIL = "hello@mail.raisemypresence.com"
FROM_NAME = "Raise My Presence"

# ---------------------------------------------------------------------------
# Pipeline controls
# ---------------------------------------------------------------------------
DRY_RUN = os.environ.get("DRY_RUN", "true").lower() == "true"
DRY_RUN_RECIPIENT = os.environ.get("DRY_RUN_RECIPIENT", "odedmarketing@gmail.com")

DAILY_SEND_CAP = int(os.environ.get("DAILY_SEND_CAP", "20"))
SCORE_THRESHOLD = int(os.environ.get("SCORE_THRESHOLD", "50"))

# Minimum hours between scan file mtime and send execution
SCAN_TO_SEND_DELAY_HOURS = int(os.environ.get("SCAN_TO_SEND_DELAY_HOURS", "24"))

# ---------------------------------------------------------------------------
# Website discovery
# ---------------------------------------------------------------------------
REQUEST_TIMEOUT = int(os.environ.get("REQUEST_TIMEOUT", "10"))  # seconds
DISCOVERY_RATE_LIMIT = float(os.environ.get("DISCOVERY_RATE_LIMIT", "1.0"))  # seconds between requests

# ---------------------------------------------------------------------------
# Webhook server
# ---------------------------------------------------------------------------
WEBHOOK_PORT = int(os.environ.get("WEBHOOK_PORT", "8099"))
WEBHOOK_BASE_URL = os.environ.get("WEBHOOK_BASE_URL", "http://43.134.33.213:8099")
SENDGRID_WEBHOOK_VERIFY_KEY = os.environ.get("SENDGRID_WEBHOOK_VERIFY_KEY", "")
# HMAC secret for signing unsubscribe links (set in .env; both sender + webhook use it)
UNSUBSCRIBE_HMAC_SECRET = os.environ.get("UNSUBSCRIBE_HMAC_SECRET", "")

# ---------------------------------------------------------------------------
# Stripe (Block 4 — webhook fulfillment)
# ---------------------------------------------------------------------------
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
STRIPE_API_KEY = os.environ.get("STRIPE_API_KEY", "")  # sk_live_... or sk_test_...

# Directory containing locale-keyed kit PDFs (kit_us.pdf, kit_uk.pdf, kit_au.pdf, kit_nz.pdf)
KIT_PDF_DIR = Path(os.environ.get("KIT_PDF_DIR", str(PIPELINE_DIR / "kits")))

# ---------------------------------------------------------------------------
# Email verification (MailValid — RMP #68)
# ---------------------------------------------------------------------------
EMAIL_VERIFY_API_KEY = os.environ.get("EMAIL_VERIFY_API_KEY", "")
EMAIL_VERIFY_ENABLED = os.environ.get("EMAIL_VERIFY_ENABLED", "true").lower() == "true"
