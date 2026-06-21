#!/usr/bin/env bash
# session-preflight.sh — read-only RMP session Step 0 verification
# Emits one consolidated, machine-parseable block of per-check verdict lines.
# Invoke: bash /root/audit-scanner/pipeline/session-preflight.sh
#
# Exit 0 = all green (nothing needs eyes). Exit 1 = something needs attention.
#
# Agent-ready: each check emits `preflight: <check> verdict=<VERDICT> ...`,
# mirroring the Surface 5/7 log convention. A future Domain Reputation Agent
# invokes this, parses the verdict lines + exit code, and surfaces only the
# non-green path. READ-ONLY by design — never add write/act capability here;
# action authority lives in the agent layer (§1.21 guardrails).

PIPELINE_DIR="/root/audit-scanner/pipeline"
STATE_DIR="/root/audit-scanner/state"
DIGEST_FILE="${PIPELINE_DIR}/deliverability_digest.py"
DIGEST_LOG="/var/log/rmp-cron/deliverability-digest.log"
DB="${PIPELINE_DIR}/pipeline.db"
MD5_BASELINE="${STATE_DIR}/digest.md5"

NEEDS_EYES=0

echo "=== RMP session-preflight $(date '+%F %T %Z') ==="

# 1. Digest integrity — current md5 vs baseline (catches a direct-on-server edit
#    that bypassed the RMP-2 deploy chain). Baseline is reseeded on legit deploy.
echo "--- 1. digest integrity ---"
if [ -f "$DIGEST_FILE" ]; then
  CUR_MD5=$(md5sum "$DIGEST_FILE" | awk '{print $1}')
  if [ -f "$MD5_BASELINE" ]; then
    EXP_MD5=$(tr -d '[:space:]' < "$MD5_BASELINE")
    if [ "$CUR_MD5" = "$EXP_MD5" ]; then
      echo "preflight: digest_md5 verdict=OK md5=${CUR_MD5}"
    else
      echo "preflight: digest_md5 verdict=DRIFT current=${CUR_MD5} expected=${EXP_MD5}"
      NEEDS_EYES=1
    fi
  else
    echo "preflight: digest_md5 verdict=NO_BASELINE current=${CUR_MD5} (seed: echo ${CUR_MD5} > ${MD5_BASELINE})"
    NEEDS_EYES=1
  fi
else
  echo "preflight: digest_md5 verdict=MISSING_FILE path=${DIGEST_FILE}"
  NEEDS_EYES=1
fi

# 2. Digest freshness — did the daily 09:00 CST auto-fire land today?
#    Catches a silently-dead cron (a reputation-monitoring blind spot).
echo "--- 2. digest freshness ---"
if [ -f "$DIGEST_LOG" ]; then
  LAST_SENT_DATE=$(grep "Digest sent" "$DIGEST_LOG" | tail -1 | awk '{print $1}')
  TODAY=$(date '+%F')
  if [ "$LAST_SENT_DATE" = "$TODAY" ]; then
    echo "preflight: digest_freshness verdict=FRESH last_sent=${LAST_SENT_DATE}"
  elif [ -n "$LAST_SENT_DATE" ]; then
    echo "preflight: digest_freshness verdict=STALE last_sent=${LAST_SENT_DATE} today=${TODAY}"
    NEEDS_EYES=1
  else
    echo "preflight: digest_freshness verdict=NO_DATA (no 'Digest sent' line in log)"
    NEEDS_EYES=1
  fi
else
  echo "preflight: digest_freshness verdict=NO_LOG path=${DIGEST_LOG}"
  NEEDS_EYES=1
fi

