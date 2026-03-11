from __future__ import annotations

import asyncio
import subprocess
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
from app.orchestrator import Orchestrator, WorkItem
from app.pipeline import DevelopmentPipeline
from app.planning_agent import PlanningAgent
from app.process_registry import ProcessRegistry
from app.repo_profiler import build_repo_profile
from app.requirements_agent import RequirementsAgent
from app.run_request import ensure_issue_and_enqueue
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
            ("repos", "アクセス可能な repository 一覧を表示します", self.repos_command),
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
            logger.info("Logged in as %s (%s)", self.user, self.user.id)
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

    async def plan_command(self, interaction: discord.Interaction, repo: str) -> None:
        await self._generate_plan(interaction, repo, alias_used=False)

    async def confirm_command(self, interaction: discord.Interaction, repo: str) -> None:
        await self._generate_plan(interaction, repo, alias_used=True)

    async def run_command(self, interaction: discord.Interaction, repo: str | None = None) -> None:
        await self._start_run(interaction, repo)

    async def repos_command(self, interaction: discord.Interaction, query: str | None = None) -> None:
        await interaction.response.defer(thinking=True, ephemeral=True)
        try:
            repos = await asyncio.to_thread(self._list_repositories_for_display, query or "")
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
            await interaction.response.send_message(
                "このコマンドは管理対象スレッド内で実行してください。", ephemeral=True
            )
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

    async def retry_command(self, interaction: discord.Interaction) -> None:
        thread_id = self._ensure_managed_thread(interaction.channel)
        if thread_id is None:
            await interaction.response.send_message(
                "このコマンドは管理対象スレッド内で実行してください。", ephemeral=True
            )
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
            await interaction.response.send_message(
                "このコマンドは管理対象スレッド内で実行してください。", ephemeral=True
            )
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
            await interaction.response.send_message(
                "このコマンドは管理対象スレッド内で実行してください。", ephemeral=True
            )
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
            await interaction.response.send_message(
                "このコマンドは管理対象スレッド内で実行してください。", ephemeral=True
            )
            return
        last_failure = self.state_store.load_artifact(thread_id, "last_failure.json")
        verification = self.state_store.load_artifact(thread_id, "verification_summary.json")
        final_result = self.state_store.load_artifact(thread_id, "final_result.json")
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
        final_result = self.state_store.load_artifact(thread_id, "final_result.json")
        verification = self.state_store.load_artifact(thread_id, "verification_summary.json")
        await self._send_interaction_text(
            interaction,
            format_budget_message(
                attempt_count=int(self.state_store.load_meta(thread_id).get("attempt_count", 0)),
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
                asyncio.to_thread(self.github_client.suggest_repositories, current, 25),
                timeout=1.5,
            )
        except Exception as exc:
            logger.warning("repo_autocomplete: GitHub repository lookup failed: %s", exc)
            fallback = self.github_client.fallback_repositories()
            return [app_commands.Choice(name=repo, value=repo) for repo in fallback[:25]]
        return [app_commands.Choice(name=repo, value=repo) for repo in repos]

    async def _warm_repo_autocomplete_cache(self) -> None:
        try:
            await asyncio.to_thread(self.github_client.warm_repository_cache)
        except Exception as exc:
            logger.warning("repo_autocomplete: cache warm failed: %s", exc)
            return

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
        plan_message = prefix + self._format_plan_message(repo, artifacts["plan"], artifacts["test_plan"])
        try:
            issue = await self._enqueue_run_for_thread(
                thread_id=thread_id, channel=interaction.channel, repo_full_name=repo
            )
        except (RuntimeError, ValueError) as exc:
            await self._send_followup_text(
                interaction,
                plan_message + f"\n\n自動 `/run` の開始に失敗しました: `{exc}`",
            )
            return

        await self._send_followup_text(
            interaction,
            plan_message
            + "\n\n自動で `/run` を開始しました。"
            + f"\n- Repo: `{repo}`"
            + f"\n- Issue: #{issue['number']}"
            + f"\n- URL: {issue['url']}",
        )
        if isinstance(interaction.channel, discord.Thread):
            await self._maybe_post_pending_approval(interaction.channel)

    async def _start_run(self, interaction: discord.Interaction, repo: str | None) -> None:
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
        repo_full_name = (
            repo or (issue.get("repo_full_name") if isinstance(issue, dict) else "") or str(meta.get("github_repo", ""))
        )
        if not repo_full_name:
            await interaction.response.send_message(
                "repo を決められませんでした。`/run repo:owner/repo` を指定してください。", ephemeral=True
            )
            return

        await interaction.response.defer(thinking=True)
        try:
            issue = await self._enqueue_run_for_thread(
                thread_id=thread_id,
                channel=interaction.channel,
                repo_full_name=repo_full_name,
            )
        except (RuntimeError, ValueError) as exc:
            await self._send_followup_text(interaction, str(exc), ephemeral=True)
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

    async def _enqueue_run_for_thread(
        self,
        *,
        thread_id: int,
        channel: discord.abc.GuildChannel | discord.Thread | None,
        repo_full_name: str,
    ) -> dict[str, Any]:
        thread_url = channel.jump_url if isinstance(channel, discord.Thread) else ""
        return await ensure_issue_and_enqueue(
            thread_id=thread_id,
            repo_full_name=repo_full_name,
            state_store=self.state_store,
            github_client=self.github_client,
            orchestrator=self.orchestrator,
            thread_url=thread_url,
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
        return format_plan_message(repo, plan, test_plan)

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
            meta = self.state_store.load_meta(thread_id)
            issue = self.state_store.load_artifact(thread_id, "issue.json")
            repo_full_name = str(meta.get("github_repo", ""))
            if resolution != "resolved" and isinstance(issue, dict) and issue and repo_full_name:
                self.state_store.update_status(thread_id, "queued")
                await self.orchestrator.enqueue(
                    WorkItem(
                        thread_id=thread_id,
                        repo_full_name=repo_full_name,
                        issue=issue,
                        workspace_key=f"{repo_full_name}#{issue.get('number')}",
                    )
                )
                await interaction.response.send_message(
                    "高リスク操作を承認しました。run を再キューしました。", ephemeral=True
                )
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
            items.append(
                WorkItem(
                    thread_id=thread_id,
                    repo_full_name=repo_full_name,
                    issue=issue,
                    workspace_key=f"{repo_full_name}#{issue.get('number')}",
                )
            )
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
    if not DISCORD_AVAILABLE:
        raise RuntimeError("discord.py is not installed")
    return DevBotClient(settings=settings, state_store=FileStateStore(runs_root=settings.runs_root))
