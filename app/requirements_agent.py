from __future__ import annotations

import json
import os
import re
from html import unescape
from urllib.error import URLError
from urllib.request import Request, urlopen
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.agent_sdk_client import ClaudeAgentClient
from app.config import Settings
from app.requirements_flow import RequirementReply, RequirementsFlow


SYSTEM_PROMPT = """あなたはソフトウェア開発の要件整理エージェントです。
Discordスレッド内の会話履歴を読み、次に返すべき内容を決めてください。

目的:
- ユーザーの自由文から、実装可能な要件へ整理する
- 不足している情報だけを、可能な限り1回でまとめて質問する
- 情報が十分なら要約して /plan に進ませる
- 最大5ターンで一度整理済み要件を提示する

ルール:
- 日本語で返す
- 1回の応答で質問は最大5個
- 質問する場合は、未確定事項を1個ずつ小出しにせず、必要な確認事項を箇条書きまたは番号付きでまとめて出す
- すでに十分に分かっていることを繰り返し聞かない
- 実装詳細を決め打ちしない
- turn_count が5以上なら、未確定事項があっても整理済み要件を提示する
- reply はDiscordにそのまま送れる自然な文章にする
- 会話内に明示された URL の内容が添付されていれば、それだけを参考にしてよい
- 一般的な web search はしない
- ツールは使わない

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

REQUIREMENTS_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": ["questioning", "ready_for_confirmation"],
        },
        "reply": {"type": "string"},
        "summary": {
            "anyOf": [
                {
                    "type": "object",
                    "properties": {
                        "background": {"type": "string"},
                        "goal": {"type": "string"},
                        "in_scope": {"type": "array", "items": {"type": "string"}},
                        "out_of_scope": {"type": "array", "items": {"type": "string"}},
                        "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
                        "constraints": {"type": "array", "items": {"type": "string"}},
                        "test_focus": {"type": "array", "items": {"type": "string"}},
                        "open_questions": {"type": "array", "items": {"type": "string"}}
                    }
                },
                {"type": "string"},
                {"type": "null"}
            ]
        },
    },
    "required": ["status", "reply"],
}


@dataclass(frozen=True)
class RequirementsAgent:
    settings: Settings
    runs_root: str | None = None

    def build_reply(self, thread_id: int) -> RequirementReply:
        messages = self._load_messages(thread_id)
        if not messages:
            return RequirementReply(body="会話履歴が見つかりません。", status="requirements_error")

        try:
            payload = self._normalize_payload(self._query_model(messages))
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
            if os.getenv("ENABLE_REQUIREMENTS_FLOW_FALLBACK", "true").lower() in {"1", "true", "yes"}:
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
        referenced_urls = _extract_urls(conversation)
        reference_materials = _collect_reference_materials(referenced_urls)
        prompt = (
            "以下はDiscordスレッドの会話履歴です。\n"
            "これを元に、次に返すべき本文を判断してください。\n"
            f"現在のuser turn数: {turn_count}\n\n"
            f"{conversation}"
        )
        if reference_materials:
            prompt += (
                "\n\n会話中に明示された参考URLの取得結果:\n"
                + "\n\n".join(_format_reference_material(item) for item in reference_materials)
                + "\n\n上記の参考資料だけを使ってよく、一般検索はしてはいけません。"
            )
        try:
            return client.json_response(
                SYSTEM_PROMPT,
                prompt,
                max_turns=1,
                allowed_tools=[],
                permission_mode="default",
                output_schema=REQUIREMENTS_SCHEMA,
            )
        except Exception:
            raw = client.run_text(
                SYSTEM_PROMPT,
                (
                    f"{prompt}\n\n"
                    "必ず JSON オブジェクトだけを返してください。"
                    " Markdown、前置き、説明文は禁止です。"
                ),
                max_turns=1,
                allowed_tools=[],
                permission_mode="default",
                output_schema=None,
            )
            text = raw.result.strip()
            if not text:
                raise RuntimeError("Requirements agent returned an empty response.")
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                extracted = _extract_json_object(text)
                if extracted is not None:
                    return extracted
                raise RuntimeError(f"Requirements agent did not return valid JSON. Raw response: {text[:500]!r}")

    def _normalize_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(payload)
        normalized["status"] = (
            "ready_for_confirmation"
            if str(normalized.get("status", "")).strip() == "ready_for_confirmation"
            else "questioning"
        )
        normalized["reply"] = str(normalized.get("reply", "")).strip()
        normalized["summary"] = self._normalize_summary(normalized.get("summary"))
        return normalized

    def _normalize_summary(self, summary: object) -> dict[str, Any]:
        if not isinstance(summary, dict):
            summary = {}
        return {
            "background": self._as_string(summary.get("background")),
            "goal": self._as_string(summary.get("goal")),
            "in_scope": self._as_string_list(summary.get("in_scope")),
            "out_of_scope": self._as_string_list(summary.get("out_of_scope")),
            "acceptance_criteria": self._as_string_list(summary.get("acceptance_criteria")),
            "constraints": self._as_string_list(summary.get("constraints")),
            "test_focus": self._as_string_list(summary.get("test_focus")),
            "open_questions": self._as_string_list(summary.get("open_questions")),
        }

    def _as_string(self, value: object) -> str:
        if value is None:
            return ""
        return str(value).strip()

    def _as_string_list(self, value: object) -> list[str]:
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        if isinstance(value, str) and value.strip():
            return [value.strip()]
        return []

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


def _extract_urls(text: str) -> list[str]:
    matches = re.findall(r"https?://[^\s)>\"']+", text)
    seen: set[str] = set()
    urls: list[str] = []
    for raw in matches:
        normalized = raw.rstrip(".,)]}")
        if normalized in seen:
            continue
        seen.add(normalized)
        urls.append(normalized)
    return urls


def _collect_reference_materials(urls: list[str]) -> list[dict[str, str]]:
    return [_fetch_reference_material(url) for url in urls[:3]]


def _fetch_reference_material(url: str) -> dict[str, str]:
    request = Request(
        url,
        headers={
            "User-Agent": "dev-bot/1.0",
        },
    )
    try:
        with urlopen(request, timeout=10) as response:
            raw = response.read(30000)
            content_type = response.headers.get("Content-Type", "")
    except URLError as exc:
        return {
            "url": url,
            "status": "error",
            "content": "",
            "error": str(exc),
        }
    text = raw.decode("utf-8", errors="replace")
    if "html" in content_type.lower():
        text = re.sub(r"(?is)<script.*?>.*?</script>", " ", text)
        text = re.sub(r"(?is)<style.*?>.*?</style>", " ", text)
        text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return {
        "url": url,
        "status": "ok",
        "content": text[:4000],
        "error": "",
    }


def _format_reference_material(item: dict[str, str]) -> str:
    if item.get("status") != "ok":
        return f"- URL: {item.get('url')}\n  status: error\n  error: {item.get('error')}"
    return (
        f"- URL: {item.get('url')}\n"
        "  status: ok\n"
        f"  excerpt: {item.get('content')}"
    )


def _extract_json_object(text: str) -> dict[str, Any] | None:
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None
