---
version: 1
repo_type: python
default_branch: main
workspace:
  reuse_per_issue: true
  allow_destructive_reset_on_population_failure: false

execution:
  planner: claude
  implementer: codex-cli
  verifier: claude
  reviewer: claude

permissions:
  default_mode: acceptEdits
  high_risk_requires_approval: true
  risky_commands:
    - "alembic revision --autogenerate"
    - "alembic upgrade head"
    - "terraform apply"
    - "kubectl apply"
    - "rm -rf"
    - "git push --force"
  protected_paths:
    - ".github/workflows/**"
    - "migrations/**"
    - "infra/prod/**"

commands:
  setup:
    - "pip install -r requirements.txt"
  lint:
    - "python -m compileall app"
  test:
    - "python -m compileall app"
  build: []
  migrate_test: []

proof_of_work:
  required_artifacts:
    - "plan.json"
    - "test_plan.json"
    - "changed_files.json"
    - "command_results.json"
    - "verification_summary.json"
    - "review_summary.json"
  require_tests_pass: true
  require_review_summary: true

retry_policy:
  max_attempts: 3
  retry_on:
    - "test_failure"
    - "command_failure"
    - "transient_tool_error"
  no_retry_on:
    - "policy_violation"
    - "missing_credentials"
    - "approval_denied"
---

# Workflow Contract

## Goal
この repo では最小変更で目的を達成する。

## Planning
- 実装前に plan を必須とする
- `plan.json` と `test_plan.json` がなければ実装を開始しない

## Implementation
- 実装担当は Codex CLI
- protected path の変更は approval なしでは行わない
- PostgreSQL migration は Alembic の revision file 生成までに留める

## Verification
- 検証担当は Claude
- テスト結果、差分、コマンド結果を要約する

## Review
- レビュー担当は Claude
- 不要変更、protected path 変更、テスト不足を確認する
