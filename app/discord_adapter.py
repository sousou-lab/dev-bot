from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from typing import Any

import discord
from discord import app_commands

from app.approvals import ApprovalCoordinator
from app.agent_sdk_client import (
    AgentContextOverloadError,
    AgentForbiddenToolError,
    AgentJsonResponseError,
    AgentOversizedReadError,
    AgentRateLimitError,
    AgentTimeoutError,
)
from app.config import Settings
from app.github_client import GitHubIssueClient
from app.issue_draft import build_issue_body, build_issue_title
from app.orchestrator import Orchestrator, WorkItem
from app.pipeline import DevelopmentPipeline
from app.planning_agent import PlanningAgent
from app.process_registry import ProcessRegistry
from app.repo_profiler import build_repo_profile
from app.requirements_agent import RequirementsAgent
from app.state_store import FileStateStore


DERIVED_ARTIFACTS = (
    "issue.json",
    "pr.json",
    "workspace.json",
    "plan.json",
    "test_plan.json",
    "repo_profile.json",
    "planning_workspace.json",
    "current_activity.json",
    "activity_history.json",
    "agent_failure.json",
    "last_failure.json",
    "agent_result.json",
    "verification_result.json",
    "verification_summary.json",
    "review_summary.json",
    "verification_history.json",
    "final_result.json",
    "changed_files.json",
    "command_results.json",
)

ALLOWED_ATTACHMENT_SUFFIXES = {".txt", ".md", ".json"}
MAX_ATTACHMENTS_PER_MESSAGE = 3
MAX_ATTACHMENT_BYTES = 2 * 1024 * 1024
MAX_DISCORD_MESSAGE_LENGTH = 2000


