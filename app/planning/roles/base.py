from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any

from app.agent_sdk_client import ClaudeAgentClient


def _normalize_payload(value: Any) -> Any:
    if is_dataclass(value):
        return asdict(value)
    return value


class JsonRoleRunner:
    def __init__(
        self,
        client: ClaudeAgentClient | None = None,
        *,
        allowed_tools: list[str] | None = None,
        setting_sources: list[str] | None = None,
        permission_mode: str = "default",
    ) -> None:
        self.client = client or ClaudeAgentClient()
        self.allowed_tools = list(allowed_tools or ["Read", "Grep", "Glob"])
        self.setting_sources = list(setting_sources or ["project"])
        self.permission_mode = permission_mode

    def run_json(
        self,
        *,
        prompt: str,
        cwd: str,
        output_schema: dict[str, Any],
        prompt_kind: str,
    ) -> dict[str, Any]:
        system = {
            "role": "planner",
            "rules": [
                "Use only read-only investigation.",
                "Return JSON that matches the schema exactly.",
            ],
        }
        return self.client.json_response(
            system=system,
            prompt=prompt,
            cwd=cwd,
            max_turns=1,
            allowed_tools=self.allowed_tools,
            permission_mode=self.permission_mode,
            setting_sources=self.setting_sources,
            output_schema=output_schema,
            prompt_kind=prompt_kind,
        )
