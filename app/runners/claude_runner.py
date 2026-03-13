from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from app.agent_sdk_client import ClaudeAgentClient
from app.contracts.artifact_models import ReviewFinding, ReviewFindingsV1
from app.review.orchestrator import ReviewOrchestrator

VERIFICATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["status", "failure_type", "retry_recommended", "human_check_recommended", "notes"],
    "properties": {
        "status": {"type": "string"},
        "failure_type": {"type": "string"},
        "retry_recommended": {"type": "boolean"},
        "human_check_recommended": {"type": "boolean"},
        "notes": {"type": "array", "items": {"type": "string"}},
        "command_results_summary": {"type": "array", "items": {"type": "string"}},
    },
}

REVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["decision", "unnecessary_changes", "test_gaps", "risk_items", "protected_path_touches"],
    "properties": {
        "decision": {"type": "string"},
        "unnecessary_changes": {"type": "array", "items": {"type": "string"}},
        "test_gaps": {"type": "array", "items": {"type": "string"}},
        "risk_items": {"type": "array", "items": {"type": "string"}},
        "protected_path_touches": {"type": "array", "items": {"type": "string"}},
    },
}

REVIEW_FINDINGS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["findings"],
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "required": [
                    "id",
                    "severity",
                    "origin",
                    "confidence",
                    "file",
                    "line_start",
                    "line_end",
                    "claim",
                    "evidence",
                    "verifier_status",
                ],
                "properties": {
                    "id": {"type": "string"},
                    "severity": {"type": "string"},
                    "origin": {"type": "string"},
                    "confidence": {"type": "number"},
                    "file": {"type": "string"},
                    "line_start": {"type": "integer"},
                    "line_end": {"type": "integer"},
                    "claim": {"type": "string"},
                    "evidence": {"type": "array", "items": {"type": "string"}},
                    "verifier_status": {"type": "string"},
                    "suggested_fix": {"type": ["string", "null"]},
                },
                "additionalProperties": False,
            },
        }
    },
    "additionalProperties": False,
}


class _ClaudeReviewRole:
    def __init__(self, client: ClaudeAgentClient, *, role_name: str, task_lines: list[str]) -> None:
        self.client = client
        self.role_name = role_name
        self.task_lines = task_lines

    async def run(self, ctx: Any) -> ReviewFindingsV1:
        prompt = (
            f"You are the {self.role_name}.\n"
            "Read the diff and repository context, then return JSON only.\n\n"
            f"git_diff:\n{ctx.git_diff[-6000:]}\n\n"
            f"changed_files:\n{json.dumps(ctx.changed_files, ensure_ascii=False, indent=2)}\n\n"
            f"verification_summary:\n{json.dumps(ctx.verification_summary, ensure_ascii=False, indent=2)}\n\n"
            f"plan:\n{json.dumps(ctx.plan, ensure_ascii=False, indent=2)}\n\n"
            f"test_plan:\n{json.dumps(ctx.test_plan, ensure_ascii=False, indent=2)}\n\n"
            "Task:\n"
            + "\n".join(f"- {line}" for line in self.task_lines)
            + "\nReturn JSON only."
        )
        payload = self.client.json_response(
            system="You are a code review specialist. Return findings JSON only.",
            prompt=prompt,
            cwd=ctx.workspace,
            max_turns=2,
            allowed_tools=["Read", "Grep", "Glob", "Skill"],
            permission_mode="acceptEdits",
            setting_sources=["user", "project"],
            output_schema=REVIEW_FINDINGS_SCHEMA,
            prompt_kind=f"review_{self.role_name}",
        )
        return _review_findings_from_payload(payload)


class _ClaudeEvidenceVerifier:
    def __init__(self, client: ClaudeAgentClient) -> None:
        self.client = client

    async def run(self, ctx: Any, findings: ReviewFindingsV1) -> ReviewFindingsV1:
        if not findings.findings:
            return findings
        prompt = (
            "You verify code review findings.\n"
            "Mark each finding as confirmed or dismissed. Return JSON only.\n\n"
            f"git_diff:\n{ctx.git_diff[-6000:]}\n\n"
            f"findings:\n{json.dumps(_review_findings_to_payload(findings), ensure_ascii=False, indent=2)}"
        )
        payload = self.client.json_response(
            system="You are an evidence verifier. Confirm only findings that are supported by the diff.",
            prompt=prompt,
            cwd=ctx.workspace,
            max_turns=2,
            allowed_tools=["Read", "Grep", "Glob", "Skill"],
            permission_mode="acceptEdits",
            setting_sources=["user", "project"],
            output_schema=REVIEW_FINDINGS_SCHEMA,
            prompt_kind="review_evidence_verifier",
        )
        return _review_findings_from_payload(payload)


class _ClaudeFindingRanker:
    def __init__(self, client: ClaudeAgentClient) -> None:
        self.client = client

    async def run(self, ctx: Any, findings: ReviewFindingsV1) -> ReviewFindingsV1:
        if not findings.findings:
            return findings
        prompt = (
            "You rank and normalize code review findings.\n"
            "Adjust confidence, severity, and ordering if needed. Return JSON only.\n\n"
            f"verification_summary:\n{json.dumps(ctx.verification_summary, ensure_ascii=False, indent=2)}\n\n"
            f"findings:\n{json.dumps(_review_findings_to_payload(findings), ensure_ascii=False, indent=2)}"
        )
        payload = self.client.json_response(
            system="You are a review ranker. Return the final findings JSON only.",
            prompt=prompt,
            cwd=ctx.workspace,
            max_turns=2,
            allowed_tools=["Read", "Grep", "Glob", "Skill"],
            permission_mode="acceptEdits",
            setting_sources=["user", "project"],
            output_schema=REVIEW_FINDINGS_SCHEMA,
            prompt_kind="review_ranker",
        )
        return _review_findings_from_payload(payload)


