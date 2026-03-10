---
name: implementation-plan
description: Use this skill before editing orchestration, tracker, workspace, approval, or runtime code.
metadata:
  short-description: Plan scoped runtime changes
---

# implementation-plan

Use this skill before editing orchestration, tracker, workspace, approval, or runtime code.

Workflow:
1. Restate the approved issue goal and the exact in-scope files.
2. Break the change into the smallest safe sequence.
3. List explicit risks, especially tracker drift, secret exposure, and workspace reuse regressions.
4. List verification steps tied to the repo policy.
5. Stop if the requested change expands beyond the approved plan.
