from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import json
import re
import shutil
import subprocess
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass, is_dataclass
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Protocol, cast

from app.approvals import ApprovalCoordinator, is_high_risk_command
from app.config import Settings
from app.contracts.artifact_models import ReviewResult, VerificationResult
from app.debug.bundle_builder import IncidentBundleBuilder
from app.github_client import GitHubIssueClient
from app.implementation.candidate_policy import candidate_rank_tuple, eligible, exact_tie, select_winner
from app.process_registry import ProcessRegistry
from app.proof_of_work import evaluate_proof_of_work
from app.repo_profiler import build_repo_profile
from app.review.github_poster import GitHubReviewPoster
from app.runners.claude_runner import ClaudeRunner
from app.runners.codex_runner import CodexRunner, RunIdentity
from app.state_store import FileStateStore
from app.telemetry.jsonl import JsonlTelemetrySink
from app.verification_profiles import build_verification_plan, workflow_verification_from_plan
from app.workflow_loader import load_workflow, workflow_text
from app.workspace_manager import WorkspaceManager


@dataclass(frozen=True)
class ExecutionContext:
    issue_key: str
    thread_id: int
    attempt_id: str
    run_id: str
    repo_full_name: str
    issue: dict[str, Any]


@dataclass(frozen=True)
class CandidateExecutionResult:
    candidate_id: str
    workspace_info: dict[str, Any]
    codex_result: Any
    codex_log_path: str
    changed_files: dict[str, Any]
    scope_analysis: dict[str, Any]
    command_results: dict[str, Any]
    verification: dict[str, Any]
    verification_json: dict[str, Any]
    review: dict[str, Any]
    proof_result: dict[str, Any]
    success: bool
    failure_type: str = ""
    failure_state: str = "Rework"
    duration_ms: int = 0


class ChatChannel(Protocol):
    async def send(self, content: str) -> None: ...


class ChatClient(Protocol):
    def get_channel(self, channel_id: int) -> ChatChannel | None: ...


