---
tracker:
  kind: github
  mode: project_v2
  owner: your-org
  repo: dev-bot
  project:
    scope: organization
    number: 7
    state_field: State
    plan_field: Plan
    branch_field: Agent Branch
    pr_field: Agent PR
    dispatch_states: [Ready, Rework]
    reconcile_states: [In Progress]
    merge_states: [Merging]
    active_states: [Ready, In Progress, Rework, Merging]
    terminal_states: [Done, Cancelled]
    planning_states: [Backlog]
    state_values:
      backlog: Backlog
      ready: Ready
      in_progress: In Progress
      human_review: Human Review
      rework: Rework
      merging: Merging
      blocked: Blocked
      done: Done
      cancelled: Cancelled
    plan_values:
      none: Not Started
      drafted: Drafted
      approved: Approved
      changes_requested: Changes Requested
  workpad:
    marker: "<!-- dev-bot-workpad -->"
    mode: single_persistent_comment

polling:
  interval_ms: 15000
  continuation_retry_ms: 1000
  failure_backoff_ms: [1000, 5000, 15000, 60000, 300000]

workspace:
  root: /var/lib/dev-bot/workspaces
  strategy: mirror_plus_worktree
  keep_successful_workspaces: true
  key_template: "{owner}/{repo}#{issue_number}"
  branch_template: "agent/gh-{issue_number}-{slug}"
  candidate_branch_template: "agent/gh-{issue_number}-{slug}-{attempt_id}-{candidate_id}"

hooks:
  after_create: ./scripts/agent_after_create.sh
  before_run: ./scripts/agent_before_run.sh
  after_run: ./scripts/agent_after_run.sh
  before_remove: ./scripts/agent_before_remove.sh
  timeout_ms: 60000

agent:
  max_concurrent_agents: 7
  max_turns: 20
  max_stalled_ms: 300000
  plan_required: true
  runtime_approval: false

codex:
  command: codex app-server
  model: gpt-5.4
  reasoning_effort: medium
  summary: concise
  approval_policy: never
  thread_sandbox: workspace-write
  writable_roots: ["{{ workspace.repo_dir }}"]
  network_access: false
  turn_timeout_ms: 3600000
  read_timeout_ms: 5000
  allow_turn_steer: true
  allow_thread_resume_same_run_only: true
  compaction_policy:
    turn_count_gte: 12
    steer_count_gte: 2
    repair_cycles_gte: 3
  service_name: dev-bot

review:
  enabled: true
  provider: claude-agent-sdk
  rules_file: REVIEW.md
  post_inline_to_github: true
  thresholds:
    min_confidence_to_report: 0.80
    verifier_required: true
  roles:
    diff_reviewer: {}
    history_reviewer: {}
    contract_reviewer: {}
    test_reviewer: {}
    evidence_verifier: {}
    ranker: {}

planning:
  provider: claude-agent-sdk
  enabled: true
  mode: auto
  test_plan_max_parallelism: 3
  # `mode: auto` を使うと、要件規模に応じて committee / legacy を自動選択する。
  # autoselect_committee:
  #   min_acceptance_criteria: 12
  #   min_acceptance_criteria_when_complex: 8
  #   min_summary_chars_when_complex: 2800
  #   min_repo_files: 120
  #   min_acceptance_criteria_with_large_repo: 6
  legacy_fallback:
    enabled: true
    use_only_on_committee_failure: true
  cwd_source: plan_workspace
  max_turns: 4
  timeout_seconds: 300
  settings_sources: [project]
  allowed_tools: [Read, Grep, Glob]
  skill_mode: explicit_project_filesystem
  committee:
    roles:
      repo_explorer:
        mode: query
        allowed_tools: [Read, Grep, Glob]
        disallowed_tools: [Edit, Write, Bash, WebSearch, WebFetch]
        output_schema: repo_explorer_v1
      risk_test_planner:
        mode: query
        allowed_tools: [Read, Grep, Glob]
        disallowed_tools: [Edit, Write, Bash, WebSearch, WebFetch]
        output_schema: risk_test_plan_v1
      constraint_checker:
        mode: query
        allowed_tools: [Read, Grep, Glob]
        disallowed_tools: [Edit, Write, Bash, WebSearch, WebFetch]
        output_schema: constraint_report_v1
      merger:
        mode: query
        allowed_tools: [Read, Grep, Glob]
        disallowed_tools: [Edit, Write, Bash, WebSearch, WebFetch]
        output_schema: plan_v2
  gates:
    require_out_of_scope: true
    require_candidate_files: true
    require_test_mapping: true
    require_verification_profile: true
    require_design_branches: true

