# AGENTS.md

## Purpose
このリポジトリは Discord から起動される自律型開発オーケストレータである。
制御面は `claude-agent-sdk`、実装面は Codex CLI worker を前提にする。

## Architecture Map
- Control plane は Claude Agent SDK を使う
- Implementation worker は Codex CLI を使う
- 実行契約は `WORKFLOW.md` を system of record とする
- Skills は `.claude/skills/` に置く
- 詳細設計は `docs/ARCHITECTURE.md` に置く

## Required Rules
- 実装前に `/plan` を通し、`plan.json` と `test_plan.json` を生成する
- Claude の新規実装は必ず `claude-agent-sdk` の公式 API を使う
- 高リスク操作は approval gateway を通す
- proof-of-work artifact を揃えてから PR を作成する
- 同一 issue / thread の workspace は再利用する
- `/abort` は状態更新だけでなく実プロセス停止まで行う

## Allowed Stacks
- `claude-agent-sdk`
- Codex CLI
- Python subprocess wrappers for Codex / git / test commands

## Prohibited
- Claude 実行を CLI 直叩きに置き換えること
- Codex を control plane として使うこと
- `bypassPermissions` を通常実行の既定値にすること
- repo 外ディレクトリを agent に触らせること
- 実行中プロセスを止めない `/abort`

## Pointer Docs
- `WORKFLOW.md`
- `docs/ARCHITECTURE.md`
- `.claude/skills/`
