from __future__ import annotations

from typing import ClassVar

from app.contracts.artifact_models import RepoExplorerV1
from app.planning.roles.base import JsonRoleRunner


class RepoExplorer(JsonRoleRunner):
    OUTPUT_SCHEMA: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "candidate_files": {"type": "array", "items": {"type": "string"}},
            "similar_files": {"type": "array", "items": {"type": "string"}},
            "architectural_notes": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["candidate_files", "similar_files", "architectural_notes"],
        "additionalProperties": False,
    }

    async def run(self, pack) -> RepoExplorerV1:
        acceptance = "\n".join(f"- {item}" for item in pack.acceptance_hints) or "- none provided"
        docs = "\n".join(f"- {item}" for item in pack.extra_docs) or "- none provided"
        prompt = (
            f"Issue: {pack.issue_key}\n\n"
            f"Issue body:\n{pack.issue_body}\n\n"
            f"Workpad:\n{pack.workpad_text}\n\n"
            f"Acceptance hints:\n{acceptance}\n\n"
            f"Extra docs to consult:\n{docs}\n\n"
            "Task:\n"
            "- identify candidate files\n"
            "- list similar files\n"
            "- summarize architectural notes\n"
            "Return JSON only."
        )
        payload = self.run_json(
            prompt=prompt,
            cwd=pack.repo_root,
            output_schema=self.OUTPUT_SCHEMA,
            prompt_kind="planner_repo_explorer",
        )
        return RepoExplorerV1(**payload)
