---
name: test-design
description: 要件と既存コードから test_plan.json を作る。境界条件、異常系、回帰観点を整理する。
---

# Purpose
実装前に必要なテストを定義する。

# Rules
- 実装しない
- ToolSearch を使わない
- 使用可能ツールは Read / Grep / Glob のみ
- skill や追加資料を探索しに行かず、この skill の指示と入力だけで完結する
- 既存テストの流儀に合わせる
- 境界条件と回帰観点を最低限含める
