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
# ---------------------------------------------------------------------------
load_dotenv(PIPELINE_DIR / ".env")

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
