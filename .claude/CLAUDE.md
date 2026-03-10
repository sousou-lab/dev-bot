# CLAUDE.md

This repository uses Claude Agent SDK only for the planning lane.

Rules for planning runs:
- Use `query()` for one-shot planning steps.
- Use `ClaudeSDKClient` only when the orchestrator explicitly needs a persistent multi-turn planning session.
- Planning is read-only. Allowed tools are `Read`, `Grep`, and `Glob`.
- Load project configuration with `setting_sources=["project"]`.
- Load project skills from `.claude/skills/`.
- Do not use Claude for the main implementation loop.
- Do not call the Claude CLI directly. Use `claude-agent-sdk` as a library.
