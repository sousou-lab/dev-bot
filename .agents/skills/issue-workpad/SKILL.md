---
name: issue-workpad
description: Use this skill before changing code or tracker state for a GitHub issue.
metadata:
  short-description: Sync issue context before changes
---

# issue-workpad

Use this skill before changing code or tracker state for a GitHub issue.

Workflow:
1. Read the issue body, workpad comment, `plan.json`, and `test_plan.json`.
2. Confirm the scheduler gates: `State` in `Ready|In Progress|Rework` and `Plan=Approved`.
3. Summarize goal, acceptance criteria, constraints, blockers, current branch, and PR status.
4. If local artifacts and workpad disagree, treat GitHub workpad and Project v2 fields as source of truth.
5. Record any mismatch before implementation continues.
