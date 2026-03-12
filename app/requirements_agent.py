from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from html import unescape
from pathlib import Path
from typing import Any
from urllib.error import URLError

from app.logging_setup import get_logger

logger = get_logger(__name__)
from urllib.request import Request, urlopen

from app.agent_sdk_client import ClaudeAgentClient
from app.config import Settings
from app.requirements_flow import RequirementReply, RequirementsFlow

PREFERENCE_ENUMS = {
    "priority_preference": [
        "minimal_change",
        "speed",
        "maintainability",
        "quality",
        "balanced",
        "unknown",
    ],
    "change_scope_preference": [
        "ui_only",
        "ui_and_api",
        "api_allowed",
        "db_allowed",
        "broad_change_allowed",
        "unknown",
    ],
    "compatibility_preference": [
        "preserve_current_behavior",
        "preserve_api",
        "preserve_db_schema",
        "backward_compatibility_required",
        "breaking_change_acceptable",
        "unknown",
    ],
    "risk_tolerance": ["low", "medium", "high", "unknown"],
    "delivery_expectation": [
        "prototype_ok",
        "production_ready",
        "tests_required",
        "reviewability_priority",
        "unknown",
    ],
}

SYSTEM_PROMPT = """あなたはソフトウェア開発の要件整理エージェントです。
Discordスレッド内の会話履歴を読み、次に返すべき内容を決めてください。

目的:
- ユーザーの自由文から、実装可能な要件へ整理する
- 不足している情報だけを、可能な限り1回でまとめて質問する
- 情報が十分なら整理済み要件を提示して /plan に進ませる
- planning の質を上げるため、ユーザーの選好や懸念を判断材料として整理する
- 最大5ターンで一度整理済み要件を提示する

責務:
- 何を作るかを整理する
- 完了条件、制約、対象範囲、対象外を整理する
- ユーザーが重視する結果、避けたい方向、許容するトレードオフを整理する
- 必要な場合のみ、選択式の質問で曖昧さを圧縮する
- 高レベルな推奨方向を短くまとめてもよい
- ただし、具体的な実装手順やファイル単位の計画は作らない

禁止事項:
- 実装ステップを列挙しない
- 変更対象ファイルを推測しない
- 技術選定を断定しない
- 詳細なテスト計画を作らない
- 設計を確定事項として押し付けない
- 一般的な web search はしない
- ツールは使わない

会話ルール:
- 日本語で返す
- 質問する場合は、未確定事項を1個ずつ小出しにせず、必要な確認事項を箇条書きまたは番号付きでまとめて出す
- すでに十分に分かっていることを繰り返し聞かない
- 自由記述で聞くべきことと、選択式で圧縮できることを区別する
- 選択式は必要なときだけ使う
- 選択式は原則2項目まで、複雑案件のみ3項目まで許容
- 各選択項目の選択肢は原則3個、必要な場合のみ4-5個まで許容
- 選択式を使う場合も、自由記述で補足できるようにする
- turn_count が5以上なら、未確定事項があっても整理済み要件を提示する
- reply はDiscordにそのまま送れる自然な文章にする
- 会話内に明示された URL の内容が添付されていれば、それだけを参考にしてよい

高レベル方向づけのルール:
- recommended_direction は高レベルな方向性だけを書く
- 例: 「既存互換を優先して最小変更で進める」「保守性を優先して多少広めの変更を許容する」
- 実装手順、変更ファイル、具体技術、詳細設計は書かない

複雑度の扱い:
- simple: 単純な変更、明確な要件、比較検討が不要
- complex: 制約や受け入れ条件が複数ある、既存影響が広い、トレードオフが大きい、比較検討が必要
- complex の場合のみ solution_options を出してよい
- simple の場合は solution_options は空でよい

preferences の正規化:
- priority_preference: minimal_change / speed / maintainability / quality / balanced / unknown
- change_scope_preference: ui_only / ui_and_api / api_allowed / db_allowed / broad_change_allowed / unknown
- compatibility_preference: preserve_current_behavior / preserve_api / preserve_db_schema / backward_compatibility_required / breaking_change_acceptable / unknown
- risk_tolerance: low / medium / high / unknown
- delivery_expectation: prototype_ok / production_ready / tests_required / reviewability_priority / unknown

decision_hints のルール:
- 選択式質問を使った場合のみ記録する
- question には質問テーマを書く
- selected_label にはユーザーに見せた選択肢文言を書く
- normalized_key / normalized_value には正規化結果を書く
- source は user_selection または inferred_from_message とする

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
    "open_questions": ["未確定事項"],
    "preferred_outcomes": ["重視する結果"],
    "tradeoffs": ["許容するトレードオフ"],
    "disallowed_approaches": ["避けたい進め方"],
    "assumptions_to_validate": ["planningで確認すべき前提"],
    "recommended_direction": "高レベルな推奨方向",
    "complexity": "simple または complex",
    "preferences": {
      "priority_preference": "unknown",
      "change_scope_preference": "unknown",
      "compatibility_preference": "unknown",
      "risk_tolerance": "unknown",
      "delivery_expectation": "unknown"
    },
    "decision_hints": [
      {
        "question": "質問テーマ",
        "selected_label": "選ばれた文言",
        "normalized_key": "priority_preference",
        "normalized_value": "speed",
        "source": "user_selection"
      }
    ],
    "solution_options": [
      {
        "name": "候補名",
        "summary": "案の概要",
        "pros": ["利点"],
        "cons": ["欠点"]
      }
    ]
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
                        "open_questions": {"type": "array", "items": {"type": "string"}},
                        "preferred_outcomes": {"type": "array", "items": {"type": "string"}},
                        "tradeoffs": {"type": "array", "items": {"type": "string"}},
                        "disallowed_approaches": {"type": "array", "items": {"type": "string"}},
                        "assumptions_to_validate": {"type": "array", "items": {"type": "string"}},
                        "recommended_direction": {"type": "string"},
                        "complexity": {"type": "string", "enum": ["simple", "complex"]},
                        "preferences": {
                            "type": "object",
                            "properties": {
                                "priority_preference": {
                                    "type": "string",
                                    "enum": PREFERENCE_ENUMS["priority_preference"],
                                },
                                "change_scope_preference": {
                                    "type": "string",
                                    "enum": PREFERENCE_ENUMS["change_scope_preference"],
                                },
                                "compatibility_preference": {
                                    "type": "string",
                                    "enum": PREFERENCE_ENUMS["compatibility_preference"],
                                },
                                "risk_tolerance": {
                                    "type": "string",
                                    "enum": PREFERENCE_ENUMS["risk_tolerance"],
                                },
                                "delivery_expectation": {
                                    "type": "string",
                                    "enum": PREFERENCE_ENUMS["delivery_expectation"],
                                },
                            },
                        },
                        "decision_hints": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "question": {"type": "string"},
                                    "selected_label": {"type": "string"},
                                    "normalized_key": {"type": "string"},
                                    "normalized_value": {"type": "string"},
                                    "source": {"type": "string"},
                                },
                            },
                        },
                        "solution_options": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "name": {"type": "string"},
                                    "summary": {"type": "string"},
                                    "pros": {"type": "array", "items": {"type": "string"}},
                                    "cons": {"type": "array", "items": {"type": "string"}},
                                },
                            },
                        },
                    },
                },
                {"type": "string"},
                {"type": "null"},
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
            logger.warning("RequirementsAgent fallback: %s", exc)
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
        path = _conversation_path(Path(self._runs_root), thread_id)
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
            max_buffer_size=self.settings.claude_agent_max_buffer_size,
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
                (f"{prompt}\n\n必ず JSON オブジェクトだけを返してください。 Markdown、前置き、説明文は禁止です。"),
                max_turns=1,
                allowed_tools=[],
                permission_mode="default",
                output_schema=None,
            )
            text = raw.result.strip()
            if not text:
                raise RuntimeError("Requirements agent returned an empty response.") from None
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                extracted = _extract_json_object(text)
                if extracted is not None:
                    return extracted
                raise RuntimeError(
                    f"Requirements agent did not return valid JSON. Raw response: {text[:500]!r}"
                ) from None

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
            "preferred_outcomes": self._as_string_list(summary.get("preferred_outcomes")),
            "tradeoffs": self._as_string_list(summary.get("tradeoffs")),
            "disallowed_approaches": self._as_string_list(summary.get("disallowed_approaches")),
            "assumptions_to_validate": self._as_string_list(summary.get("assumptions_to_validate")),
            "recommended_direction": self._as_string(summary.get("recommended_direction")),
            "complexity": self._normalize_complexity(summary.get("complexity")),
            "preferences": self._normalize_preferences(summary.get("preferences")),
            "decision_hints": self._normalize_decision_hints(summary.get("decision_hints")),
            "solution_options": self._normalize_solution_options(summary.get("solution_options")),
        }

    def _normalize_complexity(self, value: object) -> str:
        normalized = str(value).strip()
        return normalized if normalized in {"simple", "complex"} else "simple"

    def _normalize_preferences(self, preferences: object) -> dict[str, str]:
        source = preferences if isinstance(preferences, dict) else {}
        normalized: dict[str, str] = {}
        for key, allowed_values in PREFERENCE_ENUMS.items():
            value = str(source.get(key, "")).strip()
            normalized[key] = value if value in allowed_values else "unknown"
        return normalized

    def _normalize_decision_hints(self, hints: object) -> list[dict[str, str]]:
        if not isinstance(hints, list):
            return []
        normalized: list[dict[str, str]] = []
        for item in hints:
            if not isinstance(item, dict):
                continue
            normalized_key = str(item.get("normalized_key", "")).strip()
            normalized_value = str(item.get("normalized_value", "")).strip()
            if normalized_key not in PREFERENCE_ENUMS:
                continue
            if normalized_value not in PREFERENCE_ENUMS[normalized_key]:
                continue
            entry = {
                "question": self._as_string(item.get("question")),
                "selected_label": self._as_string(item.get("selected_label")),
                "normalized_key": normalized_key,
                "normalized_value": normalized_value,
                "source": self._normalize_decision_hint_source(item.get("source")),
            }
            if not any(entry.values()):
                continue
            normalized.append(entry)
        return normalized

    def _normalize_decision_hint_source(self, value: object) -> str:
        normalized = str(value).strip()
        return normalized if normalized in {"user_selection", "inferred_from_message"} else ""

    def _normalize_solution_options(self, options: object) -> list[dict[str, Any]]:
        if not isinstance(options, list):
            return []
        normalized: list[dict[str, Any]] = []
        for item in options:
            if not isinstance(item, dict):
                continue
            entry = {
                "name": self._as_string(item.get("name")),
                "summary": self._as_string(item.get("summary")),
                "pros": self._as_string_list(item.get("pros")),
                "cons": self._as_string_list(item.get("cons")),
            }
            if any(
                (
                    entry["name"],
                    entry["summary"],
                    entry["pros"],
                    entry["cons"],
                )
            ):
                normalized.append(entry)
        return normalized

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
                    return f"不足している情報をまとめて確認します。分かる範囲でまとめて回答してください。\n\n{lines}"

        return str(payload.get("reply", ""))


def _conversation_path(runs_root: Path, thread_id: int) -> Path:
    binding_path = runs_root / "bindings" / "discord_threads" / f"{thread_id}.json"
    if binding_path.exists():
        try:
            payload = json.loads(binding_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            payload = {}
        issue_key = str(payload.get("issue_key", "")).strip()
        if issue_key:
            safe_issue_key = issue_key.replace("/", "__").replace("#", "__")
            issue_path = runs_root / "issues" / safe_issue_key / "conversation.jsonl"
            if issue_path.exists():
                return issue_path
    draft_path = runs_root / "drafts" / str(thread_id) / "conversation.jsonl"
    if draft_path.exists():
        return draft_path
    return runs_root / str(thread_id) / "conversation.jsonl"


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
    return f"- URL: {item.get('url')}\n  status: ok\n  excerpt: {item.get('content')}"


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
