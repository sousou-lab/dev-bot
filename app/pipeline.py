from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import discord

from app.config import Settings
from app.github_client import GitHubIssueClient
from app.local_runner import LocalRunner
from app.state_store import FileStateStore
from app.workspace_manager import WorkspaceManager


class DevelopmentPipeline:
    def __init__(self, settings: Settings, state_store: FileStateStore, github_client: GitHubIssueClient) -> None:
        self.settings = settings
        self.state_store = state_store
        self.github_client = github_client
        self.workspace_manager = WorkspaceManager(settings)
        self.local_runner = LocalRunner(settings)

    async def run(self, client: discord.Client, thread: discord.Thread, repo_full_name: str, issue: dict) -> None:
        thread_id = thread.id
        summary = self.state_store.load_artifact(thread_id, "requirement_summary.json")
        self.state_store.update_status(thread_id, "workspace_preparing")
        await thread.send("開発パイプラインを開始します。まずワークスペースを準備します。")
        try:
            workspace_info = await asyncio.to_thread(
                self.workspace_manager.prepare, repo_full_name, int(issue["number"]), thread_id
            )
            self.state_store.write_artifact(thread_id, "workspace.json", workspace_info)
            self.state_store.update_meta(
                thread_id,
                status="local_running",
                branch_name=workspace_info["branch_name"],
                workspace=workspace_info["workspace"],
                base_branch=workspace_info["base_branch"],
            )
            await thread.send(
                f"ローカル実行を開始します。\n- branch: `{workspace_info['branch_name']}`\n- base: `{workspace_info['base_branch']}`"
            )
            cmd, env, artifacts_dir = await asyncio.to_thread(
                self.local_runner.prepare_run,
                workspace=workspace_info["workspace"],
                run_dir=workspace_info["run_root"],
                requirement_summary=summary,
                issue=issue,
            )
            process = await asyncio.to_thread(self.local_runner.start_process, cmd, env)
            await self._monitor_local_run(thread.id, thread, process, artifacts_dir)
            output, _ = await asyncio.to_thread(process.communicate)
            if process.returncode != 0:
                self.state_store.update_status(thread_id, "failed")
                self._persist_activity_artifacts(thread_id, artifacts_dir)
                await thread.send(
                    "コマンド実行で失敗しました。\n"
                    f"終了コード: `{process.returncode}`\n"
                    f"直近ログ:\n```text\n{output.strip()[-1500:]}\n```"
                )
                return
            result = await asyncio.to_thread(self.local_runner.load_final_result, artifacts_dir)
            self.state_store.write_artifact(thread_id, "final_result.json", result)
            self._persist_activity_artifacts(thread_id, artifacts_dir)
            if not result.get("success"):
                self.state_store.update_status(thread_id, "failed")
                failure = self.local_runner.load_optional_artifact(artifacts_dir, "agent_failure.json")
                details = ""
                if failure:
                    details = f"\n最後のエラー: {failure.get('message', '')}"
                await thread.send(
                    "自動実装は停止しました。テストが通りませんでした。`/status` で状態を確認してください。"
                    f"{details}"
                )
                return

            await thread.send("テストが通りました。ブランチを push して PR を作成します。")
            pushed = await asyncio.to_thread(
                self._commit_and_push, workspace_info["workspace"], workspace_info["branch_name"], issue["number"]
            )
            if not pushed:
                self.state_store.update_status(thread_id, "failed")
                await thread.send("変更差分が作られなかったため、PR 作成を中止しました。")
                return
            pr = await asyncio.to_thread(
                self.github_client.create_pull_request,
                repo_full_name=repo_full_name,
                title=f"[自動生成] Issue #{issue['number']}: {summary.get('goal', '要件対応')}",
                body=self._build_pr_body(issue, thread.jump_url, result),
                head=workspace_info["branch_name"],
                base=workspace_info["base_branch"],
                draft=True,
            )
            self.state_store.write_artifact(thread_id, "pr.json", pr)
            self.state_store.update_meta(thread_id, status="completed", pr_number=str(pr["number"]), pr_url=pr["url"])
            await thread.send(f"PR を作成しました。\n- PR: #{pr['number']}\n- URL: {pr['url']}")
        except subprocess.CalledProcessError as exc:
            self.state_store.update_status(thread_id, "failed")
            await thread.send(f"コマンド実行で失敗しました: `{exc}`")
        except Exception as exc:
            self.state_store.update_status(thread_id, "failed")
            await thread.send(f"パイプラインが失敗しました: `{exc}`")

    async def _monitor_local_run(
        self,
        thread_id: int,
        thread: discord.Thread,
        process: subprocess.Popen[str],
        artifacts_dir: str,
    ) -> None:
        artifacts_path = Path(artifacts_dir)
        activity_path = artifacts_path / "current_activity.json"
        history_path = artifacts_path / "activity_history.json"
        last_sequence = 0
        last_sent_at = 0.0
        loop = asyncio.get_running_loop()

        while True:
            returncode = await asyncio.to_thread(process.poll)
            current = self._read_json(activity_path)
            if current:
                self.state_store.write_artifact(thread_id, "current_activity.json", current)
                sequence = int(current.get("sequence", 0))
                notify = bool(current.get("notify"))
                if notify and sequence > last_sequence:
                    now = loop.time()
                    tool_name = str(current.get("tool_name", ""))
                    if tool_name in {"Bash", "Task"} or now - last_sent_at >= 5:
                        await thread.send(self._format_activity_message(current))
                        last_sent_at = now
                    last_sequence = sequence
            history = self._read_json(history_path)
            if isinstance(history, list):
                self.state_store.write_artifact(thread_id, "activity_history.json", {"items": history[-20:]})
            if returncode is not None:
                break
            await asyncio.sleep(2)

    def _persist_activity_artifacts(self, thread_id: int, artifacts_dir: str) -> None:
        for filename in ("current_activity.json", "activity_history.json", "agent_failure.json", "last_failure.json"):
            payload = self.local_runner.load_optional_artifact(artifacts_dir, filename)
            if payload:
                self.state_store.write_artifact(thread_id, filename, payload)

    def _read_json(self, path: Path) -> dict | list | None:
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None

    def _format_activity_message(self, activity: dict) -> str:
        tool_name = str(activity.get("tool_name", ""))
        status = str(activity.get("status", ""))
        summary = str(activity.get("summary", "")).strip()
        phase = str(activity.get("phase", "tool"))
        status_label = {
            "started": "開始",
            "completed": "完了",
            "failed": "失敗",
        }.get(status, status)
        label = "サブエージェント" if phase == "subagent" else "ツール"
        return f"進捗更新\n- {label}: `{tool_name}`\n- 状態: {status_label}\n- 内容: {summary}"

    def _commit_and_push(self, workspace: str, branch_name: str, issue_number: int) -> bool:
        status = subprocess.run(
            ["git", "-C", workspace, "status", "--short"],
            check=True,
            capture_output=True,
            text=True,
        )
        if not status.stdout.strip():
            return False
        subprocess.run(["git", "-C", workspace, "add", "-A"], check=True)
        subprocess.run(
            ["git", "-C", workspace, "commit", "-m", f"Implement issue #{issue_number} by dev-bot"],
            check=True,
        )
        self.workspace_manager.push_branch(workspace, branch_name)
        return True

    def _build_pr_body(self, issue: dict, thread_url: str, result: dict) -> str:
        command = result.get("test_result", {}).get("command", "")
        return (
            f"## 概要\n"
            f"Issue #{issue['number']} に基づき、自動で要件整理・テスト作成・実装を行いました。\n\n"
            f"## テスト\n"
            f"- 実行コマンド: `{command}`\n"
            f"- 結果: 成功\n\n"
            f"## 補足\n"
            f"- Discordスレッド: {thread_url}\n"
            f"- 自動生成PRです\n"
        )
