# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

dev-bot is a Discord-driven software development automation system. It orchestrates issue-to-PR workflows using GitHub Issues + GitHub Projects v2 as the source of truth. Planning uses Claude Agent SDK (read-only); implementation uses Codex app-server.

## Commands

```bash
# Run the bot
python -m app.main

# Run all tests
python -m pytest tests/ -v

# Run a single test file
python -m pytest tests/test_orchestrator.py -v

# Run a specific test
python -m pytest tests/test_orchestrator.py::OrchestratorTests::test_enqueue_sets_status_and_prevents_duplicates -v

# Install dependencies
uv sync
npm install

# Lint check
uv run ruff check app/ tests/

# Format check
uv run ruff format --check app/ tests/

# Auto-fix lint issues
uv run ruff check app/ tests/ --fix

# Auto-format
uv run ruff format app/ tests/

# Type check
uv run pyright app/
```

## Architecture

```
Discord → discord_adapter.py (DevBotClient)
  → orchestrator.py (async queue, concurrency control, dedup)
    ├─ Planning lane: planning_agent.py → claude-agent-sdk (Read/Grep/Glob only)
    └─ Execution lane: runners/codex_runner.py → codex app-server
  → pipeline.py (workspace setup → run → verify → review → PR)
  → github_client.py (GitHub App auth, Projects v2 field updates)
  → state_store.py (file-based JSON artifacts, no DB)
```

**Key modules:**
- `app/config.py` — Pydantic settings from env vars. Required: `DISCORD_BOT_TOKEN`, `GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY_PATH`, `GITHUB_APP_INSTALLATION_ID`.
- `app/orchestrator.py` — Async task queue with workspace-key dedup and max concurrency.
- `app/workspace_manager.py` — Git bare mirror + per-issue worktree isolation. Key format: `{owner}/{repo}#{issue_number}`.
- `app/pipeline.py` — Full run lifecycle: plan verification → workspace prep → codex run → claude verify/review → draft PR.
- `app/state_store.py` — FileStateStore persisting run metadata and artifacts under `runs/{thread_id}/`.
- `app/planning_agent.py` — Claude Agent SDK one-shot queries for plan generation.
- `app/runners/codex_runner.py` — Subprocess wrapper for `codex app-server`.
- `app/runners/claude_runner.py` — Verification and code review via Claude.

**State transitions:** Backlog → Planning → Ready → In Progress → Human Review → (Rework ↔ In Progress) → Done

## Planning Lane Rules

- Use `query()` for one-shot planning steps.
- Use `ClaudeSDKClient` only when the orchestrator explicitly needs a persistent multi-turn planning session.
- Planning is read-only. Allowed tools are `Read`, `Grep`, and `Glob`.
- Load project configuration with `setting_sources=["project"]`.
- Load project skills from `.claude/skills/`.
- Do not use Claude for the main implementation loop.
- Do not call the Claude CLI directly. Use `claude-agent-sdk` as a library.

## Skill Directories

- `.claude/skills/` — Claude planning lane skills (planning, code-review, verification, test-design, symphony-workflow).
- `.agents/skills/` — Codex implementation lane skills (code-change-verification, implementation-plan, issue-workpad, draft-pr, safe-push, issue-transition).

## Testing

Tests use `unittest` + `asyncio.IsolatedAsyncioTestCase`. All tests are in `tests/` and follow the `test_*.py` naming convention. Tests heavily use mocking (`unittest.mock`) since the system depends on external services (Discord, GitHub, Git, Codex, Claude).

## Configuration

- `WORKFLOW.md` — YAML frontmatter defining tracker, workspace, agent, codex, planning, and verification settings, followed by markdown operating rules for the implementation worker.
- `AGENTS.md` — Short Codex-facing repository policy (skill usage, execution boundaries, security rules).
- `.env` — Runtime secrets (not committed). See `.env.example`.

# currentDate
Today's date is 2026-03-11.

      IMPORTANT: this context may or may not be relevant to your tasks. You should not respond to this context unless it is highly relevant to your task.
