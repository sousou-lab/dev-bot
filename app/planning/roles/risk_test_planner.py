from __future__ import annotations

from typing import ClassVar

from app.contracts.artifact_models import RiskItem, RiskTestPlanV1, TestMappingItem
from app.planning.roles.base import JsonRoleRunner


class RiskTestPlanner(JsonRoleRunner):
    OUTPUT_SCHEMA: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "risks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "risk": {"type": "string"},
                        "mitigation": {"type": "string"},
                    },
                    "required": ["risk", "mitigation"],
                    "additionalProperties": False,
                },
            },
            "test_mapping": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "criterion": {"type": "string"},
                        "tests": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["criterion", "tests"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["risks", "test_mapping"],
        "additionalProperties": False,
    }

    async def run(self, pack) -> RiskTestPlanV1:
        acceptance = "\n".join(f"- {item}" for item in pack.acceptance_hints) or "- none provided"
        docs = "\n".join(f"- {item}" for item in pack.extra_docs) or "- none provided"
        prompt = (
            f"Issue: {pack.issue_key}\n\n"
            f"Issue body:\n{pack.issue_body}\n\n"
            f"Acceptance criteria:\n{acceptance}\n\n"
            f"Relevant docs:\n{docs}\n\n"
            f"Workpad:\n{pack.workpad_text}\n\n"
            "Task:\n"
            "- identify implementation risks\n"
            "- map each acceptance criterion to tests\n"
            "Return JSON only."
        )
        payload = self.run_json(
            prompt=prompt,
            cwd=pack.repo_root,
            output_schema=self.OUTPUT_SCHEMA,
            prompt_kind="planner_risk_test",
        )
        return RiskTestPlanV1(
            risks=[RiskItem(**item) for item in payload["risks"]],
            test_mapping=[TestMappingItem(**item) for item in payload["test_mapping"]],
        )
