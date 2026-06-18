#!/usr/bin/env bash
# rollback_b.sh — undo step B (revert config). S4 transactional rollback.
set -euo pipefail
SERVICE=""; DRY_RUN=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    --service) SERVICE="$2"; shift 2 ;;
    --dry-run) DRY_RUN=true; shift ;;
    *) echo "Unknown arg: $1"; exit 1 ;;
  esac
done
if $DRY_RUN; then echo "[DRY-RUN] rollback-B: would revert config on $SERVICE"; exit 0; fi
echo "[rollback-b] reverted config on $SERVICE"
exit 0
