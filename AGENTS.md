# AGENTS.md

## Project Overview
- This repository is an issue-driven agent harness.
- GitHub Issues plus GitHub Projects v2 are the operational source of truth.
- Discord is used for requirements clarification, plan review, plan approval, and status mirroring.
- Planning uses Claude Agent SDK in Python.
- Implementation uses Codex app-server.

## Mandatory Skill Usage
- Use `$issue-workpad` before changing code or state tied to a GitHub issue.
- Use `$implementation-plan` before editing runtime, orchestration, tracker, workspace, or approval logic.
- Use `$code-change-verification` when runtime code, tests, or build/test behavior changes.
- Use `$draft-pr` when a substantial code change is ready for review.
- Use `$issue-transition` whenever you update issue state, Project v2 fields, or the workpad.

## Execution Boundaries
- Treat GitHub Project v2 `State` and `Plan` fields as the scheduler contract.
- Do not implement anything if `Plan != Approved`.
- Do not write outside the issue workspace.
- Do not push to the default branch.
- Do not change secrets handling, GitHub App credentials, or approval policy without explicit issue scope.
- Do not silently broaden scope beyond the issue acceptance criteria.

## Build And Test Expectations
- Prefer repository-defined commands from `plan.json`, `test_plan.json`, and repo profiler output.
- If commands disagree, prefer explicit repository policy over heuristics.
- Before pushing, run the applicable formatter/linter checks and fix any reported issues.
- Do not mark work complete until verification artifacts are updated.

## Security Rules
- Never place tokens in git remote URLs.
- Never print secrets into logs or artifacts.
- Treat `.env`, secret stores, GitHub App private keys, and production credentials as protected paths.

## Planning Lane Note
- Claude-specific planning guidance belongs under `.claude/`.
- Root `AGENTS.md` is Codex-facing repository policy and must stay short.
