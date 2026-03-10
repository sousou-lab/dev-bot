from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class CommandExecution:
    command: str
    returncode: int
    output: str


@runtime_checkable
class ImplementationRunner(Protocol):
    """Protocol for runners that execute code implementation (e.g. Codex)."""

    def build_prompt(
        self,
        *,
        issue: dict,
        requirement_summary: dict,
        plan: dict,
        test_plan: dict,
        workflow_text: str,
    ) -> str: ...

    def run(
        self,
        *,
        prompt: str,
        cwd: str,
        writable_roots: list[str],
        thread_id: str | None,
        progress: Any | None,
    ) -> Any: ...


@runtime_checkable
class VerificationRunner(Protocol):
    """Protocol for runners that verify and review code changes (e.g. Claude)."""

    def verify(
        self,
        *,
        workspace: str,
        command_results: dict[str, Any],
        changed_files: dict[str, Any],
        codex_run_log_path: str,
        plan: dict[str, Any],
        test_plan: dict[str, Any],
    ) -> dict[str, Any]: ...

    def review(
        self,
        *,
        workspace: str,
        git_diff: str,
        changed_files: dict[str, Any],
        verification_summary: dict[str, Any],
        plan: dict[str, Any],
        test_plan: dict[str, Any],
    ) -> dict[str, Any]: ...
