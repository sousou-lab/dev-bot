from __future__ import annotations

import asyncio
import subprocess
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any

from app.logging_setup import get_logger

logger = get_logger(__name__)

try:
    import discord
    from discord import app_commands

    DISCORD_AVAILABLE = True
except ModuleNotFoundError:  # pragma: no cover - depends on local test env
    DISCORD_AVAILABLE = False

    class _StubClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            del args, kwargs

    class _StubThread:
        id = 0
        jump_url = ""

        async def send(self, *args: Any, **kwargs: Any) -> None:
            del args, kwargs

    class _StubIntents:
        message_content = False

        @classmethod
        def default(cls) -> _StubIntents:
            return cls()

    class _StubResponse:
        def is_done(self) -> bool:
            return False

        async def send_message(self, *args: Any, **kwargs: Any) -> None:
            del args, kwargs

        async def defer(self, *args: Any, **kwargs: Any) -> None:
            del args, kwargs

    class _StubFollowup:
        async def send(self, *args: Any, **kwargs: Any) -> None:
            del args, kwargs

    class _StubInteraction:
        response = _StubResponse()
        followup = _StubFollowup()
        channel = None
        user = None

    class _StubView:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            del args, kwargs

    class _StubCommand:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            del args, kwargs

        def autocomplete(self, *args: Any, **kwargs: Any):
            del args, kwargs

            def _decorator(func: Any) -> Any:
                return func

            return _decorator

    class _StubCommandTree:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            del args, kwargs

        def add_command(self, *args: Any, **kwargs: Any) -> None:
            del args, kwargs

        def copy_global_to(self, *args: Any, **kwargs: Any) -> None:
            del args, kwargs

        async def sync(self, *args: Any, **kwargs: Any) -> None:
            del args, kwargs

    class _StubChoice:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            del args, kwargs

        @classmethod
        def __class_getitem__(cls, item: Any) -> type[_StubChoice]:
            del item
            return cls

    class _StubAppCommands:
        CommandTree = _StubCommandTree
        Command = _StubCommand
        Choice = _StubChoice

    class _StubButtonStyle:
        success = 1
        danger = 2

    class _StubHTTPException(Exception):
        code: int | None = None

    class _StubDiscordModule:
        Client = _StubClient
        Thread = _StubThread
        Interaction = _StubInteraction
        Message = object
        Object = object
        HTTPException = _StubHTTPException
        Intents = _StubIntents
        ButtonStyle = _StubButtonStyle

        class abc:
            Messageable = object
            GuildChannel = object

        class ui:
            View = _StubView
            Button = object

            @staticmethod
            def button(*args: Any, **kwargs: Any):
                del args, kwargs

                def _decorator(func: Any) -> Any:
                    return func

                return _decorator

    discord = _StubDiscordModule()
    app_commands = _StubAppCommands()

from app.agent_sdk_client import (
    AgentBufferOverflowError,
    AgentContextOverloadError,
    AgentForbiddenToolError,
    AgentJsonResponseError,
    AgentOversizedReadError,
    AgentRateLimitError,
    AgentTimeoutError,
)
from app.approvals import ApprovalCoordinator
from app.chat_inputs import chunk_message, ensure_new_thread_body, materialize_message_payload, parse_message_inputs
from app.config import Settings
from app.discord_presenters import (
    format_budget_message,
    format_plan_message,
    format_status_message,
    format_why_failed_message,
)
from app.github_client import GitHubIssueClient
from app.issue_scheduler import IssueScheduler
from app.orchestrator import Orchestrator, WorkItem
from app.pipeline import DevelopmentPipeline
from app.planning_agent import PlanningAgent
from app.process_registry import ProcessRegistry
from app.repo_profiler import build_repo_profile
from app.requirements_agent import RequirementsAgent
from app.run_request import enqueue_issue_run, ensure_issue_for_thread
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

