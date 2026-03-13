from __future__ import annotations

from typing import ClassVar

from app.contracts.artifact_models import ConstraintReportV1
from app.planning.roles.base import JsonRoleRunner


class ConstraintChecker(JsonRoleRunner):
    OUTPUT_SCHEMA: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "out_of_scope": {"type": "array", "items": {"type": "string"}},
            "protected_paths": {"type": "array", "items": {"type": "string"}},
            "constraints": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["out_of_scope", "protected_paths", "constraints"],
        "additionalProperties": False,
    }

    async def run(self, pack) -> ConstraintReportV1:
        acceptance = "\n".join(f"- {item}" for item in pack.acceptance_hints) or "- none provided"
        docs = "\n".join(f"- {item}" for item in pack.extra_docs) or "- none provided"
        prompt = (
            f"Issue: {pack.issue_key}\n\n"
            f"Issue body:\n{pack.issue_body}\n\n"
            f"Acceptance criteria:\n{acceptance}\n\n"
            f"Constraint docs:\n{docs}\n\n"
            f"Workpad:\n{pack.workpad_text}\n\n"
            "Task:\n"
            "- identify out-of-scope items\n"
            "- identify protected paths or change boundaries\n"
            "- identify explicit constraints\n"
            "Return JSON only."
        )
        payload = self.run_json(
            prompt=prompt,
            cwd=pack.repo_root,
            output_schema=self.OUTPUT_SCHEMA,
            prompt_kind="planner_constraints",
        )
        return ConstraintReportV1(**payload)
