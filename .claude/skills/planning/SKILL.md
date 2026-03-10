---
name: planning
description: 要件を実装可能な plan.json に変換する。変更候補ファイル、実装手順、検証手順、リスク整理が必要なときに使う。
---

# Purpose
曖昧な要件を、実装可能な計画に変換する。

# Inputs
- requirement_summary.json
- repo_profile.json
- WORKFLOW.md
- docs/

# Rules
- 実装しない
- Bash を使わない
- ToolSearch を使わない
- 使用可能ツールは Read / Grep / Glob のみ
- skill や追加資料を探索しに行かず、この skill の指示と入力だけで完結する
- 変更対象ファイルは候補として出す
- migration や secrets 変更は必ず risk に明示する