THREAD_LOCAL_STATUSES = {
    "collecting_requirements",
    "planning",
    "awaiting_approval",
    "changes_requested",
    "promotion_failed",
    "discarded",
    "failed",
    "requirements_dialogue",
    "ready_for_confirmation",
    "requirements_error",
}

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
        self.github_client = GitHubIssueClient(
            settings.github_token,
            app_id=getattr(settings, "github_app_id", ""),
            private_key_path=getattr(settings, "github_app_private_key_path", ""),
            installation_id=getattr(settings, "github_app_installation_id", ""),
            project_id=getattr(settings, "github_project_id", ""),
            project_state_field_id=getattr(settings, "github_project_state_field_id", ""),
            project_state_option_ids=getattr(settings, "github_project_state_option_ids", ""),
            project_plan_field_id=getattr(settings, "github_project_plan_field_id", ""),
            project_plan_option_ids=getattr(settings, "github_project_plan_option_ids", ""),
        )
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
                thread_id=item.thread_id or int(self.state_store.thread_id_for_issue(item.issue_key) or 0),
                repo_full_name=item.repo_full_name,
                issue=item.issue,
            ),
            max_concurrency=settings.max_concurrent_runs,
        )
        self.issue_scheduler = IssueScheduler(
            state_store=state_store,
            github_client=self.github_client,
            orchestrator=self.orchestrator,
            process_registry=self.process_registry,
            settings=settings,
            run_blocking=self._run_blocking,
            ensure_issue_thread_binding=self._ensure_issue_thread_binding,
            process_merging_issue=self._process_merging_issue,
            reconcile_runtime_state=lambda identifier, thread_id=0: self._reconcile_runtime_state(
                identifier, thread_id=thread_id
            ),
            restore_pending_approval=self._restore_pending_approval,
        )
        self.tree = app_commands.CommandTree(self)

    def build_approval_view(self) -> discord.ui.View:
        return ApprovalView(self)

    async def _run_blocking(self, func: Any, /, *args: Any, **kwargs: Any) -> Any:
        bound = partial(func, *args, **kwargs)
        return await asyncio.to_thread(bound)

    async def setup_hook(self) -> None:
        for name, description, callback, needs_repo in (
            ("plan", "repo を読んで plan.json と test_plan.json を作成します", self.plan_command, True),
            ("approve-plan", "計画を承認して Issue 化と実装開始を行います", self.approve_plan_command, False),
            ("reject-plan", "計画を却下して修正要求状態に戻します", self.reject_plan_command, False),
            ("confirm", "互換コマンドです。/plan と同じく計画を作成します", self.confirm_command, True),
        ):
            command = app_commands.Command(name=name, description=description, callback=callback)
            if needs_repo:
                command.autocomplete("repo")(self.repo_autocomplete)
            self.tree.add_command(command)

        for name, description, callback in (
            ("repos", "アクセス可能な repository 一覧を表示します", self.repos_command),
            ("status", "現在の状態を表示します", self.status_command),
            ("issue", "作成済みIssueを表示します", self.issue_command),
            ("pr", "作成済みPRを表示します", self.pr_command),
            ("approve", "保留中の高リスク操作を承認します", self.approve_command),
            ("reject", "保留中の高リスク操作を拒否します", self.reject_command),
            ("abort", "このスレッドの実行中プロセスを停止します", self.abort_command),
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
            logger.info("Logged in as %s (%s)", self.user, self.user.id)
        self.add_view(self.build_approval_view())
        asyncio.create_task(self._warm_repo_autocomplete_cache())
        await self._restore_pending_runs()
        self._ensure_scheduler_started()

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
        reply = await self._run_blocking(self.requirements_agent.build_reply, thread.id)
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
        meta = self._load_thread_ui_meta(thread_id)
        issue_key = self.state_store.issue_key_for_thread(thread_id)
        canonical_status = str(self.state_store.load_meta(self._runtime_key(thread_id)).get("status", "")).strip()
        if issue_key and canonical_status == "Ready":
            return
        runtime_status = str(meta.get("runtime_status", "")).strip()
        has_process = bool(self.process_registry.load(self._runtime_key(thread_id)))
        if (
            str(meta.get("status", "")) == "planning"
            or runtime_status
            in {
                "queued",
                "running",
                "verifying",
                "awaiting_high_risk_approval",
            }
            or (str(meta.get("status", "")).strip() == "In Progress" and has_process)
        ):
            return
        parsed = await self._parse_message_inputs(message)
        if parsed["error"]:
            await self._send_channel_text(message.channel, str(parsed["error"]))
            return
        if meta.get("status") in {"awaiting_approval", "Blocked", "Cancelled", "Done"}:
            self._clear_execution_artifacts(thread_id)
        user_payload = await self._materialize_message_payload(thread_id, message, parsed)
        self.state_store.append_message(thread_id, "user", user_payload)
        reply = await self._run_blocking(self.requirements_agent.build_reply, thread_id)
        await self._send_channel_text(message.channel, reply.body)
        self.state_store.append_message(thread_id, "assistant", reply.body)
        if issue_key and reply.status in {"requirements_dialogue", "ready_for_confirmation", "requirements_error"}:
            self.state_store.update_draft_meta(thread_id, status=reply.status)
        else:
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

    @staticmethod
    def _artifact_dict(payload: object) -> dict[str, Any]:
        return payload if isinstance(payload, dict) else {}

    def _ensure_managed_thread(self, channel: discord.abc.GuildChannel | discord.Thread | None) -> int | None:
        if not isinstance(channel, discord.Thread):
            return None
        thread_id = getattr(channel, "id", None)
        if not isinstance(thread_id, int):
            return None
        return thread_id if self.state_store.has_run(thread_id) else None

    def _runtime_key(self, thread_id: int) -> str | int:
        return self.state_store.issue_key_for_thread(thread_id) or thread_id

    def _load_thread_ui_meta(self, thread_id: int) -> dict[str, Any]:
        runtime_key = self._runtime_key(thread_id)
        meta = self.state_store.load_meta(runtime_key)
        issue_key = self.state_store.issue_key_for_thread(thread_id)
        if not issue_key:
            return meta
        draft_meta = self.state_store.load_draft_meta(thread_id)
        draft_status = str(draft_meta.get("status", "")).strip()
        if draft_status == "planning":
            return meta
        if draft_status not in THREAD_LOCAL_STATUSES:
            return meta
        merged = dict(meta)
        merged["status"] = draft_status
        return merged

    def _build_thread_name(self, content: str) -> str:
        summary = content.replace("\n", " ").strip()
        if len(summary) > 40:
            summary = summary[:40].rstrip() + "..."
        return f"dev-bot | {summary or 'new request'}"

    async def _parse_message_inputs_for_new_thread(self, message: discord.Message) -> dict[str, Any]:
        parsed = await self._parse_message_inputs(message)
        if parsed["error"]:
            return parsed
        return ensure_new_thread_body(parsed)

    async def _parse_message_inputs(self, message: discord.Message) -> dict[str, Any]:
        return await parse_message_inputs(message)

    async def _materialize_message_payload(
        self, thread_id: int, message: discord.Message, parsed: dict[str, Any]
    ) -> str:
        return materialize_message_payload(
            thread_id=thread_id,
            message_id=message.id,
            parsed=parsed,
            state_store=self.state_store,
        )

    async def _send_channel_text(self, channel: discord.abc.Messageable, content: str) -> None:
        for chunk in self._chunk_message(content):
            await channel.send(chunk)

    async def _send_interaction_text(
        self, interaction: discord.Interaction, content: str, *, ephemeral: bool = False
    ) -> None:
        chunks = self._chunk_message(content)
        if interaction.response.is_done():
            for chunk in chunks:
                await interaction.followup.send(chunk, ephemeral=ephemeral)
            return
        await interaction.response.send_message(chunks[0], ephemeral=ephemeral)
        for chunk in chunks[1:]:
            await interaction.followup.send(chunk, ephemeral=ephemeral)

    async def _send_followup_text(
        self, interaction: discord.Interaction, content: str, *, ephemeral: bool = False
    ) -> None:
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
        return chunk_message(content, max_length=MAX_DISCORD_MESSAGE_LENGTH)

    async def _update_bound_issue_state(self, thread_id: int, state: str) -> None:
        issue_key = self.state_store.issue_key_for_thread(thread_id)
        if not issue_key:
            return
        meta = self.state_store.load_issue_meta(issue_key)
        repo_full_name = str(meta.get("github_repo", "")).strip()
        issue_number = int(str(meta.get("issue_number", "0")).strip() or 0)
        if not repo_full_name or issue_number <= 0:
            return
        await self._run_blocking(self.github_client.update_issue_state, repo_full_name, issue_number, state)

    async def plan_command(self, interaction: discord.Interaction, repo: str) -> None:
        await self._generate_plan(interaction, repo, alias_used=False)

    async def confirm_command(self, interaction: discord.Interaction, repo: str) -> None:
        await self._generate_plan(interaction, repo, alias_used=True)

    async def approve_plan_command(self, interaction: discord.Interaction) -> None:
        await self._promote_approved_plan(interaction)

    async def reject_plan_command(self, interaction: discord.Interaction) -> None:
        await self._reject_plan(interaction)

    async def repos_command(self, interaction: discord.Interaction, query: str | None = None) -> None:
        await interaction.response.defer(thinking=True, ephemeral=True)
        try:
            repos = await self._run_blocking(self._list_repositories_for_display, query or "")
        except Exception as exc:
            await self._send_followup_text(interaction, f"repository 一覧の取得に失敗しました: `{exc}`", ephemeral=True)
            return
        if not repos:
            message = "表示できる repository はありません。"
            if query:
                message = f"`{query}` に一致する repository はありません。"
            await self._send_followup_text(interaction, message, ephemeral=True)
            return
        await self._send_followup_text(interaction, self._format_repo_list_message(repos, query or ""), ephemeral=True)

    def _list_repositories_for_display(self, query: str) -> list[str]:
        return self.github_client.suggest_repositories(query, limit=100)

    def _format_repo_list_message(self, repos: list[str], query: str) -> str:
        title = "アクセス可能な repository 一覧"
        if query:
            title += f" (`{query}`)"
        body = "\n".join(f"- `{repo}`" for repo in repos[:50])
        truncated = ""
        if len(repos) > 50:
            truncated = f"\n\n他 {len(repos) - 50} 件"
        return f"{title}\n\n{body}{truncated}"

    async def status_command(self, interaction: discord.Interaction) -> None:
        thread_id = self._ensure_managed_thread(interaction.channel)
        if thread_id is None:
            await interaction.response.send_message(
                "このコマンドは管理対象スレッド内で実行してください。", ephemeral=True
            )
            return
        self._reconcile_thread_runtime_state(thread_id)
        runtime_key = self._runtime_key(thread_id)
        meta = self._load_thread_ui_meta(thread_id)
        issue = self._artifact_dict(self.state_store.load_artifact(runtime_key, "issue.json"))
        pr = self._artifact_dict(self.state_store.load_artifact(runtime_key, "pr.json"))
        summary = self._artifact_dict(self.state_store.load_artifact(thread_id, "requirement_summary.json"))
        plan = self._artifact_dict(self.state_store.load_artifact(thread_id, "plan.json"))
        test_plan = self._artifact_dict(self.state_store.load_artifact(thread_id, "test_plan.json"))
        verification = self._artifact_dict(self.state_store.load_artifact(runtime_key, "verification_summary.json"))
        review = self._artifact_dict(self.state_store.load_artifact(runtime_key, "review_summary.json"))
        pending_approval = self._artifact_dict(self.state_store.load_artifact(runtime_key, "pending_approval.json"))
        planning_progress = self._artifact_dict(self.state_store.load_artifact(thread_id, "planning_progress.json"))
        current_activity = self._artifact_dict(self.state_store.load_artifact(runtime_key, "current_activity.json"))
        process = self.process_registry.load(runtime_key)
        runtime_active = (
            self.orchestrator.is_running(thread_id) or self.orchestrator.is_queued(thread_id) or bool(process)
        )
        await self._send_interaction_text(
            interaction,
            format_status_message(
                thread_id=thread_id,
                meta=meta,
                issue=issue,
                pr=pr,
                summary=summary,
                plan=plan,
                test_plan=test_plan,
                verification=verification,
                review=review,
                pending_approval=pending_approval,
                planning_progress=planning_progress,
                current_activity=current_activity,
                process=process,
                runtime_active=runtime_active,
            ),
            ephemeral=True,
        )

    async def issue_command(self, interaction: discord.Interaction) -> None:
        thread_id = self._ensure_managed_thread(interaction.channel)
        if thread_id is None:
            await interaction.response.send_message(
                "このコマンドは管理対象スレッド内で実行してください。", ephemeral=True
            )
            return
        issue = self._artifact_dict(self.state_store.load_artifact(thread_id, "issue.json"))
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
            await interaction.response.send_message(
                "このコマンドは管理対象スレッド内で実行してください。", ephemeral=True
            )
            return
        pr = self._artifact_dict(self.state_store.load_artifact(self._runtime_key(thread_id), "pr.json"))
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
            await interaction.response.send_message(
                "このコマンドは管理対象スレッド内で実行してください。", ephemeral=True
            )
            return
        stopped = await self.pipeline.abort(thread_id)
        if stopped:
            await interaction.response.send_message("実行中プロセスの停止を要求しました。", ephemeral=True)
            return
        await interaction.response.send_message(
            "停止対象は見つかりませんでした。状態だけ `aborted` に更新しました。", ephemeral=True
        )

    async def approve_command(self, interaction: discord.Interaction) -> None:
        await self._resolve_approval(interaction, approved=True)

    async def reject_command(self, interaction: discord.Interaction) -> None:
        await self._resolve_approval(interaction, approved=False)

    async def revise_command(self, interaction: discord.Interaction) -> None:
        thread_id = self._ensure_managed_thread(interaction.channel)
        if thread_id is None:
            await interaction.response.send_message(
                "このコマンドは管理対象スレッド内で実行してください。", ephemeral=True
            )
            return
        if self.orchestrator.is_running(thread_id):
            await interaction.response.send_message("実行中です。先に `/abort` してください。", ephemeral=True)
            return
        self._clear_execution_artifacts(thread_id)
        runtime_key = self._runtime_key(thread_id)
        if runtime_key == thread_id:
            fields: dict[str, Any] = {
                "status": "requirements_dialogue",
                "pr_number": "",
                "pr_url": "",
                "workspace": "",
                "branch_name": "",
                "base_branch": "",
            }
            fields["issue_number"] = ""
        else:
            self.state_store.update_draft_meta(thread_id, status="requirements_dialogue")
            fields = {
                "status": "Backlog",
                "plan_state": "Changes Requested",
                "pr_number": "",
                "pr_url": "",
                "workspace": "",
                "branch_name": "",
                "base_branch": "",
            }
            issue_meta = self.state_store.load_issue_meta(runtime_key)
            repo_full_name = str(issue_meta.get("github_repo", "")).strip()
            issue_number = int(str(issue_meta.get("issue_number", "0")).strip() or 0)
            if repo_full_name and issue_number:
                try:
                    await self._run_blocking(
                        self.github_client.update_issue_plan,
                        repo_full_name,
                        issue_number,
                        "Changes Requested",
                    )
                    await self._run_blocking(
                        self.github_client.update_issue_state,
                        repo_full_name,
                        issue_number,
                        "Backlog",
                    )
                except Exception as exc:
                    logger.warning("revise: failed to sync GitHub state for %s: %s", runtime_key, exc)
        self.state_store.update_meta(runtime_key, **fields)
        await interaction.response.send_message("要件整理を再開しました。修正内容を投稿してください。", ephemeral=True)

    async def diff_command(self, interaction: discord.Interaction, pathspec: str | None = None) -> None:
        thread_id = self._ensure_managed_thread(interaction.channel)
        if thread_id is None:
            await interaction.response.send_message(
                "このコマンドは管理対象スレッド内で実行してください。", ephemeral=True
            )
            return
        meta = self.state_store.load_meta(self._runtime_key(thread_id))
        workspace = str(meta.get("workspace", "")).strip()
        if not workspace or not Path(workspace).exists():
            await interaction.response.send_message("workspace が見つかりません。", ephemeral=True)
            return
        try:
            diff_text = await self._run_blocking(self._build_diff_summary, workspace, pathspec or "")
        except subprocess.CalledProcessError as exc:
            await interaction.response.send_message(f"diff の取得に失敗しました: `{exc}`", ephemeral=True)
            return
        await self._send_interaction_text(interaction, diff_text, ephemeral=True)

    async def why_failed_command(self, interaction: discord.Interaction) -> None:
        thread_id = self._ensure_managed_thread(interaction.channel)
        if thread_id is None:
            await interaction.response.send_message(
                "このコマンドは管理対象スレッド内で実行してください。", ephemeral=True
            )
            return
        runtime_key = self._runtime_key(thread_id)
        last_failure = self._artifact_dict(self.state_store.load_artifact(runtime_key, "last_failure.json"))
        verification = self._artifact_dict(self.state_store.load_artifact(runtime_key, "verification_summary.json"))
        final_result = self._artifact_dict(self.state_store.load_artifact(runtime_key, "final_result.json"))
        await self._send_interaction_text(
            interaction,
            format_why_failed_message(
                last_failure=last_failure,
                verification=verification,
                final_result=final_result,
            ),
            ephemeral=True,
        )

    async def budget_command(self, interaction: discord.Interaction) -> None:
        thread_id = self._ensure_managed_thread(interaction.channel)
        if thread_id is None:
            await interaction.response.send_message(
                "このコマンドは管理対象スレッド内で実行してください。", ephemeral=True
            )
            return
        runtime_key = self._runtime_key(thread_id)
        final_result = self._artifact_dict(self.state_store.load_artifact(runtime_key, "final_result.json"))
        verification = self._artifact_dict(self.state_store.load_artifact(runtime_key, "verification_summary.json"))
        await self._send_interaction_text(
            interaction,
            format_budget_message(
                attempt_count=int(self.state_store.load_meta(runtime_key).get("attempt_count", 0)),
                verification=verification,
                final_result=final_result,
            ),
            ephemeral=True,
        )

    async def repo_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        del interaction
        cached = self.github_client.suggest_cached_repositories(current, limit=25)
        if cached:
            return [app_commands.Choice(name=repo, value=repo) for repo in cached]
        try:
            repos = await asyncio.wait_for(
                self._run_blocking(self.github_client.suggest_repositories, current, 25),
                timeout=1.5,
            )
        except Exception as exc:
            logger.warning("repo_autocomplete: GitHub repository lookup failed: %s", exc)
            fallback = self.github_client.fallback_repositories()
            return [app_commands.Choice(name=repo, value=repo) for repo in fallback[:25]]
        return [app_commands.Choice(name=repo, value=repo) for repo in repos]

    async def _warm_repo_autocomplete_cache(self) -> None:
        try:
            await self._run_blocking(self.github_client.warm_repository_cache)
        except Exception as exc:
            logger.warning("repo_autocomplete: cache warm failed: %s", exc)
            return

    def _ensure_scheduler_started(self) -> None:
        self._refresh_scheduler_dependencies()
        self.issue_scheduler.ensure_started()

    async def _scheduler_loop(self) -> None:
        self._refresh_scheduler_dependencies()
        await self.issue_scheduler._scheduler_loop()

    async def _scheduler_tick(self) -> None:
        self._refresh_scheduler_dependencies()
        await self.issue_scheduler.scheduler_tick()

    def _sync_project_board_state(self) -> list[dict[str, Any]]:
        self._refresh_scheduler_dependencies()
        return self.issue_scheduler.sync_project_board_state()

    async def _dispatch_issue_if_ready(
        self,
        *,
        thread_id: int,
        issue_key: str,
        repo_full_name: str,
        issue_number: int,
        expected_state: str,
    ) -> None:
        self._refresh_scheduler_dependencies()
        await self.issue_scheduler._dispatch_issue_if_ready(
            thread_id=thread_id,
            issue_key=issue_key,
            repo_full_name=repo_full_name,
            issue_number=issue_number,
            expected_state=expected_state,
        )

    async def _ensure_issue_thread_binding(self, issue_key: str) -> int:
        existing = str(self.state_store.thread_id_for_issue(issue_key)).strip()
        if existing:
            return int(existing)
        status_channel_id = str(getattr(self.settings, "discord_status_channel_id", "")).strip()
        if not status_channel_id:
            return 0
        channel = self.get_channel(int(status_channel_id))
        if channel is None or not hasattr(channel, "create_thread"):
            logger.warning("status channel is unavailable for issue mirror: %s", issue_key)
            return 0
        issue = self.state_store.load_artifact(issue_key, "issue.json")
        if not isinstance(issue, dict) or not issue:
            meta = self.state_store.load_issue_meta(issue_key)
            issue = {
                "repo_full_name": str(meta.get("github_repo", "")),
                "number": int(str(meta.get("issue_number", "0")).strip() or 0),
                "title": issue_key,
                "url": "",
            }
        thread_name = self._issue_thread_name(issue)
        thread = await channel.create_thread(name=thread_name, auto_archive_duration=1440)
        thread_id = int(getattr(thread, "id", 0) or 0)
        if thread_id <= 0:
            return 0
        self.state_store.bind_thread(thread_id, issue_key)
        self.state_store.update_issue_meta(
            issue_key,
            thread_id=str(thread_id),
            channel_id=status_channel_id,
        )
        await self._post_issue_mirror_summary(thread, issue_key, issue)
        return thread_id

    def _issue_thread_name(self, issue: dict[str, Any]) -> str:
        repo = str(issue.get("repo_full_name", "")).split("/")[-1]
        number = str(issue.get("number", "")).strip()
        title = str(issue.get("title", "")).replace("\n", " ").strip()
        if len(title) > 60:
            title = title[:60].rstrip() + "..."
        return f"dev-bot | {repo}#{number} | {title or 'issue'}"

    async def _post_issue_mirror_summary(
        self, thread: discord.Thread | Any, issue_key: str, issue: dict[str, Any]
    ) -> None:
        summary_bootstrapped = self._bootstrap_issue_summary(issue_key, issue)
        conversation_bootstrapped = self._bootstrap_issue_conversation(issue_key, issue)
        meta = self.state_store.load_issue_meta(issue_key)
        state = str(meta.get("status", "")).strip() or "unknown"
        plan = str(meta.get("plan_state", "")).strip() or "unknown"
        plan_hint = (
            "\n- requirement_summary を issue body から初期化しました。必要なら補足して `/plan` を実行してください。"
            if summary_bootstrapped
            else ""
        )
        conversation_hint = (
            "\n- issue 本文を会話履歴の初期入力として取り込みました。追加要件はこの thread に返信してください。"
            if conversation_bootstrapped
            else ""
        )
        await thread.send(
            "GitHub Issue を status mirror thread に同期しました。\n"
            f"- Issue: `{issue_key}`\n"
            f"- Title: {issue.get('title', '')}\n"
            f"- URL: {issue.get('url', '')}\n"
            f"- State: `{state}`\n"
            f"- Plan: `{plan}`"
            f"{plan_hint}"
            f"{conversation_hint}"
        )

    def _bootstrap_issue_summary(self, issue_key: str, issue: dict[str, Any]) -> bool:
        existing = self.state_store.load_artifact(issue_key, "requirement_summary.json")
        if isinstance(existing, dict) and existing:
            return False
        summary = self._summary_from_issue(issue)
        self.state_store.write_artifact(issue_key, "requirement_summary.json", summary)
        return True

    def _bootstrap_issue_conversation(self, issue_key: str, issue: dict[str, Any]) -> bool:
        conversation_path = self.state_store.entity_dir(issue_key) / "conversation.jsonl"
        if conversation_path.exists() and conversation_path.read_text(encoding="utf-8").strip():
            return False
        title = str(issue.get("title", "")).strip()
        body = str(issue.get("body", "")).strip()
        content_lines = [
            "GitHub issue から初期化した要件です。",
            f"Title: {title or '(no title)'}",
        ]
        if body:
            content_lines.extend(["", body])
        self.state_store.append_message(issue_key, "user", "\n".join(content_lines).strip())
        return True

    def _summary_from_issue(self, issue: dict[str, Any]) -> dict[str, Any]:
        body = str(issue.get("body", "") or "")
        title = str(issue.get("title", "") or "").strip()
        goal = self._issue_section_text(body, "目的") or title
        in_scope = self._issue_section_list(body, "やること") or ([goal] if goal else [])
        acceptance = self._issue_section_list(body, "受け入れ条件") or ([goal] if goal else [])
        return {
            "background": self._issue_section_text(body, "背景"),
            "goal": goal,
            "in_scope": in_scope,
            "out_of_scope": self._issue_section_list(body, "やらないこと"),
            "acceptance_criteria": acceptance,
            "constraints": self._issue_section_list(body, "制約"),
            "test_focus": self._issue_section_list(body, "テスト観点"),
            "open_questions": self._issue_section_list(body, "未確定事項"),
        }

    def _issue_section_text(self, body: str, heading: str) -> str:
        match = self._issue_section_body(body, heading)
        return match.strip() if match else ""

    def _issue_section_list(self, body: str, heading: str) -> list[str]:
        section = self._issue_section_body(body, heading)
        if not section:
            return []
        items: list[str] = []
        for line in section.splitlines():
            text = line.strip()
            if text.startswith("- "):
                text = text[2:].strip()
            if text:
                items.append(text)
        return items

    def _issue_section_body(self, body: str, heading: str) -> str:
        marker = f"## {heading}"
        if marker not in body:
            return ""
        after = body.split(marker, 1)[1]
        next_heading = after.find("\n## ")
        section = after[:next_heading] if next_heading >= 0 else after
        return section.strip()

    def _has_planning_artifacts(self, thread_id: int) -> bool:
        return self.issue_scheduler.has_planning_artifacts(thread_id)

    def _scheduler_gate_for_issue(self, repo_full_name: str, issue_number: int, issue_key: str) -> dict[str, str]:
        self._refresh_scheduler_dependencies()
        return self.issue_scheduler.scheduler_gate_for_issue(repo_full_name, issue_number, issue_key)

    async def _process_merging_issue(
        self,
        *,
        issue_key: str,
        thread_id: int,
        repo_full_name: str,
        issue_number: int,
    ) -> None:
        if self.process_registry.is_active(issue_key):
            return
        pr = self.state_store.load_artifact(issue_key, "pr.json")
        if not isinstance(pr, dict) or not pr or not pr.get("number"):
            await self._mark_merging_blocked(
                issue_key, thread_id, repo_full_name, issue_number, "merge 対象の PR が見つかりません"
            )
            return
        try:
            pr_status = await self._run_blocking(
                self.github_client.get_pull_request_status,
                repo_full_name,
                int(pr["number"]),
            )
        except Exception as exc:
            await self._mark_merging_blocked(
                issue_key, thread_id, repo_full_name, issue_number, f"PR status lookup failed: {exc}"
            )
            return
        mergeable_state = str(pr_status.get("mergeable_state", "")).strip().lower()
        if mergeable_state == "unknown":
            logger.info("merge pending: mergeability is still unknown for %s", issue_key)
            return
        guard_failure = self._merge_guard_failure(pr, pr_status)
        if guard_failure:
            await self._mark_merging_blocked(issue_key, thread_id, repo_full_name, issue_number, guard_failure)
            return
        try:
            result = await self._run_blocking(
                self.github_client.merge_pull_request,
                repo_full_name,
                int(pr["number"]),
            )
        except Exception as exc:
            await self._mark_merging_blocked(
                issue_key, thread_id, repo_full_name, issue_number, f"PR merge failed: {exc}"
            )
            return
        if not result.get("merged"):
            message = str(result.get("message", "")).strip() or "GitHub merge API returned merged=false"
            await self._mark_merging_blocked(issue_key, thread_id, repo_full_name, issue_number, message)
            return
        self.state_store.update_status(issue_key, "Done")
        self.state_store.update_meta(issue_key, runtime_status="", merged_sha=str(result.get("sha", "")))
        self.state_store.record_activity(
            issue_key,
            phase="merge",
            summary="PR を merge して Done に遷移しました",
            status="completed",
            run_id=str(self.state_store.load_meta(issue_key).get("current_run_id", "")),
            details={"thread_id": thread_id, "pr_number": pr.get("number"), "sha": result.get("sha", "")},
        )
        await self._run_blocking(
            self._update_issue_workpad,
            issue_key,
            repo_full_name,
            issue_number,
            "Done",
            "merge completed",
            [],
        )
        channel = self.get_channel(thread_id)
        if channel is not None and hasattr(channel, "send"):
            await channel.send(
                f"PR を merge しました。Done に更新しました。\n- PR: #{pr['number']}\n- URL: {pr.get('url', '')}"
            )

    def _merge_guard_failure(self, pr: dict[str, Any], pr_status: dict[str, Any]) -> str:
        expected_head_sha = str(pr.get("head_sha", "")).strip()
        actual_head_sha = str(pr_status.get("head_sha", "")).strip()
        if expected_head_sha and actual_head_sha and expected_head_sha != actual_head_sha:
            return f"head_sha_changed expected={expected_head_sha} actual={actual_head_sha}"
        if bool(pr_status.get("draft")):
            return "PR is still draft"
        mergeable = pr_status.get("mergeable")
        if mergeable is False:
            return "PR is not mergeable"
        mergeable_state = str(pr_status.get("mergeable_state", "")).strip().lower()
        if mergeable_state and mergeable_state not in {"clean", "has_hooks"}:
            return f"mergeable_state={mergeable_state}"
        return ""

    async def _mark_merging_blocked(
        self,
        issue_key: str,
        thread_id: int,
        repo_full_name: str,
        issue_number: int,
        reason: str,
    ) -> None:
        self.state_store.update_status(issue_key, "Blocked")
        self.state_store.update_meta(issue_key, runtime_status="")
        try:
            await self._run_blocking(self.github_client.update_issue_state, repo_full_name, issue_number, "Blocked")
        except Exception as exc:
            logger.warning("merge blocked: failed to update project state for %s: %s", issue_key, exc)
        self.state_store.record_activity(
            issue_key,
            phase="merge",
            summary="merge 中の問題で Blocked に補正しました",
            status="failed",
            run_id=str(self.state_store.load_meta(issue_key).get("current_run_id", "")),
            details={"thread_id": thread_id, "reason": reason},
        )

    def _update_issue_workpad(
        self,
        issue_key: str,
        repo_full_name: str,
        issue_number: int,
        state: str,
        latest_attempt: str,
        blockers: list[str],
    ) -> None:
        issue = self.state_store.load_artifact(issue_key, "issue_snapshot.json")
        if not isinstance(issue, dict) or not issue:
            issue = self.state_store.load_artifact(issue_key, "issue.json")
        summary = self.state_store.load_artifact(issue_key, "requirement_summary.json")
        plan = self.state_store.load_artifact(issue_key, "plan.json")
        test_plan = self.state_store.load_artifact(issue_key, "test_plan.json")
        pr = self.state_store.load_artifact(issue_key, "pr.json")
        verification = self.state_store.load_artifact(issue_key, "verification.json")
        meta = self.state_store.load_meta(issue_key)
        sections = self.pipeline._build_workpad_sections(
            summary=summary if isinstance(summary, dict) else {},
            plan=plan if isinstance(plan, dict) else {},
            test_plan=test_plan if isinstance(test_plan, dict) else {},
            issue=issue if isinstance(issue, dict) else {},
            current_state=state,
            latest_attempt=latest_attempt,
            branch=str(meta.get("branch_name", "")),
            pr=(f"draft #{pr.get('number')} {pr.get('url')}" if isinstance(pr, dict) and pr else "なし"),
            verification=verification if isinstance(verification, dict) else {},
            blockers=blockers,
            artifacts=["pr.json", "verification.json", "final_summary.json"],
            audit_trail=[f"{datetime.now(UTC).isoformat()} {latest_attempt}"],
        )
        self.github_client.update_issue_state(repo_full_name, issue_number, state)
        self.github_client.upsert_workpad_comment(repo_full_name, issue_number, sections)

    async def _generate_plan(self, interaction: discord.Interaction, repo: str, *, alias_used: bool) -> None:
        thread_id = self._ensure_managed_thread(interaction.channel)
        if thread_id is None:
            await interaction.response.send_message(
                "このコマンドは管理対象スレッド内で実行してください。", ephemeral=True
            )
            return
        if self.orchestrator.is_running(thread_id):
            await interaction.response.send_message("実行中です。先に `/abort` してください。", ephemeral=True)
            return
        summary = self.state_store.load_artifact(thread_id, "requirement_summary.json")
        if not isinstance(summary, dict) or not summary:
            await interaction.response.send_message("要件サマリーがまだ作成されていません。", ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        issue_key = self.state_store.issue_key_for_thread(thread_id)
        if issue_key:
            issue_meta = self.state_store.load_issue_meta(issue_key)
            issue_repo = str(issue_meta.get("github_repo", "")).strip()
            self.state_store.update_draft_meta(thread_id, github_repo=issue_repo or repo)
        else:
            self.state_store.update_status(thread_id, "planning")
        self.state_store.write_artifact(thread_id, "planning_progress.json", {"status": "planning", "phase": "plan"})
        try:
            artifacts = await self._run_blocking(self._build_plan_artifacts, repo, thread_id, summary)
        except Exception as exc:
            details: dict[str, Any] = {"repo": repo}
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
            elif isinstance(exc, AgentBufferOverflowError):
                details.update(
                    {
                        "prompt_kind": exc.prompt_kind or "unknown",
                        "session_id": exc.session_id or "",
                        "failure_type": "buffer_overflow",
                        "max_buffer_size": exc.max_buffer_size,
                        "likely_source": exc.likely_source,
                        "source_detail": exc.source_detail,
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
            if issue_key:
                self.state_store.update_draft_meta(thread_id, status="failed", github_repo=repo)
            else:
                self.state_store.update_status(thread_id, "failed")
            await self._send_followup_text(
                interaction,
                f"plan の生成に失敗しました: `{exc}`\n詳細は `/why-failed` を確認してください。",
                ephemeral=True,
            )
            return

        planning_artifacts = {
            "plan": artifacts["plan"],
            "test_plan": artifacts["test_plan"],
            "repo_profile": artifacts["repo_profile"],
            "planning_workspace": artifacts["planning_workspace"],
            "planning_sessions": artifacts["planning_sessions"],
        }
        base_branch = str(artifacts["planning_workspace"].get("base_branch", ""))
        if issue_key:
            issue_meta = self.state_store.load_issue_meta(issue_key)
            issue_number = int(str(issue_meta.get("issue_number", "0")).strip() or 0)
            issue_repo = str(issue_meta.get("github_repo", "")).strip() or repo
            if not issue_repo or not issue_number:
                self.state_store.record_failure(
                    thread_id,
                    stage="plan_sync",
                    message="Issue metadata is incomplete; cannot sync Plan field to GitHub.",
                    details={"issue_key": issue_key, "repo": repo, "base_branch": base_branch},
                )
                self.state_store.update_draft_meta(
                    thread_id, status="failed", github_repo=issue_repo, base_branch=base_branch
                )
                await self._send_followup_text(
                    interaction,
                    "plan は生成しましたが、既存 Issue の GitHub metadata が不足しているため成功扱いにしていません。",
                    ephemeral=True,
                )
                return
            try:
                await self._run_blocking(self.github_client.update_issue_plan, issue_repo, issue_number, "Drafted")
            except Exception as exc:
                logger.warning("plan: failed to reset GitHub plan field for %s: %s", issue_key, exc)
                self.state_store.record_failure(
                    thread_id,
                    stage="plan_sync",
                    message=str(exc),
                    details={
                        "issue_key": issue_key,
                        "repo": issue_repo,
                        "issue_number": issue_number,
                        "target_plan_state": "Drafted",
                    },
                )
                self.state_store.update_draft_meta(
                    thread_id, status="failed", github_repo=issue_repo, base_branch=base_branch
                )
                await self._send_followup_text(
                    interaction,
                    f"plan は生成しましたが GitHub の Plan を `Drafted` に戻せなかったため成功扱いにしていません: `{exc}`",
                    ephemeral=True,
                )
                return
            self._clear_execution_artifacts(thread_id)
            self._persist_artifacts(thread_id, planning_artifacts)
            self.state_store.update_draft_meta(
                thread_id,
                status="awaiting_approval",
                github_repo=issue_repo,
                base_branch=base_branch,
            )
            self.state_store.update_issue_meta(
                issue_key,
                plan_state="Drafted",
                base_branch=base_branch,
            )
        else:
            self._clear_execution_artifacts(thread_id)
            self._persist_artifacts(thread_id, planning_artifacts)
            self.state_store.update_meta(
                thread_id,
                status="awaiting_approval",
                plan_state="Drafted",
                github_repo=repo,
                base_branch=base_branch,
            )
        self.state_store.write_artifact(thread_id, "planning_progress.json", {"status": "completed", "phase": "done"})
        prefix = "互換コマンド `/confirm` を `/plan` として扱いました。\n\n" if alias_used else ""
        plan_message = prefix + self._format_plan_message(repo, artifacts["plan"], artifacts["test_plan"])

        await self._send_followup_text(
            interaction,
            plan_message
            + "\n\n`/approve-plan` で Issue 化と実装開始、`/reject-plan` で差し戻しできます。"
            + f"\n- Repo: `{repo}`",
        )

    async def _promote_approved_plan(self, interaction: discord.Interaction) -> None:
        thread_id = self._ensure_managed_thread(interaction.channel)
        if thread_id is None:
            await interaction.response.send_message(
                "このコマンドは管理対象スレッド内で実行してください。", ephemeral=True
            )
            return
        if self.orchestrator.is_running(thread_id):
            await interaction.response.send_message("すでに実行中です。", ephemeral=True)
            return

        summary = self.state_store.load_artifact(thread_id, "requirement_summary.json")
        plan = self.state_store.load_artifact(thread_id, "plan.json")
        test_plan = self.state_store.load_artifact(thread_id, "test_plan.json")
        if (
            not isinstance(summary, dict)
            or not isinstance(plan, dict)
            or not isinstance(test_plan, dict)
            or not plan
            or not test_plan
        ):
            await interaction.response.send_message("先に `/plan repo:owner/repo` を実行してください。", ephemeral=True)
            return

        meta = self.state_store.load_meta(thread_id)
        issue = self.state_store.load_artifact(thread_id, "issue.json")
        repo_full_name = (issue.get("repo_full_name") if isinstance(issue, dict) else "") or str(
            meta.get("github_repo", "")
        )
        if not repo_full_name:
            await interaction.response.send_message(
                "repo を決められませんでした。先に `/plan repo:owner/repo` を実行してください。", ephemeral=True
            )
            return

        await interaction.response.defer(thinking=True)
        promoted_issue_key = ""
        try:
            issue = await ensure_issue_for_thread(
                thread_id=thread_id,
                repo_full_name=repo_full_name,
                state_store=self.state_store,
                github_client=self.github_client,
                thread_url=interaction.channel.jump_url if isinstance(interaction.channel, discord.Thread) else "",
            )
            issue_key = self.state_store.bind_issue(thread_id, repo_full_name, int(issue["number"]))
            promoted_issue_key = issue_key
            await self._run_blocking(self.github_client.add_issue_to_project, repo_full_name, int(issue["number"]))
            await self._run_blocking(
                self.github_client.update_issue_plan, repo_full_name, int(issue["number"]), "Approved"
            )
            await self._run_blocking(
                self.github_client.update_issue_state, repo_full_name, int(issue["number"]), "Ready"
            )
            self.state_store.update_draft_meta(thread_id, status="promoted", issue_key=issue_key)
            self.state_store.update_issue_meta(
                issue_key,
                status="Ready",
                plan_state="Approved",
                github_repo=repo_full_name,
                issue_number=str(issue["number"]),
            )
            if str(getattr(self.settings, "github_project_id", "")).strip():
                await self._scheduler_tick()
            else:
                started = await enqueue_issue_run(
                    thread_id=thread_id,
                    repo_full_name=repo_full_name,
                    issue=issue,
                    issue_key=issue_key,
                    orchestrator=self.orchestrator,
                )
                if not started:
                    raise RuntimeError("パイプラインの起動に失敗しました。")
        except (RuntimeError, ValueError) as exc:
            if promoted_issue_key or self.state_store.issue_key_for_thread(thread_id):
                self.state_store.update_draft_meta(thread_id, status="promotion_failed")
            await self._send_followup_text(interaction, str(exc), ephemeral=True)
            return
        await self._send_followup_text(
            interaction,
            "plan を承認し、Issue 化して queue に登録しました。\n"
            f"- Repo: `{repo_full_name}`\n"
            f"- Issue: #{issue['number']}\n"
            f"- URL: {issue['url']}",
        )
        if isinstance(interaction.channel, discord.Thread):
            await self._maybe_post_pending_approval(interaction.channel)

    async def _reject_plan(self, interaction: discord.Interaction) -> None:
        thread_id = self._ensure_managed_thread(interaction.channel)
        if thread_id is None:
            await interaction.response.send_message(
                "このコマンドは管理対象スレッド内で実行してください。", ephemeral=True
            )
            return
        plan = self.state_store.load_artifact(thread_id, "plan.json")
        if not isinstance(plan, dict) or not plan:
            await interaction.response.send_message(
                "却下する plan がありません。先に `/plan` を実行してください。", ephemeral=True
            )
            return
        self.state_store.update_draft_meta(thread_id, status="changes_requested")
        issue_key = self.state_store.issue_key_for_thread(thread_id)
        if issue_key:
            meta = self.state_store.load_meta(issue_key)
            repo_full_name = str(meta.get("github_repo", "")).strip()
            issue_number = int(str(meta.get("issue_number", "0")).strip() or 0)
            if repo_full_name and issue_number:
                try:
                    await self._run_blocking(
                        self.github_client.update_issue_plan,
                        repo_full_name,
                        issue_number,
                        "Changes Requested",
                    )
                except Exception as exc:
                    logger.warning("reject-plan: failed to update GitHub plan field for %s: %s", issue_key, exc)
            self.state_store.update_issue_meta(issue_key, plan_state="Changes Requested")
        await interaction.response.send_message(
            "plan を差し戻しました。追加の要件を投稿してから `/plan` を再実行してください。",
            ephemeral=True,
        )

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
            if status and not self.state_store.issue_key_for_thread(thread_id):
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
        self._reconcile_runtime_state(thread_id, thread_id=thread_id)

    def _reconcile_runtime_state(self, identifier: str | int, *, thread_id: int = 0) -> None:
        runtime_key = identifier if isinstance(identifier, str) else self._runtime_key(identifier)
        meta = self.state_store.load_meta(runtime_key)
        state = str(meta.get("status", "")).strip()
        runtime_status = str(meta.get("runtime_status", "")).strip()
        if state not in {"In Progress"} and runtime_status not in {
            "queued",
            "running",
            "verifying",
            "awaiting_high_risk_approval",
        }:
            return
        has_process = self.process_registry.is_active(runtime_key) or (
            thread_id > 0 and isinstance(runtime_key, str) and self.process_registry.is_active(thread_id)
        )
        is_active = (
            (thread_id > 0 and self.orchestrator.is_running(thread_id))
            or (thread_id > 0 and self.orchestrator.is_queued(thread_id))
            or has_process
        )
        if runtime_status == "awaiting_high_risk_approval":
            pending = self.state_store.load_artifact(runtime_key, "pending_approval.json")
            if isinstance(pending, dict) and pending.get("status") == "pending":
                return
        if is_active:
            if state == "In Progress" and has_process and not runtime_status:
                self.state_store.update_meta(runtime_key, runtime_status="running")
            return
        next_state = "Rework"
        self.state_store.update_meta(runtime_key, runtime_status="")
        self.state_store.update_status(runtime_key, next_state)
        if isinstance(runtime_key, str):
            repo_full_name = str(meta.get("github_repo", "")).strip()
            issue_number = int(str(meta.get("issue_number", "0")).strip() or 0)
            if repo_full_name and issue_number:
                try:
                    self.github_client.update_issue_state(repo_full_name, issue_number, next_state)
                except Exception as exc:
                    logger.warning("runtime reconcile: failed to update project state for %s: %s", runtime_key, exc)
        self.state_store.record_activity(
            runtime_key,
            phase="reconcile",
            summary="実行状態と status の不整合を検出し補正しました",
            status="failed",
            run_id=str(meta.get("current_run_id", "")),
        )

    def _format_plan_message(self, repo: str, plan: dict[str, Any], test_plan: dict[str, Any]) -> str:
        return format_plan_message(repo, plan, test_plan)

    def _clear_execution_artifacts(self, thread_id: int) -> None:
        runtime_key = self._runtime_key(thread_id)
        issue_bound = runtime_key != thread_id
        runtime_artifacts = (
            DERIVED_ARTIFACTS
            if not issue_bound
            else tuple(filename for filename in DERIVED_ARTIFACTS if filename != "issue.json")
        )
        for filename in runtime_artifacts:
            self.state_store.delete_artifact(runtime_key, filename)
        if issue_bound:
            for filename in DERIVED_ARTIFACTS:
                self.state_store.delete_draft_artifact(thread_id, filename)
        if runtime_key != thread_id:
            self.state_store.update_meta(
                runtime_key,
                pr_number="",
                pr_url="",
                workspace="",
                branch_name="",
                base_branch="",
            )
            self.state_store.update_draft_meta(
                thread_id,
                issue_number="",
                pr_number="",
                pr_url="",
                workspace="",
                branch_name="",
                base_branch="",
            )
            return
        self.state_store.update_meta(
            runtime_key,
            issue_number="",
            pr_number="",
            pr_url="",
            workspace="",
            branch_name="",
            base_branch="",
        )

    def _build_diff_summary(self, workspace: str, pathspec: str) -> str:
        status = subprocess.run(
            ["git", "-C", workspace, "status", "--short"], check=True, capture_output=True, text=True
        )
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
            await interaction.response.send_message(
                "このコマンドは管理対象スレッド内で実行してください。", ephemeral=True
            )
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
            runtime_key = self._runtime_key(thread_id)
            meta = self.state_store.load_meta(runtime_key)
            issue = self.state_store.load_artifact(thread_id, "issue.json")
            repo_full_name = str(meta.get("github_repo", ""))
            if resolution != "resolved" and isinstance(issue, dict) and issue and repo_full_name:
                self.state_store.update_meta(runtime_key, runtime_status="queued")
                await self.orchestrator.enqueue(
                    WorkItem(
                        thread_id=thread_id,
                        repo_full_name=repo_full_name,
                        issue=issue,
                        issue_key=f"{repo_full_name}#{issue.get('number')}",
                        workspace_key=f"{repo_full_name}#{issue.get('number')}",
                    )
                )
                await interaction.response.send_message(
                    "高リスク操作を承認しました。run を再キューしました。", ephemeral=True
                )
                return
            self.state_store.update_meta(runtime_key, runtime_status="running")
            await interaction.response.send_message("高リスク操作を承認しました。run を再開します。", ephemeral=True)
            return
        runtime_key = self._runtime_key(thread_id)
        self.state_store.update_meta(runtime_key, runtime_status="")
        self.state_store.update_status(runtime_key, "Blocked")
        try:
            await self._update_bound_issue_state(thread_id, "Blocked")
        except Exception as exc:
            logger.warning("reject approval: failed to update project state for thread %s: %s", thread_id, exc)
        await interaction.response.send_message("高リスク操作を拒否しました。run を停止します。", ephemeral=True)

    async def _maybe_post_pending_approval(self, thread: discord.Thread) -> None:
        payload = self.state_store.load_artifact(self._runtime_key(thread.id), "pending_approval.json")
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
        self._refresh_scheduler_dependencies()
        await self.issue_scheduler.restore_pending_runs()

    async def _restore_pending_approval(self, thread_id: int) -> None:
        channel = self.get_channel(thread_id)
        if channel is not None and hasattr(channel, "send"):
            await self._maybe_post_pending_approval(channel)

    def _refresh_scheduler_dependencies(self) -> None:
        self.issue_scheduler.github_client = self.github_client
        self.issue_scheduler.orchestrator = self.orchestrator
        self.issue_scheduler.process_registry = self.process_registry
        self.issue_scheduler.settings = self.settings
        self.issue_scheduler._run_blocking = self._run_blocking


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
    if not DISCORD_AVAILABLE:
        raise RuntimeError("discord.py is not installed")
    return DevBotClient(settings=settings, state_store=FileStateStore(runs_root=settings.runs_root))
