from __future__ import annotations

import asyncio
import json
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any, Protocol

from app.approvals import ApprovalCoordinator, is_high_risk_command
from app.config import Settings
from app.github_client import GitHubIssueClient
from app.process_registry import ProcessRegistry
from app.proof_of_work import evaluate_proof_of_work
from app.runners.claude_runner import ClaudeRunner
from app.runners.codex_runner import CodexRunner
from app.state_store import FileStateStore
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
        if not isinstance(summary, dict) or not isinstance(plan, dict) or not isinstance(test_plan, dict):
            raise RuntimeError("Missing planning artifacts before run.")

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

        issue_snapshot = await asyncio.to_thread(self._load_issue_snapshot, repo_full_name, issue)
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
        await asyncio.to_thread(
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
                artifacts=["issue_snapshot.json", "plan.json", "test_plan.json", "run.log"],
                audit_trail=[f"{datetime.now(UTC).isoformat()} run started"],
            ),
            workpad_updates_path,
        )
        await channel.send("run を開始しました。workspace を準備します。")

        workspace_info = await asyncio.to_thread(
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

        workflow = load_workflow(workspace=workspace_info["workspace"])
        if not workflow:
            workflow = load_workflow(repo_root=".")
        self.state_store.write_execution_artifact(issue_key, "workflow.json", workflow, run_id)
        self.state_store.write_execution_artifact(issue_key, "requirement_summary.json", summary, run_id)
        self.state_store.write_execution_artifact(issue_key, "plan.json", plan, run_id)
        self.state_store.write_execution_artifact(issue_key, "test_plan.json", test_plan, run_id)
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

        codex_result = await asyncio.to_thread(
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
        self._append_run_log(run_log_path, f"codex finish rc={returncode}")

        changed_files = {"changed_files": self._detect_changed_files(workspace_info["workspace"])}
        self.state_store.write_execution_artifact(issue_key, "changed_files.json", changed_files, run_id)
        self.state_store.write_artifact(issue_key, "changed_files.json", changed_files)

        if returncode != 0:
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
        verification = await asyncio.to_thread(
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

        git_diff = await asyncio.to_thread(self._capture_git_diff, workspace_info["workspace"])
        review = await asyncio.to_thread(
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
        if review.get("decision") == "reject":
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
        proof = evaluate_proof_of_work(
            workflow,
            {
                "issue_snapshot.json",
                "requirement_summary.json",
                "plan.json",
                "test_plan.json",
                "changed_files.json",
                "verification.json",
                "final_summary.json",
                "run.log",
                "workpad_updates.jsonl",
                "runner_metadata.json",
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

        pushed = await asyncio.to_thread(
            self._commit_and_push,
            workspace_info["workspace"],
            workspace_info["branch_name"],
            int(issue["number"]),
        )
        if not pushed:
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
        pr = await asyncio.to_thread(
            self.github_client.create_pull_request,
            repo_full_name=repo_full_name,
            title=pr_title,
            body=pr_body,
            head=workspace_info["branch_name"],
            base=workspace_info["base_branch"],
            draft=True,
        )
        try:
            pr_status = await asyncio.to_thread(
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
        await asyncio.to_thread(
            self.github_client.ready_pull_request_for_review,
            repo_full_name,
            int(pr["number"]),
        )
        pr["draft"] = False
        self.state_store.write_artifact(issue_key, "pr.json", pr)
        self.state_store.write_execution_artifact(issue_key, "pr.json", pr, run_id)
        comment_body = self._build_pr_comment(channel_url, verification, review, command_results)
        await asyncio.to_thread(
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
        await asyncio.to_thread(
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
        await asyncio.to_thread(
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
        commands = workflow.get("commands", {})
        results: list[dict[str, Any]] = []
        for phase in ("setup", "lint", "test"):
            phase_commands = commands.get(phase, []) if isinstance(commands, dict) else []
            if not isinstance(phase_commands, list):
                continue
            for command in phase_commands:
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
                completed = await asyncio.to_thread(
                    subprocess.run,
                    command,
                    cwd=workspace,
                    shell=True,
                    text=True,
                    capture_output=True,
                )
                results.append(
                    {
                        "phase": phase,
                        "command": command,
                        "returncode": completed.returncode,
                        "output": (completed.stdout + completed.stderr)[-4000:],
                        "success": completed.returncode == 0,
                    }
                )
        return {"results": results}

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
        self.workspace_manager.push_branch(workspace, branch_name)
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
                f"- `{item.get('phase')}` `{item.get('command')}` => {'passed' if item.get('success') else 'failed'}"
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
                    f"  - {item.get('phase')}: `{item.get('command')}` => {'passed' if item.get('success') else 'failed'}"
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
        for item in results:
            phase = str(item.get("phase", ""))
            status = "pass" if item.get("success") else "fail"
            if phase == "lint":
                phase_status["lint"] = status
            elif phase == "test":
                phase_status["unit"] = status
        manual_checks = []
        if isinstance(test_plan, dict):
            for name in test_plan.get("manual_checks", []):
                manual_checks.append({"name": str(name), "status": "not_run"})
        return {
            **phase_status,
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
