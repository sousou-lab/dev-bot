#!/bin/sh
# Hook: after_run
# Runs after implementation execution completes (success or failure).
# Environment: DEVBOT_WORKSPACE_DIR, DEVBOT_ISSUE_NUMBER, DEVBOT_REPO, DEVBOT_RUN_STATUS
set -e
echo "[after_run] Execution completed: status=${DEVBOT_RUN_STATUS:-unknown}"
