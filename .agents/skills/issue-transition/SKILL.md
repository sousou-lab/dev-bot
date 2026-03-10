---
name: issue-transition
description: Use this skill whenever GitHub Issue state, Project v2 fields, or the workpad changes.
metadata:
  short-description: Update issue tracking state
---

# issue-transition

Use this skill whenever GitHub Issue state, Project v2 fields, or the workpad changes.

Workflow:
1. Determine the target state from the scheduler contract.
2. Update Project v2 `State` and `Plan` fields if available.
3. Fall back to the single state label only when Project v2 is unavailable.
4. Update the persistent workpad comment in the same transition.
5. Write an audit trail entry describing why the transition happened.