class DevelopmentPipeline:
    PROTECTED_CONFIG_PATTERNS: tuple[str, ...] = (
        "AGENTS.md",
        "WORKFLOW.md",
        "CLAUDE.md",
        ".claude/**",
        ".github/workflows/**",
        "pyproject.toml",
        "docs/policy/**",
        "docs/GITHUB_APP_SETUP.md",
        "docs/PROJECT_V2_SETUP.md",
    )
    PROTECTED_CONFIG_ALLOW_LABEL = "allow-protected-config"
    PROTECTED_CONFIG_ALLOWLIST_SECTION = "保護設定変更許可リスト"

    def __init__(
        self,
        settings: Settings,
        state_store: FileStateStore,
        github_client: GitHubIssueClient,
        process_registry: ProcessRegistry,
        approval_coordinator: ApprovalCoordinator,
    ) -> None:
        self.settings = settings
        self.state_store = state_store
        self.github_client = github_client
        self.process_registry = process_registry
        self.approval_coordinator = approval_coordinator
        self.workspace_manager = WorkspaceManager(settings, github_client=github_client)
        self.codex_runner = CodexRunner(
            settings.codex_bin,
            app_server_command=settings.codex_app_server_command,
            model=settings.codex_model,
        )
        self.claude_runner = ClaudeRunner(
            settings.anthropic_api_key,
            max_buffer_size=settings.claude_agent_max_buffer_size,
        )

    async def _run_blocking(self, func: Any, /, *args: Any, **kwargs: Any) -> Any:
        bound = partial(func, *args, **kwargs)
        return await asyncio.to_thread(bound)

    async def abort(self, thread_id: int) -> bool:
        issue_key = self.state_store.issue_key_for_thread(thread_id)
        target = issue_key or thread_id
        stopped = await self._run_blocking(self.process_registry.terminate, target)
        if not stopped and issue_key:
            stopped = await self._run_blocking(self.process_registry.terminate, thread_id)
        if not stopped:
            return False
        self.state_store.update_meta(target, runtime_status="")
        self.state_store.update_status(target, "Blocked")
        if issue_key:
            meta = self.state_store.load_issue_meta(issue_key)
            repo_full_name = str(meta.get("github_repo", "")).strip()
            issue_number = int(str(meta.get("issue_number", "0")).strip() or 0)
            if repo_full_name and issue_number > 0:
                await self._run_blocking(self.github_client.update_issue_state, repo_full_name, issue_number, "Blocked")
        return stopped

    async def execute_run(
        self,
        *,
        client: ChatClient | None = None,
        chat: ChatClient | None = None,
        thread_id: int,
        repo_full_name: str,
        issue: dict[str, Any],
    ) -> None:
        chat_client = client or chat
        if chat_client is None:
            raise RuntimeError("A chat client is required to execute a run.")
        channel = chat_client.get_channel(thread_id)
        if channel is None or not hasattr(channel, "send"):
            raise RuntimeError(f"Chat channel not found for thread_id={thread_id}")

        summary = self.state_store.load_artifact(thread_id, "requirement_summary.json")
        plan = self.state_store.load_artifact(thread_id, "plan.json")
        test_plan = self.state_store.load_artifact(thread_id, "test_plan.json")
        verification_plan = self.state_store.load_artifact(thread_id, "verification_plan.json")
        candidate_decision = self.state_store.load_artifact(thread_id, "candidate_decision.json")
        if not isinstance(summary, dict) or not isinstance(plan, dict) or not isinstance(test_plan, dict):
            raise RuntimeError("Missing planning artifacts before run.")
        if not isinstance(verification_plan, dict):
            verification_plan = {}
        if not isinstance(candidate_decision, dict):
            candidate_decision = {}

        issue_key = f"{repo_full_name}#{int(issue['number'])}"
        attempt_id = self.state_store.create_attempt(issue_key)
        run_id = self.state_store.create_execution_run(issue_key)
        _execution = ExecutionContext(
            issue_key=issue_key,
            thread_id=thread_id,
            attempt_id=attempt_id,
            run_id=run_id,
            repo_full_name=repo_full_name,
            issue=issue,
        )
        artifacts_dir = self.state_store.execution_artifacts_dir(issue_key, run_id)
        run_log_path = artifacts_dir / "run.log"
        workpad_updates_path = artifacts_dir / "workpad_updates.jsonl"
        self._append_run_log(run_log_path, "run start")
        self._record_telemetry_event(
            issue_key=issue_key,
            run_id=run_id,
            event="run_started",
            status="running",
            provider="dev-bot",
        )

        issue_snapshot = await self._run_blocking(self._load_issue_snapshot, repo_full_name, issue)
        self.state_store.write_execution_artifact(issue_key, "issue_snapshot.json", issue_snapshot, run_id)
        self.state_store.write_artifact(issue_key, "issue_snapshot.json", issue_snapshot)

        self.state_store.update_status(issue_key, "In Progress")
        self.state_store.update_meta(issue_key, runtime_status="running")
        self.state_store.record_activity(
            issue_key,
            phase="run_start",
            summary="run を開始しました",
            status="running",
            run_id=run_id,
            details={"repo": repo_full_name, "issue_number": issue.get("number")},
        )
        await self._run_blocking(
            self._update_issue_tracking,
            repo_full_name,
            int(issue["number"]),
            "In Progress",
            self._build_workpad_sections(
                summary=summary,
                plan=plan,
                test_plan=test_plan,
                issue=issue_snapshot,
                current_state="In Progress",
                latest_attempt="run started",
                branch="",
                pr="",
                verification={},
                blockers=[],
                artifacts=["issue_snapshot.json", "plan.json", "test_plan.json", "verification_plan.json", "run.log"],
                audit_trail=[f"{datetime.now(UTC).isoformat()} run started"],
            ),
            workpad_updates_path,
        )
        await channel.send("run を開始しました。workspace を準備します。")

        workspace_info = await self._run_blocking(
            self.workspace_manager.prepare,
            repo_full_name,
            int(issue["number"]),
            thread_id,
            str(self.state_store.execution_run_dir(issue_key, run_id)),
            issue.get("title"),
        )
        self.state_store.record_activity(
            issue_key,
            phase="workspace",
            summary="workspace の準備が完了しました",
            status="running",
            run_id=run_id,
            details={"workspace": workspace_info["workspace"], "branch": workspace_info["branch_name"]},
        )
        self.state_store.write_artifact(issue_key, "workspace.json", workspace_info)
        self.state_store.update_meta(
            issue_key,
            workspace=workspace_info["workspace"],
            branch_name=workspace_info["branch_name"],
            base_branch=workspace_info["base_branch"],
        )
        self._record_telemetry_event(
            issue_key=issue_key,
            run_id=run_id,
            event="workspace_prepared",
            status="ok",
            extra={
                "workspace": workspace_info["workspace"],
                "branch_name": workspace_info["branch_name"],
                "base_branch": workspace_info["base_branch"],
            },
        )

        workflow = self._load_effective_workflow(
            workspace=workspace_info["workspace"],
            verification_plan=verification_plan,
        )
        incident_bundle_dir = self._materialize_incident_bundle(
            workflow=workflow,
            issue_key=issue_key,
            run_id=run_id,
            workspace=workspace_info["workspace"],
            issue=issue,
            summary=summary,
            plan=plan,
            test_plan=test_plan,
        )
        self.state_store.write_execution_artifact(issue_key, "workflow.json", self._json_safe(workflow), run_id)
        self.state_store.write_execution_artifact(issue_key, "requirement_summary.json", summary, run_id)
        self.state_store.write_execution_artifact(issue_key, "plan.json", plan, run_id)
        self.state_store.write_execution_artifact(issue_key, "test_plan.json", test_plan, run_id)
        self.state_store.write_execution_artifact(issue_key, "verification_plan.json", verification_plan, run_id)
        self.state_store.write_execution_artifact(issue_key, "candidate_decision.json", candidate_decision, run_id)
        self.state_store.write_execution_artifact(issue_key, "issue.json", issue, run_id)
        self.state_store.write_attempt_artifact(issue_key, attempt_id, "requirement_summary.json", summary)
        self.state_store.write_attempt_artifact(issue_key, attempt_id, "plan.json", plan)
        self.state_store.write_attempt_artifact(issue_key, attempt_id, "test_plan.json", test_plan)
        self.state_store.write_attempt_artifact(issue_key, attempt_id, "verification_plan.json", verification_plan)
        self.state_store.write_attempt_artifact(issue_key, attempt_id, "candidate_decision.json", candidate_decision)
        effective_workflow = self._json_safe(workflow)
        scope_contract = self._build_scope_contract(
            issue_key=issue_key,
            attempt_id=attempt_id,
            plan=plan,
            workflow=workflow,
            issue=issue_snapshot,
        )
        self.state_store.write_execution_artifact(issue_key, "effective_workflow.json", effective_workflow, run_id)
        self.state_store.write_attempt_artifact(issue_key, attempt_id, "effective_workflow.json", effective_workflow)
        self.state_store.write_execution_artifact(issue_key, "scope_contract.json", scope_contract, run_id)
        self.state_store.write_attempt_artifact(issue_key, attempt_id, "scope_contract.json", scope_contract)
        self.state_store.write_execution_artifact(
            issue_key,
            "runner_metadata.json",
            {
                "runner": "codex",
                "mode": "pending",
                "workspace_key": workspace_info.get("workspace_key", ""),
                "workspace": workspace_info["workspace"],
                "branch_name": workspace_info["branch_name"],
            },
            run_id,
        )
        candidate_ids = self._resolve_candidate_ids(workflow=workflow, candidate_decision=candidate_decision)
        self.state_store.write_attempt_artifact(
            issue_key,
            attempt_id,
            "attempt_manifest.json",
            self._build_attempt_manifest(
                issue_key=issue_key,
                attempt_id=attempt_id,
                run_id=run_id,
                candidate_ids=candidate_ids,
                workflow=effective_workflow,
                plan=plan,
                scope_contract=scope_contract,
            ),
        )
        if len(candidate_ids) > 1:
            await channel.send(f"candidate mode を有効化しました。候補: {', '.join(candidate_ids)}")

        active_workspace_info = workspace_info
        candidate_results = await self._execute_candidates(
            chat_client=chat_client,
            channel=channel,
            issue_key=issue_key,
            run_id=run_id,
            workflow=workflow,
            repo_full_name=repo_full_name,
            issue=issue,
            issue_snapshot=issue_snapshot,
            summary=summary,
            plan=plan,
            test_plan=test_plan,
            verification_plan=verification_plan,
            attempt_id=attempt_id,
            workspace_info=workspace_info,
            candidate_ids=candidate_ids,
        )

        winner_result, winner_selection = self._select_candidate_result(plan=plan, candidate_results=candidate_results)
        if winner_result is not None and bool(winner_selection.get("exact_tie_detected")):
            winner_result, winner_selection, _ = await self._resolve_exact_tie_winner(
                issue_key=issue_key,
                attempt_id=attempt_id,
                run_id=run_id,
                plan=plan,
                candidate_results=candidate_results,
                winner_result=winner_result,
                winner_selection=winner_selection,
            )
        self.state_store.write_attempt_artifact(
            issue_key,
            attempt_id,
            "winner_selection.json",
            {"attempt_id": attempt_id, **winner_selection},
        )
        if winner_result is None:
            self._write_attempt_manifest_status(
                issue_key=issue_key,
                attempt_id=attempt_id,
                status="failed",
                winner_candidate_id=None,
            )
            failed_result = candidate_results[0]
            self._finalize_incident_bundle(incident_bundle_dir, issue_key, run_id)
            await self._finalize_failure(
                issue_key=issue_key,
                thread_id=thread_id,
                run_id=run_id,
                repo_full_name=repo_full_name,
                issue=issue_snapshot,
                summary=summary,
                plan=plan,
                test_plan=test_plan,
                state=failed_result.failure_state,
                failure_type=failed_result.failure_type or "candidate_failed",
                latest_attempt=f"candidate {failed_result.candidate_id} failed",
                branch=failed_result.workspace_info["branch_name"],
                blockers=[],
                artifacts=["final_summary.json", "run.log"],
                verification=failed_result.verification_json,
                extra={},
            )
            if len(candidate_results) == 1:
                await channel.send(self._candidate_failure_message(failed_result))
            else:
                await channel.send("すべての candidate が失敗したため run を終了しました。")
            return

        self._write_attempt_manifest_status(
            issue_key=issue_key,
            attempt_id=attempt_id,
            status="winner_selected",
            winner_candidate_id=winner_result.candidate_id,
        )
        self._promote_candidate_result(
            issue_key=issue_key,
            attempt_id=attempt_id,
            run_id=run_id,
            candidate_result=winner_result,
        )
        self.state_store.write_attempt_artifact(
            issue_key,
            attempt_id,
            "final_attempt_summary.json",
            self._build_final_attempt_summary(
                attempt_id=attempt_id,
                winner_result=winner_result,
                candidate_results=candidate_results,
                winner_selection=winner_selection,
                status="winner_selected",
            ),
        )
        attempt_proof = self._evaluate_attempt_proof(
            issue_key=issue_key,
            attempt_id=attempt_id,
            winner_candidate_id=winner_result.candidate_id,
        )
        self.state_store.write_attempt_artifact(issue_key, attempt_id, "proof_result.json", attempt_proof)
        self.state_store.write_execution_artifact(
            issue_key,
            f"attempts/{attempt_id}/proof_result.json",
            attempt_proof,
            run_id,
        )
        active_workspace_info = winner_result.workspace_info
        self.state_store.update_meta(
            issue_key,
            workspace=active_workspace_info["workspace"],
            branch_name=active_workspace_info["branch_name"],
            base_branch=active_workspace_info["base_branch"],
        )
        self._cleanup_loser_workspaces(workflow=workflow, winner=winner_result, candidate_results=candidate_results)
        if len(candidate_results) > 1:
            await channel.send(
                f"winner は `{winner_result.candidate_id}` です。branch: `{active_workspace_info['branch_name']}`"
            )
        await channel.send("Codex 実装が完了しました。検証を開始します。")

        workspace_info = active_workspace_info
        changed_files = winner_result.changed_files
        command_results = winner_result.command_results
        verification = winner_result.verification
        verification_json = winner_result.verification_json
        review = winner_result.review

        self.state_store.write_execution_artifact(
            issue_key,
            "final_summary.json",
            {"success": True, "state": "Human Review"},
            run_id,
        )
        self._finalize_incident_bundle(incident_bundle_dir, issue_key, run_id)
        proof = evaluate_proof_of_work(
            workflow,
            {
                "issue_snapshot.json",
                "requirement_summary.json",
                "plan.json",
                "test_plan.json",
                "verification_plan.json",
                "changed_files.json",
                "implementation_result.json",
                "review_result.json",
                "review_findings.json",
                "verification_result.json",
                "verification.json",
                "final_summary.json",
                "run.log",
                "workpad_updates.jsonl",
                "runner_metadata.json",
                "incident_bundle_manifest.json",
                "incident_bundle_summary.md",
            },
        )
        if not proof.complete:
            self._write_attempt_manifest_status(
                issue_key=issue_key,
                attempt_id=attempt_id,
                status="failed",
                winner_candidate_id=winner_result.candidate_id,
            )
            await self._finalize_failure(
                issue_key=issue_key,
                thread_id=thread_id,
                run_id=run_id,
                repo_full_name=repo_full_name,
                issue=issue_snapshot,
                summary=summary,
                plan=plan,
                test_plan=test_plan,
                state="Rework",
                failure_type="missing_artifacts",
                latest_attempt="proof of work incomplete",
                branch=workspace_info["branch_name"],
                blockers=[f"missing artifacts: {', '.join(proof.missing_artifacts)}"],
                artifacts=["verification.json", "final_summary.json", "run.log"],
                verification=verification_json,
                extra={"missing_artifacts": proof.missing_artifacts},
            )
            await channel.send("proof-of-work artifact が不足しているため完了できませんでした。")
            return

        pushed = await self._run_blocking(
            self._commit_and_push,
            workspace_info["workspace"],
            workspace_info["branch_name"],
            int(issue["number"]),
        )
        if not pushed:
            self._finalize_incident_bundle(incident_bundle_dir, issue_key, run_id)
            self._write_attempt_manifest_status(
                issue_key=issue_key,
                attempt_id=attempt_id,
                status="failed",
                winner_candidate_id=winner_result.candidate_id,
            )
            await self._finalize_failure(
                issue_key=issue_key,
                thread_id=thread_id,
                run_id=run_id,
                repo_full_name=repo_full_name,
                issue=issue_snapshot,
                summary=summary,
                plan=plan,
                test_plan=test_plan,
                state="Rework",
                failure_type="no_changes",
                latest_attempt="no changes to commit",
                branch=workspace_info["branch_name"],
                blockers=[],
                artifacts=["changed_files.json", "final_summary.json", "run.log"],
                verification=verification_json,
                extra={},
            )
            await channel.send("変更差分が作られなかったため PR 作成を中止しました。")
            return

        pr_title = f"feat: {issue['title']}"
        channel_url = getattr(channel, "channel_url", getattr(channel, "jump_url", ""))
        pr_body = self._build_pr_body(issue, channel_url, changed_files, command_results, verification, review)
        pr = await self._run_blocking(
            self.github_client.create_pull_request,
            repo_full_name=repo_full_name,
            title=pr_title,
            body=pr_body,
            head=workspace_info["branch_name"],
            base=workspace_info["base_branch"],
            draft=True,
        )
        try:
            pr_status = await self._run_blocking(
                self.github_client.get_pull_request_status,
                repo_full_name,
                int(pr["number"]),
            )
        except Exception:
            pr_status = {}
        if isinstance(pr_status, dict):
            head_sha = str(pr_status.get("head_sha", "")).strip()
            if head_sha:
                pr["head_sha"] = head_sha
        await self._run_blocking(
            self.github_client.ready_pull_request_for_review,
            repo_full_name,
            int(pr["number"]),
        )
        pr["draft"] = False
        await self._post_inline_review_comments(
            workflow=workflow,
            repo_full_name=repo_full_name,
            pr_number=int(pr["number"]),
            review=review,
        )
        self.state_store.write_artifact(issue_key, "pr.json", pr)
        self.state_store.write_execution_artifact(issue_key, "pr.json", pr, run_id)
        comment_body = self._build_pr_comment(channel_url, verification, review, command_results)
        await self._run_blocking(
            self.github_client.create_issue_comment,
            repo_full_name,
            int(pr["number"]),
            comment_body,
        )

        final_summary = {
            "success": True,
            "state": "Human Review",
            "branch": workspace_info["branch_name"],
            "pr": pr["url"],
            "changed_files": changed_files.get("changed_files", []),
        }
        final_result = {"success": True, "pr": pr, "review": review, "verification": verification}
        self.state_store.write_execution_artifact(issue_key, "final_summary.json", final_summary, run_id)
        self.state_store.write_artifact(issue_key, "final_summary.json", final_summary)
        self.state_store.write_execution_artifact(issue_key, "final_result.json", final_result, run_id)
        self.state_store.write_artifact(issue_key, "final_result.json", final_result)
        self._write_attempt_manifest_status(
            issue_key=issue_key,
            attempt_id=attempt_id,
            status="completed",
            winner_candidate_id=winner_result.candidate_id,
        )
        self.state_store.update_meta(
            issue_key,
            status="Human Review",
            runtime_status="",
            pr_number=str(pr["number"]),
            pr_url=pr["url"],
        )
        self.state_store.record_activity(
            issue_key,
            phase="completed",
            summary="run が完了しました",
            status="completed",
            run_id=run_id,
            details={"pr_number": pr["number"], "pr_url": pr["url"]},
        )
        self._record_telemetry_event(
            workflow=workflow,
            issue_key=issue_key,
            run_id=run_id,
            event="run_finished",
            status="ok",
            extra={"pr_number": pr["number"], "pr_url": pr["url"]},
        )
        await self._run_blocking(
            self._update_issue_tracking,
            repo_full_name,
            int(issue["number"]),
            "Human Review",
            self._build_workpad_sections(
                summary=summary,
                plan=plan,
                test_plan=test_plan,
                issue=issue_snapshot,
                current_state="Human Review",
                latest_attempt="draft pr created",
                branch=workspace_info["branch_name"],
                pr=f"draft #{pr['number']} {pr['url']}",
                verification=verification_json,
                blockers=[],
                artifacts=[
                    "issue_snapshot.json",
                    "plan.json",
                    "test_plan.json",
                    "verification_plan.json",
                    "changed_files.json",
                    "verification.json",
                    "final_summary.json",
                    "run.log",
                ],
                audit_trail=[f"{datetime.now(UTC).isoformat()} draft pr created"],
            ),
            workpad_updates_path,
        )
        await channel.send(f"draft PR を作成しました。\n- PR: #{pr['number']}\n- URL: {pr['url']}")

    async def _finalize_failure(
        self,
        *,
        issue_key: str,
        thread_id: int,
        run_id: str,
        repo_full_name: str,
        issue: dict[str, Any],
        summary: dict[str, Any],
        plan: dict[str, Any],
        test_plan: dict[str, Any],
        state: str,
        failure_type: str,
        latest_attempt: str,
        branch: str,
        blockers: list[str],
        artifacts: list[str],
        verification: dict[str, Any],
        extra: dict[str, Any],
    ) -> None:
        workpad_updates_path = self.state_store.execution_artifacts_dir(issue_key, run_id) / "workpad_updates.jsonl"
        final_summary = {"success": False, "state": state, "failure_type": failure_type, **extra}
        final_result = {"success": False, "failure_type": failure_type, **extra}
        self.state_store.update_status(issue_key, state)
        self.state_store.update_meta(issue_key, runtime_status="")
        self.state_store.write_execution_artifact(issue_key, "final_summary.json", final_summary, run_id)
        self.state_store.write_artifact(issue_key, "final_summary.json", final_summary)
        self.state_store.write_execution_artifact(issue_key, "final_result.json", final_result, run_id)
        self.state_store.write_artifact(issue_key, "final_result.json", final_result)
        self.state_store.record_activity(
            issue_key,
            phase="run_failed",
            summary=failure_type,
            status="failed",
            run_id=run_id,
            details=extra,
        )
        self._record_telemetry_event(
            issue_key=issue_key,
            run_id=run_id,
            event="run_failed",
            status=state,
            extra={"failure_type": failure_type, **extra},
        )
        await self._run_blocking(
            self._update_issue_tracking,
            repo_full_name,
            int(issue["number"]),
            state,
            self._build_workpad_sections(
                summary=summary,
                plan=plan,
                test_plan=test_plan,
                issue=issue,
                current_state=state,
                latest_attempt=latest_attempt,
                branch=branch,
                pr="",
                verification=verification,
                blockers=blockers,
                artifacts=artifacts,
                audit_trail=[f"{datetime.now(UTC).isoformat()} failure: {failure_type}"],
            ),
            workpad_updates_path,
        )

    def _resolve_candidate_ids(self, *, workflow: dict[str, Any], candidate_decision: dict[str, Any]) -> list[str]:
        implementation = getattr(workflow.get("config"), "implementation", None)
        candidate_mode = getattr(implementation, "candidate_mode", None)
        if candidate_mode is not None and not bool(getattr(candidate_mode, "enabled", True)):
            return ["primary"]
        enabled = bool(candidate_decision.get("enabled"))
        raw_ids = candidate_decision.get("candidate_ids", [])
        if not enabled or not isinstance(raw_ids, list):
            return ["primary"]
        candidate_ids: list[str] = []
        for item in raw_ids:
            candidate_id = str(item).strip()
            if candidate_id and candidate_id not in candidate_ids:
                candidate_ids.append(candidate_id)
        return candidate_ids[:2] or ["primary"]

    def _prepare_candidate_workspace(
        self,
        *,
        repo_full_name: str,
        issue: dict[str, Any],
        attempt_id: str,
        workspace_info: dict[str, Any],
        candidate_id: str,
    ) -> dict[str, Any]:
        if candidate_id == "primary":
            return dict(workspace_info)
        return self.workspace_manager.prepare_attempt_candidate_workspace(
            repo_full_name=repo_full_name,
            issue_number=int(issue["number"]),
            attempt_id=attempt_id,
            candidate_id=candidate_id,
            issue_title=str(issue.get("title", "")),
        )

    async def _execute_candidates(
        self,
        *,
        chat_client: ChatClient,
        channel: ChatChannel,
        issue_key: str,
        run_id: str,
        workflow: dict[str, Any],
        repo_full_name: str,
        issue: dict[str, Any],
        issue_snapshot: dict[str, Any],
        summary: dict[str, Any],
        plan: dict[str, Any],
        test_plan: dict[str, Any],
        verification_plan: dict[str, Any],
        attempt_id: str,
        workspace_info: dict[str, Any],
        candidate_ids: list[str],
    ) -> list[CandidateExecutionResult]:
        prepared: list[tuple[str, dict[str, Any]]] = []
        for candidate_id in candidate_ids:
            current_workspace = await self._run_blocking(
                self._prepare_candidate_workspace,
                repo_full_name=repo_full_name,
                issue=issue,
                attempt_id=attempt_id,
                workspace_info=workspace_info,
                candidate_id=candidate_id,
            )
            prepared.append((candidate_id, current_workspace))
            if len(candidate_ids) > 1:
                await channel.send(f"候補 `{candidate_id}` を実行します。branch: `{current_workspace['branch_name']}`")

        semaphore = asyncio.Semaphore(self._candidate_parallelism(workflow=workflow, candidate_count=len(prepared)))

        async def _run_one(candidate_id: str, current_workspace: dict[str, Any]) -> CandidateExecutionResult:
            async with semaphore:
                return await self._execute_candidate(
                    chat_client=chat_client,
                    channel=channel,
                    issue_key=issue_key,
                    run_id=run_id,
                    workflow=workflow,
                    issue=issue_snapshot,
                    summary=summary,
                    plan=plan,
                    test_plan=test_plan,
                    verification_plan=verification_plan,
                    attempt_id=attempt_id,
                    workspace_info=current_workspace,
                    candidate_id=candidate_id,
                )

        tasks = [
            asyncio.create_task(_run_one(candidate_id, current_workspace))
            for candidate_id, current_workspace in prepared
        ]
        results = await asyncio.gather(*tasks)
        by_candidate = {result.candidate_id: result for result in results}
        return [by_candidate[candidate_id] for candidate_id in candidate_ids if candidate_id in by_candidate]

    def _candidate_parallelism(self, *, workflow: dict[str, Any], candidate_count: int) -> int:
        if candidate_count <= 1:
            return 1
        implementation = getattr(workflow.get("config"), "implementation", None)
        candidate_mode = getattr(implementation, "candidate_mode", None) if implementation is not None else None
        if candidate_mode is not None:
            max_parallel = int(getattr(candidate_mode, "max_parallel_editors", 1) or 1)
        else:
            max_parallel = int(getattr(implementation, "max_parallel_editors", 1) or 1) if implementation else 1
        return max(1, min(candidate_count, max_parallel))

    async def _execute_candidate(
        self,
        *,
        chat_client: ChatClient,
        channel: ChatChannel,
        issue_key: str,
        run_id: str,
        workflow: dict[str, Any],
        issue: dict[str, Any],
        summary: dict[str, Any],
        plan: dict[str, Any],
        test_plan: dict[str, Any],
        verification_plan: dict[str, Any],
        attempt_id: str,
        workspace_info: dict[str, Any],
        candidate_id: str,
        session_id: str | None = None,
        handoff_bundle: dict[str, Any] | None = None,
    ) -> CandidateExecutionResult:
        started_at = time.monotonic()

        def build_candidate_result(**kwargs: Any) -> CandidateExecutionResult:
            return CandidateExecutionResult(duration_ms=int((time.monotonic() - started_at) * 1000), **kwargs)

        preflight_command_results = await self._preflight_workflow_policy(
            channel=channel,
            workflow=workflow,
        )
        if preflight_command_results is not None:
            return build_candidate_result(
                candidate_id=candidate_id,
                workspace_info=workspace_info,
                codex_result=SimpleNamespace(mode="app-server"),
                codex_log_path="",
                changed_files={"changed_files": []},
                scope_analysis={},
                command_results=preflight_command_results,
                verification={},
                verification_json={},
                review={},
                proof_result={},
                success=False,
                failure_type="policy_violation",
                failure_state="Human Review",
            )

        allow_turn_steer = self._should_allow_turn_steer(workflow=workflow, handoff_bundle=handoff_bundle)
        allow_thread_resume_same_run_only = self._should_allow_thread_resume_same_run_only(workflow=workflow)
        candidate_run_dir = self.state_store.execution_run_dir(issue_key, run_id) / "candidates" / candidate_id
        candidate_run_dir.mkdir(parents=True, exist_ok=True)
        pending_metadata = self._build_runner_metadata(
            workspace_info=workspace_info,
            attempt_id=attempt_id,
            candidate_id=candidate_id,
        )
        self.state_store.write_execution_artifact(
            issue_key,
            f"candidates/{candidate_id}/runner_metadata.json",
            pending_metadata,
            run_id,
        )
        self.state_store.write_candidate_artifact(
            issue_key,
            attempt_id,
            candidate_id,
            "runner_metadata.json",
            pending_metadata,
        )

        codex_result = await self._run_blocking(
            self.codex_runner.run,
            workspace=workspace_info["workspace"],
            run_dir=str(candidate_run_dir),
            issue=issue,
            requirement_summary=summary,
            plan=plan,
            test_plan=test_plan,
            workflow_text=workflow_text(workspace=workspace_info["workspace"]) or workflow_text(repo_root="."),
            run_identity=RunIdentity(
                issue_key=issue_key,
                attempt_id=attempt_id,
                candidate_id=candidate_id,
                session_id=session_id,
            ),
            allow_turn_steer=allow_turn_steer,
            allow_thread_resume_same_run_only=allow_thread_resume_same_run_only,
            handoff_bundle=handoff_bundle,
            steer_message=self._build_handoff_steer_message(handoff_bundle),
            on_process_start=lambda pid: self.process_registry.register(
                issue_key, run_id, pid, f"codex:{candidate_id}"
            ),
            on_process_exit=lambda: self.process_registry.unregister(issue_key, f"codex:{candidate_id}"),
        )
        codex_log_path = Path(codex_result.stdout_path)
        runner_metadata = self._build_runner_metadata(
            workspace_info=workspace_info,
            attempt_id=attempt_id,
            candidate_id=candidate_id,
            mode=codex_result.mode,
            session_id=codex_result.session_id,
        )
        self.state_store.write_execution_artifact(
            issue_key,
            f"candidates/{candidate_id}/runner_metadata.json",
            runner_metadata,
            run_id,
        )
        self.state_store.write_candidate_artifact(
            issue_key, attempt_id, candidate_id, "runner_metadata.json", runner_metadata
        )
        self.state_store.write_candidate_artifact(
            issue_key,
            attempt_id,
            candidate_id,
            "candidate_manifest.json",
            self._build_candidate_manifest(
                attempt_id=attempt_id,
                candidate_id=candidate_id,
                branch_name=str(workspace_info["branch_name"]),
                workspace=str(workspace_info["workspace"]),
                session_id=codex_result.session_id,
            ),
        )
        self.state_store.write_candidate_artifact(
            issue_key,
            attempt_id,
            candidate_id,
            "candidate_strategy.json",
            self._build_candidate_strategy(candidate_id=candidate_id, plan=plan, test_plan=test_plan),
        )
        self._sync_candidate_generated_artifacts(
            issue_key=issue_key,
            attempt_id=attempt_id,
            run_id=run_id,
            candidate_id=candidate_id,
            artifacts_dir=candidate_run_dir / "artifacts",
        )
        implementation_result = self.state_store.load_candidate_artifact(
            issue_key,
            attempt_id,
            candidate_id,
            "implementation_result.json",
        )
        if not isinstance(implementation_result, dict) or not implementation_result:
            implementation_result = {
                "candidate_id": candidate_id,
                "summary": codex_result.summary,
                "changed_files": list(codex_result.changed_files),
            }
            self.state_store.write_execution_artifact(
                issue_key,
                f"candidates/{candidate_id}/implementation_result.json",
                implementation_result,
                run_id,
            )
            self.state_store.write_candidate_artifact(
                issue_key,
                attempt_id,
                candidate_id,
                "implementation_result.json",
                implementation_result,
            )

        candidate_verification_plan = self._refresh_verification_plan_for_workspace(
            workspace=workspace_info["workspace"],
            plan=plan,
            verification_plan=verification_plan,
        )
        candidate_workflow = self._load_effective_workflow(
            workspace=workspace_info["workspace"],
            verification_plan=candidate_verification_plan,
        )
        self.state_store.write_execution_artifact(
            issue_key,
            f"candidates/{candidate_id}/verification_plan.json",
            candidate_verification_plan,
            run_id,
        )
        self.state_store.write_candidate_artifact(
            issue_key,
            attempt_id,
            candidate_id,
            "verification_plan.json",
            candidate_verification_plan,
        )
        candidate_effective_workflow = self._json_safe(candidate_workflow)
        self.state_store.write_execution_artifact(
            issue_key,
            f"candidates/{candidate_id}/effective_workflow.json",
            candidate_effective_workflow,
            run_id,
        )
        self.state_store.write_candidate_artifact(
            issue_key,
            attempt_id,
            candidate_id,
            "effective_workflow.json",
            candidate_effective_workflow,
        )

        changed_files = {"changed_files": self._detect_changed_files(workspace_info["workspace"])}
        self.state_store.write_execution_artifact(
            issue_key,
            f"candidates/{candidate_id}/changed_files.json",
            changed_files,
            run_id,
        )
        self.state_store.write_candidate_artifact(
            issue_key, attempt_id, candidate_id, "changed_files.json", changed_files
        )
        scope_analysis = self._build_scope_analysis(
            plan=plan,
            changed_files=changed_files,
            issue=issue,
            issue_key=issue_key,
            workflow=candidate_workflow,
        )
        self.state_store.write_execution_artifact(
            issue_key,
            f"candidates/{candidate_id}/scope_analysis.json",
            scope_analysis,
            run_id,
        )
        self.state_store.write_candidate_artifact(
            issue_key, attempt_id, candidate_id, "scope_analysis.json", scope_analysis
        )
        if codex_result.returncode != 0:
            return build_candidate_result(
                candidate_id=candidate_id,
                workspace_info=workspace_info,
                codex_result=codex_result,
                codex_log_path=str(codex_log_path),
                changed_files=changed_files,
                scope_analysis=scope_analysis,
                command_results={},
                verification={},
                verification_json={},
                review={},
                proof_result={},
                success=False,
                failure_type="codex_failure",
            )

        command_results = await self.execute_workflow_commands(
            client=chat_client,
            channel=channel,
            workspace=workspace_info["workspace"],
            workflow=candidate_workflow,
            issue_key=issue_key,
            run_id=run_id,
        )
        self.state_store.write_execution_artifact(
            issue_key,
            f"candidates/{candidate_id}/command_results.json",
            command_results,
            run_id,
        )
        self.state_store.write_candidate_artifact(
            issue_key, attempt_id, candidate_id, "command_results.json", command_results
        )
        if command_results.get("failure_type"):
            failure_type = str(command_results["failure_type"])
            failure_state = "Human Review" if failure_type == "policy_violation" else "Blocked"
            return build_candidate_result(
                candidate_id=candidate_id,
                workspace_info=workspace_info,
                codex_result=codex_result,
                codex_log_path=str(codex_log_path),
                changed_files=changed_files,
                scope_analysis=scope_analysis,
                command_results=command_results,
                verification={},
                verification_json={},
                review={},
                proof_result={},
                success=False,
                failure_type=failure_type,
                failure_state=failure_state,
            )
        if scope_analysis.get("must_not_touch_violations"):
            return build_candidate_result(
                candidate_id=candidate_id,
                workspace_info=workspace_info,
                codex_result=codex_result,
                codex_log_path=str(codex_log_path),
                changed_files=changed_files,
                scope_analysis=scope_analysis,
                command_results=command_results,
                verification={},
                verification_json={},
                review={},
                proof_result={},
                success=False,
                failure_type="scope_violation",
                failure_state="Human Review",
            )
        if scope_analysis.get("protected_config_violations"):
            return build_candidate_result(
                candidate_id=candidate_id,
                workspace_info=workspace_info,
                codex_result=codex_result,
                codex_log_path=str(codex_log_path),
                changed_files=changed_files,
                scope_analysis=scope_analysis,
                command_results=command_results,
                verification={},
                verification_json={},
                review={},
                proof_result={},
                success=False,
                failure_type="protected_config_violation",
                failure_state="Human Review",
            )

        repair_results = await self._execute_fast_repair_checks(
            workspace=workspace_info["workspace"],
            verification_plan=candidate_verification_plan,
        )
        repair_feedback = self._build_repair_feedback(
            candidate_id=candidate_id,
            repair_results=repair_results,
        )
        self.state_store.write_execution_artifact(
            issue_key,
            f"candidates/{candidate_id}/repair_feedback.json",
            repair_feedback,
            run_id,
        )
        self.state_store.write_candidate_artifact(
            issue_key, attempt_id, candidate_id, "repair_feedback.json", repair_feedback
        )
        self._write_repair_feedback_jsonl(
            issue_key=issue_key,
            attempt_id=attempt_id,
            run_id=run_id,
            candidate_id=candidate_id,
            repair_feedback=repair_feedback,
        )
        if repair_results.get("failure_type"):
            failure_type = str(repair_results["failure_type"])
            failure_state = "Human Review" if failure_type == "policy_violation" else "Blocked"
            return build_candidate_result(
                candidate_id=candidate_id,
                workspace_info=workspace_info,
                codex_result=codex_result,
                codex_log_path=str(codex_log_path),
                changed_files=changed_files,
                scope_analysis=scope_analysis,
                command_results=repair_results,
                verification={},
                verification_json={},
                review={},
                proof_result={},
                success=False,
                failure_type=failure_type,
                failure_state=failure_state,
            )
        if (
            bool(repair_feedback.get("applicable"))
            and handoff_bundle is None
            and self._can_fast_repair_reuse(repair_feedback=repair_feedback, changed_files=changed_files)
        ):
            return await self._rollover_same_candidate(
                chat_client=chat_client,
                channel=channel,
                issue_key=issue_key,
                run_id=run_id,
                workflow=candidate_workflow,
                issue=issue,
                summary=summary,
                plan=plan,
                test_plan=test_plan,
                verification_plan=candidate_verification_plan,
                attempt_id=attempt_id,
                workspace_info=workspace_info,
                candidate_id=candidate_id,
                reason="fast_repair",
                latest_failures=self._repair_feedback_failures(repair_feedback),
            )

        verification = await self._run_blocking(
            self.claude_runner.verify,
            workspace=workspace_info["workspace"],
            command_results=command_results,
            changed_files=changed_files,
            codex_run_log_path=str(codex_log_path),
            plan=plan,
            test_plan=test_plan,
        )
        verification_result = self._build_verification_result(command_results, verification)
        verification_json = self._build_verification_json(command_results, verification, test_plan)
        self.state_store.write_execution_artifact(
            issue_key,
            f"candidates/{candidate_id}/verification_result.json",
            verification_result,
            run_id,
        )
        self.state_store.write_execution_artifact(
            issue_key,
            f"candidates/{candidate_id}/verification_summary.json",
            verification,
            run_id,
        )
        self.state_store.write_execution_artifact(
            issue_key,
            f"candidates/{candidate_id}/verification.json",
            verification_json,
            run_id,
        )
        self.state_store.write_candidate_artifact(
            issue_key, attempt_id, candidate_id, "verification_result.json", verification_result
        )
        self.state_store.write_candidate_artifact(
            issue_key, attempt_id, candidate_id, "verification_summary.json", verification
        )
        self.state_store.write_candidate_artifact(
            issue_key, attempt_id, candidate_id, "verification.json", verification_json
        )
        if self._has_failed_hard_checks(command_results):
            return build_candidate_result(
                candidate_id=candidate_id,
                workspace_info=workspace_info,
                codex_result=codex_result,
                codex_log_path=str(codex_log_path),
                changed_files=changed_files,
                scope_analysis=scope_analysis,
                command_results=command_results,
                verification=verification,
                verification_json=verification_json,
                review={},
                proof_result={},
                success=False,
                failure_type="hard_check_failed",
            )
        if verification.get("status") not in {"success", "passed", "completed"}:
            return build_candidate_result(
                candidate_id=candidate_id,
                workspace_info=workspace_info,
                codex_result=codex_result,
                codex_log_path=str(codex_log_path),
                changed_files=changed_files,
                scope_analysis=scope_analysis,
                command_results=command_results,
                verification=verification,
                verification_json=verification_json,
                review={},
                proof_result={},
                success=False,
                failure_type=str(verification.get("failure_type", "verification_failed")),
            )

        git_diff = await self._run_blocking(self._capture_git_diff, workspace_info["workspace"])
        review = await self._run_blocking(
            self.claude_runner.review,
            workspace=workspace_info["workspace"],
            git_diff=git_diff,
            changed_files=changed_files,
            verification_summary=verification,
            plan=plan,
            test_plan=test_plan,
        )
        review_result = self._build_review_result(
            candidate_id=candidate_id,
            review=review,
            scope_analysis=scope_analysis,
        )
        review_decision_payload = self._build_review_decision_payload(review_result)
        review_findings = self._build_review_findings_payload(review)
        postable_findings = self._build_postable_findings_payload(review)
        repair_instructions = self._build_repair_instructions(
            candidate_id=candidate_id,
            review_result=review_result,
            review_findings=review,
        )
        latest_repair_feedback = self._build_latest_repair_feedback(
            candidate_id=candidate_id,
            verification=verification,
            repair_instructions=repair_instructions,
        )
        latest_review_delta = self._build_latest_review_delta(
            candidate_id=candidate_id,
            review_result=review_result,
            review_findings=review,
        )
        self.state_store.write_execution_artifact(
            issue_key,
            f"candidates/{candidate_id}/review_summary.json",
            review_result,
            run_id,
        )
        self.state_store.write_execution_artifact(
            issue_key,
            f"candidates/{candidate_id}/review_findings.json",
            review_findings,
            run_id,
        )
        self.state_store.write_execution_artifact(
            issue_key,
            f"candidates/{candidate_id}/review_result.json",
            review_result,
            run_id,
        )
        self.state_store.write_execution_artifact(
            issue_key,
            f"candidates/{candidate_id}/review_decision.json",
            review_decision_payload,
            run_id,
        )
        self.state_store.write_execution_artifact(
            issue_key,
            f"candidates/{candidate_id}/postable_findings.json",
            postable_findings,
            run_id,
        )
        self.state_store.write_execution_artifact(
            issue_key,
            f"candidates/{candidate_id}/repair_instructions.json",
            repair_instructions,
            run_id,
        )
        self.state_store.write_execution_artifact(
            issue_key,
            f"candidates/{candidate_id}/latest_repair_feedback.json",
            latest_repair_feedback,
            run_id,
        )
        self.state_store.write_execution_artifact(
            issue_key,
            f"candidates/{candidate_id}/latest_review_delta.json",
            latest_review_delta,
            run_id,
        )
        self.state_store.write_candidate_artifact(
            issue_key, attempt_id, candidate_id, "review_summary.json", review_result
        )
        self.state_store.write_candidate_artifact(
            issue_key, attempt_id, candidate_id, "review_findings.json", review_findings
        )
        self.state_store.write_candidate_artifact(
            issue_key, attempt_id, candidate_id, "review_result.json", review_result
        )
        self.state_store.write_candidate_artifact(
            issue_key, attempt_id, candidate_id, "review_decision.json", review_decision_payload
        )
        self.state_store.write_candidate_artifact(
            issue_key, attempt_id, candidate_id, "repair_instructions.json", repair_instructions
        )
        self.state_store.write_candidate_artifact(
            issue_key, attempt_id, candidate_id, "postable_findings.json", postable_findings
        )
        self.state_store.write_candidate_artifact(
            issue_key, attempt_id, candidate_id, "latest_repair_feedback.json", latest_repair_feedback
        )
        self.state_store.write_candidate_artifact(
            issue_key, attempt_id, candidate_id, "latest_review_delta.json", latest_review_delta
        )
        self._write_session_checkpoint(
            issue_key=issue_key,
            attempt_id=attempt_id,
            candidate_id=candidate_id,
            workspace=workspace_info["workspace"],
            changed_files=changed_files["changed_files"],
            last_completed_phase="review_complete",
            session_id=codex_result.session_id or str((handoff_bundle or {}).get("rollover_id", "")),
        )
        proof_result = self._evaluate_candidate_proof(
            issue_key=issue_key, attempt_id=attempt_id, candidate_id=candidate_id
        )
        self.state_store.write_candidate_artifact(
            issue_key, attempt_id, candidate_id, "proof_result.json", proof_result
        )
        review_decision = str(review_result.get("decision", "")).strip().lower()
        if review_decision == "repairable":
            latest_failures = self._review_follow_up_failures(
                review_result=review_result,
                repair_instructions=repair_instructions,
            )
            self._write_session_checkpoint(
                issue_key=issue_key,
                attempt_id=attempt_id,
                candidate_id=candidate_id,
                workspace=workspace_info["workspace"],
                changed_files=changed_files["changed_files"],
                last_completed_phase="review_repairable",
                session_id=codex_result.session_id or str((handoff_bundle or {}).get("rollover_id", "")),
            )
            if handoff_bundle is None and self._can_minor_repair_reuse(
                review_result=review_result,
                verification=verification,
                changed_files=changed_files,
            ):
                return await self._rollover_same_candidate(
                    chat_client=chat_client,
                    channel=channel,
                    issue_key=issue_key,
                    run_id=run_id,
                    workflow=candidate_workflow,
                    issue=issue,
                    summary=summary,
                    plan=plan,
                    test_plan=test_plan,
                    verification_plan=candidate_verification_plan,
                    attempt_id=attempt_id,
                    workspace_info=workspace_info,
                    candidate_id=candidate_id,
                    reason="review_repairable",
                    latest_failures=latest_failures,
                )
            return build_candidate_result(
                candidate_id=candidate_id,
                workspace_info=workspace_info,
                codex_result=codex_result,
                codex_log_path=str(codex_log_path),
                changed_files=changed_files,
                scope_analysis=scope_analysis,
                command_results=command_results,
                verification=verification,
                verification_json=verification_json,
                review=review,
                proof_result=proof_result,
                success=False,
                failure_type="review_repairable",
            )
        if review_decision == "reject":
            self._maybe_write_replan_reason(
                workflow=candidate_workflow,
                issue_key=issue_key,
                attempt_id=attempt_id,
                review_result=review_result,
                scope_analysis=scope_analysis,
            )
            self._write_session_checkpoint(
                issue_key=issue_key,
                attempt_id=attempt_id,
                candidate_id=candidate_id,
                workspace=workspace_info["workspace"],
                changed_files=changed_files["changed_files"],
                last_completed_phase="review_reject",
                session_id=codex_result.session_id or str((handoff_bundle or {}).get("rollover_id", "")),
            )
            return CandidateExecutionResult(
                duration_ms=int((time.monotonic() - started_at) * 1000),
                candidate_id=candidate_id,
                workspace_info=workspace_info,
                codex_result=codex_result,
                codex_log_path=str(codex_log_path),
                changed_files=changed_files,
                scope_analysis=scope_analysis,
                command_results=command_results,
                verification=verification,
                verification_json=verification_json,
                review=review,
                proof_result=proof_result,
                success=False,
                failure_type="review_reject",
            )
        if not bool(proof_result.get("complete")):
            self._write_session_checkpoint(
                issue_key=issue_key,
                attempt_id=attempt_id,
                candidate_id=candidate_id,
                workspace=workspace_info["workspace"],
                changed_files=changed_files["changed_files"],
                last_completed_phase="candidate_proof_incomplete",
                session_id=codex_result.session_id or str((handoff_bundle or {}).get("rollover_id", "")),
            )
            return build_candidate_result(
                candidate_id=candidate_id,
                workspace_info=workspace_info,
                codex_result=codex_result,
                codex_log_path=str(codex_log_path),
                changed_files=changed_files,
                scope_analysis=scope_analysis,
                command_results=command_results,
                verification=verification,
                verification_json=verification_json,
                review=review,
                proof_result=proof_result,
                success=False,
                failure_type="candidate_proof_incomplete",
            )
        self._write_session_checkpoint(
            issue_key=issue_key,
            attempt_id=attempt_id,
            candidate_id=candidate_id,
            workspace=workspace_info["workspace"],
            changed_files=changed_files["changed_files"],
            last_completed_phase="candidate_complete",
            session_id=codex_result.session_id or str((handoff_bundle or {}).get("rollover_id", "")),
        )
        return build_candidate_result(
            candidate_id=candidate_id,
            workspace_info=workspace_info,
            codex_result=codex_result,
            codex_log_path=str(codex_log_path),
            changed_files=changed_files,
            scope_analysis=scope_analysis,
            command_results=command_results,
            verification=verification,
            verification_json=verification_json,
            review=review,
            proof_result=proof_result,
            success=True,
        )

    def _select_candidate_result(
        self,
        *,
        plan: dict[str, Any],
        candidate_results: list[CandidateExecutionResult],
    ) -> tuple[CandidateExecutionResult | None, dict[str, Any]]:
        successful = [result for result in candidate_results if result.success]
        selection_payload = {
            "winner_candidate_id": "",
            "successful_candidates": [result.candidate_id for result in successful],
            "selection_basis": "deterministic_rank",
            "winner_reason": "no eligible candidate completed successfully",
            "exact_tie_detected": False,
            "tied_candidate_ids": [],
            "winner_tiebreak_artifact": "",
            "candidates": [],
        }
        if not successful:
            return None, selection_payload
        if len(successful) == 1:
            winner = successful[0]
            selection_payload["winner_candidate_id"] = winner.candidate_id
            selection_payload["winner_reason"] = "only one candidate completed successfully"
            selection_payload["candidates"] = [self._winner_metrics_payload(plan=plan, result=winner)]
            return winner, selection_payload
        score_inputs: dict[str, Any] = {}
        selection_payload["candidates"] = [
            self._winner_metrics_payload(plan=plan, result=result) for result in successful
        ]
        for result in successful:
            score_inputs[result.candidate_id] = self._winner_input(plan=plan, result=result)
        winner_id = select_winner(list(score_inputs.values()))
        tied_candidate_ids = self._exact_tie_candidate_ids(score_inputs=score_inputs, winner_candidate_id=winner_id)
        selection_payload["winner_candidate_id"] = winner_id
        selection_payload["exact_tie_detected"] = len(tied_candidate_ids) > 1
        selection_payload["tied_candidate_ids"] = tied_candidate_ids
        selection_payload["winner_reason"] = self._winner_reason(
            winner_input=score_inputs[winner_id],
            all_inputs=score_inputs,
        )
        for result in successful:
            if result.candidate_id == winner_id:
                return result, selection_payload
        return successful[0], selection_payload

    def _review_finding_severity(self, finding: Any) -> str:
        if isinstance(finding, dict):
            return str(finding.get("severity", "")).lower()
        return str(getattr(finding, "severity", "")).lower()

    def _winner_input(self, *, plan: dict[str, Any], result: CandidateExecutionResult) -> Any:
        expected_files = self._expected_plan_files(plan)
        changed_files = [
            str(item).strip() for item in result.changed_files.get("changed_files", []) if str(item).strip()
        ]
        unexpected_files = [path for path in changed_files if expected_files and path not in expected_files]
        findings = result.review.get("postable_findings") or result.review.get("findings") or []
        high_count = sum(1 for finding in findings if self._review_finding_severity(finding) == "high")
        medium_count = sum(1 for finding in findings if self._review_finding_severity(finding) == "medium")
        low_count = sum(1 for finding in findings if self._review_finding_severity(finding) == "low")
        plan_alignment_ok = not bool(
            result.scope_analysis.get("unexpected_file_count", len(unexpected_files))
        ) and not bool(result.review.get("unnecessary_changes"))
        return SimpleNamespace(
            candidate_id=result.candidate_id,
            verification=SimpleNamespace(
                hard_checks_pass=not self._has_failed_hard_checks(result.command_results),
                failure_type=result.failure_type,
            ),
            review=SimpleNamespace(
                high_count=high_count,
                medium_count=medium_count,
                low_count=low_count,
                plan_alignment_ok=plan_alignment_ok,
            ),
            scope=SimpleNamespace(
                unexpected_file_count=int(result.scope_analysis.get("unexpected_file_count", len(unexpected_files))),
                protected_path_violations=bool(result.scope_analysis.get("must_not_touch_violations"))
                or bool(result.scope_analysis.get("protected_config_violations"))
                or bool(result.review.get("protected_path_touches")),
            ),
            proof=SimpleNamespace(
                complete=bool(result.proof_result.get("complete")),
                missing_artifacts=list(result.proof_result.get("missing_artifacts", [])),
            ),
            diff_size=len(changed_files),
            duration_ms=int(result.duration_ms),
        )

    def _winner_metrics_payload(self, *, plan: dict[str, Any], result: CandidateExecutionResult) -> dict[str, Any]:
        winner_input = self._winner_input(plan=plan, result=result)
        return {
            "candidate_id": result.candidate_id,
            "eligible": eligible(winner_input),
            "rank_tuple": list(candidate_rank_tuple(winner_input)),
            "review_severity": {
                "high": int(getattr(winner_input.review, "high_count", 0)),
                "medium": int(getattr(winner_input.review, "medium_count", 0)),
                "low": int(getattr(winner_input.review, "low_count", 0)),
            },
            "plan_alignment_ok": bool(getattr(winner_input.review, "plan_alignment_ok", True)),
            "unexpected_file_count": int(getattr(winner_input.scope, "unexpected_file_count", 0)),
            "protected_path_violations": bool(getattr(winner_input.scope, "protected_path_violations", False)),
            "proof_complete": bool(getattr(winner_input.proof, "complete", False)),
            "missing_artifacts": list(getattr(winner_input.proof, "missing_artifacts", [])),
            "diff_size": int(getattr(winner_input, "diff_size", 0)),
            "duration_ms": int(getattr(winner_input, "duration_ms", 0)),
        }

    def _exact_tie_candidate_ids(
        self,
        *,
        score_inputs: dict[str, Any],
        winner_candidate_id: str,
    ) -> list[str]:
        winner_input = score_inputs.get(winner_candidate_id)
        if winner_input is None or not eligible(winner_input):
            return []
        tied_candidate_ids = [
            candidate_id
            for candidate_id, candidate_input in score_inputs.items()
            if eligible(candidate_input) and exact_tie(candidate_input, winner_input)
        ]
        return sorted(tied_candidate_ids)

    def _winner_reason(self, *, winner_input: Any, all_inputs: dict[str, Any]) -> str:
        competitors = [item for candidate_id, item in all_inputs.items() if candidate_id != winner_input.candidate_id]
        if not competitors:
            return "only one eligible candidate remained after deterministic gating"
        runner_up = sorted(competitors, key=candidate_rank_tuple)[0]
        winner_rank = candidate_rank_tuple(winner_input)
        runner_rank = candidate_rank_tuple(runner_up)
        dimensions = (
            "review_severity",
            "plan_alignment",
            "unexpected_file_count",
            "missing_artifacts",
            "verification_failure_type",
            "diff_size",
            "duration_ms",
        )
        for index, dimension in enumerate(dimensions):
            if winner_rank[index] != runner_rank[index]:
                return f"won by lower deterministic rank on {dimension}"
        return "requires exact tie-break judge after identical deterministic rank"

    def _tiebreak_candidate_payload(
        self,
        *,
        plan: dict[str, Any],
        result: CandidateExecutionResult,
    ) -> dict[str, Any]:
        return {
            "candidate_id": result.candidate_id,
            "branch_name": str(result.workspace_info.get("branch_name", "")),
            "changed_files": list(result.changed_files.get("changed_files", [])),
            "verification_result": self._build_verification_result(result.command_results, result.verification),
            "review_result": {
                "decision": str(result.review.get("decision", "")),
                "risk_items": list(result.review.get("risk_items", [])),
                "test_gaps": list(result.review.get("test_gaps", [])),
                "unnecessary_changes": list(result.review.get("unnecessary_changes", [])),
                "protected_path_touches": list(result.review.get("protected_path_touches", [])),
            },
            "review_findings": list(result.review.get("postable_findings") or result.review.get("findings") or []),
            "scope_analysis": result.scope_analysis,
            "proof_result": result.proof_result,
            "metrics": self._winner_metrics_payload(plan=plan, result=result),
        }

    async def _resolve_exact_tie_winner(
        self,
        *,
        issue_key: str,
        attempt_id: str,
        run_id: str,
        plan: dict[str, Any],
        candidate_results: list[CandidateExecutionResult],
        winner_result: CandidateExecutionResult,
        winner_selection: dict[str, Any],
    ) -> tuple[CandidateExecutionResult, dict[str, Any], dict[str, Any] | None]:
        tied_candidate_ids = [
            str(candidate_id).strip()
            for candidate_id in winner_selection.get("tied_candidate_ids", [])
            if str(candidate_id).strip()
        ]
        if len(tied_candidate_ids) < 2:
            return winner_result, winner_selection, None
        result_by_id = {result.candidate_id: result for result in candidate_results if result.success}
        tied_results = [
            result_by_id[candidate_id] for candidate_id in tied_candidate_ids if candidate_id in result_by_id
        ]
        if len(tied_results) < 2:
            return winner_result, winner_selection, None
        tied_result_by_id = {result.candidate_id: result for result in tied_results}
        fallback_winner_candidate_id = winner_result.candidate_id
        tiebreak_payload: dict[str, Any] = {
            "version": 1,
            "attempt_id": attempt_id,
            "provider": "claude-agent-sdk",
            "provider_status": "success",
            "selection_basis": "exact_tie_claude_judge",
            "tied_candidate_ids": tied_candidate_ids,
            "fallback_winner_candidate_id": fallback_winner_candidate_id,
            "winner_candidate_id": fallback_winner_candidate_id,
            "judge_summary": "exact tie resolved by deterministic fallback ordering",
            "explanation": [],
            "error": "",
        }
        comparison_payload = [self._tiebreak_candidate_payload(plan=plan, result=result) for result in tied_results]
        try:
            judge_result = await self._run_blocking(
                self.claude_runner.judge_winner_tiebreak,
                workspace=str(winner_result.workspace_info["workspace"]),
                plan=plan,
                candidates=comparison_payload,
                fallback_winner_candidate_id=fallback_winner_candidate_id,
            )
            judged_winner_candidate_id = str(judge_result.get("winner_candidate_id", "")).strip()
            if judged_winner_candidate_id not in tied_result_by_id:
                tiebreak_payload["provider_status"] = "fallback"
                tiebreak_payload["selection_basis"] = "exact_tie_deterministic_fallback"
                tiebreak_payload["judge_summary"] = (
                    "judge returned an invalid winner_candidate_id; used deterministic fallback ordering"
                )
                tiebreak_payload["explanation"] = [
                    f"invalid winner_candidate_id: {judged_winner_candidate_id or '<empty>'}"
                ]
                judged_winner_candidate_id = fallback_winner_candidate_id
            else:
                tiebreak_payload["winner_candidate_id"] = judged_winner_candidate_id
                tiebreak_payload["judge_summary"] = str(judge_result.get("summary", "")).strip()
                tiebreak_payload["explanation"] = [
                    str(item).strip() for item in judge_result.get("explanation", []) if str(item).strip()
                ]
        except Exception as exc:
            judged_winner_candidate_id = fallback_winner_candidate_id
            tiebreak_payload["provider_status"] = "failed"
            tiebreak_payload["selection_basis"] = "exact_tie_deterministic_fallback"
            tiebreak_payload["judge_summary"] = "judge failed; used deterministic fallback ordering"
            tiebreak_payload["error"] = f"{type(exc).__name__}: {exc}"
        winner_selection = {
            **winner_selection,
            "winner_candidate_id": judged_winner_candidate_id,
            "selection_basis": str(tiebreak_payload["selection_basis"]),
            "winner_reason": str(tiebreak_payload["judge_summary"]),
            "winner_tiebreak_artifact": "winner_tiebreak_judge.json",
        }
        self.state_store.write_attempt_artifact(
            issue_key,
            attempt_id,
            "winner_tiebreak_judge.json",
            tiebreak_payload,
        )
        self.state_store.write_execution_artifact(
            issue_key,
            f"attempts/{attempt_id}/winner_tiebreak_judge.json",
            tiebreak_payload,
            run_id,
        )
        return tied_result_by_id[judged_winner_candidate_id], winner_selection, tiebreak_payload

    def _expected_plan_files(self, plan: dict[str, Any]) -> set[str]:
        expected_files = {str(path).strip() for path in plan.get("candidate_files", []) if str(path).strip()}
        for task in plan.get("tasks", []):
            if not isinstance(task, dict):
                continue
            for path in task.get("files", []):
                normalized = str(path).strip()
                if normalized:
                    expected_files.add(normalized)
        return expected_files

    def _promote_candidate_result(
        self,
        *,
        issue_key: str,
        attempt_id: str,
        run_id: str,
        candidate_result: CandidateExecutionResult,
    ) -> None:
        payloads = {
            "runner_metadata.json": {
                "runner": "codex",
                "candidate_id": candidate_result.candidate_id,
                "mode": candidate_result.codex_result.mode,
                "workspace_key": candidate_result.workspace_info.get("workspace_key", ""),
                "workspace": candidate_result.workspace_info["workspace"],
                "branch_name": candidate_result.workspace_info["branch_name"],
            },
            "implementation_result.json": self.state_store.load_execution_artifact(
                issue_key, f"candidates/{candidate_result.candidate_id}/implementation_result.json", run_id
            ),
            "changed_files.json": candidate_result.changed_files,
            "scope_analysis.json": candidate_result.scope_analysis,
            "verification_plan.json": self.state_store.load_candidate_artifact(
                issue_key, attempt_id, candidate_result.candidate_id, "verification_plan.json"
            ),
            "effective_workflow.json": self.state_store.load_candidate_artifact(
                issue_key, attempt_id, candidate_result.candidate_id, "effective_workflow.json"
            ),
            "repair_feedback.json": self.state_store.load_candidate_artifact(
                issue_key, attempt_id, candidate_result.candidate_id, "repair_feedback.json"
            ),
            "command_results.json": candidate_result.command_results,
            "verification_result.json": self.state_store.load_candidate_artifact(
                issue_key, attempt_id, candidate_result.candidate_id, "verification_result.json"
            ),
            "verification_summary.json": candidate_result.verification,
            "verification.json": candidate_result.verification_json,
            "review_result.json": self.state_store.load_candidate_artifact(
                issue_key, attempt_id, candidate_result.candidate_id, "review_result.json"
            ),
            "review_decision.json": self.state_store.load_candidate_artifact(
                issue_key, attempt_id, candidate_result.candidate_id, "review_decision.json"
            ),
            "review_summary.json": self.state_store.load_candidate_artifact(
                issue_key, attempt_id, candidate_result.candidate_id, "review_summary.json"
            ),
            "review_findings.json": self.state_store.load_candidate_artifact(
                issue_key, attempt_id, candidate_result.candidate_id, "review_findings.json"
            ),
            "postable_findings.json": self.state_store.load_candidate_artifact(
                issue_key, attempt_id, candidate_result.candidate_id, "postable_findings.json"
            ),
        }
        for filename, payload in payloads.items():
            self.state_store.write_execution_artifact(issue_key, filename, payload, run_id)
            self.state_store.write_artifact(issue_key, filename, payload)
        self.state_store.promote_candidate_to_views(issue_key, attempt_id, candidate_result.candidate_id)

    def _cleanup_loser_workspaces(
        self,
        *,
        workflow: dict[str, Any],
        winner: CandidateExecutionResult,
        candidate_results: list[CandidateExecutionResult],
    ) -> None:
        implementation = getattr(workflow.get("config"), "implementation", None)
        if implementation is not None and not bool(getattr(implementation, "cleanup_loser_local_branches", True)):
            return
        for result in candidate_results:
            if result.candidate_id == winner.candidate_id or result.candidate_id == "primary":
                continue
            workspace = Path(str(result.workspace_info["workspace"]))
            subprocess.run(
                ["git", "-C", str(winner.workspace_info["workspace"]), "worktree", "remove", "--force", str(workspace)],
                check=False,
                capture_output=True,
                text=True,
            )
            shutil.rmtree(workspace.parent, ignore_errors=True)

    def _build_runner_metadata(
        self,
        *,
        workspace_info: dict[str, Any],
        attempt_id: str,
        candidate_id: str,
        mode: str = "pending",
        session_id: str = "",
    ) -> dict[str, Any]:
        """Build runner metadata dict for artifact persistence."""
        return {
            "runner": "codex",
            "attempt_id": attempt_id,
            "candidate_id": candidate_id,
            "mode": mode,
            "session_id": session_id,
            "workspace_key": workspace_info.get("workspace_key", ""),
            "workspace": workspace_info["workspace"],
            "branch_name": workspace_info["branch_name"],
        }

    def _build_candidate_manifest(
        self,
        *,
        attempt_id: str,
        candidate_id: str,
        branch_name: str,
        workspace: str,
        session_id: str = "",
    ) -> dict[str, Any]:
        return {
            "version": 1,
            "attempt_id": attempt_id,
            "candidate_id": candidate_id,
            "branch_name": branch_name,
            "workspace": workspace,
            "session_id": session_id,
            "strategy_origin": "implementation_self_divergence",
            "strategy_summary": "candidate execution lane",
            "reused_from_candidate_id": None,
            "reuse_reason": None,
        }

    def _session_snapshot_count(self, snapshot: dict[str, Any] | None, *keys: str) -> int | None:
        if not isinstance(snapshot, dict):
            return None
        for key in keys:
            value = snapshot.get(key)
            if isinstance(value, int):
                return max(0, value)
            if isinstance(value, list):
                return len(value)
        return None

    async def _read_session_snapshot(self, *, workspace: str, session_id: str) -> dict[str, Any] | None:
        if not session_id.strip():
            return None
        snapshot = await self._run_blocking(
            self.codex_runner.read_session,
            workspace=workspace,
            session_id=session_id,
        )
        if not isinstance(snapshot, dict):
            return None
        normalized = dict(snapshot)
        normalized.setdefault("session_id", session_id)
        return self._json_safe(normalized)

    def _should_allow_turn_steer(
        self,
        *,
        workflow: dict[str, Any],
        handoff_bundle: dict[str, Any] | None,
    ) -> bool:
        if handoff_bundle:
            return True
        config = workflow.get("config")
        codex_config = getattr(config, "codex", None) if config is not None else None
        if codex_config is not None:
            return bool(getattr(codex_config, "allow_turn_steer", False))
        return bool((workflow.get("codex") or {}).get("allow_turn_steer", False))

    def _should_allow_thread_resume_same_run_only(self, *, workflow: dict[str, Any]) -> bool:
        config = workflow.get("config")
        codex_config = getattr(config, "codex", None) if config is not None else None
        if codex_config is not None:
            return bool(getattr(codex_config, "allow_thread_resume_same_run_only", True))
        return bool((workflow.get("codex") or {}).get("allow_thread_resume_same_run_only", True))

    def _should_rollover_session(
        self,
        *,
        turn_count: int,
        steer_count: int,
        repair_cycles: int,
        workflow: dict[str, Any] | None = None,
    ) -> bool:
        turn_count_gte = 12
        steer_count_gte = 2
        repair_cycles_gte = 3
        if isinstance(workflow, dict):
            config = workflow.get("config")
            codex_config = getattr(config, "codex", None) if config is not None else None
            compaction_policy = getattr(codex_config, "compaction_policy", None) if codex_config is not None else None
            if compaction_policy is not None:
                turn_count_gte = int(getattr(compaction_policy, "turn_count_gte", turn_count_gte))
                steer_count_gte = int(getattr(compaction_policy, "steer_count_gte", steer_count_gte))
                repair_cycles_gte = int(getattr(compaction_policy, "repair_cycles_gte", repair_cycles_gte))
        return turn_count >= turn_count_gte or steer_count >= steer_count_gte or repair_cycles >= repair_cycles_gte

    async def _rollover_same_candidate(
        self,
        *,
        chat_client: ChatClient,
        channel: ChatChannel,
        issue_key: str,
        run_id: str,
        workflow: dict[str, Any],
        issue: dict[str, Any],
        summary: dict[str, Any],
        plan: dict[str, Any],
        test_plan: dict[str, Any],
        verification_plan: dict[str, Any],
        attempt_id: str,
        workspace_info: dict[str, Any],
        candidate_id: str,
        reason: str,
        latest_failures: list[dict[str, Any]],
    ) -> CandidateExecutionResult:
        workspace = str(workspace_info["workspace"])
        head_sha = self._git_head_sha(workspace)
        dirty_files = self._detect_changed_files(workspace)
        previous_checkpoint = self.state_store.load_candidate_artifact(
            issue_key, attempt_id, candidate_id, "session_checkpoint.json"
        )
        previous_session_id = (
            str(previous_checkpoint.get("session_id", "")).strip() if isinstance(previous_checkpoint, dict) else ""
        )
        session_snapshot = await self._read_session_snapshot(workspace=workspace, session_id=previous_session_id)
        self._write_session_checkpoint(
            issue_key=issue_key,
            attempt_id=attempt_id,
            candidate_id=candidate_id,
            workspace=workspace,
            changed_files=dirty_files,
            last_completed_phase=reason,
            session_id=previous_session_id,
            repair_cycle_increment=1,
            session_snapshot=session_snapshot,
        )
        handoff_bundle = self._write_session_handoff_bundle(
            issue_key=issue_key,
            attempt_id=attempt_id,
            candidate_id=candidate_id,
            reason=reason,
            branch_name=str(workspace_info["branch_name"]),
            workspace=workspace,
            head_sha=head_sha,
            dirty_files=dirty_files,
            plan_v2=plan,
            latest_failures=latest_failures,
            session_snapshot=session_snapshot,
        )
        return await self._execute_candidate(
            chat_client=chat_client,
            channel=channel,
            issue_key=issue_key,
            run_id=run_id,
            workflow=workflow,
            issue=issue,
            summary=summary,
            plan=plan,
            test_plan=test_plan,
            verification_plan=verification_plan,
            attempt_id=attempt_id,
            workspace_info=workspace_info,
            candidate_id=candidate_id,
            session_id=previous_session_id or None,
            handoff_bundle=handoff_bundle,
        )

    def _build_candidate_strategy(
        self,
        *,
        candidate_id: str,
        plan: dict[str, Any],
        test_plan: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "version": 1,
            "candidate_id": candidate_id,
            "candidate_files": list(plan.get("candidate_files", [])),
            "must_not_touch": list(plan.get("must_not_touch", [])),
            "verification_focus": list(plan.get("verification_focus", [])),
            "tasks": self._candidate_strategy_tasks(plan),
            "tests": {
                "unit": list(test_plan.get("unit", [])),
                "integration": list(test_plan.get("integration", [])),
                "manual_checks": list(test_plan.get("manual_checks", [])),
            },
        }

    def _candidate_strategy_tasks(self, plan: dict[str, Any]) -> list[str]:
        tasks: list[str] = []
        raw_tasks = plan.get("tasks", [])
        if isinstance(raw_tasks, list):
            for item in raw_tasks:
                if isinstance(item, dict):
                    summary = str(item.get("summary", "")).strip()
                else:
                    summary = str(item).strip()
                if summary and summary not in tasks:
                    tasks.append(summary)
        for key in ("implementation_steps", "steps"):
            raw_steps = plan.get(key, [])
            if not isinstance(raw_steps, list):
                continue
            for item in raw_steps:
                summary = str(item).strip()
                if summary and summary not in tasks:
                    tasks.append(summary)
        return tasks

    def _build_attempt_manifest(
        self,
        *,
        issue_key: str,
        attempt_id: str,
        run_id: str,
        candidate_ids: list[str],
        workflow: dict[str, Any],
        plan: dict[str, Any],
        scope_contract: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "version": 1,
            "issue_key": issue_key,
            "attempt_id": attempt_id,
            "trigger": "ready",
            "rework_of_attempt_id": None,
            "plan_version": int(plan.get("version", 2) or 2),
            "run_id": run_id,
            "created_at": datetime.now(UTC).isoformat(),
            "candidate_ids": list(candidate_ids),
            "winner_candidate_id": None,
            "status": "running",
            "workflow_hash": self._stable_hash(workflow),
            "plan_hash": self._stable_hash(plan),
            "scope_contract_hash": self._stable_hash(scope_contract),
        }

    def _build_final_attempt_summary(
        self,
        *,
        attempt_id: str,
        winner_result: CandidateExecutionResult,
        candidate_results: list[CandidateExecutionResult],
        winner_selection: dict[str, Any],
        status: str,
    ) -> dict[str, Any]:
        return {
            "version": 1,
            "attempt_id": attempt_id,
            "status": status,
            "winner_candidate_id": winner_result.candidate_id,
            "winner_branch_name": str(winner_result.workspace_info["branch_name"]),
            "successful_candidates": list(winner_selection.get("successful_candidates", [])),
            "selection_basis": str(winner_selection.get("selection_basis", "deterministic_rank")),
            "winner_reason": str(winner_selection.get("winner_reason", "")),
            "candidates": [
                {
                    "candidate_id": result.candidate_id,
                    "success": bool(result.success),
                    "failure_type": str(result.failure_type or ""),
                    "branch_name": str(result.workspace_info.get("branch_name", "")),
                }
                for result in candidate_results
            ],
            "changed_files": list(winner_result.changed_files.get("changed_files", [])),
        }

    def _build_scope_contract(
        self,
        *,
        issue_key: str,
        attempt_id: str,
        plan: dict[str, Any],
        workflow: dict[str, Any] | None = None,
        issue: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        allowed_paths = [str(item).strip() for item in plan.get("candidate_files", []) if str(item).strip()]
        must_not_touch = [str(item).strip() for item in plan.get("must_not_touch", []) if str(item).strip()]
        verification_focus = [str(item).strip() for item in plan.get("verification_focus", []) if str(item).strip()]
        protected_policy = self._protected_config_policy(workflow)
        protected_allowlist = (
            self._resolve_protected_config_allowlist(
                issue=issue,
                issue_key=issue_key,
                issue_body_section=str(protected_policy["issue_body_section"]),
                artifact_names=list(protected_policy["allowlist_artifacts"]),
            )
            if issue is not None
            else []
        )
        return {
            "version": 1,
            "issue_key": issue_key,
            "attempt_id": attempt_id,
            "allowed_paths": allowed_paths,
            "candidate_files": allowed_paths,
            "must_not_touch": must_not_touch,
            "unexpected_file_policy": "report_and_penalize",
            "verification_focus": verification_focus,
            "protected_config_default": str(protected_policy["default_policy"]),
            "protected_config_allow_label_present": (
                str(protected_policy["allow_label"]) in self._issue_labels(issue or {})
            ),
            "protected_config_allowlist": protected_allowlist,
        }

    def _build_scope_analysis(
        self,
        *,
        plan: dict[str, Any],
        changed_files: dict[str, Any],
        issue: dict[str, Any] | None = None,
        issue_key: str = "",
        workflow: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        changed = [str(item).strip() for item in changed_files.get("changed_files", []) if str(item).strip()]
        expected_files = self._expected_plan_files(plan)
        must_not_touch = [str(item).strip() for item in plan.get("must_not_touch", []) if str(item).strip()]
        unexpected_files = [path for path in changed if expected_files and path not in expected_files]
        must_not_touch_violations = [
            path for path in changed if any(fnmatch.fnmatch(path, pattern) for pattern in must_not_touch)
        ]
        protected_policy = self._protected_config_policy(workflow)
        protected_config_violations = []
        protected_config_label_present = False
        protected_config_allowlist: list[str] = []
        if issue is not None:
            (
                protected_config_violations,
                protected_config_label_present,
                protected_config_allowlist,
            ) = self._protected_config_violations(
                changed_files=changed,
                issue=issue,
                issue_key=issue_key,
                workflow=workflow,
            )
        return {
            "version": 1,
            "changed_files": changed,
            "unexpected_files": unexpected_files,
            "unexpected_file_count": len(unexpected_files),
            "must_not_touch": must_not_touch,
            "must_not_touch_violations": must_not_touch_violations,
            "protected_config_default": str(protected_policy["default_policy"]),
            "protected_config_patterns": list(protected_policy["protected_paths"]),
            "protected_config_allow_label": str(protected_policy["allow_label"]),
            "protected_config_label_present": protected_config_label_present,
            "protected_config_allowlist": protected_config_allowlist,
            "protected_config_violations": protected_config_violations,
        }

    def _protected_config_violations(
        self,
        *,
        changed_files: list[str],
        issue: dict[str, Any],
        issue_key: str,
        workflow: dict[str, Any] | None = None,
    ) -> tuple[list[str], bool, list[str]]:
        protected_policy = self._protected_config_policy(workflow)
        labels = self._issue_labels(issue)
        label_present = str(protected_policy["allow_label"]) in labels
        protected_paths = [
            path
            for path in changed_files
            if any(fnmatch.fnmatch(path, pattern) for pattern in protected_policy["protected_paths"])
        ]
        if not protected_paths:
            return [], label_present, []
        if not label_present:
            return protected_paths, False, []
        allowlist = self._resolve_protected_config_allowlist(
            issue=issue,
            issue_key=issue_key,
            issue_body_section=str(protected_policy["issue_body_section"]),
            artifact_names=list(protected_policy["allowlist_artifacts"]),
        )
        if not allowlist:
            return protected_paths, True, []
        violations = [
            path for path in protected_paths if not any(fnmatch.fnmatch(path, pattern) for pattern in allowlist)
        ]
        return violations, True, allowlist

    def _protected_config_policy(self, workflow: dict[str, Any] | None) -> dict[str, Any]:
        policy = {
            "default_policy": "deny",
            "allow_label": self.PROTECTED_CONFIG_ALLOW_LABEL,
            "protected_paths": list(self.PROTECTED_CONFIG_PATTERNS),
            "issue_body_section": self.PROTECTED_CONFIG_ALLOWLIST_SECTION,
            "allowlist_artifacts": ["protected_config_allowlist.json"],
        }
        if not isinstance(workflow, dict):
            return policy
        config = workflow.get("config")
        protected_config = getattr(config, "protected_config", None)
        if protected_config is None:
            return policy
        configured_paths = [
            str(item).strip() for item in getattr(protected_config, "protected_paths", []) if str(item).strip()
        ]
        if configured_paths:
            policy["protected_paths"] = configured_paths
        allow_label = str(getattr(protected_config, "allow_label", "")).strip()
        if allow_label:
            policy["allow_label"] = allow_label
        default_policy = str(getattr(protected_config, "default_policy", "")).strip()
        if default_policy:
            policy["default_policy"] = default_policy
        allowlist_source = getattr(protected_config, "allowlist_source", None)
        if allowlist_source is not None:
            issue_body_section = str(getattr(allowlist_source, "issue_body_section", "")).strip()
            if issue_body_section:
                policy["issue_body_section"] = issue_body_section
            artifacts = [str(item).strip() for item in getattr(allowlist_source, "artifacts", []) if str(item).strip()]
            if artifacts:
                policy["allowlist_artifacts"] = artifacts
        return policy

    def _resolve_protected_config_allowlist(
        self,
        *,
        issue: dict[str, Any] | None,
        issue_key: str,
        issue_body_section: str,
        artifact_names: list[str],
    ) -> list[str]:
        if issue is not None:
            body_allowlist = self._extract_issue_body_allowlist(str(issue.get("body", "") or ""), issue_body_section)
            if body_allowlist:
                return body_allowlist
        for artifact_name in artifact_names:
            payload = self.state_store.load_artifact(issue_key, artifact_name)
            artifact_allowlist = self._coerce_protected_config_allowlist(payload)
            if artifact_allowlist:
                return artifact_allowlist
        return []

    def _extract_issue_body_allowlist(self, body: str, section_name: str) -> list[str]:
        if not body.strip() or not section_name.strip():
            return []
        heading_pattern = re.compile(rf"^\s{{0,3}}#+\s*{re.escape(section_name)}\s*$")
        lines = body.splitlines()
        capture = False
        section_lines: list[str] = []
        for raw_line in lines:
            line = raw_line.rstrip()
            if not capture:
                if heading_pattern.match(line):
                    capture = True
                continue
            if re.match(r"^\s{0,3}#+\s+\S", line):
                break
            section_lines.append(line)
        return self._normalize_protected_config_allowlist(section_lines)

    def _coerce_protected_config_allowlist(self, payload: object) -> list[str]:
        if isinstance(payload, list):
            return self._normalize_protected_config_allowlist(payload)
        if isinstance(payload, dict):
            for key in ("allowlist", "paths", "protected_paths"):
                value = payload.get(key)
                if isinstance(value, list):
                    return self._normalize_protected_config_allowlist(value)
        return []

    def _normalize_protected_config_allowlist(self, values: Sequence[object]) -> list[str]:
        normalized: list[str] = []
        for value in values:
            text = str(value).strip()
            if not text or text.startswith("<!--"):
                continue
            text = re.sub(r"^\s*(?:[-*+]\s+|\d+\.\s+)", "", text).strip()
            text = text.strip("`").strip()
            if text and text not in normalized:
                normalized.append(text)
        return normalized

    def _issue_labels(self, issue: dict[str, Any]) -> set[str]:
        raw_labels = issue.get("labels", []) if isinstance(issue, dict) else []
        if not isinstance(raw_labels, list):
            return set()
        labels: set[str] = set()
        for item in raw_labels:
            if isinstance(item, str):
                label = item.strip()
            elif isinstance(item, dict):
                label = str(item.get("name", "")).strip()
            else:
                label = str(getattr(item, "name", "")).strip()
            if label:
                labels.add(label)
        return labels

    def _stable_hash(self, payload: object) -> str:
        encoded = json.dumps(self._json_safe(payload), ensure_ascii=False, sort_keys=True).encode("utf-8")
        return f"sha256:{hashlib.sha256(encoded).hexdigest()}"

    def _write_attempt_manifest_status(
        self,
        *,
        issue_key: str,
        attempt_id: str,
        status: str,
        winner_candidate_id: str | None,
    ) -> None:
        manifest = self.state_store.load_attempt_artifact(issue_key, attempt_id, "attempt_manifest.json")
        if not isinstance(manifest, dict):
            manifest = {}
        manifest["status"] = status
        manifest["winner_candidate_id"] = winner_candidate_id
        self.state_store.write_attempt_artifact(issue_key, attempt_id, "attempt_manifest.json", manifest)

    def _evaluate_candidate_proof(self, *, issue_key: str, attempt_id: str, candidate_id: str) -> dict[str, Any]:
        required = {
            "candidate_manifest.json",
            "candidate_strategy.json",
            "runner_metadata.json",
            "implementation_result.json",
            "changed_files.json",
            "scope_analysis.json",
            "repair_feedback.json",
            "command_results.json",
            "verification_result.json",
            "verification.json",
            "review_result.json",
            "review_decision.json",
            "review_findings.json",
            "postable_findings.json",
            "repair_instructions.json",
            "latest_repair_feedback.json",
            "latest_review_delta.json",
            "session_checkpoint.json",
        }
        candidate_root = self.state_store.candidate_artifacts_dir(issue_key, attempt_id, candidate_id)
        present = {path.name for path in candidate_root.glob("*.json")}
        missing = sorted(required - present)
        if not (candidate_root / "repair_feedback.jsonl").exists():
            missing.append("repair_feedback.jsonl")
        return {
            "version": 1,
            "candidate_id": candidate_id,
            "complete": not missing,
            "missing_artifacts": missing,
            "required_artifacts_present": sorted(required & present),
        }

    def _evaluate_attempt_proof(self, *, issue_key: str, attempt_id: str, winner_candidate_id: str) -> dict[str, Any]:
        required = {
            "attempt_manifest.json",
            "scope_contract.json",
            "candidate_decision.json",
            "winner_selection.json",
            "final_attempt_summary.json",
        }
        attempt_root = self.state_store.attempt_artifacts_dir(issue_key, attempt_id)
        present = {path.name for path in attempt_root.glob("*.json")}
        missing = sorted(required - present)
        required_present = sorted(required & present)
        winner_selection = self.state_store.load_attempt_artifact(issue_key, attempt_id, "winner_selection.json")
        if isinstance(winner_selection, dict) and bool(winner_selection.get("exact_tie_detected")):
            if "winner_tiebreak_judge.json" not in present:
                missing.append("winner_tiebreak_judge.json")
            else:
                required_present.append("winner_tiebreak_judge.json")
        candidate_proof = self.state_store.load_candidate_artifact(
            issue_key, attempt_id, winner_candidate_id, "proof_result.json"
        )
        if not isinstance(candidate_proof, dict) or not bool(candidate_proof.get("complete")):
            missing.append(f"candidates/{winner_candidate_id}/proof_result.json")
        else:
            required_present.append(f"candidates/{winner_candidate_id}/proof_result.json")
        winner_views_dir = self.state_store.views_dir(issue_key)
        required_views = {
            "runner_metadata.json",
            "implementation_result.json",
            "changed_files.json",
            "scope_analysis.json",
            "repair_feedback.json",
            "command_results.json",
            "verification_result.json",
            "verification.json",
            "review_result.json",
            "review_decision.json",
            "review_findings.json",
            "postable_findings.json",
        }
        present_views = {path.name for path in winner_views_dir.glob("*.json")}
        missing_views = sorted(required_views - present_views)
        if missing_views:
            missing.extend(f"views/{name}" for name in missing_views)
        else:
            required_present.extend(f"views/{name}" for name in sorted(required_views))
        return {
            "version": 1,
            "attempt_id": attempt_id,
            "winner_candidate_id": winner_candidate_id,
            "complete": not missing,
            "missing_artifacts": missing,
            "required_artifacts_present": required_present,
        }

    def _write_session_checkpoint(
        self,
        *,
        issue_key: str,
        attempt_id: str,
        candidate_id: str,
        workspace: str,
        changed_files: list[str],
        last_completed_phase: str,
        session_id: str = "",
        turn_count: int | None = None,
        steer_count: int | None = None,
        repair_cycle_increment: int = 0,
        session_snapshot: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        previous = self.state_store.load_candidate_artifact(
            issue_key, attempt_id, candidate_id, "session_checkpoint.json"
        )
        previous_turn_count = int(previous.get("turn_count", 0) or 0) if isinstance(previous, dict) else 0
        previous_steer_count = int(previous.get("steer_count", 0) or 0) if isinstance(previous, dict) else 0
        previous_repair_cycles = int(previous.get("repair_cycles", 0) or 0) if isinstance(previous, dict) else 0
        observed_turn_count = self._session_snapshot_count(session_snapshot, "turn_count", "turnCount", "turns")
        observed_steer_count = self._session_snapshot_count(session_snapshot, "steer_count", "steerCount", "steers")
        payload = {
            "version": 1,
            "issue_key": issue_key,
            "attempt_id": attempt_id,
            "candidate_id": candidate_id,
            "session_id": session_id,
            "turn_count": max(
                0,
                turn_count
                if turn_count is not None
                else observed_turn_count
                if observed_turn_count is not None
                else previous_turn_count,
            ),
            "steer_count": max(
                0,
                steer_count
                if steer_count is not None
                else observed_steer_count
                if observed_steer_count is not None
                else previous_steer_count,
            ),
            "repair_cycles": max(0, previous_repair_cycles + repair_cycle_increment),
            "head_sha": self._git_head_sha(workspace),
            "dirty_files": [str(item).strip() for item in changed_files if str(item).strip()],
            "last_completed_phase": last_completed_phase,
            "last_updated_at": datetime.now(UTC).isoformat(),
        }
        if session_snapshot:
            payload["session_snapshot"] = session_snapshot
        self.state_store.write_candidate_artifact(
            issue_key, attempt_id, candidate_id, "session_checkpoint.json", payload
        )
        return payload

    def _next_handoff_id(self, issue_key: str, attempt_id: str, candidate_id: str) -> str:
        handoffs_dir = self.state_store.candidate_dir(issue_key, attempt_id, candidate_id) / "handoffs"
        handoffs_dir.mkdir(parents=True, exist_ok=True)
        existing = sorted(path.stem for path in handoffs_dir.glob("handoff-*.json"))
        next_index = 1
        if existing:
            latest = existing[-1]
            try:
                next_index = int(latest.removeprefix("handoff-")) + 1
            except ValueError:
                next_index = 1
        return f"handoff-{next_index:03d}"

    def _write_session_handoff_bundle(
        self,
        *,
        issue_key: str,
        attempt_id: str,
        candidate_id: str,
        reason: str,
        branch_name: str,
        workspace: str,
        head_sha: str,
        dirty_files: list[str],
        plan_v2: dict[str, Any],
        latest_failures: list[dict[str, Any]],
        session_snapshot: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        rollover_id = self._next_handoff_id(issue_key, attempt_id, candidate_id)
        bundle = {
            "version": 1,
            "attempt_id": attempt_id,
            "candidate_id": candidate_id,
            "rollover_id": rollover_id,
            "reason": reason,
            "objective": "finish remaining fixes without expanding scope",
            "current_status": {
                "branch_name": branch_name,
                "workspace_path": workspace,
                "head_sha": head_sha,
                "dirty_files": list(dirty_files),
            },
            "immutable_constraints": {
                "must_not_touch": list(plan_v2.get("must_not_touch", [])),
                "protected_config_allowed": False,
            },
            "plan_context": {
                "goal": str(plan_v2.get("goal", "")),
                "verification_focus": list(plan_v2.get("verification_focus", [])),
            },
            "completed_work": [],
            "remaining_work": [],
            "latest_failures": latest_failures,
            "repair_feedback_refs": ["repair_feedback.json", "repair_feedback.jsonl"],
            "latest_repair_feedback_refs": ["latest_repair_feedback.json"],
            "latest_review_delta_refs": ["latest_review_delta.json"],
            "input_artifacts": [
                "scope_contract.json",
                "verification_plan.json",
                "candidate_manifest.json",
                "repair_feedback.json",
            ],
        }
        if session_snapshot:
            bundle["current_status"]["session"] = session_snapshot
        self.state_store.write_candidate_artifact(
            issue_key,
            attempt_id,
            candidate_id,
            "session_handoff_bundle.json",
            bundle,
        )
        handoffs_dir = self.state_store.candidate_dir(issue_key, attempt_id, candidate_id) / "handoffs"
        self.state_store._write_json(handoffs_dir / f"{rollover_id}.json", bundle)
        return bundle

    def _build_handoff_steer_message(self, handoff_bundle: dict[str, Any] | None) -> str:
        if not isinstance(handoff_bundle, dict) or not handoff_bundle:
            return ""
        reason = str(handoff_bundle.get("reason", "")).strip() or "handoff"
        current_status = handoff_bundle.get("current_status", {})
        if not isinstance(current_status, dict):
            current_status = {}
        dirty_files = [str(item).strip() for item in current_status.get("dirty_files", []) if str(item).strip()]
        immutable_constraints = handoff_bundle.get("immutable_constraints", {})
        if not isinstance(immutable_constraints, dict):
            immutable_constraints = {}
        must_not_touch = [
            str(item).strip() for item in immutable_constraints.get("must_not_touch", []) if str(item).strip()
        ]
        latest_failures = handoff_bundle.get("latest_failures", [])
        if not isinstance(latest_failures, list):
            latest_failures = []
        messages = [
            str(item.get("message", "")).strip()
            for item in latest_failures
            if isinstance(item, dict) and str(item.get("message", "")).strip()
        ]
        lines = [
            f"Handoff reason: {reason}. Continue from the current workspace state.",
            "Keep the fix narrow and do not expand scope.",
        ]
        if dirty_files:
            lines.append(f"Focus on these files first: {', '.join(dirty_files[:8])}.")
        if messages:
            lines.append(f"Resolve these issues: {'; '.join(messages[:3])}.")
        if must_not_touch:
            lines.append(f"Do not modify: {', '.join(must_not_touch[:8])}.")
        lines.append("Do not modify protected configs or workflow contracts.")
        return "\n".join(lines)

    def _git_head_sha(self, workspace: str) -> str:
        completed = subprocess.run(
            ["git", "-C", workspace, "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            return ""
        return completed.stdout.strip()

    def _sync_candidate_generated_artifacts(
        self,
        *,
        issue_key: str,
        attempt_id: str,
        run_id: str,
        candidate_id: str,
        artifacts_dir: Path,
    ) -> None:
        for filename in ("implementation_result.json", "changed_files.json"):
            path = artifacts_dir / filename
            if not path.exists():
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            self.state_store.write_execution_artifact(
                issue_key,
                f"candidates/{candidate_id}/{filename}",
                payload,
                run_id,
            )
            self.state_store.write_candidate_artifact(issue_key, attempt_id, candidate_id, filename, payload)

    def _candidate_failure_message(self, result: CandidateExecutionResult) -> str:
        if result.failure_type == "codex_failure":
            return f"Codex 実装で失敗しました。終了コード: `{result.codex_result.returncode}`"
        if result.failure_type == "hard_check_failed":
            return "verification の hard check が失敗しました。`/status` と `/why-failed` を確認してください。"
        if result.failure_type == "environment_blocked":
            return "verification 環境の bootstrap に失敗したため run を停止しました。環境要因を解消してから再実行してください。"
        if result.failure_type == "review_reject":
            return "review が reject を返したため PR 作成を中止しました。"
        if result.failure_type == "review_repairable":
            return "review が follow-up 修正を要求したため PR 作成を中止しました。"
        if result.failure_type == "candidate_proof_incomplete":
            return "candidate artifact が不足しているため run を継続できませんでした。"
        if result.failure_type == "scope_violation":
            return "変更範囲が plan の `must_not_touch` または期待ファイル範囲を外れたため run を停止しました。"
        if result.failure_type == "protected_config_violation":
            return (
                "protected config を変更したため run を停止しました。"
                f" 例外が必要な場合は issue に `{self.PROTECTED_CONFIG_ALLOW_LABEL}` ラベルを付け、"
                f" `{self.PROTECTED_CONFIG_ALLOWLIST_SECTION}` セクションまたは `protected_config_allowlist.json` で許可対象を明示してください。"
            )
        if result.failure_type == "policy_violation":
            return "禁止または高リスクの workflow command を検出したため run を停止しました。"
        if result.failure_type:
            return "verification が失敗しました。`/status` と `/why-failed` を確認してください。"
        return "run が失敗しました。`/status` を確認してください。"

    def _build_review_result(
        self,
        *,
        candidate_id: str,
        review: dict[str, Any],
        scope_analysis: dict[str, Any],
    ) -> dict[str, Any]:
        findings = review.get("postable_findings") or review.get("findings") or []
        severity_counts = {"critical": 0, "high": 0, "medium": 0, "low": 0}
        verified_finding_count = 0
        unverified_finding_count = 0
        for finding in findings:
            severity = self._review_finding_severity(finding)
            if severity in severity_counts:
                severity_counts[severity] += 1
            if isinstance(finding, dict):
                status = str(finding.get("verifier_status", "")).strip().lower()
            else:
                status = str(getattr(finding, "verifier_status", "")).strip().lower()
            if status in {"confirmed", "verified", "pass", "passed"}:
                verified_finding_count += 1
            else:
                unverified_finding_count += 1
        scope_drift = bool(scope_analysis.get("unexpected_file_count", 0)) or bool(
            scope_analysis.get("must_not_touch_violations")
        )
        protected_contract_ok = not (
            bool(scope_analysis.get("must_not_touch_violations"))
            or bool(scope_analysis.get("protected_config_violations"))
            or bool(review.get("protected_path_touches"))
        )
        reject_reasons: list[str] = []
        if str(review.get("decision", "")).strip().lower() == "reject":
            for key in ("risk_items", "unnecessary_changes", "test_gaps"):
                raw_items = review.get(key, [])
                if not isinstance(raw_items, list):
                    continue
                for item in raw_items:
                    text = str(item).strip()
                    if text and text not in reject_reasons:
                        reject_reasons.append(text)
            if not reject_reasons:
                reject_reasons.append("review returned reject")
        payload = ReviewResult(
            candidate_id=candidate_id,
            decision=str(review.get("decision", "")).strip(),
            reject_reasons=reject_reasons,
            severity_counts=severity_counts,
            verified_finding_count=verified_finding_count,
            unverified_finding_count=unverified_finding_count,
            plan_alignment_ok=not scope_drift and not bool(review.get("unnecessary_changes")),
            scope_drift=scope_drift,
            protected_contract_ok=protected_contract_ok,
            findings_ref="review_findings.json",
        )
        return self._json_safe(payload)

    def _build_review_decision_payload(self, review_result: dict[str, Any]) -> dict[str, Any]:
        return {
            "version": 1,
            "candidate_id": str(review_result.get("candidate_id", "")).strip(),
            "decision": str(review_result.get("decision", "")).strip(),
            "reject_reasons": list(review_result.get("reject_reasons", [])),
            "severity_counts": dict(review_result.get("severity_counts", {})),
            "verified_finding_count": int(review_result.get("verified_finding_count", 0)),
            "unverified_finding_count": int(review_result.get("unverified_finding_count", 0)),
            "plan_alignment_ok": bool(review_result.get("plan_alignment_ok", True)),
            "scope_drift": bool(review_result.get("scope_drift", False)),
            "protected_contract_ok": bool(review_result.get("protected_contract_ok", True)),
            "findings_ref": "review_findings.json",
            "postable_findings_ref": "postable_findings.json",
        }

    def _build_review_findings_payload(self, review: dict[str, Any]) -> dict[str, Any]:
        findings = review.get("findings")
        if not isinstance(findings, list):
            findings = []
        postable_findings = review.get("postable_findings")
        if not isinstance(postable_findings, list):
            postable_findings = findings
        return {
            "version": 1,
            "findings": self._json_safe(findings),
            "postable_findings": self._json_safe(postable_findings),
        }

    def _build_postable_findings_payload(self, review: dict[str, Any]) -> dict[str, Any]:
        postable_findings = review.get("postable_findings")
        if not isinstance(postable_findings, list):
            postable_findings = review.get("findings") if isinstance(review.get("findings"), list) else []
        return {
            "version": 1,
            "findings": self._json_safe(postable_findings),
        }

    def _build_repair_feedback(
        self,
        *,
        candidate_id: str,
        repair_results: dict[str, Any],
    ) -> dict[str, Any]:
        issues: list[dict[str, Any]] = []
        results = repair_results.get("results", []) if isinstance(repair_results, dict) else []
        for item in results:
            if not isinstance(item, dict):
                continue
            if str(item.get("status", "")).strip() in {"pass", "not_applicable"}:
                continue
            phase = str(item.get("phase", "")).strip() or "repair"
            issues.append(
                {
                    "tool": phase,
                    "rule_id": "",
                    "severity": "error",
                    "file": "",
                    "line": 0,
                    "message": str(item.get("output", "")).strip()[:1000],
                    "why": f"{phase} check failed during fast repair preflight",
                    "fix": f"Fix the {phase} failure without expanding scope.",
                    "example": {},
                    "source": str(repair_results.get("repair_profile", "")).strip() or "verification_plan",
                }
            )
        return {
            "version": 1,
            "candidate_id": candidate_id,
            "phase": "fast-repair",
            "repair_profile": str(repair_results.get("repair_profile", "")).strip(),
            "applicable": bool(issues),
            "issues": issues,
        }

    def _build_repair_instructions(
        self,
        *,
        candidate_id: str,
        review_result: dict[str, Any],
        review_findings: dict[str, Any],
    ) -> dict[str, Any]:
        instructions: list[str] = []

        def _append(value: Any) -> None:
            text = str(value).strip()
            if text and text not in instructions:
                instructions.append(text)

        for finding in review_findings.get("postable_findings") or review_findings.get("findings") or []:
            if isinstance(finding, dict):
                _append(finding.get("suggested_fix") or finding.get("claim"))
            else:
                _append(getattr(finding, "suggested_fix", "") or getattr(finding, "claim", ""))
        for key in ("risk_items", "test_gaps", "unnecessary_changes"):
            raw_items = review_findings.get(key, [])
            if not isinstance(raw_items, list):
                continue
            for item in raw_items:
                _append(item)

        applicable = bool(instructions) or str(review_result.get("decision", "")).strip().lower() in {
            "repairable",
            "reject",
        }
        if applicable:
            _append("Do not touch protected config or must_not_touch files.")
            _append("Re-run verification checks after the change.")
        scope = "narrow"
        if review_result.get("scope_drift") or not review_result.get("protected_contract_ok", True):
            scope = "broad"
        return {
            "version": 1,
            "candidate_id": candidate_id,
            "applicable": applicable,
            "scope": scope,
            "instructions": instructions,
            "source_review_result": "review_result.json",
        }

    def _build_latest_repair_feedback(
        self,
        *,
        candidate_id: str,
        verification: dict[str, Any],
        repair_instructions: dict[str, Any],
    ) -> dict[str, Any]:
        raw_notes = verification.get("notes", [])
        notes = list(raw_notes) if isinstance(raw_notes, list) else []
        return {
            "version": 1,
            "candidate_id": candidate_id,
            "failure_type": str(verification.get("failure_type", "")).strip(),
            "retry_recommended": bool(verification.get("retry_recommended", False)),
            "notes": notes,
            "instructions": list(repair_instructions.get("instructions", [])),
            "repair_feedback_ref": "repair_feedback.json",
            "source_repair_instructions": "repair_instructions.json",
        }

    def _build_latest_review_delta(
        self,
        *,
        candidate_id: str,
        review_result: dict[str, Any],
        review_findings: dict[str, Any],
    ) -> dict[str, Any]:
        findings = review_findings.get("postable_findings") or review_findings.get("findings") or []
        finding_ids: list[str] = []
        for finding in findings:
            if isinstance(finding, dict):
                finding_id = str(finding.get("id", "")).strip()
            else:
                finding_id = str(getattr(finding, "id", "")).strip()
            if finding_id and finding_id not in finding_ids:
                finding_ids.append(finding_id)
        summary: list[str] = []
        decision = str(review_result.get("decision", "")).strip()
        if decision:
            summary.append(f"decision={decision}")
        raw_reasons = review_result.get("reject_reasons", [])
        if isinstance(raw_reasons, list):
            for reason in raw_reasons:
                text = str(reason).strip()
                if text and text not in summary:
                    summary.append(text)
        return {
            "version": 1,
            "candidate_id": candidate_id,
            "decision": decision,
            "severity_counts": dict(review_result.get("severity_counts", {})),
            "verified_finding_count": int(review_result.get("verified_finding_count", 0)),
            "unverified_finding_count": int(review_result.get("unverified_finding_count", 0)),
            "finding_ids": finding_ids,
            "summary": summary,
            "source_review_result": "review_result.json",
            "source_review_findings": "review_findings.json",
        }

    def _review_follow_up_failures(
        self,
        *,
        review_result: dict[str, Any],
        repair_instructions: dict[str, Any],
    ) -> list[dict[str, Any]]:
        latest_failures: list[dict[str, Any]] = []
        for item in repair_instructions.get("instructions", []):
            text = str(item).strip()
            if text:
                latest_failures.append({"type": "review", "message": text})
        if latest_failures:
            return latest_failures[:5]
        decision = str(review_result.get("decision", "")).strip() or "review_follow_up"
        return [{"type": "review", "message": decision}]

    def _max_review_severity(self, review_result: dict[str, Any]) -> str:
        counts = review_result.get("severity_counts", {})
        if not isinstance(counts, dict):
            return ""
        for severity in ("critical", "high", "medium", "low"):
            if int(counts.get(severity, 0) or 0) > 0:
                return severity
        return ""

    def _can_minor_repair_reuse(
        self,
        *,
        review_result: dict[str, Any],
        verification: dict[str, Any],
        changed_files: dict[str, Any],
    ) -> bool:
        if str(review_result.get("decision", "")).strip().lower() != "repairable":
            return False
        if bool(review_result.get("scope_drift")):
            return False
        if not bool(review_result.get("protected_contract_ok", True)):
            return False
        if self._max_review_severity(review_result) in {"critical", "high"}:
            return False
        failure_type = str(verification.get("failure_type", "")).strip().lower()
        hard_checks_pass = str(verification.get("status", "")).strip().lower() in {"success", "passed", "completed"}
        if not hard_checks_pass and failure_type not in {"format", "lint", "typecheck"}:
            return False
        changed = changed_files.get("changed_files", [])
        if not isinstance(changed, list):
            return False
        return len([str(item).strip() for item in changed if str(item).strip()]) <= 5

    def _can_fast_repair_reuse(
        self,
        *,
        repair_feedback: dict[str, Any],
        changed_files: dict[str, Any],
    ) -> bool:
        issues = repair_feedback.get("issues", [])
        if not isinstance(issues, list) or not issues:
            return False
        changed = changed_files.get("changed_files", [])
        if not isinstance(changed, list):
            return False
        if len([str(item).strip() for item in changed if str(item).strip()]) > 5:
            return False
        allowed_tools = {"format", "lint", "typecheck", "tests"}
        for issue in issues:
            if not isinstance(issue, dict):
                return False
            if str(issue.get("tool", "")).strip() not in allowed_tools:
                return False
        return True

    def _repair_feedback_failures(self, repair_feedback: dict[str, Any]) -> list[dict[str, Any]]:
        latest_failures: list[dict[str, Any]] = []
        raw_issues = repair_feedback.get("issues", [])
        if isinstance(raw_issues, list):
            for issue in raw_issues:
                if not isinstance(issue, dict):
                    continue
                message = str(issue.get("fix", "")).strip() or str(issue.get("message", "")).strip()
                if message:
                    latest_failures.append({"type": "fast_repair", "message": message})
        return latest_failures[:5] or [{"type": "fast_repair", "message": "fast repair checks failed"}]

    def _next_attempt_id(self, issue_key: str) -> str:
        current = self.state_store.current_attempt_id(issue_key)
        if current.startswith("att-"):
            try:
                return f"att-{int(current.removeprefix('att-')) + 1:03d}"
            except ValueError:
                pass
        return "att-001"

    def _maybe_write_replan_reason(
        self,
        *,
        workflow: dict[str, Any],
        issue_key: str,
        attempt_id: str,
        review_result: dict[str, Any],
        scope_analysis: dict[str, Any],
    ) -> None:
        config = workflow.get("config")
        replanning = getattr(config, "replanning", None) if config is not None else None
        if replanning is None:
            raw_replanning = workflow.get("replanning") if isinstance(workflow, dict) else None
            replanning_enabled = bool(raw_replanning.get("enabled", True)) if isinstance(raw_replanning, dict) else True
            allowed_reasons = (
                [str(item).strip() for item in raw_replanning.get("auto_replan_on_reject_reasons", [])]
                if isinstance(raw_replanning, dict)
                else ["plan_misalignment", "scope_drift"]
            )
            max_replans_per_issue = (
                int(raw_replanning.get("max_replans_per_issue", 2)) if isinstance(raw_replanning, dict) else 2
            )
            emit_replan_reason_artifact = (
                bool(raw_replanning.get("emit_replan_reason_artifact", True))
                if isinstance(raw_replanning, dict)
                else True
            )
        else:
            replanning_enabled = bool(getattr(replanning, "enabled", True))
            allowed_reasons = [
                str(item).strip()
                for item in getattr(replanning, "auto_replan_on_reject_reasons", [])
                if str(item).strip()
            ]
            max_replans_per_issue = int(getattr(replanning, "max_replans_per_issue", 2) or 0)
            emit_replan_reason_artifact = bool(getattr(replanning, "emit_replan_reason_artifact", True))
        if not replanning_enabled or not emit_replan_reason_artifact:
            return
        if str(review_result.get("decision", "")).strip().lower() != "reject":
            return
        raw_reasons = review_result.get("reject_reasons", [])
        reasons = (
            {str(item).strip() for item in raw_reasons if str(item).strip()} if isinstance(raw_reasons, list) else set()
        )
        allowed = {item for item in allowed_reasons if item}
        if not allowed:
            allowed = {"plan_misalignment", "scope_drift"}
        matched = sorted(reasons & allowed)
        if not matched:
            return
        meta = self.state_store.load_issue_meta(issue_key)
        current_replan_count = int(meta.get("replan_count", 0) or 0)
        if current_replan_count >= max_replans_per_issue:
            return
        next_replan_count = current_replan_count + 1
        payload = {
            "version": 1,
            "issue_key": issue_key,
            "previous_attempt_id": attempt_id,
            "new_attempt_id": self._next_attempt_id(issue_key),
            "triggered_by_review": True,
            "reasons": matched,
            "evidence": [
                f"review_result.plan_alignment_ok={review_result.get('plan_alignment_ok', True)}",
                f"review_result.scope_drift={review_result.get('scope_drift', False)}",
                f"scope_analysis.unexpected_files={scope_analysis.get('unexpected_files', [])}",
            ],
            "replan_count_after_increment": next_replan_count,
        }
        self.state_store.write_attempt_artifact(issue_key, attempt_id, "replan_reason.json", payload)
        self.state_store.update_issue_meta(issue_key, replan_count=next_replan_count)

    def _sync_runner_generated_artifacts(self, *, issue_key: str, run_id: str, artifacts_dir: Path) -> None:
        for filename in ("implementation_result.json", "changed_files.json"):
            path = artifacts_dir / filename
            if not path.exists():
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            self.state_store.write_execution_artifact(issue_key, filename, payload, run_id)
            self.state_store.write_artifact(issue_key, filename, payload)

    def _incident_bundle_enabled(self, workflow: dict[str, Any]) -> bool:
        config = workflow.get("config")
        incident_bundle = getattr(config, "incident_bundle", None)
        return bool(getattr(incident_bundle, "enabled", False))

    def _materialize_incident_bundle(
        self,
        *,
        workflow: dict[str, Any],
        issue_key: str,
        run_id: str,
        workspace: str,
        issue: dict[str, Any],
        summary: dict[str, Any],
        plan: dict[str, Any],
        test_plan: dict[str, Any],
    ) -> Path | None:
        if not self._incident_bundle_enabled(workflow):
            return None
        builder = IncidentBundleBuilder(self.state_store.execution_run_dir(issue_key, run_id))
        payload = {
            "summary.md": "\n".join(
                [
                    f"Issue: {issue_key}",
                    f"Workspace: {workspace}",
                    f"Title: {issue.get('title', '')}",
                    f"Goal: {summary.get('goal', '')}",
                ]
            ),
            "issue_snapshot.json": issue,
            "requirement_summary.json": summary,
            "plan.json": plan,
            "test_plan.json": test_plan,
        }
        return builder.materialize(issue_key, run_id, payload)

    def _finalize_incident_bundle(self, bundle_dir: Path | None, issue_key: str, run_id: str) -> None:
        if bundle_dir is None or not bundle_dir.exists():
            return
        builder = IncidentBundleBuilder(self.state_store.execution_run_dir(issue_key, run_id))
        builder.freeze(bundle_dir)
        builder.cleanup_keep_provenance(bundle_dir, self.state_store.execution_artifacts_dir(issue_key, run_id))

    async def _post_inline_review_comments(
        self,
        *,
        workflow: dict[str, Any],
        repo_full_name: str,
        pr_number: int,
        review: dict[str, Any],
    ) -> None:
        if not self._review_inline_comments_enabled(workflow):
            return
        findings = review.get("postable_findings")
        if not isinstance(findings, list) or not findings:
            return
        poster = GitHubReviewPoster(cast(Any, self.github_client))
        await poster.post_inline(
            pr_number=pr_number,
            repo_full_name=repo_full_name,
            findings=self._coerce_review_findings(findings),
        )

    def _review_inline_comments_enabled(self, workflow: dict[str, Any]) -> bool:
        config = workflow.get("config")
        review = getattr(config, "review", None)
        return bool(getattr(review, "post_inline_to_github", False))

    def _coerce_review_findings(self, findings: list[dict[str, Any]]) -> Any:
        from app.contracts.artifact_models import ReviewFinding, ReviewFindingsV1

        normalized: list[ReviewFinding] = []
        for item in findings:
            if not isinstance(item, dict):
                continue
            normalized.append(
                ReviewFinding(
                    id=str(item.get("id", "")).strip(),
                    severity=str(item.get("severity", "")).strip(),
                    origin=str(item.get("origin", "")).strip(),
                    confidence=float(item.get("confidence", 0.0)),
                    file=str(item.get("file", "")).strip(),
                    line_start=int(item.get("line_start", 0)),
                    line_end=int(item.get("line_end", 0)),
                    claim=str(item.get("claim", "")).strip(),
                    evidence=[str(entry) for entry in item.get("evidence", []) if str(entry).strip()],
                    verifier_status=str(item.get("verifier_status", "unverified")).strip() or "unverified",
                    suggested_fix=(str(item["suggested_fix"]) if item.get("suggested_fix") is not None else None),
                )
            )
        return ReviewFindingsV1(findings=normalized)

    def _json_safe(self, value: Any) -> Any:
        if is_dataclass(value):
            return self._json_safe(asdict(cast(Any, value)))
        if isinstance(value, dict):
            return {str(key): self._json_safe(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [self._json_safe(item) for item in value]
        if isinstance(value, Path):
            return str(value)
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        return str(value)

    async def execute_workflow_commands(
        self,
        *,
        client: ChatClient,
        channel: ChatChannel,
        workspace: str,
        workflow: dict[str, Any],
        issue_key: str,
        run_id: str,
    ) -> dict[str, Any]:
        del client, issue_key, run_id
        results: list[dict[str, Any]] = []
        for check in self._workflow_commands(workflow):
            phase = str(check["phase"])
            command = str(check["command"])
            category = str(check.get("category", "hard"))
            allow_not_applicable = bool(check.get("allow_not_applicable", False))
            if is_high_risk_command(command):
                await channel.send(
                    "禁止または高リスクの workflow command を検出したため停止します。\n"
                    f"- phase: `{phase}`\n"
                    f"- command: `{command}`"
                )
                return {
                    "results": results,
                    "failure_type": "policy_violation",
                    "stopped_before_command": command,
                }
            completed = await self._run_blocking(
                subprocess.run,
                command,
                cwd=workspace,
                shell=True,
                text=True,
                capture_output=True,
            )
            results.append(
                self._command_result(
                    phase=phase,
                    category=category,
                    command=command,
                    returncode=completed.returncode,
                    output=(completed.stdout + completed.stderr)[-4000:],
                    allow_not_applicable=allow_not_applicable,
                )
            )
            failure_type = self._classify_command_failure(
                phase=phase,
                category=category,
                returncode=completed.returncode,
                output=completed.stdout + completed.stderr,
            )
            if failure_type:
                return {
                    "results": results,
                    "failure_type": failure_type,
                    "stopped_before_command": command,
                }
        return {"results": results}

    async def _preflight_workflow_policy(
        self,
        *,
        channel: ChatChannel,
        workflow: dict[str, Any],
    ) -> dict[str, Any] | None:
        for check in self._workflow_commands(workflow):
            phase = str(check["phase"])
            command = str(check["command"])
            if not is_high_risk_command(command):
                continue
            await channel.send(
                "禁止または高リスクの workflow command を検出したため停止します。\n"
                f"- phase: `{phase}`\n"
                f"- command: `{command}`"
            )
            return {
                "results": [],
                "failure_type": "policy_violation",
                "stopped_before_command": command,
            }
        return None

    async def _execute_fast_repair_checks(
        self,
        *,
        workspace: str,
        verification_plan: dict[str, Any],
    ) -> dict[str, Any]:
        checks = verification_plan.get("repair_checks", []) if isinstance(verification_plan, dict) else []
        if not isinstance(checks, list) or not checks:
            return {
                "repair_profile": str(verification_plan.get("repair_profile", "")).strip()
                if isinstance(verification_plan, dict)
                else "",
                "results": [],
            }
        results: list[dict[str, Any]] = []
        for check in self._normalize_required_checks(checks, category="fast-repair"):
            command = str(check["command"])
            if is_high_risk_command(command):
                return {
                    "repair_profile": str(verification_plan.get("repair_profile", "")).strip(),
                    "results": results,
                    "failure_type": "policy_violation",
                    "stopped_before_command": command,
                }
            completed = await self._run_blocking(
                subprocess.run,
                command,
                cwd=workspace,
                shell=True,
                text=True,
                capture_output=True,
            )
            results.append(
                self._command_result(
                    phase=str(check["phase"]),
                    category="fast-repair",
                    command=command,
                    returncode=completed.returncode,
                    output=(completed.stdout + completed.stderr)[-4000:],
                    allow_not_applicable=bool(check.get("allow_not_applicable", False)),
                )
            )
        return {
            "repair_profile": str(verification_plan.get("repair_profile", "")).strip(),
            "results": results,
        }

    def _workflow_commands(self, workflow: dict[str, Any]) -> list[dict[str, Any]]:
        verification = workflow.get("verification", {})
        if isinstance(verification, dict):
            bootstrap_commands = verification.get("bootstrap_commands", [])
            checks = verification.get("required_checks", [])
            advisory_checks = verification.get("advisory_checks", [])
            if isinstance(bootstrap_commands, list) or isinstance(checks, list) or isinstance(advisory_checks, list):
                normalized_checks = self._normalize_required_checks(
                    bootstrap_commands if isinstance(bootstrap_commands, list) else [],
                    category="bootstrap",
                )
                normalized_checks.extend(
                    self._normalize_required_checks(
                        checks if isinstance(checks, list) else [],
                        category="hard",
                    )
                )
                normalized_checks.extend(
                    self._normalize_required_checks(
                        advisory_checks if isinstance(advisory_checks, list) else [],
                        category="advisory",
                    )
                )
                if normalized_checks:
                    return normalized_checks
        commands = workflow.get("commands", {})
        if not isinstance(commands, dict):
            return []
        normalized: list[dict[str, Any]] = []
        for phase in ("setup", "lint", "test"):
            phase_commands = commands.get(phase, [])
            if not isinstance(phase_commands, list):
                continue
            normalized.extend(
                {
                    "phase": phase,
                    "command": str(command).strip(),
                    "category": "bootstrap" if phase == "setup" else "hard",
                    "allow_not_applicable": False,
                }
                for command in phase_commands
                if str(command).strip()
            )
        return normalized

    def _normalize_required_checks(self, checks: list[Any], *, category: str) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for item in checks:
            if isinstance(item, dict):
                command = str(item.get("command", "")).strip()
                if not command:
                    continue
                name = str(item.get("name", "")).strip() or "check"
                normalized.append(
                    {
                        "phase": name,
                        "command": command,
                        "category": str(item.get("category", category)).strip() or category,
                        "allow_not_applicable": bool(item.get("allow_not_applicable", False)),
                    }
                )
                continue
            if isinstance(item, str) and item.strip() and category == "bootstrap":
                normalized.append(
                    {
                        "phase": "bootstrap",
                        "command": item.strip(),
                        "category": category,
                        "allow_not_applicable": False,
                    }
                )
                continue
            if isinstance(item, str) and item.strip():
                # String-only checks are accepted for backwards compatibility but do not
                # provide an executable command. Keep runtime behavior explicit.
                continue
        return normalized

    def _classify_command_failure(
        self,
        *,
        phase: str,
        category: str,
        returncode: int,
        output: str,
    ) -> str:
        if returncode == 0:
            return ""
        lowered = output.lower()
        environment_markers = (
            "failed to spawn",
            "no such file or directory",
            "command not found",
            "is not recognized as an internal or external command",
            "temporary failure in name resolution",
            "name or service not known",
            "network is unreachable",
            "connection refused",
            "could not resolve host",
            "failed to download",
            "failed to fetch",
            "tls handshake timeout",
            "timed out",
            "connection reset by peer",
        )
        if category == "bootstrap":
            return "environment_blocked"
        if any(marker in lowered for marker in environment_markers):
            return "environment_blocked"
        return ""

    def _load_effective_workflow(self, *, workspace: str, verification_plan: dict[str, Any]) -> dict[str, Any]:
        workflow = load_workflow(workspace=workspace)
        prefer_verification_plan = False
        if not workflow:
            workflow = load_workflow(repo_root=".")
            prefer_verification_plan = True
        return self._resolve_workflow(
            workflow,
            verification_plan,
            prefer_verification_plan=prefer_verification_plan,
        )

    def _refresh_verification_plan_for_workspace(
        self,
        *,
        workspace: str,
        plan: dict[str, Any],
        verification_plan: dict[str, Any],
    ) -> dict[str, Any]:
        refreshed_verification_plan = build_verification_plan(
            workspace=workspace,
            repo_profile=build_repo_profile(workspace),
            plan=plan,
        )
        if self._should_refresh_verification_plan(
            existing_verification_plan=verification_plan,
            refreshed_verification_plan=refreshed_verification_plan,
        ):
            return refreshed_verification_plan
        return verification_plan

    def _should_refresh_verification_plan(
        self,
        *,
        existing_verification_plan: dict[str, Any],
        refreshed_verification_plan: dict[str, Any],
    ) -> bool:
        if not existing_verification_plan:
            return True
        existing_profile = str(existing_verification_plan.get("profile", "")).strip()
        refreshed_profile = str(refreshed_verification_plan.get("profile", "")).strip()
        if existing_profile == "generic-minimal" and refreshed_profile and refreshed_profile != existing_profile:
            return True
        existing_bootstrap = existing_verification_plan.get("bootstrap_commands", [])
        refreshed_bootstrap = refreshed_verification_plan.get("bootstrap_commands", [])
        if not existing_bootstrap and bool(refreshed_bootstrap):
            return True
        existing_checks = existing_verification_plan.get("hard_checks", [])
        refreshed_checks = refreshed_verification_plan.get("hard_checks", [])
        if not existing_checks and bool(refreshed_checks):
            return True
        return False

    def _resolve_workflow(
        self,
        workflow: dict[str, Any],
        verification_plan: dict[str, Any],
        *,
        prefer_verification_plan: bool = False,
    ) -> dict[str, Any]:
        resolved = dict(workflow)
        verification = resolved.get("verification", {})
        if not prefer_verification_plan and isinstance(verification, dict) and verification.get("required_checks"):
            return resolved
        if verification_plan:
            resolved["verification"] = workflow_verification_from_plan(verification_plan)
        return resolved

    def _command_result(
        self,
        *,
        phase: str,
        category: str,
        command: str,
        returncode: int,
        output: str,
        allow_not_applicable: bool,
    ) -> dict[str, Any]:
        status = "pass" if returncode == 0 else "fail"
        lowered = output.lower()
        if (
            returncode != 0
            and allow_not_applicable
            and any(
                marker in lowered
                for marker in ("no tests ran", "no tests collected", "not found", "no such file or directory")
            )
        ):
            status = "not_applicable"
        return {
            "phase": phase,
            "category": category,
            "command": command,
            "returncode": returncode,
            "output": output,
            "success": status == "pass",
            "status": status,
        }

    def _has_failed_hard_checks(self, command_results: dict[str, Any]) -> bool:
        results = command_results.get("results", []) if isinstance(command_results, dict) else []
        for item in results:
            if not isinstance(item, dict):
                continue
            if str(item.get("category", "hard")) != "hard":
                continue
            if str(item.get("status", "fail")) == "fail":
                return True
        return False

    def _detect_changed_files(self, workspace: str) -> list[str]:
        output = subprocess.check_output(["git", "-C", workspace, "status", "--porcelain"], text=True)
        return [line[3:] for line in output.splitlines() if len(line) >= 4]

    def _capture_git_diff(self, workspace: str) -> str:
        completed = subprocess.run(
            ["git", "-C", workspace, "diff", "--stat", "--patch"], capture_output=True, text=True, check=True
        )
        return completed.stdout

    def _commit_and_push(self, workspace: str, branch_name: str, issue_number: int) -> bool:
        status = subprocess.run(
            ["git", "-C", workspace, "status", "--porcelain"], capture_output=True, text=True, check=True
        )
        if not status.stdout.strip():
            return False
        subprocess.run(["git", "-C", workspace, "add", "-A"], check=True)
        subprocess.run(
            ["git", "-C", workspace, "commit", "-m", f"feat: automated changes for issue #{issue_number}"],
            check=True,
        )
        bootstrap_base_branch = ""
        if (
            subprocess.run(["git", "-C", workspace, "rev-parse", "--verify", "main"], capture_output=True).returncode
            == 0
        ):
            ls_remote = subprocess.run(
                ["git", "-C", workspace, "ls-remote", "--heads", "origin", "main"],
                capture_output=True,
                text=True,
                check=True,
            )
            if not ls_remote.stdout.strip():
                bootstrap_base_branch = "main"
        self.workspace_manager.push_branch(workspace, branch_name, bootstrap_base_branch=bootstrap_base_branch)
        return True

    def _build_pr_body(
        self,
        issue: dict[str, Any],
        thread_url: str,
        changed_files: dict[str, Any],
        command_results: dict[str, Any],
        verification: dict[str, Any],
        review: dict[str, Any],
    ) -> str:
        changed = changed_files.get("changed_files", [])
        lines = [
            "## Purpose",
            f"- {issue.get('title', '')}",
            "",
            "## Changed Files",
            *[f"- `{item}`" for item in changed[:20]],
            "",
            "## Test Results",
            *[
                f"- `{item.get('phase')}` `{item.get('command')}` => {item.get('status', 'fail')}"
                for item in command_results.get("results", [])[:20]
            ],
            "",
            "## Verification",
            *[f"- {item}" for item in verification.get("notes", [])[:10]],
            "",
            "## Review",
            *[f"- {item}" for item in review.get("risk_items", [])[:10]],
            "",
            "## Links",
            f"- Discord thread: {thread_url}",
            f"- Issue: {issue.get('url', '')}",
        ]
        return "\n".join(lines)

    def _build_pr_comment(
        self,
        thread_url: str,
        verification: dict[str, Any],
        review: dict[str, Any],
        command_results: dict[str, Any],
    ) -> str:
        return "\n".join(
            [
                "dev-bot run summary",
                "",
                f"- Discord thread: {thread_url}",
                f"- verification status: {verification.get('status', 'unknown')}",
                f"- review decision: {review.get('decision', 'unknown')}",
                "- command results:",
                *[
                    f"  - {item.get('phase')}: `{item.get('command')}` => {item.get('status', 'fail')}"
                    for item in command_results.get("results", [])[:10]
                ],
            ]
        )

    def _load_issue_snapshot(self, repo_full_name: str, issue: dict[str, Any]) -> dict[str, Any]:
        try:
            return self.github_client.get_issue_snapshot(repo_full_name, int(issue["number"]))
        except Exception:
            return dict(issue)

    def _update_issue_tracking(
        self,
        repo_full_name: str,
        issue_number: int,
        state: str,
        sections: dict[str, Any],
        workpad_updates_path: Path,
    ) -> None:
        self.github_client.update_issue_state(repo_full_name, issue_number, state)
        self.github_client.upsert_workpad_comment(repo_full_name, issue_number, sections)
        self._write_jsonl(
            workpad_updates_path,
            {"timestamp": datetime.now(UTC).isoformat(), "state": state, "sections": sections},
        )

    def _build_verification_json(
        self,
        command_results: dict[str, Any],
        verification_summary: dict[str, Any],
        test_plan: dict[str, Any],
    ) -> dict[str, Any]:
        results = command_results.get("results", []) if isinstance(command_results, dict) else []
        phase_status = {"unit": "not_run", "integration": "not_run", "lint": "not_run", "typecheck": "not_run"}
        hard_checks: list[dict[str, Any]] = []
        advisory_checks: list[dict[str, Any]] = []
        for item in results:
            phase = str(item.get("phase", ""))
            status = str(item.get("status", "fail"))
            if phase == "lint":
                phase_status["lint"] = status
            elif phase in {"test", "tests"}:
                phase_status["unit"] = status
            elif phase == "typecheck":
                phase_status["typecheck"] = status
            normalized = {
                "name": phase,
                "status": status,
                "command": str(item.get("command", "")),
            }
            if str(item.get("category", "hard")) == "advisory":
                advisory_checks.append(normalized)
            else:
                hard_checks.append(normalized)
        manual_checks = []
        if isinstance(test_plan, dict):
            for name in test_plan.get("manual_checks", []):
                manual_checks.append({"name": str(name), "status": "not_run"})
        return {
            **phase_status,
            "hard_checks": hard_checks,
            "advisory_checks": advisory_checks,
            "manual_checks": manual_checks,
            "notes": verification_summary.get("notes", []) if isinstance(verification_summary, dict) else [],
        }

    def _build_verification_result(
        self,
        command_results: dict[str, Any],
        verification_summary: dict[str, Any],
    ) -> dict[str, Any]:
        failure_type = str(verification_summary.get("failure_type", "")).strip()
        status = str(verification_summary.get("status", "")).strip()
        payload = VerificationResult(
            candidate_id=str(verification_summary.get("candidate_id", "")).strip() or "primary",
            status=status,
            failure_type=failure_type,
            hard_checks_pass=not self._has_failed_hard_checks(command_results),
            retry_recommended=bool(verification_summary.get("retry_recommended", False)),
            human_check_recommended=bool(verification_summary.get("human_check_recommended", False)),
            notes=(
                [str(item) for item in verification_summary.get("notes", [])]
                if isinstance(verification_summary.get("notes", []), list)
                else []
            ),
        )
        return self._json_safe(payload)

    def _build_workpad_sections(
        self,
        *,
        summary: dict[str, Any],
        plan: dict[str, Any],
        test_plan: dict[str, Any],
        issue: dict[str, Any],
        current_state: str,
        latest_attempt: str,
        branch: str,
        pr: str,
        verification: dict[str, Any],
        blockers: list[str],
        artifacts: list[str],
        audit_trail: list[str],
    ) -> dict[str, Any]:
        return {
            "Current State": current_state,
            "Plan Approved": "true",
            "Goal": summary.get("goal", issue.get("title", "")),
            "Acceptance Criteria": summary.get("acceptance_criteria", []),
            "Constraints": summary.get("constraints", []),
            "Plan Summary": plan.get("steps", []),
            "Test Plan": self._flatten_test_plan(test_plan),
            "Latest Attempt": latest_attempt,
            "Verification": json.dumps(verification, ensure_ascii=False, indent=2) if verification else "not available",
            "Branch": branch or "なし",
            "PR": pr or "なし",
            "Blockers": blockers or ["なし"],
            "Artifacts": artifacts or ["なし"],
            "Audit Trail": audit_trail or ["なし"],
        }

    def _flatten_test_plan(self, test_plan: dict[str, Any]) -> list[str]:
        items: list[str] = []
        for key in ("unit", "integration", "manual_checks"):
            value = test_plan.get(key, [])
            if isinstance(value, list):
                items.extend(f"{key}: {entry}" for entry in value)
        return items

    def _append_run_log(self, run_log_path: Path, line: str) -> None:
        run_log_path.parent.mkdir(parents=True, exist_ok=True)
        with run_log_path.open("a", encoding="utf-8") as fh:
            fh.write(line.rstrip() + "\n")

    def _write_repair_feedback_jsonl(
        self,
        *,
        issue_key: str,
        attempt_id: str,
        run_id: str,
        candidate_id: str,
        repair_feedback: dict[str, Any],
    ) -> None:
        candidate_path = (
            self.state_store.candidate_artifacts_dir(issue_key, attempt_id, candidate_id) / "repair_feedback.jsonl"
        )
        execution_path = (
            self.state_store.execution_artifacts_dir(issue_key, run_id)
            / "candidates"
            / candidate_id
            / "repair_feedback.jsonl"
        )
        records = repair_feedback.get("issues", [])
        if not isinstance(records, list) or not records:
            records = [{"applicable": False, "issue_count": 0}]
        for record in records:
            payload = {
                "candidate_id": candidate_id,
                "phase": "fast-repair",
                **(record if isinstance(record, dict) else {"message": str(record)}),
            }
            self._write_jsonl(candidate_path, payload)
            self._write_jsonl(execution_path, payload)

    def _write_jsonl(self, path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def _record_telemetry_event(
        self,
        *,
        issue_key: str,
        run_id: str,
        event: str,
        status: str,
        workflow: dict[str, Any] | None = None,
        candidate_id: str | None = None,
        role: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        duration_ms: int | None = None,
        tokens_in: int | None = None,
        tokens_out: int | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        if not self._telemetry_enabled(workflow):
            return
        sink = JsonlTelemetrySink(
            self.state_store.execution_artifacts_dir(issue_key, run_id) / "telemetry" / "events.jsonl"
        )
        sink.write_event(
            event=event,
            issue_key=issue_key,
            run_id=run_id,
            status=status,
            candidate_id=candidate_id,
            role=role,
            provider=provider,
            model=model,
            duration_ms=duration_ms,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            extra=extra,
        )

    def _telemetry_enabled(self, workflow: dict[str, Any] | None) -> bool:
        if workflow is None:
            return True
        config = workflow.get("config") if isinstance(workflow, dict) else None
        telemetry = getattr(config, "telemetry", None)
        if telemetry is None:
            return True
        return str(getattr(telemetry, "sink", "jsonl")).strip() == "jsonl"