implementation:
  backend: codex-app-server
  optional_backends: [codex-sdk-sidecar]
  single_writer_default: true
  candidate_mode:
    enabled: true
    max_parallel_editors: 2
    triggers:
      rework_count_gte: 1
      planner_confidence_lt: 0.75
      require_clear_design_branches: false
  push_policy:
    push_only_winner: true
    cleanup_loser_local_branches: true

replanning:
  enabled: true
  auto_replan_on_reject_reasons:
    - plan_misalignment
    - scope_drift
  max_replans_per_issue: 2
  emit_replan_reason_artifact: true
  create_new_attempt_on_replan: true

protected_config:
  default: deny
  allow_label: allow-protected-config
  protected_paths:
    - AGENTS.md
    - WORKFLOW.md
    - CLAUDE.md
    - .claude/**
    - .github/workflows/**
    - docs/policy/**
    - docs/GITHUB_APP_SETUP.md
    - docs/PROJECT_V2_SETUP.md
  allowlist_source:
    priority:
      - issue_body_section: "保護設定変更許可リスト"
      - artifact: protected_config_allowlist.json

verification:
  required_artifacts:
    - issue_snapshot.json
    - requirement_summary.json
    - plan.json
    - test_plan.json
    - verification_plan.json
    - changed_files.json
    - implementation_result.json
    - repair_feedback.json
    - review_result.json
    - review_decision.json
    - review_findings.json
    - postable_findings.json
    - verification.json
    - final_summary.json
    - run.log
    - workpad_updates.jsonl
    - runner_metadata.json
    - incident_bundle_manifest.json
    - incident_bundle_summary.md
  required_checks:
    - name: format
      command: uv run ruff format --check app/ tests/
    - name: lint
      command: uv run ruff check .
    - name: tests
      command: uv run python -m pytest -q
    - name: typecheck
      command: uv run pyright app

github:
  auth: app
  create_draft_pr: true
  merge_by_bot: true
  update_project_fields: true
  sync_workpad: true
  state_source_of_truth: project_v2
  require_plan_field_approved: true

debug:
  incident_bundle:
    enabled: true
    mount_readonly: true
    freeze_on_human_review: true
    cleanup_on_terminal: true
    keep_provenance_after_cleanup: true

evals:
  enabled: true
  strategy: synthetic_first
  fixtures_root: tests/agent_evals/fixtures/python
  graders:
    planning: mechanical_plus_judge
    review: precision_recall_plus_judge
    implementation: pass1_scope_latency

telemetry:
  sink: jsonl
  otel_compatible_fields: true

discord:
  enabled: true
  mode: planning_and_status_surface
  bind_command: /issue
  plan_command: /plan
  approve_plan_command: /approve-plan
  reject_plan_command: /reject-plan
  abort_command: /abort
  runtime_approval: false
---

You are the dev-bot implementation worker for GitHub issue-based delivery.

Operating rules:
- The source of truth is the GitHub issue, its Project v2 fields, and the persistent workpad comment.
- Only start a new implementation run unless Project field `Plan` is `Approved` and Project field `State` is one of `Ready` or `Rework`.
- Treat Project field `State = In Progress` as an already-active run that must be reconciled, not as a signal to start a second run.
- Treat Project field `State = Merging` as an active post-approval land step owned by the agent.
- Reserve `thread/resume` for crash recovery of the same `run_id`; do not use it for normal retries.
- Only modify files inside the current issue workspace.
- Never push directly to the default branch.
- Use the repository root `AGENTS.md` and repo-local skills before making substantial changes.
- Keep changes narrowly scoped to the issue goal and acceptance criteria.
- If you discover out-of-scope work, record it in the workpad and stop expanding scope.
- Before finishing, update verification artifacts and produce a draft PR summary.
- Do not use Discord thread-local state as the execution source of truth; treat Discord as intake, approval, status, and abort UI only.

Required workflow:
1. Read the workpad and the issue body.
2. Use `$issue-workpad` to sync the local picture of the issue.
3. Use `$implementation-plan` before editing if the change is not obviously trivial.
4. Implement only the approved plan.
5. Use `$code-change-verification` when code, tests, or build behavior changed.
6. Use `$draft-pr` once the branch is ready for review.
7. Update the workpad with branch, PR, verification, and blockers before ending the run.

Verification command policy:
- Prefer the commands in `verification.required_checks` as the canonical pre-push checks.
- Run Python-based tooling with `uv run` unless an issue-specific plan explicitly overrides it.
- When Ruff formatting is part of the repo policy, run `uv run ruff format --check` before push in addition to `ruff check`.