# 3. Last surface verdicts (informational — full detail lands in the digest email).
echo "--- 3. last surface verdicts (info) ---"
if [ -f "$DIGEST_LOG" ]; then
  LAST_S3=$(grep "Surface 3:" "$DIGEST_LOG" | tail -1 | sed 's/.*Surface 3:/Surface 3:/')
  LAST_S4=$(grep "Surface 4:" "$DIGEST_LOG" | tail -1 | sed 's/.*Surface 4:/Surface 4:/')
  LAST_S5=$(grep "Surface 5:" "$DIGEST_LOG" | tail -1 | sed 's/.*Surface 5:/Surface 5:/')
  LAST_S7=$(grep "Surface 7:" "$DIGEST_LOG" | tail -1 | sed 's/.*Surface 7:/Surface 7:/')
  if [ -n "$LAST_S3" ]; then echo "preflight: ${LAST_S3}"; else echo "preflight: surface_3 verdict=NO_DATA"; fi
  if [ -n "$LAST_S4" ]; then echo "preflight: ${LAST_S4}"; else echo "preflight: surface_4 verdict=NO_DATA"; fi
  if [ -n "$LAST_S5" ]; then echo "preflight: ${LAST_S5}"; else echo "preflight: surface_5 verdict=NO_DATA"; fi
  if [ -n "$LAST_S7" ]; then echo "preflight: ${LAST_S7}"; else echo "preflight: surface_7 verdict=NO_DATA"; fi
else
  echo "preflight: surface_verdicts verdict=NO_LOG"
fi

# 4. Launch gate — first real paying customer (excludes operator test purchases).
echo "--- 4. launch gate ---"
if [ -f "$DB" ]; then
  CUST=$(sqlite3 "$DB" "SELECT COUNT(*) FROM purchase_log WHERE fulfillment_status='sent' AND amount_cents > 0 AND email != 'odedmarketing@gmail.com';" 2>/dev/null)
  if [ -z "$CUST" ]; then
    echo "preflight: customer_count verdict=QUERY_FAILED"
    NEEDS_EYES=1
  elif [ "$CUST" -eq 0 ] 2>/dev/null; then
    echo "preflight: customer_count=0 verdict=GATE_OPEN"
  else
    echo "preflight: customer_count=${CUST} verdict=CUSTOMER (launch gate flipped — surface)"
    NEEDS_EYES=1
  fi
else
  echo "preflight: customer_count verdict=NO_DB path=${DB}"
  NEEDS_EYES=1
fi


# 5. Deploy-chain integrity — runtime must match the repo (RMP #77, T-022).
echo "--- 5. deploy-chain integrity (runtime vs repo) ---"
REPO_DIR="/root/raisemypresence"
if [ -d "${REPO_DIR}/.git" ]; then
  git -C "$REPO_DIR" fetch -q origin main 2>/dev/null
  AB=$(git -C "$REPO_DIR" rev-list --left-right --count origin/main...HEAD 2>/dev/null | tr -d '[:space:]')
  if [ -n "$AB" ] && [ "$AB" != "00" ]; then
    echo "preflight: repo_sync verdict=CLONE_OUT_OF_SYNC origin_vs_head=${AB} (run: git -C ${REPO_DIR} pull)"
    NEEDS_EYES=1
  else
    echo "preflight: repo_sync verdict=OK"
  fi
  DRIFT=$(diff -rq "${REPO_DIR}/pipeline/" "${PIPELINE_DIR}/" 2>/dev/null | grep -vE '__pycache__|\.db$|\.env|/kits|\.pyc|\.bak|gmail_|\.json$|deliverability-digest|rmp-pipeline|test_|_diagnostic|oauth_setup')
  if [ -n "$DRIFT" ]; then
    echo "preflight: runtime_vs_repo verdict=DRIFT"
    echo "$DRIFT" | sed 's/^/preflight:   /'
    NEEDS_EYES=1
  else
    echo "preflight: runtime_vs_repo verdict=OK"
  fi
else
  echo "preflight: repo_sync verdict=NO_REPO path=${REPO_DIR}"
  NEEDS_EYES=1
fi
echo "=== preflight complete — needs_eyes=${NEEDS_EYES} ==="
exit $NEEDS_EYES
