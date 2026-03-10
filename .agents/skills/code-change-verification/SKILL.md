---
name: code-change-verification
description: Use this skill after code, tests, or build behavior changes.
metadata:
  short-description: Verify code and artifacts
---

# code-change-verification

Use this skill after code, tests, or build behavior changes.

Workflow:
1. Run repository-defined verification commands.
2. Update `changed_files.json`, `verification.json`, and `final_summary.json`.
3. Capture failing commands exactly and classify them as `Rework`, `Blocked`, or `Human Review`.
4. Check for protected-path edits, secret leakage, and missing proof-of-work artifacts.
5. Do not mark the run complete until verification artifacts match the final state.
