from __future__ import annotations

import asyncio
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import discord

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
    thread_id: int
    run_id: str
    repo_full_name: str
    issue: dict[str, Any]


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
        self.workspace_manager = WorkspaceManager(settings)
        self.codex_runner = CodexRunner(settings.codex_bin)
        self.claude_runner = ClaudeRunner(settings.anthropic_api_key)

    async def abort(self, thread_id: int) -> bool:
        stopped = await asyncio.to_thread(self.process_registry.terminate, thread_id)
        self.state_store.update_status(thread_id, "aborted")
        return stopped

    async def execute_run(
        self,
        *,
        client: discord.Client,
        thread_id: int,
        repo_full_name: str,
        issue: dict[str, Any],
    ) -> None:
        channel = client.get_channel(thread_id)
        if not isinstance(channel, discord.Thread):
            raise RuntimeError(f"Discord thread not found for thread_id={thread_id}")

        summary = self.state_store.load_artifact(thread_id, "requirement_summary.json")
        plan = self.state_store.load_artifact(thread_id, "plan.json")
        test_plan = self.state_store.load_artifact(thread_id, "test_plan.json")
        if not isinstance(summary, dict) or not isinstance(plan, dict) or not isinstance(test_plan, dict):
            raise RuntimeError("Missing planning artifacts before run.")

        run_id = self.state_store.create_execution_run(thread_id)
        execution = ExecutionContext(thread_id=thread_id, run_id=run_id, repo_full_name=repo_full_name, issue=issue)
        self.state_store.update_status(thread_id, "running")
        self.state_store.record_activity(
            thread_id,
            phase="run_start",
            summary="run を開始しました",
            status="running",
            run_id=run_id,
            details={"repo": repo_full_name, "issue_number": issue.get("number")},
        )
        await channel.send("run を開始しました。workspace を準備します。")

        workspace_info = await asyncio.to_thread(
            self.workspace_manager.prepare,
            repo_full_name,
            int(issue["number"]),
            thread_id,
            str(self.state_store.execution_run_dir(thread_id, run_id)),
        )
        self.state_store.record_activity(
            thread_id,
            phase="workspace",
            summary="workspace の準備が完了しました",
            status="running",
            run_id=run_id,
            details={"workspace": workspace_info["workspace"], "branch": workspace_info["branch_name"]},
        )
        self.state_store.write_artifact(thread_id, "workspace.json", workspace_info)
        self.state_store.update_meta(
            thread_id,
            workspace=workspace_info["workspace"],
            branch_name=workspace_info["branch_name"],
            base_branch=workspace_info["base_branch"],
        )

        workflow = load_workflow(workspace=workspace_info["workspace"])
        if not workflow:
            workflow = load_workflow(repo_root=".")
        self.state_store.write_execution_artifact(thread_id, "workflow.json", workflow, run_id)
        self.state_store.write_execution_artifact(thread_id, "requirement_summary.json", summary, run_id)
        self.state_store.write_execution_artifact(thread_id, "plan.json", plan, run_id)
        self.state_store.write_execution_artifact(thread_id, "test_plan.json", test_plan, run_id)
        self.state_store.write_execution_artifact(thread_id, "issue.json", issue, run_id)

        artifacts_dir = self.state_store.execution_artifacts_dir(thread_id, run_id)
        codex_log_path = artifacts_dir / "codex_run.log"
        prompt = self.codex_runner.build_prompt(
            issue=issue,
            requirement_summary=summary,
            plan=plan,
            test_plan=test_plan,
            workflow_text=workflow_text(workspace=workspace_info["workspace"]) or workflow_text(repo_root="."),
        )
        process = await asyncio.to_thread(
            self.codex_runner.start,
            workspace=workspace_info["workspace"],
            stdout_path=str(codex_log_path),
        )
        self.process_registry.register(thread_id, run_id, process.pid, "codex")
        self.state_store.record_activity(
            thread_id,
            phase="codex_start",
            summary="Codex 実装を開始しました",
            status="running",
            run_id=run_id,
            details={"pid": process.pid, "log_path": str(codex_log_path)},
        )
        assert process.stdin is not None
        process.stdin.write(prompt)
        process.stdin.close()
        returncode = await asyncio.to_thread(process.wait)
        self.process_registry.unregister(thread_id)
        self.state_store.record_activity(
            thread_id,
            phase="codex_finish",
            summary="Codex 実装が終了しました",
            status="running" if returncode == 0 else "failed",
            run_id=run_id,
            details={"returncode": returncode},
        )

        changed_files = {"changed_files": self._detect_changed_files(workspace_info["workspace"])}
        self.state_store.write_execution_artifact(thread_id, "changed_files.json", changed_files, run_id)
        self.state_store.write_artifact(thread_id, "changed_files.json", changed_files)

        if returncode != 0:
            self.state_store.update_status(thread_id, "failed")
            self.state_store.record_activity(
                thread_id,
                phase="run_failed",
                summary="Codex 実装で失敗しました",
                status="failed",
                run_id=run_id,
                details={"returncode": returncode},
            )
            await channel.send(f"Codex 実装で失敗しました。終了コード: `{returncode}`")
            self.state_store.write_execution_artifact(
                thread_id,
                "final_result.json",
                {"success": False, "failure_type": "codex_failure", "returncode": returncode},
                run_id,
            )
            return

        await channel.send("Codex 実装が完了しました。検証を開始します。")
        self.state_store.record_activity(
            thread_id,
            phase="workflow_commands",
            summary="workflow command の実行を開始しました",
            status="running",
            run_id=run_id,
        )
        command_results = await self.execute_workflow_commands(
            client=client,
            channel=channel,
            workspace=workspace_info["workspace"],
            workflow=workflow,
            thread_id=thread_id,
            run_id=run_id,
        )
        self.state_store.write_execution_artifact(thread_id, "command_results.json", command_results, run_id)
        self.state_store.write_artifact(thread_id, "command_results.json", command_results)
        if command_results.get("failure_type") == "approval_denied":
            self.state_store.record_activity(
                thread_id,
                phase="run_failed",
                summary="高リスク操作が拒否されました",
                status="failed",
                run_id=run_id,
                details={"failure_type": "approval_denied"},
            )
            self.state_store.write_execution_artifact(
                thread_id,
                "final_result.json",
                {"success": False, "failure_type": "approval_denied"},
                run_id,
            )
            await channel.send("高リスク操作が拒否されたため run を停止しました。")
            return

        self.state_store.update_status(thread_id, "verifying")
        self.state_store.record_activity(
            thread_id,
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
        self.state_store.write_execution_artifact(thread_id, "verification_summary.json", verification, run_id)
        self.state_store.write_artifact(thread_id, "verification_summary.json", verification)
        if verification.get("status") not in {"success", "passed", "completed"}:
            self.state_store.update_status(thread_id, "failed")
            self.state_store.record_activity(
                thread_id,
                phase="run_failed",
                summary="verification が失敗しました",
                status="failed",
                run_id=run_id,
                details={"failure_type": verification.get("failure_type", "verification_failed")},
            )
            await channel.send("verification が失敗しました。`/status` と `/why-failed` を確認してください。")
            self.state_store.write_execution_artifact(
                thread_id,
                "final_result.json",
                {"success": False, "failure_type": verification.get("failure_type", "verification_failed")},
                run_id,
            )
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
        self.state_store.write_execution_artifact(thread_id, "review_summary.json", review, run_id)
        self.state_store.write_artifact(thread_id, "review_summary.json", review)
        if review.get("decision") == "reject":
            self.state_store.update_status(thread_id, "failed")
            self.state_store.record_activity(
                thread_id,
                phase="run_failed",
                summary="review が reject を返しました",
                status="failed",
                run_id=run_id,
                details={"failure_type": "review_reject"},
            )
            await channel.send("review が reject を返したため PR 作成を中止しました。")
            self.state_store.write_execution_artifact(
                thread_id,
                "final_result.json",
                {"success": False, "failure_type": "review_reject"},
                run_id,
            )
            return

        proof = evaluate_proof_of_work(
            workflow,
            {
                "plan.json",
                "test_plan.json",
                "changed_files.json",
                "command_results.json",
                "verification_summary.json",
                "review_summary.json",
            },
        )
        if not proof.complete:
            self.state_store.update_status(thread_id, "failed")
            self.state_store.record_activity(
                thread_id,
                phase="run_failed",
                summary="proof-of-work artifact が不足しています",
                status="failed",
                run_id=run_id,
                details={"missing_artifacts": proof.missing_artifacts},
            )
            self.state_store.write_execution_artifact(
                thread_id,
                "final_result.json",
                {"success": False, "failure_type": "missing_artifacts", "missing_artifacts": proof.missing_artifacts},
                run_id,
            )
            await channel.send("proof-of-work artifact が不足しているため完了できませんでした。")
            return

        pushed = await asyncio.to_thread(
            self._commit_and_push,
            workspace_info["workspace"],
            workspace_info["branch_name"],
            issue["number"],
        )
        if not pushed:
            self.state_store.update_status(thread_id, "failed")
            self.state_store.record_activity(
                thread_id,
                phase="run_failed",
                summary="変更差分が作られませんでした",
                status="failed",
                run_id=run_id,
            )
            await channel.send("変更差分が作られなかったため PR 作成を中止しました。")
            return

        pr_title = f"feat: {issue['title']}"
        pr_body = self._build_pr_body(issue, channel.jump_url, changed_files, command_results, verification, review)
        pr = await asyncio.to_thread(
            self.github_client.create_pull_request,
            repo_full_name=repo_full_name,
            title=pr_title,
            body=pr_body,
            head=workspace_info["branch_name"],
            base=workspace_info["base_branch"],
            draft=True,
        )
        self.state_store.write_artifact(thread_id, "pr.json", pr)
        self.state_store.write_execution_artifact(thread_id, "pr.json", pr, run_id)
        comment_body = self._build_pr_comment(channel.jump_url, verification, review, command_results)
        await asyncio.to_thread(
            self.github_client.create_issue_comment,
            repo_full_name,
            int(pr["number"]),
            comment_body,
        )
        final_result = {"success": True, "pr": pr, "review": review, "verification": verification}
        self.state_store.write_execution_artifact(thread_id, "final_result.json", final_result, run_id)
        self.state_store.write_artifact(thread_id, "final_result.json", final_result)
        self.state_store.update_meta(thread_id, status="completed", pr_number=str(pr["number"]), pr_url=pr["url"])
        self.state_store.record_activity(
            thread_id,
            phase="completed",
            summary="run が完了しました",
            status="completed",
            run_id=run_id,
            details={"pr_number": pr["number"], "pr_url": pr["url"]},
        )
        await channel.send(f"draft PR を作成しました。\n- PR: #{pr['number']}\n- URL: {pr['url']}")

    def _execute_repo_commands(self, workspace: str, workflow: dict[str, Any], thread_id: int, run_id: str) -> dict[str, Any]:
        commands = workflow.get("commands", {})
        results: list[dict[str, Any]] = []
        for phase in ("setup", "lint", "test"):
            phase_commands = commands.get(phase, []) if isinstance(commands, dict) else []
            if not isinstance(phase_commands, list):
                continue
            for command in phase_commands:
                if is_high_risk_command(command):
                    raise RuntimeError(f"High-risk command reached sync path unexpectedly: {command}")
                completed = subprocess.run(
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

    async def execute_workflow_commands(
        self,
        *,
        client: discord.Client,
        channel: discord.Thread,
        workspace: str,
        workflow: dict[str, Any],
        thread_id: int,
        run_id: str,
    ) -> dict[str, Any]:
        commands = workflow.get("commands", {})
        results: list[dict[str, Any]] = []
        for phase in ("setup", "lint", "test"):
            phase_commands = commands.get(phase, []) if isinstance(commands, dict) else []
            if not isinstance(phase_commands, list):
                continue
            for command in phase_commands:
                if is_high_risk_command(command):
                    request = self.approval_coordinator.create_request(
                        thread_id=thread_id,
                        run_id=run_id,
                        tool_name="Bash",
                        input_text=command,
                        reason=f"workflow command `{phase}` is marked high risk",
                    )
                    view = client.build_approval_view() if hasattr(client, "build_approval_view") else None
                    await channel.send(
                        "高リスク操作の承認が必要です。\n"
                        f"- phase: `{phase}`\n"
                        f"- command: `{command}`\n"
                        f"- reason: {request.reason}",
                        view=view,
                    )
                    approved = await self.approval_coordinator.wait_for_resolution(
                        thread_id,
                        timeout_seconds=self.settings.approval_timeout_seconds,
                    )
                    if not approved:
                        self.state_store.update_status(thread_id, "failed")
                        pending = self.state_store.load_artifact(thread_id, "pending_approval.json")
                        failure_type = "approval_timeout" if isinstance(pending, dict) and pending.get("status") == "expired" else "approval_denied"
                        return {
                            "results": results,
                            "failure_type": failure_type,
                            "stopped_before_command": command,
                        }
                    self.state_store.update_status(thread_id, "running")

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
        completed = subprocess.run(["git", "-C", workspace, "diff", "--stat", "--patch"], capture_output=True, text=True, check=True)
        return completed.stdout

    def _commit_and_push(self, workspace: str, branch_name: str, issue_number: int) -> bool:
        status = subprocess.run(["git", "-C", workspace, "status", "--porcelain"], capture_output=True, text=True, check=True)
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
