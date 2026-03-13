from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ContextPack:
    issue_key: str
    repo_root: str
    workpad_text: str
    issue_body: str
    acceptance_hints: list[str]
    extra_docs: list[str]


@dataclass(frozen=True, slots=True)
class CommitteeContext:
    repo_pack: ContextPack
    risk_pack: ContextPack
    constraint_pack: ContextPack

    def merge_pack(self, *, repo_out: Any, risk_out: Any, constraint_out: Any) -> dict[str, Any]:
        return {
            "issue_key": self.repo_pack.issue_key,
            "repo_root": self.repo_pack.repo_root,
            "issue_body": self.repo_pack.issue_body,
            "workpad_text": self.repo_pack.workpad_text,
            "repo_out": repo_out,
            "risk_out": risk_out,
            "constraint_out": constraint_out,
        }


class CommitteeContextBuilder:
    @staticmethod
    def from_issue(issue_ctx: Any) -> CommitteeContext:
        issue_key = str(issue_ctx.issue_key)
        repo_root = str(issue_ctx.repo_root)
        workpad_text = str(getattr(issue_ctx, "workpad_text", ""))
        issue_body = str(getattr(issue_ctx, "issue_body", ""))
        acceptance_hints = [
            str(item) for item in (getattr(issue_ctx, "acceptance_hints", []) or []) if str(item).strip()
        ]
        extra_docs = [str(item) for item in (getattr(issue_ctx, "extra_docs", []) or []) if str(item).strip()]

        repo_pack = ContextPack(
            issue_key=issue_key,
            repo_root=repo_root,
            workpad_text=CommitteeContextBuilder._join_sections(
                "Repository-facing workpad context",
                workpad_text,
                CommitteeContextBuilder._bullet_section("Extra repository docs", extra_docs),
            ),
            issue_body=CommitteeContextBuilder._join_sections(
                issue_body,
                CommitteeContextBuilder._bullet_section("Acceptance criteria", acceptance_hints),
            ),
            acceptance_hints=acceptance_hints,
            extra_docs=extra_docs,
        )
        risk_pack = ContextPack(
            issue_key=issue_key,
            repo_root=repo_root,
            workpad_text=CommitteeContextBuilder._join_sections(
                CommitteeContextBuilder._bullet_section("Acceptance criteria", acceptance_hints),
                CommitteeContextBuilder._bullet_section("Risk-focused notes", extra_docs),
            ),
            issue_body=issue_body,
            acceptance_hints=acceptance_hints,
            extra_docs=extra_docs,
        )
        constraint_pack = ContextPack(
            issue_key=issue_key,
            repo_root=repo_root,
            workpad_text=CommitteeContextBuilder._join_sections(
                CommitteeContextBuilder._bullet_section("Constraint notes", extra_docs),
                workpad_text,
            ),
            issue_body=CommitteeContextBuilder._join_sections(
                issue_body,
                CommitteeContextBuilder._bullet_section("Acceptance criteria", acceptance_hints),
            ),
            acceptance_hints=acceptance_hints,
            extra_docs=extra_docs,
        )
        return CommitteeContext(repo_pack=repo_pack, risk_pack=risk_pack, constraint_pack=constraint_pack)

    @staticmethod
    def _bullet_section(title: str, items: list[str]) -> str:
        if not items:
            return ""
        return "\n".join([f"{title}:", *[f"- {item}" for item in items]])

    @staticmethod
    def _join_sections(*sections: str) -> str:
        normalized = [section.strip() for section in sections if section and section.strip()]
        return "\n\n".join(normalized)
