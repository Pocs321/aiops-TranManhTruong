#!/usr/bin/env bash
# step_c.sh — deploy step C (re-enable traffic). S4: forced to FAIL (exit 1)
# to trigger the transactional rollback of completed steps B then A.
set -euo pipefail
SERVICE=""; DRY_RUN=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    --service) SERVICE="$2"; shift 2 ;;
    --dry-run) DRY_RUN=true; shift ;;
    *) echo "Unknown arg: $1"; exit 1 ;;
  esac
done
if $DRY_RUN; then echo "[DRY-RUN] step-C: would re-enable traffic for $SERVICE"; exit 0; fi
echo "[step-c] ERROR: deploy failed re-enabling traffic for $SERVICE" >&2
exit 1
