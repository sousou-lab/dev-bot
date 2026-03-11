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

hooks:
  after_create: ./scripts/agent_after_create.sh
  before_run: ./scripts/agent_before_run.sh
  after_run: ./scripts/agent_after_run.sh
  before_remove: ./scripts/agent_before_remove.sh
  timeout_ms: 60000

agent:
  max_concurrent_agents: 3
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

planning:
  provider: claude-agent-sdk
  enabled: true
  cwd_source: plan_workspace
  max_turns: 4
  timeout_seconds: 300
  settings_sources: [project]
  allowed_tools: [Read, Grep, Glob]
  skill_mode: explicit_project_filesystem

verification:
  required_artifacts:
    - issue_snapshot.json
    - requirement_summary.json
    - plan.json
    - test_plan.json
    - changed_files.json
    - verification.json
    - final_summary.json
    - run.log
  required_checks:
    - tests
    - lint
    - typecheck

github:
  auth: app
  create_draft_pr: true
  merge_by_bot: true
  update_project_fields: true
  sync_workpad: true
  state_source_of_truth: project_v2
  require_plan_field_approved: true

discord:
  enabled: true
  mode: planning_and_status_surface
  bind_command: /issue bind
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
