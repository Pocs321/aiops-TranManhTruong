#!/usr/bin/env bash
# step_b.sh — deploy step B (apply config). S4 transactional-deploy wrapper.
set -euo pipefail
SERVICE=""; DRY_RUN=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    --service) SERVICE="$2"; shift 2 ;;
    --dry-run) DRY_RUN=true; shift ;;
    *) echo "Unknown arg: $1"; exit 1 ;;
  esac
done
if $DRY_RUN; then echo "[DRY-RUN] step-B: would apply new config to $SERVICE"; exit 0; fi
echo "[step-b] applied new config to $SERVICE"
exit 0
