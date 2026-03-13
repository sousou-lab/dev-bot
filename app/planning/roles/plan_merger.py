from __future__ import annotations

from typing import ClassVar

from app.contracts.artifact_models import DesignBranch, PlanTask, PlanV2, RiskItem, TestMappingItem
from app.planning.roles.base import JsonRoleRunner


class RiskTestPlannerSchema:
    @staticmethod
    def risks_schema() -> dict:
        return {
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
        }

    @staticmethod
    def test_mapping_schema() -> dict:
        return {
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
        }


class PlanMerger(JsonRoleRunner):
    OUTPUT_SCHEMA: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "goal": {"type": "string"},
            "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
            "out_of_scope": {"type": "array", "items": {"type": "string"}},
            "constraints": {"type": "array", "items": {"type": "string"}},
            "candidate_files": {"type": "array", "items": {"type": "string"}},
            "tasks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "summary": {"type": "string"},
                        "files": {"type": "array", "items": {"type": "string"}},
                        "done_when": {"type": "string"},
                    },
                    "required": ["id", "summary", "files", "done_when"],
                    "additionalProperties": False,
                },
            },
            "design_branches": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "summary": {"type": "string"},
                        "pros": {"type": "array", "items": {"type": "string"}},
                        "cons": {"type": "array", "items": {"type": "string"}},
                        "recommended": {"type": "boolean"},
                    },
                    "required": ["id", "summary", "pros", "cons", "recommended"],
                    "additionalProperties": False,
                },
            },
            "risks": RiskTestPlannerSchema.risks_schema(),
            "test_mapping": RiskTestPlannerSchema.test_mapping_schema(),
            "verification_profile": {"type": "string"},
            "planner_confidence": {"type": "number"},
        },
        "required": [
            "goal",
            "acceptance_criteria",
            "out_of_scope",
            "constraints",
            "candidate_files",
            "tasks",
            "design_branches",
            "risks",
            "test_mapping",
            "verification_profile",
            "planner_confidence",
        ],
        "additionalProperties": False,
    }

    async def run(self, merged_input: dict) -> PlanV2:
        prompt = (
            f"Issue: {merged_input['issue_key']}\n\n"
            f"Issue body:\n{merged_input['issue_body']}\n\n"
            f"Workpad:\n{merged_input['workpad_text']}\n\n"
            f"Repo explorer output:\n{merged_input['repo_out']}\n\n"
            f"Risk/test planner output:\n{merged_input['risk_out']}\n\n"
            f"Constraint checker output:\n{merged_input['constraint_out']}\n\n"
            "Task:\n"
            "- produce the canonical implementation plan\n"
            "- include design_branches\n"
            "- include planner_confidence\n"
            "Return JSON only."
        )
        payload = self.run_json(
            prompt=prompt,
            cwd=merged_input["repo_root"],
            output_schema=self.OUTPUT_SCHEMA,
            prompt_kind="planner_merger",
        )
        return PlanV2(
            goal=payload["goal"],
            acceptance_criteria=list(payload["acceptance_criteria"]),
            out_of_scope=list(payload["out_of_scope"]),
            constraints=list(payload["constraints"]),
            candidate_files=list(payload["candidate_files"]),
            tasks=[PlanTask(**item) for item in payload["tasks"]],
            design_branches=[DesignBranch(**item) for item in payload["design_branches"]],
            risks=[RiskItem(**item) for item in payload["risks"]],
            test_mapping=[TestMappingItem(**item) for item in payload["test_mapping"]],
            verification_profile=payload["verification_profile"],
            planner_confidence=float(payload["planner_confidence"]),
        )
