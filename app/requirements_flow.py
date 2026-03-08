from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path


QUESTIONS = [
    ("change_type", "これは新機能ですか、それとも既存機能の改善ですか？"),
    ("completion", "完了条件は何ですか？"),
    ("out_of_scope", "今回対象外にしたいものはありますか？"),
    ("users", "誰が使う機能ですか？ 想定ユーザーを教えてください。"),
    ("constraints", "絶対に守りたい制約はありますか？ 例: APIは変えない、DB変更なし"),
]


@dataclass(frozen=True)
class RequirementReply:
    body: str
    status: str
    artifacts: dict | None = None


class RequirementsFlow:
    def __init__(self, runs_root: str = "runs") -> None:
        self.runs_root = Path(runs_root)

    def build_reply(self, thread_id: int) -> RequirementReply:
        messages = self._load_messages(thread_id)
        answers = self._map_answers(messages)

        pending_questions = self._find_pending_questions(answers)
        if pending_questions:
            answered = len([value for value in answers.values() if value])
            prompts = "\n".join(f"{index}. {prompt}" for index, (_, prompt) in enumerate(pending_questions, start=1))
            body = (
                "不足している情報をまとめて確認します。分かる範囲でまとめて回答してください。\n\n"
                f"{prompts}\n\n"
                f"現在の整理状況: {answered}/{len(QUESTIONS)}"
            )
            first_key = pending_questions[0][0]
            return RequirementReply(body=body, status=f"requirements_{first_key}", artifacts=None)

        summary = self._build_summary(answers)
        body = (
            "整理済み要件です。\n\n"
            f"{summary}\n\n"
            "この内容で進める場合は `/confirm`、修正したい場合はそのまま追記してください。"
        )
        return RequirementReply(
            body=body,
            status="ready_for_confirmation",
            artifacts={
                "summary": {
                    "change_type": answers["change_type"],
                    "completion": answers["completion"],
                    "out_of_scope": answers["out_of_scope"],
                    "users": answers["users"],
                    "constraints": answers["constraints"],
                }
            },
        )

    def _load_messages(self, thread_id: int) -> list[dict]:
        path = self.runs_root / str(thread_id) / "conversation.jsonl"
        if not path.exists():
            return []
        rows: list[dict] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
        return rows

    def _map_answers(self, messages: list[dict]) -> dict[str, str]:
        answers = {key: "" for key, _ in QUESTIONS}
        question_index = 0
        for message in messages:
            content = message.get("content", "").strip()
            if not content:
                continue
            if message.get("role") == "assistant":
                continue
            if not answers["change_type"]:
                answers["change_type"] = content
                question_index = 1
                continue

            pending = [key for key, _ in QUESTIONS[question_index:] if not answers[key]]
            if not pending:
                break

            consumed = self._assign_by_labels(answers, content)
            consumed = self._assign_grouped_shortcuts(answers, content) or consumed
            if consumed:
                continue

            answers[pending[0]] = content
            question_index = max(question_index, QUESTIONS.index((pending[0], dict(QUESTIONS)[pending[0]])) + 1)
        return answers

    def _assign_by_labels(self, answers: dict[str, str], text: str) -> bool:
        normalized = text.strip()
        assignments = [
            ("completion", ("完了条件",)),
            ("out_of_scope", ("対象外",)),
            ("users", ("ユーザー", "想定ユーザー", "利用者", "誰が使う")),
            ("constraints", ("制約", "守りたい", "DB変更なし", "APIは変えない")),
        ]
        matched = False
        for key, labels in assignments:
            if answers[key]:
                continue
            if any(label in normalized for label in labels):
                value = self._extract_labeled_value(normalized, labels)
                if value:
                    answers[key] = value
                    matched = True
        return matched

    def _extract_labeled_value(self, text: str, labels: tuple[str, ...]) -> str:
        for label in labels:
            pattern = rf"{re.escape(label)}(?:は|:|：)?\s*(.+?)(?=(?:完了条件|対象外|想定ユーザー|ユーザー|利用者|誰が使う|制約|守りたい|$))"
            match = re.search(pattern, text)
            if match:
                return match.group(1).strip(" 　、,。")
        return ""

    def _assign_grouped_shortcuts(self, answers: dict[str, str], text: str) -> bool:
        normalized = text.replace(" ", "").replace("　", "")
        matched = False
        grouped_patterns = [
            (("out_of_scope", "users", "constraints"), r"([2-4]{2,3})は(.+)"),
            (("out_of_scope", "users"), r"(23|32)は(.+)"),
            (("out_of_scope", "constraints"), r"(24|42)は(.+)"),
            (("users", "constraints"), r"(34|43)は(.+)"),
            (("out_of_scope", "users", "constraints"), r"2[-,、]4は(.+)"),
        ]
        for keys, pattern in grouped_patterns:
            match = re.search(pattern, normalized)
            if not match:
                continue
            value = match.group(match.lastindex or 0).strip("、,。")
            target_keys = keys
            if pattern.startswith("("):
                digits = match.group(1)
                target_keys = tuple(_digits_to_keys(digits))
                value = match.group(2).strip("、,。")
            for key in target_keys:
                if not answers[key]:
                    answers[key] = value
                    matched = True
        return matched

    def _find_pending_questions(self, answers: dict[str, str]) -> list[tuple[str, str]]:
        return [(key, prompt) for key, prompt in QUESTIONS if not answers[key]]

    def _build_summary(self, answers: dict[str, str]) -> str:
        return "\n".join(
            [
                f"- 種別: {answers['change_type']}",
                f"- 完了条件: {answers['completion']}",
                f"- 対象外: {answers['out_of_scope']}",
                f"- 想定ユーザー: {answers['users']}",
                f"- 制約: {answers['constraints']}",
            ]
        )
