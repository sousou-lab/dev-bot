# Architecture

## Control Plane
- Discord command handling
- requirement summary
- planning
- test design
- verification
- review
- approvals
- orchestration state

## Execution Plane
- Codex CLI non-interactive execution
- repository-local edits
- test and lint command execution
- changed files detection

## State Model
- `draft`
- `planned`
- `awaiting_plan_approval`
- `queued`
- `running`
- `awaiting_high_risk_approval`
- `verifying`
- `completed`
- `failed`
- `aborted`

## Artifacts
- `requirement_summary.json`
- `plan.json`
- `test_plan.json`
- `repo_profile.json`
- `codex_run.log`
- `changed_files.json`
- `command_results.json`
- `verification_summary.json`
- `review_summary.json`
- `final_result.json`
