# First PR Scope

The first PR should stop at the policy and tracker boundary.

Success criteria:
1. Replace root `AGENTS.md`.
2. Introduce the new `WORKFLOW.md`.
3. Add `.agents/skills/` and `.claude/CLAUDE.md`.
4. Keep GitHub App settings in `config.py`.
5. Read one issue's Project v2 `State` and `Plan` gating information.

Rationale:
- The current repo is still small and Python-centric.
- Replacing policy, auth, workspace identity, and runner behavior in one PR is review-heavy.
- Reading the scheduler contract correctly before broad runtime changes is the safer cut.
