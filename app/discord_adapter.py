from __future__ import annotations

import asyncio

import discord
from discord import app_commands

from app.config import Settings
from app.github_client import GitHubIssueClient
from app.issue_draft import build_issue_body, build_issue_title
from app.pipeline import DevelopmentPipeline
from app.requirements_agent import RequirementsAgent
from app.state_store import FileStateStore


class DevBotClient(discord.Client):
    def __init__(self, settings: Settings, state_store: FileStateStore) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self.settings = settings
        self.state_store = state_store
        self.requirements_agent = RequirementsAgent(settings=settings)
        self.github_client = GitHubIssueClient(settings.github_token)
        self.pipeline = DevelopmentPipeline(settings=settings, state_store=state_store, github_client=self.github_client)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self) -> None:
        confirm = app_commands.Command(
            name="confirm",
            description="整理済み要件からGitHub Issueを作成します",
            callback=self.confirm_command,
        )
        confirm.autocomplete("repo")(self.repo_autocomplete)
        self.tree.add_command(confirm)
        self.tree.add_command(
            app_commands.Command(
                name="status",
                description="現在の状態を表示します",
                callback=self.status_command,
            )
        )
        self.tree.add_command(
            app_commands.Command(
                name="issue",
                description="作成済みIssueを表示します",
                callback=self.issue_command,
            )
        )
        self.tree.add_command(
            app_commands.Command(
                name="abort",
                description="このスレッドの処理を停止します",
                callback=self.abort_command,
            )
        )
        self.tree.add_command(
            app_commands.Command(
                name="retry",
                description="失敗したパイプラインを再実行します",
                callback=self.retry_command,
            )
        )
        self.tree.add_command(
            app_commands.Command(
                name="pr",
                description="作成済みPRを表示します",
                callback=self.pr_command,
            )
        )
        self.tree.add_command(
            app_commands.Command(
                name="revise",
                description="要件整理を再開します",
                callback=self.revise_command,
            )
        )
        if self.settings.discord_guild_id:
            guild = discord.Object(id=int(self.settings.discord_guild_id))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            return
        await self.tree.sync()

    async def on_ready(self) -> None:
        if self.user is None:
            return
        print(f"Logged in as {self.user} ({self.user.id})")

    async def on_message(self, message: discord.Message) -> None:
        print(
            "Received message:",
            {
                "channel_id": message.channel.id,
                "author": str(message.author),
                "author_is_bot": message.author.bot,
                "mentions_bot": self.user in message.mentions if self.user else False,
                "content": message.content,
            },
        )
        if message.author.bot:
            return
        if isinstance(message.channel, discord.Thread):
            await self._handle_thread_message(message)
            return

        await self._handle_requirements_channel_message(message)

    def _build_thread_name(self, content: str) -> str:
        summary = content.replace("\n", " ").strip()
        if len(summary) > 40:
            summary = summary[:40].rstrip() + "..."
        return f"dev-bot | {summary or 'new request'}"

    async def _handle_requirements_channel_message(self, message: discord.Message) -> None:
        if str(message.channel.id) != self.settings.requirements_channel_id:
            print(
                "Ignoring message due to channel mismatch:",
                {
                    "expected": self.settings.requirements_channel_id,
                    "actual": str(message.channel.id),
                },
            )
            return
        if self.user is None or self.user not in message.mentions:
            print("Ignoring message because bot was not mentioned.")
            return

        print("Creating thread for request message.")
        thread = await message.create_thread(
            name=self._build_thread_name(message.content),
            auto_archive_duration=1440,
        )
        self.state_store.create_run(
            thread_id=thread.id,
            parent_message_id=message.id,
            channel_id=message.channel.id,
        )
        self.state_store.append_message(thread.id, "user", message.content)
        reply = await asyncio.to_thread(self.requirements_agent.build_reply, thread.id)
        await thread.send(reply.body)
        self.state_store.append_message(thread.id, "assistant", reply.body)
        self.state_store.update_status(thread.id, reply.status)
        if reply.artifacts:
            self._persist_artifacts(thread.id, reply.artifacts)

    async def _handle_thread_message(self, message: discord.Message) -> None:
        if not self.state_store.has_run(message.channel.id):
            print("Ignoring thread message because it is not managed by this bot.")
            return

        self.state_store.append_message(message.channel.id, "user", message.content)
        reply = await asyncio.to_thread(self.requirements_agent.build_reply, message.channel.id)
        await message.channel.send(reply.body)
        self.state_store.append_message(message.channel.id, "assistant", reply.body)
        self.state_store.update_status(message.channel.id, reply.status)
        if reply.artifacts:
            self._persist_artifacts(message.channel.id, reply.artifacts)

    def _persist_artifacts(self, thread_id: int, artifacts: dict) -> None:
        summary = artifacts.get("summary")
        if isinstance(summary, dict):
            self.state_store.write_artifact(thread_id, "requirement_summary.json", summary)
        agent_error = artifacts.get("agent_error")
        if isinstance(agent_error, dict):
            self.state_store.write_artifact(thread_id, "agent_error.json", agent_error)

    def _ensure_managed_thread(self, channel: discord.abc.GuildChannel | discord.Thread | None) -> int | None:
        if not isinstance(channel, discord.Thread):
            return None
        if not self.state_store.has_run(channel.id):
            return None
        return channel.id

    async def confirm_command(self, interaction: discord.Interaction, repo: str) -> None:
        thread_id = self._ensure_managed_thread(interaction.channel)
        if thread_id is None:
            await interaction.response.send_message("このコマンドは dev-bot が管理しているスレッド内で実行してください。", ephemeral=True)
            return

        summary = self.state_store.load_artifact(thread_id, "requirement_summary.json")
        if not summary:
            await interaction.response.send_message("要件サマリーがまだ作成されていません。もう少し会話を続けてください。", ephemeral=True)
            return

        await interaction.response.defer(thinking=True)
        thread_url = interaction.channel.jump_url if isinstance(interaction.channel, discord.Thread) else ""
        title = build_issue_title(summary)
        body = build_issue_body(summary, thread_url)
        created = self.github_client.create_issue(repo_full_name=repo, title=title, body=body)

        self.state_store.write_artifact(
            thread_id,
            "issue.json",
            {
                "repo_full_name": created.repo_full_name,
                "number": created.number,
                "title": created.title,
                "body": created.body,
                "url": created.url,
            },
        )
        self.state_store.update_meta(thread_id, status="issue_created", github_repo=repo, issue_number=str(created.number))
        await interaction.followup.send(
            f"Issue を作成しました。\n- Repo: `{created.repo_full_name}`\n- Issue: #{created.number}\n- URL: {created.url}"
        )
        if isinstance(interaction.channel, discord.Thread):
            asyncio.create_task(
                self.pipeline.run(
                    client=self,
                    thread=interaction.channel,
                    repo_full_name=repo,
                    issue={
                        "repo_full_name": created.repo_full_name,
                        "number": created.number,
                        "title": created.title,
                        "body": created.body,
                        "url": created.url,
                    },
                )
            )

    async def status_command(self, interaction: discord.Interaction) -> None:
        thread_id = self._ensure_managed_thread(interaction.channel)
        if thread_id is None:
            await interaction.response.send_message("このコマンドは dev-bot が管理しているスレッド内で実行してください。", ephemeral=True)
            return

        meta = self.state_store.load_meta(thread_id)
        issue = self.state_store.load_artifact(thread_id, "issue.json")
        summary = self.state_store.load_artifact(thread_id, "requirement_summary.json")
        current_activity = self.state_store.load_artifact(thread_id, "current_activity.json")
        agent_failure = self.state_store.load_artifact(thread_id, "agent_failure.json")
        last_failure = self.state_store.load_artifact(thread_id, "last_failure.json")
        lines = [
            f"status: `{meta.get('status', 'unknown')}`",
            f"thread_id: `{thread_id}`",
        ]
        if issue:
            lines.append(f"issue: [#{issue.get('number')}]({issue.get('url')})")
        if summary:
            lines.append(f"goal: {summary.get('goal', '未整理')}")
            open_questions = summary.get("open_questions", [])
            if isinstance(open_questions, list) and open_questions:
                lines.append(f"open_questions: {len(open_questions)}件")
        if current_activity:
            lines.append(
                "current_activity: "
                f"`{current_activity.get('tool_name', 'unknown')}` {current_activity.get('status', '')} "
                f"- {current_activity.get('summary', '')}"
            )
        if last_failure:
            lines.append(
                "last_failure: "
                f"`{last_failure.get('tool_name', last_failure.get('message', 'unknown'))}` "
                f"- {last_failure.get('summary', last_failure.get('message', ''))}"
            )
        if agent_failure:
            lines.append(f"last_error: {agent_failure.get('message', '')}")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    async def issue_command(self, interaction: discord.Interaction) -> None:
        thread_id = self._ensure_managed_thread(interaction.channel)
        if thread_id is None:
            await interaction.response.send_message("このコマンドは dev-bot が管理しているスレッド内で実行してください。", ephemeral=True)
            return

        issue = self.state_store.load_artifact(thread_id, "issue.json")
        if not issue:
            await interaction.response.send_message("まだ Issue は作成されていません。`/confirm repo:owner/repo` を実行してください。", ephemeral=True)
            return
        await interaction.response.send_message(
            f"Repo: `{issue.get('repo_full_name')}`\nIssue: #{issue.get('number')}\nURL: {issue.get('url')}",
            ephemeral=True,
        )

    async def pr_command(self, interaction: discord.Interaction) -> None:
        thread_id = self._ensure_managed_thread(interaction.channel)
        if thread_id is None:
            await interaction.response.send_message("このコマンドは dev-bot が管理しているスレッド内で実行してください。", ephemeral=True)
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
            await interaction.response.send_message("このコマンドは dev-bot が管理しているスレッド内で実行してください。", ephemeral=True)
            return
        self.state_store.update_status(thread_id, "aborted")
        await interaction.response.send_message("この案件を `aborted` に更新しました。", ephemeral=True)

    async def retry_command(self, interaction: discord.Interaction) -> None:
        thread_id = self._ensure_managed_thread(interaction.channel)
        if thread_id is None:
            await interaction.response.send_message("このコマンドは dev-bot が管理しているスレッド内で実行してください。", ephemeral=True)
            return
        issue = self.state_store.load_artifact(thread_id, "issue.json")
        if not issue:
            await interaction.response.send_message("Issue がないため再試行できません。先に `/confirm repo:owner/repo` を実行してください。", ephemeral=True)
            return
        await interaction.response.send_message("パイプラインを再実行します。", ephemeral=True)
        if isinstance(interaction.channel, discord.Thread):
            asyncio.create_task(
                self.pipeline.run(
                    client=self,
                    thread=interaction.channel,
                    repo_full_name=issue["repo_full_name"],
                    issue=issue,
                )
            )

    async def revise_command(self, interaction: discord.Interaction) -> None:
        thread_id = self._ensure_managed_thread(interaction.channel)
        if thread_id is None:
            await interaction.response.send_message("このコマンドは dev-bot が管理しているスレッド内で実行してください。", ephemeral=True)
            return
        self.state_store.update_status(thread_id, "requirements_dialogue")
        await interaction.response.send_message("要件整理を再開しました。修正したい内容をそのまま投稿してください。", ephemeral=True)

    async def repo_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        del interaction
        try:
            repos = self.github_client.suggest_repositories(current, limit=25)
        except Exception as exc:
            print(f"repo_autocomplete failed: {exc}")
            return []
        return [app_commands.Choice(name=repo, value=repo) for repo in repos]


def build_client(settings: Settings) -> DevBotClient:
    return DevBotClient(settings=settings, state_store=FileStateStore(runs_root=settings.runs_root))
