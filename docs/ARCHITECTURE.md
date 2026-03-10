# Architecture

## Source Of Truth
- GitHub Issues: work item 本体
- GitHub Projects v2: canonical scheduler state
- Discord: conversation UI / planning approval UI / status mirror

## Control Plane
- Python orchestrator が唯一の control plane
- planning lane は Claude Agent SDK に限定する
- execution lane は Codex app-server に統一する
- repo-owned contract は `WORKFLOW.md` / `AGENTS.md` / Skills が担う

## Runtime Model
- workspace key: `{owner}/{repo}#{issue_number}`
- per-issue workspace を再利用し、retry / recovery 時も同じ worktree を使う
- 永続 source は GitHub issue workpad と filesystem artifacts
- DB なしでも再開できる構成を優先する

## State Model
- `Backlog`
- `Ready`
- `In Progress`
- `Human Review`
- `Rework`
- `Merging`
- `Done`
- `Blocked`
- `Cancelled`

## Required Artifacts
- `issue_snapshot.json`
- `requirement_summary.json`
- `plan.json`
- `test_plan.json`
- `verification.json`
- `changed_files.json`
- `final_summary.json`
- `run.log`
- `discord_events.jsonl`
- `workpad_updates.jsonl`
- `runner_metadata.json`
