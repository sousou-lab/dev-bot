# dev-bot

GitHub Issue を Discord から依頼するだけで、計画・実装・検証・PR作成まで自動で完了させる開発エージェントシステム。

このシステムは symphonyn の思想を受け継ぎ、対話 UI と実行系を分離しつつ、GitHub 上の状態を実行の正本として扱う設計を採用している。

## 何ができるか

1. **Discord で自然言語で依頼** — 「ログイン画面を追加して」のように伝える
2. **要件整理** — AI が不足情報を質問し、実装可能な要件にまとめる
3. **計画生成** — 対象リポジトリを読み取り、変更ファイル・手順・テスト計画を自動作成
4. **承認ゲート** — 人間が計画を確認し、Discord 上で承認/却下
5. **自動実装** — 承認された計画に基づき、コードを生成・テスト・検証
6. **Draft PR 作成** — 変更を GitHub に push し、レビュー可能な PR を自動作成

```
Discord で依頼 → 要件整理 → 計画生成 → 人間が承認 → 自動実装 → 検証 → Draft PR
```

## アーキテクチャ

```
┌─────────────────────────────────────────────────────┐
│  Discord (対話 UI / 承認 / ステータス表示)             │
└──────────────────────┬──────────────────────────────┘
                       │
              discord_adapter.py
                       │
              orchestrator.py  ← 非同期キュー / 重複防止 / 並行数制御
                       │
         ┌─────────────┴─────────────┐
         │                           │
   計画レーン                    実装レーン
   Claude Agent SDK              Codex app-server
   (Read/Grep/Glob のみ)         (workspace 内書き込み)
         │                           │
         └─────────────┬─────────────┘
                       │
              pipeline.py  ← planning artifacts → attempt/candidates → winner選定 → PR
                       │
         ┌─────────────┼─────────────┐
         │             │             │
  workspace_manager  state_store   github_client
  (git worktree)     (JSON永続化)   (GitHub App認証)
```

**Source of Truth:**
- **GitHub Issue** — 作業項目の本体
- **GitHub Projects v2** — スケジューラ状態（`State` / `Plan` フィールド）
- **Discord スレッド** — 対話・承認・ステータス通知
- **Filesystem artifacts** — 実行ログ・計画・検証結果（DB不要）

## セットアップ

### 前提条件

- Python 3.11+
- Node.js（claude-agent-sdk 用）
- Git
- [GitHub App](docs/GITHUB_APP_SETUP.md) の作成と設定
- [GitHub Projects v2](docs/PROJECT_V2_SETUP.md) のフィールド設定
- Discord Bot の作成

### インストール

```bash
pip install -r requirements.txt
npm install
```

### 環境変数

```bash
cp .env.example .env
```

`.env` に以下を設定:

| 変数 | 必須 | 説明 |
|------|------|------|
| `DISCORD_BOT_TOKEN` | Yes | Discord Bot トークン |
| `DISCORD_GUILD_ID` | Yes | 対象サーバーID |
| `GITHUB_APP_ID` | Yes | GitHub App ID |
| `GITHUB_APP_PRIVATE_KEY_PATH` | Yes | GitHub App 秘密鍵のパス |
| `GITHUB_APP_INSTALLATION_ID` | Yes | GitHub App Installation ID |
| `GITHUB_OWNER` | Yes | 対象 Organization/User |
| `GITHUB_REPO` | Yes | 対象リポジトリ名 |
| `ANTHROPIC_API_KEY` | Yes* | 計画レーン用（`PLANNING_LANE_ENABLED=true` 時） |
| `GITHUB_PROJECT_ID` | No | Projects v2 連携時に必要 |
| `STATE_DIR` | No | 状態保存先（デフォルト: `./runs`） |
| `WORKSPACE_ROOT` | No | worktree 保存先 |

### 起動

```bash
python -m app.main
```

`GITHUB_PROJECT_ID` を設定して Projects v2 を scheduler contract として使う場合は、`GITHUB_PROJECT_STATE_FIELD_ID` / `GITHUB_PROJECT_STATE_OPTION_IDS` / `GITHUB_PROJECT_PLAN_FIELD_ID` / `GITHUB_PROJECT_PLAN_OPTION_IDS` も必須です。不足している場合は起動時 validation で停止します。

## Discord コマンド

| コマンド | 説明 |
|---------|------|
| `/plan <repo>` | 要件整理を開始し、計画を生成 |
| `/approve-plan` | 計画を承認して実装可能状態にする |
| `/reject-plan` | 計画を却下して修正を要求 |
| `/abort` | 実行中のタスクを中止 |

## Issue のライフサイクル

```
Backlog → Ready → In Progress → Human Review → Merging → Done
                     ↓              ↓
                  Blocked        Rework ───────────────┘
                     ↓
                 Cancelled
```

新しい実行を開始するには、Projects v2 で `State` が `Ready` または `Rework`、かつ `Plan` が `Approved` である必要がある。`In Progress` は新規 dispatch ではなく restore / reconcile 対象。

## Workspace 戦略

Issue ごとに bare mirror を共有しつつ、execution は attempt / candidate 単位の Git worktree で分離する。実装ブランチは `agent/gh-{issue_number}-{slug}-{attempt_id}-{candidate_id}` を使い、winner だけを canonical views に昇格させる。

planning artifacts は issue 配下の `planning/` に保持し、execution artifacts は `attempts/{attempt_id}/candidates/{candidate_id}/` に保持する。session rollover が発生しても candidate と attempt は維持し、handoff bundle だけを次 session へ渡す。

## テスト

```bash
# 全テスト実行
python -m pytest tests/ -v

# 個別テスト
python -m pytest tests/test_orchestrator.py -v

# 特定テストケース
python -m pytest tests/test_orchestrator.py::OrchestratorTests::test_enqueue_sets_status_and_prevents_duplicates -v
```

## プロジェクト構成

```
app/                    # メインアプリケーション
├── main.py             # エントリポイント
├── config.py           # Pydantic 設定管理
├── discord_adapter.py  # Discord Bot / コマンドハンドラ
├── orchestrator.py     # 非同期タスクキュー
├── pipeline.py         # 実装パイプライン全体
├── planning_agent.py   # Claude Agent SDK 計画エージェント
├── requirements_agent.py # 要件整理対話エージェント
├── workspace_manager.py  # Git worktree 管理
├── github_client.py    # GitHub App API クライアント
├── state_store.py      # JSON ファイルベース永続化
└── runners/
    ├── codex_runner.py  # Codex 実装実行
    └── claude_runner.py # Claude 検証・レビュー
tests/                  # unittest + asyncio テスト
.claude/skills/         # Claude 計画レーン用スキル
.agents/skills/         # Codex 実装レーン用スキル
WORKFLOW.md             # ランタイム設定契約（YAML + Markdown）
AGENTS.md               # Codex 向けリポジトリポリシー
docs/                   # セットアップガイド・設計文書
```

## 関連ドキュメント

- [ARCHITECTURE.md](docs/ARCHITECTURE.md) — 全体設計
- [GITHUB_APP_SETUP.md](docs/GITHUB_APP_SETUP.md) — GitHub App 認証の設定手順
- [PROJECT_V2_SETUP.md](docs/PROJECT_V2_SETUP.md) — Projects v2 フィールド設定
- [WORKFLOW.md](WORKFLOW.md) — ランタイム設定と実装者向け契約
- [AGENTS.md](AGENTS.md) — Codex 実装者向けポリシー
