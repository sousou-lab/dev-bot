---
name: safe-push
description: Use this skill before pushing or creating a draft PR.
metadata:
  short-description: Check branch and push safety
---

# safe-push

Use this skill before pushing or creating a draft PR.

Workflow:
1. Verify the branch name matches `agent/gh-{issue_number}-{slug}`.
2. Verify the remote URL does not embed a token.
3. Confirm only workspace-local files were changed.
4. Refuse force-push to protected branches.
5. Push the issue branch and preserve logs and verification artifacts.