def _review_findings_from_payload(payload: dict[str, Any]) -> ReviewFindingsV1:
    findings = []
    for item in payload.get("findings", []):
        findings.append(
            ReviewFinding(
                id=str(item.get("id", "")).strip(),
                severity=str(item.get("severity", "")).strip(),
                origin=str(item.get("origin", "")).strip(),
                confidence=float(item.get("confidence", 0.0)),
                file=str(item.get("file", "")).strip(),
                line_start=int(item.get("line_start", 0)),
                line_end=int(item.get("line_end", 0)),
                claim=str(item.get("claim", "")).strip(),
                evidence=[str(entry) for entry in item.get("evidence", []) if str(entry).strip()],
                verifier_status=str(item.get("verifier_status", "unverified")).strip() or "unverified",
                suggested_fix=(
                    str(item["suggested_fix"]).strip() if item.get("suggested_fix") is not None else None
                ),
            )
        )
    return ReviewFindingsV1(findings=findings)


def _review_findings_to_payload(findings: ReviewFindingsV1) -> dict[str, Any]:
    return {
        "findings": [
            {
                "id": finding.id,
                "severity": finding.severity,
                "origin": finding.origin,
                "confidence": finding.confidence,
                "file": finding.file,
                "line_start": finding.line_start,
                "line_end": finding.line_end,
                "claim": finding.claim,
                "evidence": list(finding.evidence),
                "verifier_status": finding.verifier_status,
                "suggested_fix": finding.suggested_fix,
            }
            for finding in findings.findings
        ]
    }


def _run_async(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class ClaudeRunner:
    def __init__(self, api_key: str | None, *, max_buffer_size: int | None = None) -> None:
        self.client = ClaudeAgentClient(api_key=api_key, timeout_seconds=180, max_buffer_size=max_buffer_size)

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
        ctx = type(
            "ReviewContext",
            (),
            {
                "workspace": workspace,
                "git_diff": git_diff,
                "changed_files": changed_files,
                "verification_summary": verification_summary,
                "plan": plan,
                "test_plan": test_plan,
                "thresholds": type("Thresholds", (), {"min_confidence_to_report": 0.8})(),
            },
        )()
        orchestrator = ReviewOrchestrator(
            diff_reviewer=_ClaudeReviewRole(
                self.client,
                role_name="diff_reviewer",
                task_lines=[
                    "Find introduced bugs or logic regressions in the diff.",
                    "Call out only concrete issues anchored to files and lines.",
                ],
            ),
            history_reviewer=_ClaudeReviewRole(
                self.client,
                role_name="history_reviewer",
                task_lines=[
                    "Check for changes that conflict with existing behavior or likely call contracts.",
                    "Focus on backward compatibility and data flow risks.",
                ],
            ),
            contract_reviewer=_ClaudeReviewRole(
                self.client,
                role_name="contract_reviewer",
                task_lines=[
                    "Check for workflow, policy, or protected-path violations in the diff.",
                    "Call out unnecessary or out-of-scope changes if present.",
                ],
            ),
            test_reviewer=_ClaudeReviewRole(
                self.client,
                role_name="test_reviewer",
                task_lines=[
                    "Identify missing tests or weak verification coverage for the changes.",
                    "Prefer concrete test gaps tied to acceptance criteria.",
                ],
            ),
            evidence_verifier=_ClaudeEvidenceVerifier(self.client),
            ranker=_ClaudeFindingRanker(self.client),
        )
        bundle = _run_async(orchestrator.run(ctx))
        return self._summarize_review_bundle(bundle)

    def _summarize_review_bundle(self, bundle: Any) -> dict[str, Any]:
        all_findings = list(bundle.all_findings.findings)
        postable_findings = list(bundle.postable_findings.findings)
        decision = "reject" if any(finding.severity.lower() == "high" for finding in postable_findings) else "approve"
        unnecessary_changes = [
            finding.claim
            for finding in all_findings
            if "unnecessary" in finding.claim.lower() or finding.origin == "contract_reviewer"
        ]
        test_gaps = [
            finding.claim
            for finding in all_findings
            if finding.origin == "test_reviewer" or "test" in finding.claim.lower()
        ]
        protected_path_touches = sorted(
            {
                finding.file
                for finding in all_findings
                if "protected" in finding.claim.lower() or "/.github/" in finding.file or finding.file.startswith(".github/")
            }
        )
        risk_items = [
            finding.claim
            for finding in all_findings
            if finding.claim not in unnecessary_changes and finding.claim not in test_gaps
        ]
        return {
            "decision": decision,
            "unnecessary_changes": unnecessary_changes,
            "test_gaps": test_gaps,
            "risk_items": risk_items,
            "protected_path_touches": protected_path_touches,
            "findings": _review_findings_to_payload(bundle.all_findings)["findings"],
            "postable_findings": _review_findings_to_payload(bundle.postable_findings)["findings"],
        }
