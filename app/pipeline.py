from __future__ import annotations

import asyncio
import json
import subprocess
from dataclasses import asdict, dataclass, is_dataclass
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any, Protocol

from app.approvals import ApprovalCoordinator, is_high_risk_command
from app.config import Settings
from app.debug.bundle_builder import IncidentBundleBuilder
from app.github_client import GitHubIssueClient
from app.process_registry import ProcessRegistry
from app.proof_of_work import evaluate_proof_of_work
from app.review.github_poster import GitHubReviewPoster
from app.runners.claude_runner import ClaudeRunner
from app.runners.codex_runner import CodexRunner
from app.state_store import FileStateStore
from app.telemetry.jsonl import JsonlTelemetrySink
from app.verification_profiles import workflow_verification_from_plan
from app.workflow_loader import load_workflow, workflow_text
from app.workspace_manager import WorkspaceManager


@dataclass(frozen=True)
class ExecutionContext:
    issue_key: str
    thread_id: int
    run_id: str
    repo_full_name: str
    issue: dict[str, Any]


class ChatChannel(Protocol):
    async def send(self, content: str) -> None: ...


class ChatClient(Protocol):
    def get_channel(self, channel_id: int) -> ChatChannel | None: ...


class DevelopmentPipeline:
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
        if not isinstance(summary, dict) or not isinstance(plan, dict) or not isinstance(test_plan, dict):
            raise RuntimeError("Missing planning artifacts before run.")
        if not isinstance(verification_plan, dict):
            verification_plan = {}

        issue_key = f"{repo_full_name}#{int(issue['number'])}"
        run_id = self.state_store.create_execution_run(issue_key)
        _execution = ExecutionContext(
            issue_key=issue_key,
            thread_id=thread_id,
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

        workflow = load_workflow(workspace=workspace_info["workspace"])
        if not workflow:
            workflow = load_workflow(repo_root=".")
        workflow = self._resolve_workflow(workflow, verification_plan)
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
        self.state_store.write_execution_artifact(issue_key, "issue.json", issue, run_id)
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

        codex_result = await self._run_blocking(
            self.codex_runner.run,
            workspace=workspace_info["workspace"],
            run_dir=str(self.state_store.execution_run_dir(issue_key, run_id)),
            issue=issue,
            requirement_summary=summary,
            plan=plan,
            test_plan=test_plan,
            workflow_text=workflow_text(workspace=workspace_info["workspace"]) or workflow_text(repo_root="."),
            on_process_start=lambda pid: self.process_registry.register(issue_key, run_id, pid, "codex"),
            on_process_exit=lambda: self.process_registry.unregister(issue_key),
        )
        codex_log_path = Path(codex_result.stdout_path)
        self.state_store.write_execution_artifact(
            issue_key,
            "runner_metadata.json",
            {
                "runner": "codex",
                "mode": codex_result.mode,
                "workspace_key": workspace_info.get("workspace_key", ""),
                "workspace": workspace_info["workspace"],
                "branch_name": workspace_info["branch_name"],
            },
            run_id,
        )
        self.state_store.record_activity(
            issue_key,
            phase="codex_start",
            summary="Codex 実装を開始しました",
            status="running",
            run_id=run_id,
            details={"log_path": str(codex_log_path), "mode": codex_result.mode},
        )
        self._append_run_log(run_log_path, "codex start")
        returncode = codex_result.returncode
        self.state_store.record_activity(
            issue_key,
            phase="codex_finish",
            summary="Codex 実装が終了しました",
            status="running" if returncode == 0 else "failed",
            run_id=run_id,
            details={"returncode": returncode, "mode": codex_result.mode},
        )
        self._record_telemetry_event(
            workflow=workflow,
            issue_key=issue_key,
            run_id=run_id,
            event="implementation_finished",
            status="ok" if returncode == 0 else "failed",
            provider="codex-app-server",
            model=self.settings.codex_model,
            extra={"returncode": returncode, "mode": codex_result.mode},
        )
        self._append_run_log(run_log_path, f"codex finish rc={returncode}")
        self._sync_runner_generated_artifacts(issue_key=issue_key, run_id=run_id, artifacts_dir=codex_log_path.parent)

        changed_files = {"changed_files": self._detect_changed_files(workspace_info["workspace"])}
        self.state_store.write_execution_artifact(issue_key, "changed_files.json", changed_files, run_id)
        self.state_store.write_artifact(issue_key, "changed_files.json", changed_files)

        if returncode != 0:
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
                state="Rework",
                failure_type="codex_failure",
                latest_attempt=f"codex failed rc={returncode}",
                branch=workspace_info["branch_name"],
                blockers=[],
                artifacts=["changed_files.json", "final_summary.json", "run.log"],
                verification={},
                extra={"returncode": returncode},
            )
            await channel.send(f"Codex 実装で失敗しました。終了コード: `{returncode}`")
            return

        await channel.send("Codex 実装が完了しました。検証を開始します。")
        self.state_store.record_activity(
            issue_key,
            phase="workflow_commands",
            summary="workflow command の実行を開始しました",
            status="running",
            run_id=run_id,
        )
        command_results = await self.execute_workflow_commands(
            client=chat_client,
            channel=channel,
            workspace=workspace_info["workspace"],
            workflow=workflow,
            issue_key=issue_key,
            run_id=run_id,
        )
        self.state_store.write_execution_artifact(issue_key, "command_results.json", command_results, run_id)
        self.state_store.write_artifact(issue_key, "command_results.json", command_results)
        if command_results.get("failure_type"):
            failure_type = str(command_results["failure_type"])
            state = "Human Review" if failure_type == "policy_violation" else "Blocked"
            await self._finalize_failure(
                issue_key=issue_key,
                thread_id=thread_id,
                run_id=run_id,
                repo_full_name=repo_full_name,
                issue=issue_snapshot,
                summary=summary,
                plan=plan,
                test_plan=test_plan,
                state=state,
                failure_type=failure_type,
                latest_attempt="workflow command blocked",
                branch=workspace_info["branch_name"],
                blockers=[f"blocked command: {command_results.get('stopped_before_command', '')}"],
                artifacts=["command_results.json", "final_summary.json", "run.log"],
                verification={},
                extra={"stopped_before_command": command_results.get("stopped_before_command", "")},
            )
            await channel.send("禁止または高リスクの workflow command を検出したため run を停止しました。")
            return

        self.state_store.update_meta(issue_key, runtime_status="verifying")
        self.state_store.record_activity(
            issue_key,
            phase="verification",
            summary="verification を開始しました",
            status="verifying",
            run_id=run_id,
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
        verification_json = self._build_verification_json(command_results, verification, test_plan)
        self.state_store.write_execution_artifact(issue_key, "verification_summary.json", verification, run_id)
        self.state_store.write_artifact(issue_key, "verification_summary.json", verification)
        self.state_store.write_execution_artifact(issue_key, "verification.json", verification_json, run_id)
        self.state_store.write_artifact(issue_key, "verification.json", verification_json)
        self._record_telemetry_event(
            workflow=workflow,
            issue_key=issue_key,
            run_id=run_id,
            event="verification_finished",
            status=str(verification.get("status", "unknown")),
            provider="claude-agent-sdk",
            extra={
                "hard_checks_pass": not self._has_failed_hard_checks(command_results),
                "failure_type": verification.get("failure_type", ""),
            },
        )
        if self._has_failed_hard_checks(command_results):
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
                failure_type="hard_check_failed",
                latest_attempt="verification hard checks failed",
                branch=workspace_info["branch_name"],
                blockers=[],
                artifacts=["verification.json", "final_summary.json", "run.log"],
                verification=verification_json,
                extra={},
            )
            await channel.send(
                "verification の hard check が失敗しました。`/status` と `/why-failed` を確認してください。"
            )
            return
        if verification.get("status") not in {"success", "passed", "completed"}:
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
                failure_type=str(verification.get("failure_type", "verification_failed")),
                latest_attempt="verification failed",
                branch=workspace_info["branch_name"],
                blockers=[],
                artifacts=["verification.json", "final_summary.json", "run.log"],
                verification=verification_json,
                extra={},
            )
            await channel.send("verification が失敗しました。`/status` と `/why-failed` を確認してください。")
            return

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
        self.state_store.write_execution_artifact(issue_key, "review_summary.json", review, run_id)
        self.state_store.write_artifact(issue_key, "review_summary.json", review)
        self.state_store.write_execution_artifact(issue_key, "review_findings.json", review, run_id)
        self.state_store.write_artifact(issue_key, "review_findings.json", review)
        self._record_telemetry_event(
            workflow=workflow,
            issue_key=issue_key,
            run_id=run_id,
            event="review_finished",
            status=str(review.get("decision", "unknown")),
            provider="claude-agent-sdk",
        )
        if review.get("decision") == "reject":
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
                state="Rework",
                failure_type="review_reject",
                latest_attempt="review rejected",
                branch=workspace_info["branch_name"],
                blockers=[],
                artifacts=["verification.json", "review_summary.json", "final_summary.json", "run.log"],
                verification=verification_json,
                extra={},
            )
            await channel.send("review が reject を返したため PR 作成を中止しました。")
            return

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
                "review_findings.json",
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
        poster = GitHubReviewPoster(self.github_client)
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
            return self._json_safe(asdict(value))
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
        return {"results": results}

    def _workflow_commands(self, workflow: dict[str, Any]) -> list[dict[str, Any]]:
        verification = workflow.get("verification", {})
        if isinstance(verification, dict):
            checks = verification.get("required_checks", [])
            advisory_checks = verification.get("advisory_checks", [])
            if isinstance(checks, list) or isinstance(advisory_checks, list):
                normalized_checks = self._normalize_required_checks(
                    checks if isinstance(checks, list) else [],
                    category="hard",
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
                    "category": "hard",
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
            if isinstance(item, str) and item.strip():
                # String-only checks are accepted for backwards compatibility but do not
                # provide an executable command. Keep runtime behavior explicit.
                continue
        return normalized

    def _resolve_workflow(self, workflow: dict[str, Any], verification_plan: dict[str, Any]) -> dict[str, Any]:
        resolved = dict(workflow)
        verification = resolved.get("verification", {})
        if isinstance(verification, dict) and verification.get("required_checks"):
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
