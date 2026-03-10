#!/bin/sh
# Hook: after_create
# Runs after a workspace is created for an issue.
# Environment: DEVBOT_WORKSPACE_DIR, DEVBOT_ISSUE_NUMBER, DEVBOT_REPO
set -e
echo "[after_create] Workspace ready: ${DEVBOT_WORKSPACE_DIR:-unknown}"
