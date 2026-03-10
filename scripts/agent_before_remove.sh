#!/bin/sh
# Hook: before_remove
# Runs before a workspace is removed.
# Environment: DEVBOT_WORKSPACE_DIR, DEVBOT_ISSUE_NUMBER, DEVBOT_REPO
set -e
echo "[before_remove] Cleaning up workspace: ${DEVBOT_WORKSPACE_DIR:-unknown}"
