from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from app.agent_sdk_client import ClaudeAgentClient
from app.config import Settings
from app.requirements_flow import RequirementReply, RequirementsFlow


SYSTEM_PROMPT = """あなたはソフトウェア開発の要件整理エージェントです。
Discordスレッド内の会話履歴を読み、次に返すべき内容を決めてください。

目的:
- ユーザーの自由文から、実装可能な要件へ整理する
- 不足している情報だけを、可能な限り1回でまとめて質問する
- 情報が十分なら要約して /confirm に進ませる
- 最大5ターンで一度整理済み要件を提示する

ルール:
- 日本語で返す
- 1回の応答で質問は最大5個
- 質問する場合は、未確定事項を1個ずつ小出しにせず、必要な確認事項を箇条書きまたは番号付きでまとめて出す
- すでに十分に分かっていることを繰り返し聞かない
- 実装詳細を決め打ちしない
- turn_count が5以上なら、未確定事項があっても整理済み要件を提示する
- reply はDiscordにそのまま送れる自然な文章にする

必ず次のJSONだけを返してください。
{
  "status": "questioning" または "ready_for_confirmation",
  "reply": "Discordにそのまま送る本文",
  "summary": {
    "background": "背景",
    "goal": "目的",
    "in_scope": ["やること"],
    "out_of_scope": ["やらないこと"],
    "acceptance_criteria": ["受け入れ条件"],
    "constraints": ["制約"],
    "test_focus": ["テスト観点"],
    "open_questions": ["未確定事項"]
  }
}
"""


@dataclass(frozen=True)
class RequirementsAgent:
    settings: Settings
    runs_root: str | None = None

    def build_reply(self, thread_id: int) -> RequirementReply:
        messages = self._load_messages(thread_id)
        if not messages:
            return RequirementReply(body="会話履歴が見つかりません。", status="requirements_error")

        try:
            payload = self._query_model(messages)
            status = payload.get("status", "questioning")
            body = self._build_body(payload).strip()
            if not body:
                raise ValueError("empty reply")
            mapped_status = "ready_for_confirmation" if status == "ready_for_confirmation" else "requirements_dialogue"
            artifacts = None
            if isinstance(payload.get("summary"), dict):
                artifacts = {"summary": payload["summary"]}
            return RequirementReply(body=body, status=mapped_status, artifacts=artifacts)
        except Exception as exc:
            print(f"RequirementsAgent fallback: {exc}")
            debug_artifacts = {
                "agent_error": {
                    "message": str(exc),
                    "type": type(exc).__name__,
                    "thread_id": thread_id,
                }
            }
            if os.getenv("ENABLE_REQUIREMENTS_FLOW_FALLBACK", "").lower() in {"1", "true", "yes"}:
                reply = RequirementsFlow(runs_root=self._runs_root).build_reply(thread_id)
                merged = dict(reply.artifacts or {})
                merged.update(debug_artifacts)
                return RequirementReply(body=reply.body, status=reply.status, artifacts=merged)
            return RequirementReply(
                body=(
                    "要件整理 agent の実行に失敗しました。固定質問には切り替えていません。\n"
                    "Claude Code の認証状態か SDK 実行環境を確認してください。"
                ),
                status="requirements_error",
                artifacts=debug_artifacts,
            )

    @property
    def _runs_root(self) -> str:
        return self.runs_root or self.settings.runs_root

    def _load_messages(self, thread_id: int) -> list[dict]:
        path = Path(self._runs_root) / str(thread_id) / "conversation.jsonl"
        rows: list[dict] = []
        if not path.exists():
            return rows
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
        return rows

    def _query_model(self, messages: list[dict]) -> dict:
        timeout_seconds = float(os.getenv("REQUIREMENTS_AGENT_TIMEOUT_SECONDS", "90"))
        client = ClaudeAgentClient(
            api_key=self.settings.anthropic_api_key,
            timeout_seconds=timeout_seconds,
        )
        turn_count = sum(1 for row in messages if row.get("role") == "user")
        conversation = "\n".join(
            f"{row['role']}: {row['content']}" for row in messages if row.get("content", "").strip()
        )
        return client.json_response(
            SYSTEM_PROMPT,
            (
                "以下はDiscordスレッドの会話履歴です。\n"
                "これを元に、次に返すべき本文を判断してください。\n"
                f"現在のuser turn数: {turn_count}\n\n"
                f"{conversation}"
            ),
            max_turns=1,
        )

    def _build_body(self, payload: dict) -> str:
        status = payload.get("status", "questioning")
        summary = payload.get("summary")
        if status == "ready_for_confirmation":
            return str(payload.get("reply", ""))

        if isinstance(summary, dict):
            open_questions = summary.get("open_questions", [])
            if isinstance(open_questions, list):
                normalized = [str(item).strip() for item in open_questions if str(item).strip()]
                if normalized:
                    lines = "\n".join(f"{index}. {question}" for index, question in enumerate(normalized[:5], start=1))
                    return (
                        "不足している情報をまとめて確認します。分かる範囲でまとめて回答してください。\n\n"
                        f"{lines}"
                    )

        return str(payload.get("reply", ""))
