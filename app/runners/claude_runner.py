from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.agent_sdk_client import ClaudeAgentClient


VERIFICATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["status", "failure_type", "retry_recommended", "human_check_recommended", "notes"],
    "properties": {
        "status": {"type": "string"},
        "failure_type": {"type": "string"},
        "retry_recommended": {"type": "boolean"},
        "human_check_recommended": {"type": "boolean"},
        "notes": {"type": "array", "items": {"type": "string"}},
        "command_results_summary": {"type": "array", "items": {"type": "string"}}
    }
}

REVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["decision", "unnecessary_changes", "test_gaps", "risk_items", "protected_path_touches"],
    "properties": {
        "decision": {"type": "string"},
        "unnecessary_changes": {"type": "array", "items": {"type": "string"}},
        "test_gaps": {"type": "array", "items": {"type": "string"}},
        "risk_items": {"type": "array", "items": {"type": "string"}},
        "protected_path_touches": {"type": "array", "items": {"type": "string"}}
    }
}


class ClaudeRunner:
    def __init__(self, api_key: str | None) -> None:
        self.client = ClaudeAgentClient(api_key=api_key, timeout_seconds=180)

    def verify(
        self,
        *,
        workspace: str,
        command_results: dict[str, Any],
        changed_files: dict[str, Any],
        codex_run_log_path: str,
        plan: dict[str, Any],
        test_plan: dict[str, Any],
    ) -> dict[str, Any]:
        log_excerpt = Path(codex_run_log_path).read_text(encoding="utf-8")[-4000:]
        prompt = (
            "You are the verification worker.\n"
            "Return JSON only.\n\n"
            f"command_results:\n{json.dumps(command_results, ensure_ascii=False, indent=2)}\n\n"
            f"changed_files:\n{json.dumps(changed_files, ensure_ascii=False, indent=2)}\n\n"
            f"plan:\n{json.dumps(plan, ensure_ascii=False, indent=2)}\n\n"
            f"test_plan:\n{json.dumps(test_plan, ensure_ascii=False, indent=2)}\n\n"
            f"codex_run_log_excerpt:\n{log_excerpt}"
        )
        return self.client.json_response(
            system="You are a verification specialist. Summarize outcome and failure taxonomy in JSON.",
            prompt=prompt,
            cwd=workspace,
            max_turns=2,
            allowed_tools=["Read", "Grep", "Glob", "Skill"],
            permission_mode="acceptEdits",
            setting_sources=["user", "project"],
            output_schema=VERIFICATION_SCHEMA,
        )

    def review(
        self,
        *,
        workspace: str,
        git_diff: str,
        changed_files: dict[str, Any],
        verification_summary: dict[str, Any],
        plan: dict[str, Any],
        test_plan: dict[str, Any],
    ) -> dict[str, Any]:
        prompt = (
            "You are the code review worker.\n"
            "Return JSON only.\n\n"
            f"git_diff:\n{git_diff[-6000:]}\n\n"
            f"changed_files:\n{json.dumps(changed_files, ensure_ascii=False, indent=2)}\n\n"
            f"verification_summary:\n{json.dumps(verification_summary, ensure_ascii=False, indent=2)}\n\n"
            f"plan:\n{json.dumps(plan, ensure_ascii=False, indent=2)}\n\n"
            f"test_plan:\n{json.dumps(test_plan, ensure_ascii=False, indent=2)}"
        )
        return self.client.json_response(
            system="You are a code review specialist. Identify risks and output JSON.",
            prompt=prompt,
            cwd=workspace,
            max_turns=2,
            allowed_tools=["Read", "Grep", "Glob", "Skill"],
            permission_mode="acceptEdits",
            setting_sources=["user", "project"],
            output_schema=REVIEW_SCHEMA,
        )
