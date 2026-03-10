#!/bin/sh
# Hook: before_run
# Runs before implementation execution starts.
# Environment: DEVBOT_WORKSPACE_DIR, DEVBOT_ISSUE_NUMBER, DEVBOT_REPO
set -e
echo "[before_run] Starting execution for issue #${DEVBOT_ISSUE_NUMBER:-unknown}"
