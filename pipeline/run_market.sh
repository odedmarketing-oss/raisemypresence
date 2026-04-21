#!/bin/bash
#
# run_market.sh — Per-market cron runner for RMP outreach pipeline
#
# Usage:
#   ./run_market.sh <market> <cap>
#
# Arguments:
#   market  — uk | us | au
#   cap     — cumulative daily send cap (integer)
#
# Behavior:
#   1. Finds newest scan file in /root/audit-scanner/output/ matching market
#   2. Invokes pipeline.py with appropriate flags
#   3. On hard failure (exit != 0 OR pipeline aborted), calls alert_on_failure.py
#   4. Logs all output to /var/log/rmp-cron/<market>-<date>.log
#
# Env vars:
#   DRY_RUN_OVERRIDE=1  → runs pipeline in --dry-run mode (for testing)
#
# Exit codes:
#   0   success (pipeline ran, may or may not have sent)
#   2   no scan file found for market
#   64  invalid arguments
#   *   pipeline's own exit code on failure

# Cron has minimal PATH — set explicitly
export PATH="/usr/local/bin:/usr/bin:/bin:$PATH"

MARKET="$1"
CAP="$2"

# --- Argument validation ---
case "$MARKET" in
  uk) PATTERN=', UK"' ;;
  us) PATTERN=', USA"' ;;
  au) PATTERN=', Australia"' ;;
  *)
    echo "Usage: $0 <uk|us|au> <cap>" >&2
    exit 64
    ;;
esac

if ! [[ "$CAP" =~ ^[0-9]+$ ]]; then
  echo "Error: cap must be a non-negative integer, got: '$CAP'" >&2
  exit 64
fi

# --- Paths ---
OUTPUT_DIR="/root/audit-scanner/output"
PIPELINE_DIR="/root/audit-scanner/pipeline"
LOG_DIR="/var/log/rmp-cron"
ALERT_SCRIPT="$PIPELINE_DIR/alert_on_failure.py"

mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/${MARKET}-$(date -u +%Y-%m-%d).log"

# --- Banner ---
{
  echo ""
  echo "========================================================"
  echo "run_market.sh — $MARKET @ $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "  cap:          $CAP"
  echo "  pattern:      $PATTERN"
  echo "  log file:     $LOG_FILE"
  echo "========================================================"
} | tee -a "$LOG_FILE"

# --- Find newest scan file matching market ---
# Iterate newest-first, return first file whose contents contain the market pattern
SCAN_FILE=""
while IFS= read -r f; do
  if grep -qF "$PATTERN" "$f" 2>/dev/null; then
    SCAN_FILE="$f"
    break
  fi
done < <(ls -t "$OUTPUT_DIR"/scan_results_*.json 2>/dev/null)

if [[ -z "$SCAN_FILE" ]]; then
  echo "[ERROR] No scan file found matching market '$MARKET' (pattern: '$PATTERN')" | tee -a "$LOG_FILE" >&2
  exit 2
fi

echo "[INFO] Selected scan file: $SCAN_FILE" | tee -a "$LOG_FILE"

# --- Resolve mode (live vs dry-run override for testing) ---
if [[ "${DRY_RUN_OVERRIDE:-0}" == "1" ]]; then
  MODE_FLAGS=(--dry-run)
  echo "[INFO] DRY_RUN_OVERRIDE=1 — running in dry-run mode" | tee -a "$LOG_FILE"
else
  MODE_FLAGS=(--no-dry-run --yes)
  echo "[INFO] LIVE mode — will send real emails" | tee -a "$LOG_FILE"
fi

# --- Invoke pipeline ---
cd "$PIPELINE_DIR" || {
  echo "[ERROR] Cannot cd to $PIPELINE_DIR" | tee -a "$LOG_FILE" >&2
  exit 1
}

python3 pipeline.py \
  --scan-file "$SCAN_FILE" \
  "${MODE_FLAGS[@]}" \
  --cap "$CAP" \
  --skip-delay \
  -v 2>&1 | tee -a "$LOG_FILE"

PIPELINE_EXIT=${PIPESTATUS[0]}

# --- Check for scan-delay abort via pipeline_run.json ---
# Pipeline returns abort via dict but exits 0; detect by parsing summary file
SUMMARY_FILE="$(dirname "$SCAN_FILE")/pipeline_run.json"
ABORTED=0
if [[ -f "$SUMMARY_FILE" ]]; then
  ABORTED=$(python3 -c "
import json, sys
try:
    with open('$SUMMARY_FILE') as f:
        d = json.load(f)
    print(1 if d.get('aborted') else 0)
except Exception:
    print(0)
" 2>/dev/null || echo 0)
fi

# --- Alert on hard failure ---
if [[ "$PIPELINE_EXIT" != "0" ]] || [[ "$ABORTED" == "1" ]]; then
  echo "[ERROR] Hard failure detected (exit=$PIPELINE_EXIT, aborted=$ABORTED) — dispatching alert" | tee -a "$LOG_FILE" >&2

  if [[ -f "$ALERT_SCRIPT" ]]; then
    python3 "$ALERT_SCRIPT" \
      --market "$MARKET" \
      --exit-code "$PIPELINE_EXIT" \
      --aborted "$ABORTED" \
      --log-file "$LOG_FILE" 2>&1 | tee -a "$LOG_FILE"
  else
    echo "[WARN] alert_on_failure.py not found at $ALERT_SCRIPT — alert skipped" | tee -a "$LOG_FILE" >&2
  fi
fi

echo "========================================================" | tee -a "$LOG_FILE"
echo "run_market.sh complete — exit code: $PIPELINE_EXIT" | tee -a "$LOG_FILE"
echo "" | tee -a "$LOG_FILE"

exit "$PIPELINE_EXIT"