class DevBotClient(discord.Client):
    def __init__(self, settings: Settings, state_store: FileStateStore) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self.settings = settings
        self.state_store = state_store
        self.requirements_agent = RequirementsAgent(settings=settings)
        self.planning_agent = PlanningAgent(settings=settings)
        self.github_client = GitHubIssueClient(settings.github_token)
        self.process_registry = ProcessRegistry(settings.runs_root)
        self.approval_coordinator = ApprovalCoordinator(state_store)
        self.pipeline = DevelopmentPipeline(
            settings=settings,
            state_store=state_store,
            github_client=self.github_client,
            process_registry=self.process_registry,
            approval_coordinator=self.approval_coordinator,
        )
        self.orchestrator = Orchestrator(
            state_store=state_store,
            executor=lambda item: self.pipeline.execute_run(
                client=self,
                thread_id=item.thread_id,
                repo_full_name=item.repo_full_name,
                issue=item.issue,
            ),
            max_concurrency=settings.max_concurrent_runs,
        )
        self.tree = app_commands.CommandTree(self)

    def build_approval_view(self) -> discord.ui.View:
        return ApprovalView(self)

    async def setup_hook(self) -> None:
        for name, description, callback, needs_repo in (
            ("plan", "repo を読んで plan.json と test_plan.json を作成します", self.plan_command, True),
            ("run", "確認済み plan に基づいて Issue 作成と実装を開始します", self.run_command, True),
            ("confirm", "互換コマンドです。/plan と同じく計画を作成します", self.confirm_command, True),
        ):
            command = app_commands.Command(name=name, description=description, callback=callback)
            if needs_repo:
                command.autocomplete("repo")(self.repo_autocomplete)
            self.tree.add_command(command)

        for name, description, callback in (
            ("status", "現在の状態を表示します", self.status_command),
            ("issue", "作成済みIssueを表示します", self.issue_command),
            ("pr", "作成済みPRを表示します", self.pr_command),
            ("approve", "保留中の高リスク操作を承認します", self.approve_command),
            ("reject", "保留中の高リスク操作を拒否します", self.reject_command),
            ("abort", "このスレッドの実行中プロセスを停止します", self.abort_command),
            ("retry", "直前の plan / issue で再実行します", self.retry_command),
            ("revise", "要件整理を再開し、plan/run の派生成果物をクリアします", self.revise_command),
            ("diff", "現在の作業差分を表示します", self.diff_command),
            ("why-failed", "直近の失敗理由を要約します", self.why_failed_command),
            ("budget", "直近 run の usage / cost を表示します", self.budget_command),
        ):
            self.tree.add_command(app_commands.Command(name=name, description=description, callback=callback))

        if self.settings.discord_guild_id:
            guild = discord.Object(id=int(self.settings.discord_guild_id))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            return
        await self.tree.sync()

    async def on_ready(self) -> None:
        if self.user is not None:
            print(f"Logged in as {self.user} ({self.user.id})")
        self.add_view(self.build_approval_view())
        asyncio.create_task(self._warm_repo_autocomplete_cache())
        await self._restore_pending_runs()

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        if isinstance(message.channel, discord.Thread):
            await self._handle_thread_message(message)
            return
        await self._handle_requirements_channel_message(message)

    async def _handle_requirements_channel_message(self, message: discord.Message) -> None:
        if str(message.channel.id) != self.settings.requirements_channel_id:
            return
        if self.user is None or self.user not in message.mentions:
            return

        parsed = await self._parse_message_inputs_for_new_thread(message)
        if parsed["error"]:
            await message.reply(str(parsed["error"]))
            return

        thread = await message.create_thread(
            name=self._build_thread_name(message.content),
            auto_archive_duration=1440,
        )
        self.state_store.create_run(thread_id=thread.id, parent_message_id=message.id, channel_id=message.channel.id)
        user_payload = await self._materialize_message_payload(thread.id, message, parsed)
        self.state_store.append_message(thread.id, "user", user_payload)
        reply = await asyncio.to_thread(self.requirements_agent.build_reply, thread.id)
        await self._send_channel_text(thread, reply.body)
        self.state_store.append_message(thread.id, "assistant", reply.body)
        self.state_store.update_status(thread.id, reply.status)
        if reply.artifacts:
            self._persist_artifacts(thread.id, reply.artifacts)

    async def _handle_thread_message(self, message: discord.Message) -> None:
        thread_id = message.channel.id
        if not self.state_store.has_run(thread_id):
            return
        if self.orchestrator.is_running(thread_id):
            return
        self._reconcile_thread_runtime_state(thread_id)
        meta = self.state_store.load_meta(thread_id)
        if str(meta.get("status", "")) in {"planning", "queued", "running", "verifying", "awaiting_high_risk_approval"}:
            return
        parsed = await self._parse_message_inputs(message)
        if parsed["error"]:
            await self._send_channel_text(message.channel, str(parsed["error"]))
            return
        if meta.get("status") in {"planned", "queued", "running", "completed", "failed", "aborted"}:
            self._clear_execution_artifacts(thread_id)
        user_payload = await self._materialize_message_payload(thread_id, message, parsed)
        self.state_store.append_message(thread_id, "user", user_payload)
        reply = await asyncio.to_thread(self.requirements_agent.build_reply, thread_id)
        await self._send_channel_text(message.channel, reply.body)
        self.state_store.append_message(thread_id, "assistant", reply.body)
        self.state_store.update_status(thread_id, reply.status)
        if reply.artifacts:
            self._persist_artifacts(thread_id, reply.artifacts)

    def _persist_artifacts(self, thread_id: int, artifacts: dict[str, Any]) -> None:
        for key, filename in (
            ("summary", "requirement_summary.json"),
            ("plan", "plan.json"),
            ("test_plan", "test_plan.json"),
            ("repo_profile", "repo_profile.json"),
            ("planning_workspace", "planning_workspace.json"),
            ("agent_error", "agent_error.json"),
        ):
            payload = artifacts.get(key)
            if isinstance(payload, dict):
                self.state_store.write_artifact(thread_id, filename, payload)

    def _ensure_managed_thread(self, channel: discord.abc.GuildChannel | discord.Thread | None) -> int | None:
        if not isinstance(channel, discord.Thread):
            return None
        return channel.id if self.state_store.has_run(channel.id) else None

    def _build_thread_name(self, content: str) -> str:
        summary = content.replace("\n", " ").strip()
        if len(summary) > 40:
            summary = summary[:40].rstrip() + "..."
        return f"dev-bot | {summary or 'new request'}"

    async def _parse_message_inputs_for_new_thread(self, message: discord.Message) -> dict[str, Any]:
        parsed = await self._parse_message_inputs(message)
        if parsed["error"]:
            return parsed
        body = str(parsed["body"]).strip()
        if not body:
            parsed["error"] = (
                "本文か対応添付ファイルが必要です。`txt` `md` `json` を最大3件、各2MB以内で再送してください。"
            )
        return parsed

    async def _parse_message_inputs(self, message: discord.Message) -> dict[str, Any]:
        attachments = list(message.attachments)
        if len(attachments) > MAX_ATTACHMENTS_PER_MESSAGE:
            return {
                "error": f"添付は最大{MAX_ATTACHMENTS_PER_MESSAGE}件までです。必要なファイルだけ再送してください。",
                "body": "",
                "attachments": [],
            }

        parsed_attachments: list[dict[str, str]] = []
        for attachment in attachments:
            suffix = Path(attachment.filename).suffix.lower()
            if suffix not in ALLOWED_ATTACHMENT_SUFFIXES:
                allowed = ", ".join(sorted(ALLOWED_ATTACHMENT_SUFFIXES))
                return {
                    "error": (
                        f"`{attachment.filename}` は非対応形式です。"
                        f" {allowed} のいずれかにして再送してください。"
                    ),
                    "body": "",
                    "attachments": [],
                }
            if attachment.size > MAX_ATTACHMENT_BYTES:
                return {
                    "error": (
                        f"`{attachment.filename}` はサイズ上限を超えています。"
                        " 2MB 以下にして再送してください。"
                    ),
                    "body": "",
                    "attachments": [],
                }
            raw = await attachment.read()
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                text = raw.decode("utf-8", errors="replace")
            parsed_attachments.append(
                {
                    "filename": attachment.filename,
                    "content": text,
                    "url": attachment.url,
                }
            )

        content = message.content.strip()
        body_parts: list[str] = []
        if content:
            body_parts.append(content)
        for item in parsed_attachments:
            body_parts.append(
                "\n".join(
                    [
                        f"[attachment:{item['filename']}]",
                        item["content"],
                        f"[/attachment:{item['filename']}]",
                    ]
                )
            )
        return {
            "error": "",
            "body": "\n\n".join(part for part in body_parts if part.strip()),
            "attachments": parsed_attachments,
        }

    async def _materialize_message_payload(self, thread_id: int, message: discord.Message, parsed: dict[str, Any]) -> str:
        attachments = parsed.get("attachments", [])
        materialized: list[dict[str, str]] = []
        for item in attachments:
            safe_name = self._safe_attachment_name(message.id, str(item["filename"]))
            saved_path = self.state_store.write_attachment_text(thread_id, safe_name, str(item["content"]))
            materialized.append(
                {
                    "filename": str(item["filename"]),
                    "saved_path": saved_path,
                    "url": str(item["url"]),
                }
            )
        payload = str(parsed.get("body", "")).strip()
        if materialized:
            payload += (
                "\n\n[attachment-metadata]\n"
                + json.dumps({"items": materialized}, ensure_ascii=False, indent=2)
            )
        return payload.strip()

    def _safe_attachment_name(self, message_id: int, filename: str) -> str:
        suffix = Path(filename).suffix
        stem = Path(filename).stem
        sanitized = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in stem)[:80] or "attachment"
        return f"{message_id}_{sanitized}{suffix}"

    async def _send_channel_text(self, channel: discord.abc.Messageable, content: str) -> None:
        for chunk in self._chunk_message(content):
            await channel.send(chunk)

    async def _send_interaction_text(self, interaction: discord.Interaction, content: str, *, ephemeral: bool = False) -> None:
        chunks = self._chunk_message(content)
        if interaction.response.is_done():
            for chunk in chunks:
                await interaction.followup.send(chunk, ephemeral=ephemeral)
            return
        await interaction.response.send_message(chunks[0], ephemeral=ephemeral)
        for chunk in chunks[1:]:
            await interaction.followup.send(chunk, ephemeral=ephemeral)

    async def _send_followup_text(self, interaction: discord.Interaction, content: str, *, ephemeral: bool = False) -> None:
        for chunk in self._chunk_message(content):
            try:
                await interaction.followup.send(chunk, ephemeral=ephemeral)
            except discord.HTTPException as exc:
                if getattr(exc, "code", None) == 50027:
                    if not ephemeral and interaction.channel is not None:
                        await interaction.channel.send(chunk)
                    return
                raise

    def _chunk_message(self, content: str) -> list[str]:
        if len(content) <= MAX_DISCORD_MESSAGE_LENGTH:
            return [content]
        chunks: list[str] = []
        remaining = content
        while len(remaining) > MAX_DISCORD_MESSAGE_LENGTH:
            split_at = remaining.rfind("\n", 0, MAX_DISCORD_MESSAGE_LENGTH)
            if split_at <= 0:
                split_at = MAX_DISCORD_MESSAGE_LENGTH
            chunks.append(remaining[:split_at].rstrip())
            remaining = remaining[split_at:].lstrip("\n")
        if remaining:
            chunks.append(remaining)
        return chunks or [""]

    async def plan_command(self, interaction: discord.Interaction, repo: str) -> None:
        await self._generate_plan(interaction, repo, alias_used=False)

    async def confirm_command(self, interaction: discord.Interaction, repo: str) -> None:
        await self._generate_plan(interaction, repo, alias_used=True)

    async def run_command(self, interaction: discord.Interaction, repo: str | None = None) -> None:
        await self._start_run(interaction, repo)

    async def status_command(self, interaction: discord.Interaction) -> None:
        thread_id = self._ensure_managed_thread(interaction.channel)
        if thread_id is None:
            await interaction.response.send_message("このコマンドは管理対象スレッド内で実行してください。", ephemeral=True)
            return
        self._reconcile_thread_runtime_state(thread_id)
        meta = self.state_store.load_meta(thread_id)
        issue = self.state_store.load_artifact(thread_id, "issue.json")
        pr = self.state_store.load_artifact(thread_id, "pr.json")
        summary = self.state_store.load_artifact(thread_id, "requirement_summary.json")
        plan = self.state_store.load_artifact(thread_id, "plan.json")
        test_plan = self.state_store.load_artifact(thread_id, "test_plan.json")
        verification = self.state_store.load_artifact(thread_id, "verification_summary.json")
        review = self.state_store.load_artifact(thread_id, "review_summary.json")
        pending_approval = self.state_store.load_artifact(thread_id, "pending_approval.json")
        planning_progress = self.state_store.load_artifact(thread_id, "planning_progress.json")
        current_activity = self.state_store.load_artifact(thread_id, "current_activity.json")
        process = self.process_registry.load(thread_id)
        runtime_active = self.orchestrator.is_running(thread_id) or self.orchestrator.is_queued(thread_id) or bool(process)
        lines = [
            f"status: `{meta.get('status', 'unknown')}`",
            f"thread_id: `{thread_id}`",
            f"running: `{runtime_active}`",
            f"attempts: `{meta.get('attempt_count', 0)}`",
        ]
        if meta.get("github_repo"):
            lines.append(f"repo: `{meta.get('github_repo')}`")
        if summary:
            lines.append(f"goal: {summary.get('goal', '(no goal)')}")
        if plan:
            lines.append(f"plan: `{len(plan.get('implementation_steps', []))}` steps")
        if test_plan:
            lines.append(f"test_plan: `{len(test_plan.get('cases', []))}` cases")
        if isinstance(planning_progress, dict) and planning_progress:
            current = int(planning_progress.get("current", 0))
            total = int(planning_progress.get("total", 0))
            phase = str(planning_progress.get("phase", ""))
            if total > 0:
                lines.append(f"planning_progress: `{phase} {current}/{total}`")
            elif phase:
                lines.append(f"planning_progress: `{phase}`")
        if isinstance(current_activity, dict) and current_activity:
            phase = str(current_activity.get("phase", "")).strip()
            summary_text = str(current_activity.get("summary", "")).strip()
            status_text = str(current_activity.get("status", "")).strip()
            timestamp = str(current_activity.get("timestamp", "")).strip()
            if phase or summary_text:
                lines.append(f"activity: `{phase or 'unknown'}` `{status_text or 'unknown'}`")
            if summary_text:
                lines.append(f"activity_summary: {summary_text}")
            if timestamp:
                lines.append(f"activity_updated_at: `{timestamp}`")
        if issue:
            lines.append(f"issue: [#{issue.get('number')}]({issue.get('url')})")
        if pr:
            lines.append(f"pr: [#{pr.get('number')}]({pr.get('url')})")
        if verification:
            lines.append(f"verification: `{verification.get('status', 'unknown')}`")
        if review:
            lines.append(f"review: `{review.get('decision', 'unknown')}`")
        if isinstance(pending_approval, dict) and pending_approval.get("status") == "pending":
            lines.append(
                "pending_approval: "
                f"`{pending_approval.get('tool_name', 'unknown')}` "
                f"- {pending_approval.get('input_text', '')}"
            )
        if process:
            lines.append(f"process: pid=`{process.get('pid')}` pgid=`{process.get('pgid')}`")
        await self._send_interaction_text(interaction, "\n".join(lines), ephemeral=True)

    async def issue_command(self, interaction: discord.Interaction) -> None:
        thread_id = self._ensure_managed_thread(interaction.channel)
        if thread_id is None:
            await interaction.response.send_message("このコマンドは管理対象スレッド内で実行してください。", ephemeral=True)
            return
        issue = self.state_store.load_artifact(thread_id, "issue.json")
        if not issue:
            await interaction.response.send_message("まだ Issue は作成されていません。", ephemeral=True)
            return
        await interaction.response.send_message(
            f"Repo: `{issue.get('repo_full_name')}`\nIssue: #{issue.get('number')}\nURL: {issue.get('url')}",
            ephemeral=True,
        )

    async def pr_command(self, interaction: discord.Interaction) -> None:
        thread_id = self._ensure_managed_thread(interaction.channel)
        if thread_id is None:
            await interaction.response.send_message("このコマンドは管理対象スレッド内で実行してください。", ephemeral=True)
            return
        pr = self.state_store.load_artifact(thread_id, "pr.json")
        if not pr:
            await interaction.response.send_message("まだ PR は作成されていません。", ephemeral=True)
            return
        await interaction.response.send_message(
            f"Repo: `{pr.get('repo_full_name')}`\nPR: #{pr.get('number')}\nURL: {pr.get('url')}",
            ephemeral=True,
        )

    async def abort_command(self, interaction: discord.Interaction) -> None:
        thread_id = self._ensure_managed_thread(interaction.channel)
        if thread_id is None:
            await interaction.response.send_message("このコマンドは管理対象スレッド内で実行してください。", ephemeral=True)
            return
        stopped = await self.pipeline.abort(thread_id)
        if stopped:
            await interaction.response.send_message("実行中プロセスの停止を要求しました。", ephemeral=True)
            return
        await interaction.response.send_message("停止対象は見つかりませんでした。状態だけ `aborted` に更新しました。", ephemeral=True)

    async def approve_command(self, interaction: discord.Interaction) -> None:
        await self._resolve_approval(interaction, approved=True)

    async def reject_command(self, interaction: discord.Interaction) -> None:
        await self._resolve_approval(interaction, approved=False)

    async def retry_command(self, interaction: discord.Interaction) -> None:
        thread_id = self._ensure_managed_thread(interaction.channel)
        if thread_id is None:
            await interaction.response.send_message("このコマンドは管理対象スレッド内で実行してください。", ephemeral=True)
            return
        verification = self.state_store.load_artifact(thread_id, "verification_summary.json")
        failure_type = str(verification.get("failure_type", "")) if isinstance(verification, dict) else ""
        if failure_type and failure_type not in {"test_failure", "command_failure", "transient_tool_error"}:
            await interaction.response.send_message(
                f"この失敗分類 `{failure_type}` は自動 retry 対象外です。",
                ephemeral=True,
            )
            return
        issue = self.state_store.load_artifact(thread_id, "issue.json")
        meta = self.state_store.load_meta(thread_id)
        repo = issue.get("repo_full_name") if isinstance(issue, dict) else meta.get("github_repo")
        await self._start_run(interaction, str(repo) if repo else None)

    async def revise_command(self, interaction: discord.Interaction) -> None:
        thread_id = self._ensure_managed_thread(interaction.channel)
        if thread_id is None:
            await interaction.response.send_message("このコマンドは管理対象スレッド内で実行してください。", ephemeral=True)
            return
        if self.orchestrator.is_running(thread_id):
            await interaction.response.send_message("実行中です。先に `/abort` してください。", ephemeral=True)
            return
        self._clear_execution_artifacts(thread_id)
        self.state_store.update_meta(
            thread_id,
            status="requirements_dialogue",
            issue_number="",
            pr_number="",
            pr_url="",
            workspace="",
            branch_name="",
            base_branch="",
        )
        await interaction.response.send_message("要件整理を再開しました。修正内容を投稿してください。", ephemeral=True)

    async def diff_command(self, interaction: discord.Interaction, pathspec: str | None = None) -> None:
        thread_id = self._ensure_managed_thread(interaction.channel)
        if thread_id is None:
            await interaction.response.send_message("このコマンドは管理対象スレッド内で実行してください。", ephemeral=True)
            return
        meta = self.state_store.load_meta(thread_id)
        workspace = str(meta.get("workspace", "")).strip()
        if not workspace or not Path(workspace).exists():
            await interaction.response.send_message("workspace が見つかりません。", ephemeral=True)
            return
        try:
            diff_text = await asyncio.to_thread(self._build_diff_summary, workspace, pathspec or "")
        except subprocess.CalledProcessError as exc:
            await interaction.response.send_message(f"diff の取得に失敗しました: `{exc}`", ephemeral=True)
            return
        await self._send_interaction_text(interaction, diff_text, ephemeral=True)

    async def why_failed_command(self, interaction: discord.Interaction) -> None:
        thread_id = self._ensure_managed_thread(interaction.channel)
        if thread_id is None:
            await interaction.response.send_message("このコマンドは管理対象スレッド内で実行してください。", ephemeral=True)
            return
        last_failure = self.state_store.load_artifact(thread_id, "last_failure.json")
        verification = self.state_store.load_artifact(thread_id, "verification_summary.json")
        final_result = self.state_store.load_artifact(thread_id, "final_result.json")
        lines = ["直近の失敗要約"]
        if isinstance(last_failure, dict) and last_failure:
            if last_failure.get("stage"):
                lines.append(f"- stage: `{last_failure.get('stage')}`")
            if last_failure.get("message"):
                lines.append(f"- message: {last_failure.get('message')}")
            details = last_failure.get("details")
            if isinstance(details, dict):
                repo = str(details.get("repo", "")).strip()
                if repo:
                    lines.append(f"- repo: `{repo}`")
                planning_progress = details.get("planning_progress")
                if isinstance(planning_progress, dict) and planning_progress:
                    phase = str(planning_progress.get("phase", "")).strip()
                    current = int(planning_progress.get("current", 0))
                    total = int(planning_progress.get("total", 0))
                    acceptance = str(planning_progress.get("acceptance_criterion", "")).strip()
                    last_session_id = str(planning_progress.get("last_session_id", "")).strip()
                    if total > 0:
                        lines.append(f"- planning_progress: `{phase} {current}/{total}`")
                    elif phase:
                        lines.append(f"- planning_progress: `{phase}`")
                    if acceptance:
                        lines.append(f"- acceptance_criterion: {acceptance}")
                    if last_session_id:
                        lines.append(f"- planning_session: `{last_session_id}`")
            stderr_lines = last_failure.get("stderr")
            if isinstance(stderr_lines, list):
                for item in stderr_lines[-3:]:
                    snippet = str(item).strip()
                    if snippet:
                        lines.append(f"- stderr: {snippet[:400]}")
        if isinstance(verification, dict) and verification:
            lines.append(f"- status: `{verification.get('status', 'unknown')}`")
            lines.append(f"- failure_type: `{verification.get('failure_type', 'unknown')}`")
            for note in verification.get("notes", [])[:5]:
                lines.append(f"- note: {note}")
        if isinstance(final_result, dict) and final_result and not final_result.get("success", True):
            lines.append(f"- final_failure_type: `{final_result.get('failure_type', 'unknown')}`")
        if len(lines) == 1:
            lines.append("- 直近の失敗情報は見つかりませんでした。")
        await self._send_interaction_text(interaction, "\n".join(lines), ephemeral=True)

    async def budget_command(self, interaction: discord.Interaction) -> None:
        thread_id = self._ensure_managed_thread(interaction.channel)
        if thread_id is None:
            await interaction.response.send_message("このコマンドは管理対象スレッド内で実行してください。", ephemeral=True)
            return
        final_result = self.state_store.load_artifact(thread_id, "final_result.json")
        verification = self.state_store.load_artifact(thread_id, "verification_summary.json")
        lines = [
            f"attempts: `{self.state_store.load_meta(thread_id).get('attempt_count', 0)}`",
            f"verification_status: `{verification.get('status', 'unknown') if isinstance(verification, dict) else 'unknown'}`",
        ]
        if isinstance(final_result, dict) and final_result:
            lines.append(f"success: `{final_result.get('success', False)}`")
        await self._send_interaction_text(interaction, "\n".join(lines), ephemeral=True)

    async def repo_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        del interaction
        cached = self.github_client.suggest_cached_repositories(current, limit=25)
        if cached:
            return [app_commands.Choice(name=repo, value=repo) for repo in cached]
        try:
            repos = await asyncio.wait_for(
                asyncio.to_thread(self.github_client.suggest_repositories, current, 25),
                timeout=1.5,
            )
        except Exception:
            return []
        return [app_commands.Choice(name=repo, value=repo) for repo in repos]

    async def _warm_repo_autocomplete_cache(self) -> None:
        try:
            await asyncio.to_thread(self.github_client.warm_repository_cache)
        except Exception:
            return

    async def _generate_plan(self, interaction: discord.Interaction, repo: str, *, alias_used: bool) -> None:
        thread_id = self._ensure_managed_thread(interaction.channel)
        if thread_id is None:
            await interaction.response.send_message("このコマンドは管理対象スレッド内で実行してください。", ephemeral=True)
            return
        if self.orchestrator.is_running(thread_id):
            await interaction.response.send_message("実行中です。先に `/abort` してください。", ephemeral=True)
            return
        summary = self.state_store.load_artifact(thread_id, "requirement_summary.json")
        if not isinstance(summary, dict) or not summary:
            await interaction.response.send_message("要件サマリーがまだ作成されていません。", ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        self.state_store.update_status(thread_id, "planning")
        self.state_store.write_artifact(thread_id, "planning_progress.json", {"status": "planning", "phase": "plan"})
        try:
            artifacts = await asyncio.to_thread(self._build_plan_artifacts, repo, thread_id, summary)
        except Exception as exc:
            details = {"repo": repo}
            stderr: list[str] | None = None
            planning_progress = self.state_store.load_artifact(thread_id, "planning_progress.json")
            if isinstance(planning_progress, dict) and planning_progress:
                details["planning_progress"] = planning_progress
            if isinstance(exc, AgentForbiddenToolError):
                details.update(
                    {
                        "prompt_kind": exc.prompt_kind or "unknown",
                        "session_id": exc.session_id or "",
                        "forbidden_tool": exc.tool_name,
                        "forbidden_reason": exc.reason,
                    }
                )
                stderr = exc.stderr
            elif isinstance(exc, AgentOversizedReadError):
                details.update(
                    {
                        "prompt_kind": exc.prompt_kind or "unknown",
                        "session_id": exc.session_id or "",
                        "failure_type": "oversized_file_read",
                        "observed_tokens": exc.observed_tokens,
                        "max_tokens": exc.max_tokens,
                    }
                )
                stderr = exc.stderr
            elif isinstance(exc, AgentContextOverloadError):
                details.update(
                    {
                        "prompt_kind": exc.prompt_kind or "unknown",
                        "session_id": exc.session_id or "",
                        "failure_type": "context_overload",
                        "peak_tokens": exc.peak_tokens,
                        "read_count": exc.read_count,
                    }
                )
                stderr = exc.stderr
            elif isinstance(exc, AgentRateLimitError):
                details.update(
                    {
                        "prompt_kind": exc.prompt_kind or "unknown",
                        "session_id": exc.session_id or "",
                        "failure_type": "rate_limited",
                        "request_id": exc.request_id,
                    }
                )
                stderr = exc.stderr
            elif isinstance(exc, AgentTimeoutError):
                details.update(
                    {
                        "prompt_kind": exc.prompt_kind or "unknown",
                        "session_id": exc.session_id or "",
                        "failure_type": "timeout",
                    }
                )
                stderr = exc.stderr
            elif isinstance(exc, AgentJsonResponseError):
                details.update(
                    {
                        "prompt_kind": exc.prompt_kind or "unknown",
                        "session_id": exc.session_id or "",
                        "raw_response": exc.raw_response,
                    }
                )
                stderr = exc.stderr
            self.state_store.record_failure(
                thread_id,
                stage="plan_generation",
                message=str(exc),
                details=details,
                stderr=stderr,
            )
            self.state_store.update_status(thread_id, "failed")
            await self._send_followup_text(
                interaction,
                f"plan の生成に失敗しました: `{exc}`\n詳細は `/why-failed` を確認してください。",
                ephemeral=True,
            )
            return

        self._clear_execution_artifacts(thread_id)
        self._persist_artifacts(
            thread_id,
            {
                "plan": artifacts["plan"],
                "test_plan": artifacts["test_plan"],
                "repo_profile": artifacts["repo_profile"],
                "planning_workspace": artifacts["planning_workspace"],
                "planning_sessions": artifacts["planning_sessions"],
            },
        )
        self.state_store.update_meta(
            thread_id,
            status="planned",
            github_repo=repo,
            base_branch=str(artifacts["planning_workspace"].get("base_branch", "")),
        )
        self.state_store.write_artifact(thread_id, "planning_progress.json", {"status": "completed", "phase": "done"})
        prefix = "互換コマンド `/confirm` を `/plan` として扱いました。\n\n" if alias_used else ""
        await self._send_followup_text(
            interaction,
            prefix + self._format_plan_message(repo, artifacts["plan"], artifacts["test_plan"]),
        )

    async def _start_run(self, interaction: discord.Interaction, repo: str | None) -> None:
        thread_id = self._ensure_managed_thread(interaction.channel)
        if thread_id is None:
            await interaction.response.send_message("このコマンドは管理対象スレッド内で実行してください。", ephemeral=True)
            return
        if self.orchestrator.is_running(thread_id):
            await interaction.response.send_message("すでに実行中です。", ephemeral=True)
            return

        summary = self.state_store.load_artifact(thread_id, "requirement_summary.json")
        plan = self.state_store.load_artifact(thread_id, "plan.json")
        test_plan = self.state_store.load_artifact(thread_id, "test_plan.json")
        if not isinstance(summary, dict) or not isinstance(plan, dict) or not isinstance(test_plan, dict) or not plan or not test_plan:
            await interaction.response.send_message("先に `/plan repo:owner/repo` を実行してください。", ephemeral=True)
            return

        meta = self.state_store.load_meta(thread_id)
        issue = self.state_store.load_artifact(thread_id, "issue.json")
        repo_full_name = repo or (issue.get("repo_full_name") if isinstance(issue, dict) else "") or str(meta.get("github_repo", ""))
        if not repo_full_name:
            await interaction.response.send_message("repo を決められませんでした。`/run repo:owner/repo` を指定してください。", ephemeral=True)
            return

        await interaction.response.defer(thinking=True)
        if not isinstance(issue, dict) or not issue:
            thread_url = interaction.channel.jump_url if isinstance(interaction.channel, discord.Thread) else ""
            title = build_issue_title(summary)
            body = build_issue_body(summary, thread_url)
            try:
                created = await asyncio.to_thread(
                    self.github_client.create_issue,
                    repo_full_name=repo_full_name,
                    title=title,
                    body=body,
                )
            except Exception as exc:
                await self._send_followup_text(interaction, f"Issue 作成に失敗しました: `{exc}`", ephemeral=True)
                return
            issue = {
                "repo_full_name": created.repo_full_name,
                "number": created.number,
                "title": created.title,
                "body": created.body,
                "url": created.url,
            }
            self.state_store.write_artifact(thread_id, "issue.json", issue)

        self.state_store.update_meta(thread_id, github_repo=repo_full_name, issue_number=str(issue["number"]))
        started = await self.orchestrator.enqueue(
            WorkItem(thread_id=thread_id, repo_full_name=repo_full_name, issue=issue)
        )
        if not started:
            await self._send_followup_text(interaction, "パイプラインの起動に失敗しました。", ephemeral=True)
            return
        await self._send_followup_text(
            interaction,
            "run を queue に登録しました。\n"
            f"- Repo: `{repo_full_name}`\n"
            f"- Issue: #{issue['number']}\n"
            f"- URL: {issue['url']}",
        )
        if isinstance(interaction.channel, discord.Thread):
            await self._maybe_post_pending_approval(interaction.channel)

    def _build_plan_artifacts(self, repo: str, thread_id: int, summary: dict[str, Any]) -> dict[str, Any]:
        planning_workspace = self.pipeline.workspace_manager.prepare_plan_workspace(repo, thread_id)
        repo_profile = build_repo_profile(planning_workspace["workspace"])
        progress_state: dict[str, Any] = {"session_ids": []}

        def report_progress(payload: dict[str, Any]) -> None:
            normalized_payload = _json_safe_value(payload)
            if not isinstance(normalized_payload, dict):
                normalized_payload = {}
            progress_state.update(normalized_payload)
            session_id = str(normalized_payload.get("session_id", "")).strip()
            if session_id:
                session_ids = progress_state.setdefault("session_ids", [])
                if session_id not in session_ids:
                    session_ids.append(session_id)
                progress_state["last_session_id"] = session_id
            self.state_store.write_artifact(thread_id, "planning_progress.json", progress_state)
            status = str(payload.get("status", "")).strip()
            if status:
                self.state_store.update_status(thread_id, status)

        built = self.planning_agent.build_artifacts(
            workspace=planning_workspace["workspace"],
            summary=summary,
            repo_profile=repo_profile,
            progress_callback=report_progress,
        )
        return {
            "repo_profile": built.repo_profile,
            "plan": built.plan,
            "test_plan": built.test_plan,
            "planning_workspace": planning_workspace,
            "planning_sessions": progress_state,
        }

    def _reconcile_thread_runtime_state(self, thread_id: int) -> None:
        meta = self.state_store.load_meta(thread_id)
        status = str(meta.get("status", "")).strip()
        if status not in {"queued", "running", "verifying", "awaiting_high_risk_approval"}:
            return
        has_process = bool(self.process_registry.load(thread_id))
        is_active = self.orchestrator.is_running(thread_id) or self.orchestrator.is_queued(thread_id) or has_process
        if status == "awaiting_high_risk_approval":
            pending = self.state_store.load_artifact(thread_id, "pending_approval.json")
            if isinstance(pending, dict) and pending.get("status") == "pending":
                return
        if is_active:
            return
        self.state_store.update_status(thread_id, "failed")
        self.state_store.record_activity(
            thread_id,
            phase="reconcile",
            summary="実行状態と status の不整合を検出し failed に補正しました",
            status="failed",
            run_id=str(meta.get("current_run_id", "")),
        )

    def _format_plan_message(self, repo: str, plan: dict[str, Any], test_plan: dict[str, Any]) -> str:
        scope = "\n".join(f"- {item}" for item in plan.get("scope", [])[:6]) or "- なし"
        steps = "\n".join(f"- {item}" for item in plan.get("implementation_steps", [])[:6]) or "- なし"
        risks = "\n".join(f"- {item}" for item in plan.get("risks", [])[:4]) or "- なし"
        test_cases = "\n".join(
            f"- {case.get('id', 'TC')} {case.get('name', '')} [{case.get('category', '')}/{case.get('priority', '')}]"
            for case in test_plan.get("cases", [])[:6]
            if isinstance(case, dict)
        ) or "- なし"
        return (
            "plan.json / test_plan.json を生成しました。\n"
            f"- Repo: `{repo}`\n"
            f"- Goal: {plan.get('goal', '(no goal)')}\n\n"
            "Scope\n"
            f"{scope}\n\n"
            "Implementation steps\n"
            f"{steps}\n\n"
            "Test cases\n"
            f"{test_cases}\n\n"
            "Risks\n"
            f"{risks}\n\n"
            "問題なければ `/run` を実行してください。"
        )

    def _clear_execution_artifacts(self, thread_id: int) -> None:
        for filename in DERIVED_ARTIFACTS:
            self.state_store.delete_artifact(thread_id, filename)
        self.state_store.update_meta(
            thread_id,
            issue_number="",
            pr_number="",
            pr_url="",
            workspace="",
            branch_name="",
            base_branch="",
        )

    def _build_diff_summary(self, workspace: str, pathspec: str) -> str:
        status = subprocess.run(["git", "-C", workspace, "status", "--short"], check=True, capture_output=True, text=True)
        if not status.stdout.strip():
            return "作業差分はありません。"
        diff_stat_cmd = ["git", "-C", workspace, "diff", "--stat"]
        diff_name_cmd = ["git", "-C", workspace, "diff", "--name-only"]
        if pathspec:
            diff_stat_cmd.extend(["--", pathspec])
            diff_name_cmd.extend(["--", pathspec])
        diff_stat = subprocess.run(diff_stat_cmd, check=True, capture_output=True, text=True)
        diff_names = subprocess.run(diff_name_cmd, check=True, capture_output=True, text=True)
        names = diff_names.stdout.strip().splitlines()
        return (
            "現在の差分\n"
            f"- files: {len(names)}\n"
            f"- names:\n{chr(10).join(f'  - {line}' for line in names[:20]) or '  - none'}\n\n"
            f"```text\n{diff_stat.stdout.strip()[:1500]}\n```"
        )

    async def _resolve_approval(self, interaction: discord.Interaction, approved: bool) -> None:
        thread_id = self._ensure_managed_thread(interaction.channel)
        if thread_id is None:
            await interaction.response.send_message("このコマンドは管理対象スレッド内で実行してください。", ephemeral=True)
            return
        if not self.approval_coordinator.has_pending_request(thread_id):
            await interaction.response.send_message("承認待ちの操作はありません。", ephemeral=True)
            return
        actor = str(interaction.user) if interaction.user else "unknown"
        resolution = self.approval_coordinator.resolve(thread_id, approved=approved, actor=actor)
        if resolution == "stale_future":
            await interaction.response.send_message("承認待ちの解決に失敗しました。", ephemeral=True)
            return
        if approved:
            meta = self.state_store.load_meta(thread_id)
            issue = self.state_store.load_artifact(thread_id, "issue.json")
            repo_full_name = str(meta.get("github_repo", ""))
            if resolution != "resolved" and isinstance(issue, dict) and issue and repo_full_name:
                self.state_store.update_status(thread_id, "queued")
                await self.orchestrator.enqueue(
                    WorkItem(thread_id=thread_id, repo_full_name=repo_full_name, issue=issue)
                )
                await interaction.response.send_message("高リスク操作を承認しました。run を再キューしました。", ephemeral=True)
                return
            self.state_store.update_status(thread_id, "running")
            await interaction.response.send_message("高リスク操作を承認しました。run を再開します。", ephemeral=True)
            return
        self.state_store.update_status(thread_id, "failed")
        await interaction.response.send_message("高リスク操作を拒否しました。run を停止します。", ephemeral=True)

    async def _maybe_post_pending_approval(self, thread: discord.Thread) -> None:
        payload = self.state_store.load_artifact(thread.id, "pending_approval.json")
        if not isinstance(payload, dict) or payload.get("status") != "pending":
            return
        await thread.send(
            "高リスク操作の承認待ちです。\n"
            f"- tool: `{payload.get('tool_name', 'unknown')}`\n"
            f"- input: `{payload.get('input_text', '')}`\n"
            f"- reason: {payload.get('reason', '')}",
            view=self.build_approval_view(),
        )

    async def _restore_pending_runs(self) -> None:
        metas = self.state_store.list_runs_by_status({"queued", "running", "awaiting_high_risk_approval"})
        items: list[WorkItem] = []
        for meta in metas:
            thread_id = int(meta["thread_id"])
            issue = self.state_store.load_artifact(thread_id, "issue.json")
            repo_full_name = str(meta.get("github_repo", ""))
            if not isinstance(issue, dict) or not issue or not repo_full_name:
                continue
            if meta.get("status") == "awaiting_high_risk_approval":
                channel = self.get_channel(thread_id)
                if isinstance(channel, discord.Thread):
                    await self._maybe_post_pending_approval(channel)
                continue
            if meta.get("status") == "running":
                if self.process_registry.load(thread_id):
                    await asyncio.to_thread(self.process_registry.terminate, thread_id)
                    self.process_registry.unregister(thread_id)
                self.state_store.update_status(thread_id, "queued")
            items.append(WorkItem(thread_id=thread_id, repo_full_name=repo_full_name, issue=issue))
        if items:
            await self.orchestrator.restore(items)


class ApprovalView(discord.ui.View):
    def __init__(self, client: DevBotClient) -> None:
        super().__init__(timeout=None)
        self.client = client

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.success, custom_id="devbot:approve")
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        del button
        await self.client._resolve_approval(interaction, approved=True)

    @discord.ui.button(label="Reject", style=discord.ButtonStyle.danger, custom_id="devbot:reject")
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        del button
        await self.client._resolve_approval(interaction, approved=False)


def _json_safe_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe_value(item) for item in value]
    if isinstance(value, tuple):
        return [_json_safe_value(item) for item in value]
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def build_client(settings: Settings) -> DevBotClient:
    return DevBotClient(settings=settings, state_store=FileStateStore(runs_root=settings.runs_root))
