"""Pipeline E2E tests using InMemoryAdapter — no Discord server required."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

from app.approvals import ApprovalCoordinator
from app.pipeline import CandidateExecutionResult, DevelopmentPipeline
from app.process_registry import ProcessRegistry
from app.runners.codex_runner import CodexRunResult
from app.state_store import FileStateStore
from app.testing.in_memory_adapter import InMemoryAdapter
from tests.helpers import make_test_issue, make_test_settings, setup_planning_artifacts

THREAD_ID = 42
ISSUE_KEY = "owner/repo#1"


def _make_workspace_info(workspace: str) -> dict[str, Any]:
    return {
        "workspace": workspace,
        "branch_name": "agent/gh-1-test",
        "base_branch": "main",
        "workspace_key": "owner/repo#1",
    }


def _make_codex_result(
    returncode: int = 0,
    stdout_path: str = "/dev/null",
    mode: str = "app-server",
    session_id: str = "thread_primary",
) -> CodexRunResult:
    return CodexRunResult(
        returncode=returncode,
        stdout_path=stdout_path,
        changed_files=["file.py"],
        summary="done",
        mode=mode,
        session_id=session_id,
    )


def _make_verification(status: str = "success") -> dict[str, Any]:
    return {
        "status": status,
        "failure_type": "" if status == "success" else "test_failure",
        "retry_recommended": False,
        "human_check_recommended": False,
        "notes": ["all good"],
    }


def _make_review(decision: str = "approve") -> dict[str, Any]:
    return {
        "decision": decision,
        "unnecessary_changes": [],
        "test_gaps": [],
        "risk_items": [],
        "protected_path_touches": [],
    }


def _make_pr() -> dict[str, Any]:
    return {"number": 99, "url": "https://github.com/owner/repo/pull/99"}


def _build_sync_pipeline_case(
    *,
    workspace_dir: str,
    state_dir: str,
    github_client: MagicMock | None = None,
) -> tuple[DevelopmentPipeline, FileStateStore, InMemoryAdapter, Path]:
    state_store = FileStateStore(state_dir)
    state_store.create_run(thread_id=THREAD_ID, parent_message_id=1, channel_id=2)
    setup_planning_artifacts(THREAD_ID, state_store)
    state_store.bind_issue(THREAD_ID, "owner/repo", 1)

    client = github_client or MagicMock()
    client.get_issue_snapshot.return_value = make_test_issue()
    client.create_pull_request.return_value = _make_pr()
    client.get_pull_request_status.return_value = {
        "draft": True,
        "mergeable": True,
        "mergeable_state": "clean",
        "head_sha": "headsha123",
    }
    client.ready_pull_request_for_review.return_value = {"ready_for_review": True}
    client.create_issue_comment.return_value = None
    client.update_issue_state.return_value = None
    client.upsert_workpad_comment.return_value = None

    pipeline = DevelopmentPipeline(
        settings=make_test_settings(workspace_root=workspace_dir, state_dir=state_dir),
        state_store=state_store,
        github_client=client,
        process_registry=ProcessRegistry(state_dir),
        approval_coordinator=ApprovalCoordinator(state_store),
    )

    async def _run_blocking(func, /, *args, **kwargs):
        return func(*args, **kwargs)

    pipeline._run_blocking = _run_blocking  # type: ignore[method-assign]
    adapter = InMemoryAdapter()
    adapter.register_channel(THREAD_ID)
    codex_log_path = Path(workspace_dir) / "codex.log"
    codex_log_path.write_text("codex run log\n", encoding="utf-8")
    return pipeline, state_store, adapter, codex_log_path


class PipelineE2EBase(unittest.IsolatedAsyncioTestCase):
    """Shared setup for pipeline E2E tests."""

    async def asyncSetUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.state_dir = self.tmpdir.name
        self.workspace_dir = tempfile.mkdtemp()

        self.state_store = FileStateStore(self.state_dir)
        self.state_store.create_run(thread_id=THREAD_ID, parent_message_id=1, channel_id=2)
        setup_planning_artifacts(THREAD_ID, self.state_store)
        self.state_store.bind_issue(THREAD_ID, "owner/repo", 1)

        self.github_client = MagicMock()
        self.github_client.get_issue_snapshot.return_value = make_test_issue()
        self.github_client.create_pull_request.return_value = _make_pr()
        self.github_client.get_pull_request_status.return_value = {
            "draft": True,
            "mergeable": True,
            "mergeable_state": "clean",
            "head_sha": "headsha123",
        }
        self.github_client.ready_pull_request_for_review.return_value = {"ready_for_review": True}
        self.github_client.create_issue_comment.return_value = None
        self.github_client.update_issue_state.return_value = None
        self.github_client.upsert_workpad_comment.return_value = None

        self.process_registry = ProcessRegistry(self.state_dir)
        self.approval_coordinator = ApprovalCoordinator(self.state_store)

        # Build a Settings that skips Pydantic file validation
        self.settings = make_test_settings(
            workspace_root=self.workspace_dir,
            state_dir=self.state_dir,
        )

        self.pipeline = DevelopmentPipeline(
            settings=self.settings,
            state_store=self.state_store,
            github_client=self.github_client,
            process_registry=self.process_registry,
            approval_coordinator=self.approval_coordinator,
        )

        async def _run_blocking(func, /, *args, **kwargs):
            return func(*args, **kwargs)

        self.pipeline._run_blocking = _run_blocking  # type: ignore[method-assign]

        self.adapter = InMemoryAdapter()
        self.adapter.register_channel(THREAD_ID)

        # Create a dummy codex log file
        self.codex_log_path = Path(self.workspace_dir) / "codex.log"
        self.codex_log_path.write_text("codex run log\n", encoding="utf-8")

    async def asyncTearDown(self) -> None:
        self.tmpdir.cleanup()

    def _patch_workspace(self) -> Any:
        return patch.object(
            self.pipeline.workspace_manager,
            "prepare",
            side_effect=lambda *args, **kwargs: _make_workspace_info(self.workspace_dir),
        )

    def _patch_codex(self, returncode: int = 0) -> Any:
        return patch.object(
            self.pipeline.codex_runner,
            "run",
            side_effect=lambda *args, **kwargs: _make_codex_result(
                returncode=returncode,
                stdout_path=str(self.codex_log_path),
            ),
        )

    def _patch_verify(self, status: str = "success") -> Any:
        return patch.object(
            self.pipeline.claude_runner,
            "verify",
            side_effect=lambda *args, **kwargs: _make_verification(status),
        )

    def _patch_review(self, decision: str = "approve") -> Any:
        return patch.object(
            self.pipeline.claude_runner,
            "review",
            side_effect=lambda *args, **kwargs: _make_review(decision),
        )

    def _patch_git_diff(self, diff: str = "diff --git a/file.py b/file.py") -> Any:
        return patch.object(self.pipeline, "_capture_git_diff", side_effect=lambda *args, **kwargs: diff)

    def _patch_commit_push(self, pushed: bool = True) -> Any:
        return patch.object(self.pipeline, "_commit_and_push", side_effect=lambda *args, **kwargs: pushed)

    def _patch_detect_changed(self, files: list[str] | None = None) -> Any:
        resolved = files or ["file.py"]
        return patch.object(self.pipeline, "_detect_changed_files", side_effect=lambda *args, **kwargs: resolved)

    def _patch_workflow(self, workflow: dict[str, Any] | None = None) -> Any:
        return patch("app.pipeline.load_workflow", return_value=workflow or {"commands": {}})

    def _patch_workflow_text(self) -> Any:
        return patch("app.pipeline.workflow_text", return_value="workflow text")


class TestFullSuccessPath(PipelineE2EBase):
    def test_workflow_commands_use_verification_required_checks(self) -> None:
        commands = self.pipeline._workflow_commands(
            {
                "verification": {
                    "required_checks": [
                        {"name": "lint", "command": "ruff check ."},
                        {"name": "tests", "command": "pytest -q"},
                    ]
                }
            }
        )

        self.assertEqual(
            [
                {"phase": "lint", "command": "ruff check .", "category": "hard", "allow_not_applicable": False},
                {"phase": "tests", "command": "pytest -q", "category": "hard", "allow_not_applicable": False},
            ],
            commands,
        )

    def test_workflow_commands_include_advisory_checks(self) -> None:
        commands = self.pipeline._workflow_commands(
            {
                "verification": {
                    "required_checks": [{"name": "lint", "command": "ruff check ."}],
                    "advisory_checks": [{"name": "format", "command": "ruff format --check ."}],
                }
            }
        )

        self.assertEqual("hard", commands[0]["category"])
        self.assertEqual("advisory", commands[1]["category"])

    def test_resolve_workflow_prefers_repo_workflow_over_verification_plan(self) -> None:
        resolved = self.pipeline._resolve_workflow(
            {"verification": {"required_checks": [{"name": "lint", "command": "ruff check ."}]}},
            {"hard_checks": [{"name": "tests", "command": "pytest -q"}]},
        )

        self.assertEqual("ruff check .", resolved["verification"]["required_checks"][0]["command"])

    async def test_full_success_path_creates_pr(self) -> None:
        with (
            self._patch_workspace(),
            self._patch_codex(),
            self._patch_detect_changed(),
            self._patch_workflow(),
            self._patch_workflow_text(),
            self._patch_verify(),
            self._patch_review(),
            self._patch_git_diff(),
            self._patch_commit_push(True),
        ):
            await self.pipeline.execute_run(
                chat=self.adapter,
                thread_id=THREAD_ID,
                repo_full_name="owner/repo",
                issue=make_test_issue(),
            )

        messages = self.adapter.messages_for(THREAD_ID)
        self.assertTrue(len(messages) >= 3)
        self.adapter.assert_message_contains(THREAD_ID, "run を開始")
        self.adapter.assert_message_contains(THREAD_ID, "Codex 実装が完了")
        self.adapter.assert_message_contains(THREAD_ID, "draft PR")
        self.assertIn("https://test.local/channels/42", self.github_client.create_pull_request.call_args.kwargs["body"])
        issue_comment_args = self.github_client.create_issue_comment.call_args.args
        self.assertIn("https://test.local/channels/42", issue_comment_args[2])

        meta = self.state_store.load_meta(ISSUE_KEY)
        self.assertEqual(meta.get("status"), "Human Review")
        self.assertEqual("headsha123", self.state_store.load_artifact(ISSUE_KEY, "pr.json")["head_sha"])
        self.assertEqual(
            "completed", self.state_store.load_attempt_artifact(ISSUE_KEY, "att-001", "attempt_manifest.json")["status"]
        )
        self.assertEqual(
            "ready",
            self.state_store.load_attempt_artifact(ISSUE_KEY, "att-001", "attempt_manifest.json")["trigger"],
        )
        self.assertEqual(
            1,
            self.state_store.load_attempt_artifact(ISSUE_KEY, "att-001", "scope_contract.json")["version"],
        )
        self.assertTrue(self.state_store.load_attempt_artifact(ISSUE_KEY, "att-001", "proof_result.json")["complete"])
        self.assertEqual(
            "primary",
            self.state_store.load_attempt_artifact(ISSUE_KEY, "att-001", "final_attempt_summary.json")[
                "winner_candidate_id"
            ],
        )
        self.assertIsInstance(
            self.state_store.load_attempt_artifact(ISSUE_KEY, "att-001", "effective_workflow.json"),
            dict,
        )
        self.assertEqual(
            "approve",
            self.state_store.load_candidate_artifact(ISSUE_KEY, "att-001", "primary", "latest_review_delta.json")[
                "decision"
            ],
        )
        self.assertFalse(
            self.state_store.load_candidate_artifact(ISSUE_KEY, "att-001", "primary", "repair_instructions.json")[
                "applicable"
            ]
        )
        self.assertEqual(
            "candidate_complete",
            self.state_store.load_candidate_artifact(ISSUE_KEY, "att-001", "primary", "session_checkpoint.json")[
                "last_completed_phase"
            ],
        )
        self.assertEqual(
            "thread_primary",
            self.state_store.load_candidate_artifact(ISSUE_KEY, "att-001", "primary", "session_checkpoint.json")[
                "session_id"
            ],
        )
        self.assertEqual(
            "thread_primary",
            self.state_store.load_candidate_artifact(ISSUE_KEY, "att-001", "primary", "candidate_manifest.json")[
                "session_id"
            ],
        )
        self.assertEqual(
            "approve",
            self.state_store.load_candidate_artifact(ISSUE_KEY, "att-001", "primary", "review_summary.json")[
                "decision"
            ],
        )
        self.assertEqual(
            "approve",
            self.state_store.load_candidate_artifact(ISSUE_KEY, "att-001", "primary", "review_decision.json")[
                "decision"
            ],
        )
        self.assertNotIn(
            "decision",
            self.state_store.load_candidate_artifact(ISSUE_KEY, "att-001", "primary", "review_findings.json"),
        )
        self.assertEqual(
            [],
            self.state_store.load_candidate_artifact(ISSUE_KEY, "att-001", "primary", "postable_findings.json")[
                "findings"
            ],
        )
        self.assertFalse(
            self.state_store.load_candidate_artifact(ISSUE_KEY, "att-001", "primary", "repair_feedback.json")[
                "applicable"
            ],
        )
        self.assertEqual(
            ["all good"],
            self.state_store.load_candidate_artifact(ISSUE_KEY, "att-001", "primary", "latest_repair_feedback.json")[
                "notes"
            ],
        )
        self.github_client.ready_pull_request_for_review.assert_called_with("owner/repo", 99)
        self.assertFalse(self.state_store.load_artifact(ISSUE_KEY, "pr.json")["draft"])

    async def test_full_success_path_posts_inline_review_findings_when_enabled(self) -> None:
        review = {
            "decision": "approve",
            "unnecessary_changes": [],
            "test_gaps": [],
            "risk_items": ["bug"],
            "protected_path_touches": [],
            "postable_findings": [
                {
                    "id": "R1",
                    "severity": "high",
                    "origin": "introduced",
                    "confidence": 0.91,
                    "file": "app/file.py",
                    "line_start": 10,
                    "line_end": 12,
                    "claim": "bug",
                    "evidence": ["diff shows regression"],
                    "verifier_status": "confirmed",
                }
            ],
        }
        workflow = {
            "commands": {},
            "config": type(
                "WorkflowCfg",
                (),
                {"review": type("ReviewCfg", (), {"post_inline_to_github": True})()},
            )(),
        }
        with (
            self._patch_workspace(),
            self._patch_codex(),
            self._patch_detect_changed(),
            self._patch_workflow(workflow),
            self._patch_workflow_text(),
            self._patch_verify(),
            patch.object(self.pipeline.claude_runner, "review", side_effect=lambda *args, **kwargs: review),
            self._patch_git_diff(),
            self._patch_commit_push(True),
        ):
            await self.pipeline.execute_run(
                chat=self.adapter,
                thread_id=THREAD_ID,
                repo_full_name="owner/repo",
                issue=make_test_issue(),
            )

        self.github_client.create_inline_review_comment.assert_called_once()
        self.assertEqual(99, self.github_client.create_inline_review_comment.call_args.kwargs["pr_number"])
        self.assertEqual("app/file.py", self.github_client.create_inline_review_comment.call_args.kwargs["path"])

    async def test_message_order_in_success_path(self) -> None:
        with (
            self._patch_workspace(),
            self._patch_codex(),
            self._patch_detect_changed(),
            self._patch_workflow(),
            self._patch_workflow_text(),
            self._patch_verify(),
            self._patch_review(),
            self._patch_git_diff(),
            self._patch_commit_push(True),
        ):
            await self.pipeline.execute_run(
                chat=self.adapter,
                thread_id=THREAD_ID,
                repo_full_name="owner/repo",
                issue=make_test_issue(),
            )
        self.adapter.assert_message_order(THREAD_ID, ["run を開始", "Codex 実装が完了", "draft PR"])

    async def test_candidate_mode_promotes_winner_branch(self) -> None:
        alt_workspace = tempfile.mkdtemp()
        self.state_store.write_artifact(
            THREAD_ID,
            "candidate_decision.json",
            {"enabled": True, "candidate_ids": ["primary", "alt1"]},
        )

        def _prepare_candidate(**kwargs: Any) -> dict[str, Any]:
            if kwargs["candidate_id"] == "primary":
                return _make_workspace_info(self.workspace_dir)
            return {
                "workspace": alt_workspace,
                "branch_name": "agent/gh-1-test-att-001-alt1",
                "base_branch": "main",
                "workspace_key": "owner/repo#1",
                "workspace_root": alt_workspace,
            }

        def _codex_run(*args, **kwargs):
            run_identity = kwargs.get("run_identity")
            candidate_id = getattr(run_identity, "candidate_id", "primary")
            workspace = kwargs["workspace"]
            log_path = Path(workspace) / f"{candidate_id}.log"
            log_path.write_text(f"{candidate_id} log\n", encoding="utf-8")
            artifacts_dir = Path(kwargs["run_dir"]) / "artifacts"
            artifacts_dir.mkdir(parents=True, exist_ok=True)
            (artifacts_dir / "implementation_result.json").write_text(
                f'{{"candidate_id":"{candidate_id}","summary":"ok","changed_files":["file.py"]}}',
                encoding="utf-8",
            )
            (artifacts_dir / "changed_files.json").write_text(
                '{"changed_files":["file.py"]}',
                encoding="utf-8",
            )
            return _make_codex_result(stdout_path=str(log_path))

        def _review(*args, **kwargs):
            workspace = kwargs["workspace"]
            if workspace == self.workspace_dir:
                return {
                    "decision": "approve",
                    "unnecessary_changes": [],
                    "test_gaps": [],
                    "risk_items": ["primary is riskier"],
                    "protected_path_touches": [],
                    "postable_findings": [
                        {
                            "id": "R1",
                            "severity": "high",
                            "origin": "diff_reviewer",
                            "confidence": 0.9,
                            "file": "file.py",
                            "line_start": 1,
                            "line_end": 1,
                            "claim": "bug",
                            "evidence": ["diff"],
                            "verifier_status": "confirmed",
                        }
                    ],
                }
            return _make_review("approve")

        with (
            self._patch_workspace(),
            patch.object(self.pipeline, "_prepare_candidate_workspace", side_effect=_prepare_candidate),
            patch.object(self.pipeline.codex_runner, "run", side_effect=_codex_run),
            self._patch_detect_changed(),
            self._patch_workflow(),
            self._patch_workflow_text(),
            self._patch_verify(),
            patch.object(self.pipeline.claude_runner, "review", side_effect=_review),
            self._patch_git_diff(),
            self._patch_commit_push(True),
        ):
            await self.pipeline.execute_run(
                chat=self.adapter,
                thread_id=THREAD_ID,
                repo_full_name="owner/repo",
                issue=make_test_issue(),
            )

        self.assertEqual(
            "agent/gh-1-test-att-001-alt1", self.github_client.create_pull_request.call_args.kwargs["head"]
        )
        self.assertEqual("alt1", self.state_store.load_artifact(ISSUE_KEY, "runner_metadata.json")["candidate_id"])
        self.assertEqual("att-001", self.state_store.current_attempt_id(ISSUE_KEY))
        self.assertEqual(
            "alt1",
            self.state_store.load_attempt_artifact(ISSUE_KEY, "att-001", "winner_selection.json")[
                "winner_candidate_id"
            ],
        )
        winner_selection = self.state_store.load_attempt_artifact(ISSUE_KEY, "att-001", "winner_selection.json")
        self.assertEqual(["primary", "alt1"], winner_selection["successful_candidates"])
        self.assertEqual("deterministic_rank", winner_selection["selection_basis"])
        self.assertIn("winner_reason", winner_selection)
        self.assertEqual(2, len(winner_selection["candidates"]))
        self.assertEqual(
            "alt1",
            self.state_store.load_attempt_artifact(ISSUE_KEY, "att-001", "final_attempt_summary.json")[
                "winner_candidate_id"
            ],
        )
        self.assertEqual(
            "completed",
            self.state_store.load_attempt_artifact(ISSUE_KEY, "att-001", "attempt_manifest.json")["status"],
        )
        self.assertEqual(
            "alt1",
            self.state_store.load_candidate_artifact(ISSUE_KEY, "att-001", "alt1", "runner_metadata.json")[
                "candidate_id"
            ],
        )
        self.assertEqual(
            "approve",
            self.state_store.load_candidate_artifact(ISSUE_KEY, "att-001", "alt1", "review_result.json")["decision"],
        )
        self.assertEqual(
            "approve",
            self.state_store.load_candidate_artifact(ISSUE_KEY, "att-001", "alt1", "review_summary.json")["decision"],
        )
        self.assertTrue(
            self.state_store.load_candidate_artifact(ISSUE_KEY, "att-001", "alt1", "proof_result.json")["complete"]
        )
        self.assertEqual(
            "approve",
            self.state_store.load_candidate_artifact(ISSUE_KEY, "att-001", "alt1", "latest_review_delta.json")[
                "decision"
            ],
        )
        self.assertEqual(
            [],
            self.state_store.load_candidate_artifact(ISSUE_KEY, "att-001", "alt1", "scope_analysis.json")[
                "must_not_touch_violations"
            ],
        )
        self.adapter.assert_message_contains(THREAD_ID, "candidate mode を有効化")
        self.adapter.assert_message_contains(THREAD_ID, "winner は `alt1`")

    async def test_repairable_narrow_fix_reuses_same_attempt_same_candidate_new_session(self) -> None:
        review_calls = 0
        codex_calls = 0

        def _codex_run(*args, **kwargs):
            nonlocal codex_calls
            codex_calls += 1
            workspace = kwargs["workspace"]
            candidate_id = getattr(kwargs.get("run_identity"), "candidate_id", "primary")
            log_path = Path(workspace) / f"{candidate_id}-{codex_calls}.log"
            log_path.write_text(f"{candidate_id} log {codex_calls}\n", encoding="utf-8")
            artifacts_dir = Path(kwargs["run_dir"]) / "artifacts"
            artifacts_dir.mkdir(parents=True, exist_ok=True)
            (artifacts_dir / "implementation_result.json").write_text(
                f'{{"candidate_id":"{candidate_id}","summary":"ok-{codex_calls}","changed_files":["file.py"]}}',
                encoding="utf-8",
            )
            (artifacts_dir / "changed_files.json").write_text(
                '{"changed_files":["file.py"]}',
                encoding="utf-8",
            )
            return _make_codex_result(stdout_path=str(log_path))

        def _review(*args, **kwargs):
            nonlocal review_calls
            review_calls += 1
            if review_calls == 1:
                return {
                    "decision": "repairable",
                    "unnecessary_changes": [],
                    "test_gaps": [],
                    "risk_items": ["rerun focused checks"],
                    "protected_path_touches": [],
                    "postable_findings": [
                        {
                            "id": "R1",
                            "severity": "medium",
                            "origin": "diff_reviewer",
                            "confidence": 0.8,
                            "file": "file.py",
                            "line_start": 1,
                            "line_end": 1,
                            "claim": "needs a small follow-up",
                            "evidence": ["diff"],
                            "verifier_status": "confirmed",
                        }
                    ],
                }
            return _make_review("approve")

        with (
            self._patch_workspace(),
            patch.object(self.pipeline.codex_runner, "run", side_effect=_codex_run),
            self._patch_detect_changed(["file.py"]),
            self._patch_workflow(),
            self._patch_workflow_text(),
            self._patch_verify(),
            patch.object(self.pipeline.claude_runner, "review", side_effect=_review),
            self._patch_git_diff(),
            self._patch_commit_push(True),
        ):
            await self.pipeline.execute_run(
                chat=self.adapter,
                thread_id=THREAD_ID,
                repo_full_name="owner/repo",
                issue=make_test_issue(),
            )

        self.assertEqual(2, codex_calls)
        self.assertEqual(2, review_calls)
        self.assertTrue(
            self.state_store.load_candidate_artifact(ISSUE_KEY, "att-001", "primary", "session_handoff_bundle.json")
        )
        self.assertEqual(
            "candidate_complete",
            self.state_store.load_candidate_artifact(ISSUE_KEY, "att-001", "primary", "session_checkpoint.json")[
                "last_completed_phase"
            ],
        )
        self.assertEqual(
            "completed", self.state_store.load_attempt_artifact(ISSUE_KEY, "att-001", "attempt_manifest.json")["status"]
        )
        self.github_client.create_pull_request.assert_called_once()

    async def test_reject_plan_misalignment_emits_replan_reason_artifact(self) -> None:
        review = {
            "decision": "reject",
            "unnecessary_changes": [],
            "test_gaps": [],
            "risk_items": ["plan_misalignment"],
            "protected_path_touches": [],
        }

        with (
            self._patch_workspace(),
            self._patch_codex(),
            self._patch_detect_changed(["file.py"]),
            self._patch_workflow(),
            self._patch_workflow_text(),
            self._patch_verify(),
            patch.object(self.pipeline.claude_runner, "review", return_value=review),
            self._patch_git_diff(),
        ):
            await self.pipeline.execute_run(
                chat=self.adapter,
                thread_id=THREAD_ID,
                repo_full_name="owner/repo",
                issue=make_test_issue(),
            )

        replan_reason = self.state_store.load_attempt_artifact(ISSUE_KEY, "att-001", "replan_reason.json")
        self.assertEqual(["plan_misalignment"], replan_reason["reasons"])
        self.assertEqual("att-002", replan_reason["new_attempt_id"])
        self.github_client.create_pull_request.assert_not_called()

    async def test_reject_plan_misalignment_stops_auto_replan_at_workflow_limit(self) -> None:
        review = {
            "decision": "reject",
            "unnecessary_changes": [],
            "test_gaps": [],
            "risk_items": ["plan_misalignment"],
            "protected_path_touches": [],
        }
        self.state_store.update_issue_meta(ISSUE_KEY, replan_count=2)

        workflow = {
            "commands": {},
            "config": SimpleNamespace(
                replanning=SimpleNamespace(
                    enabled=True,
                    auto_replan_on_reject_reasons=["plan_misalignment", "scope_drift"],
                    max_replans_per_issue=2,
                    emit_replan_reason_artifact=True,
                )
            ),
        }

        with (
            self._patch_workspace(),
            self._patch_codex(),
            self._patch_detect_changed(["file.py"]),
            self._patch_workflow(workflow),
            self._patch_workflow_text(),
            self._patch_verify(),
            patch.object(self.pipeline.claude_runner, "review", return_value=review),
            self._patch_git_diff(),
        ):
            await self.pipeline.execute_run(
                chat=self.adapter,
                thread_id=THREAD_ID,
                repo_full_name="owner/repo",
                issue=make_test_issue(),
            )

        replan_reason = self.state_store.load_attempt_artifact(ISSUE_KEY, "att-001", "replan_reason.json")
        self.assertEqual({}, replan_reason)
        self.assertEqual(2, self.state_store.load_issue_meta(ISSUE_KEY)["replan_count"])

    async def test_candidate_mode_runs_in_parallel_up_to_workflow_limit(self) -> None:
        self.state_store.write_artifact(
            THREAD_ID,
            "candidate_decision.json",
            {"enabled": True, "candidate_ids": ["primary", "alt1"]},
        )
        workflow = {
            "commands": {},
            "config": SimpleNamespace(implementation=SimpleNamespace(max_parallel_editors=2)),
        }
        started: list[str] = []
        finished: list[str] = []
        active = 0
        max_active = 0
        lock = asyncio.Lock()

        async def _execute_candidate(**kwargs: Any) -> CandidateExecutionResult:
            nonlocal active, max_active
            candidate_id = kwargs["candidate_id"]
            async with lock:
                started.append(candidate_id)
                active += 1
                max_active = max(max_active, active)
            await asyncio.sleep(0.05)
            async with lock:
                active -= 1
                finished.append(candidate_id)
            return CandidateExecutionResult(
                candidate_id=candidate_id,
                workspace_info=_make_workspace_info(self.workspace_dir),
                codex_result=_make_codex_result(stdout_path=str(self.codex_log_path)),
                codex_log_path=str(self.codex_log_path),
                changed_files={"changed_files": ["file.py"]},
                scope_analysis={"unexpected_file_count": 0, "must_not_touch_violations": []},
                command_results={},
                verification={},
                verification_json={},
                review={},
                proof_result={"complete": True, "missing_artifacts": []},
                success=True,
            )

        def _prepare_candidate(**kwargs: Any) -> dict[str, Any]:
            candidate_id = kwargs["candidate_id"]
            workspace = Path(self.workspace_dir) / candidate_id
            workspace.mkdir(parents=True, exist_ok=True)
            return {
                "workspace": str(workspace),
                "branch_name": f"agent/gh-1-test-att-001-{candidate_id}",
                "base_branch": "main",
                "workspace_key": ISSUE_KEY,
                "workspace_root": str(workspace.parent),
            }

        with (
            patch.object(self.pipeline, "_prepare_candidate_workspace", side_effect=_prepare_candidate),
            patch.object(self.pipeline, "_execute_candidate", side_effect=_execute_candidate),
        ):
            results = await self.pipeline._execute_candidates(
                chat_client=self.adapter,
                channel=self.adapter.get_channel(THREAD_ID),
                issue_key=ISSUE_KEY,
                run_id="run-1",
                workflow=workflow,
                repo_full_name="owner/repo",
                issue=make_test_issue(),
                issue_snapshot=make_test_issue(),
                summary={},
                plan={},
                test_plan={},
                verification_plan={},
                attempt_id="att-001",
                workspace_info=_make_workspace_info(self.workspace_dir),
                candidate_ids=["primary", "alt1"],
            )

        self.assertEqual(["primary", "alt1"], [result.candidate_id for result in results])
        self.assertEqual({"primary", "alt1"}, set(started))
        self.assertEqual({"primary", "alt1"}, set(finished))
        self.assertEqual(2, max_active)

    async def test_scope_violation_stops_before_review(self) -> None:
        setup_planning_artifacts(
            THREAD_ID,
            self.state_store,
            plan={
                "steps": ["step1"],
                "candidate_files": ["file.py"],
                "must_not_touch": ["pyproject.toml"],
                "verification_focus": ["keep config stable"],
            },
        )
        with (
            self._patch_workspace(),
            self._patch_codex(),
            self._patch_detect_changed(["pyproject.toml"]),
            self._patch_workflow(),
            self._patch_workflow_text(),
            patch.object(self.pipeline.claude_runner, "verify") as verify,
            patch.object(self.pipeline.claude_runner, "review") as review,
        ):
            await self.pipeline.execute_run(
                chat=self.adapter,
                thread_id=THREAD_ID,
                repo_full_name="owner/repo",
                issue=make_test_issue(),
            )

        verify.assert_not_called()
        review.assert_not_called()
        self.assertEqual("Human Review", self.state_store.load_meta(ISSUE_KEY)["status"])
        self.assertEqual(
            ["pyproject.toml"],
            self.state_store.load_candidate_artifact(ISSUE_KEY, "att-001", "primary", "scope_analysis.json")[
                "must_not_touch_violations"
            ],
        )
        self.adapter.assert_message_contains(THREAD_ID, "must_not_touch")

    async def test_protected_config_violation_requires_label(self) -> None:
        setup_planning_artifacts(
            THREAD_ID,
            self.state_store,
            plan={
                "steps": ["step1"],
                "candidate_files": ["app/service.py"],
                "must_not_touch": [],
                "verification_focus": ["keep config stable"],
            },
        )
        self.github_client.get_issue_snapshot.return_value = make_test_issue(labels=[])
        with (
            self._patch_workspace(),
            self._patch_codex(),
            self._patch_detect_changed(["WORKFLOW.md"]),
            self._patch_workflow(),
            self._patch_workflow_text(),
            patch.object(self.pipeline.claude_runner, "verify") as verify,
            patch.object(self.pipeline.claude_runner, "review") as review,
        ):
            await self.pipeline.execute_run(
                chat=self.adapter,
                thread_id=THREAD_ID,
                repo_full_name="owner/repo",
                issue=make_test_issue(),
            )

        verify.assert_not_called()
        review.assert_not_called()
        self.assertEqual("Human Review", self.state_store.load_meta(ISSUE_KEY)["status"])
        scope = self.state_store.load_candidate_artifact(ISSUE_KEY, "att-001", "primary", "scope_analysis.json")
        self.assertEqual(["WORKFLOW.md"], scope["protected_config_violations"])
        self.assertFalse(scope["protected_config_label_present"])
        self.assertEqual("allow-protected-config", scope["protected_config_allow_label"])
        self.adapter.assert_message_contains(THREAD_ID, "allow-protected-config")


class TestCodexFailure(unittest.TestCase):
    def test_codex_failure_notifies_and_marks_failed(self) -> None:
        async def run_case() -> tuple[list[str], dict[str, Any]]:
            tmpdir = tempfile.TemporaryDirectory()
            state_dir = tmpdir.name
            workspace_dir = tempfile.mkdtemp()

            state_store = FileStateStore(state_dir)
            state_store.create_run(thread_id=THREAD_ID, parent_message_id=1, channel_id=2)
            setup_planning_artifacts(THREAD_ID, state_store)
            state_store.bind_issue(THREAD_ID, "owner/repo", 1)

            github_client = MagicMock()
            github_client.get_issue_snapshot.return_value = make_test_issue()
            github_client.update_issue_state.return_value = None
            github_client.upsert_workpad_comment.return_value = None

            pipeline = DevelopmentPipeline(
                settings=make_test_settings(workspace_root=workspace_dir, state_dir=state_dir),
                state_store=state_store,
                github_client=github_client,
                process_registry=ProcessRegistry(state_dir),
                approval_coordinator=ApprovalCoordinator(state_store),
            )

            async def _run_blocking(func, /, *args, **kwargs):
                return func(*args, **kwargs)

            pipeline._run_blocking = _run_blocking  # type: ignore[method-assign]
            adapter = InMemoryAdapter()
            adapter.register_channel(THREAD_ID)
            codex_log_path = Path(workspace_dir) / "codex.log"
            codex_log_path.write_text("codex run log\n", encoding="utf-8")

            with (
                patch.object(
                    pipeline.workspace_manager,
                    "prepare",
                    side_effect=lambda *args, **kwargs: _make_workspace_info(workspace_dir),
                ),
                patch.object(
                    pipeline.codex_runner,
                    "run",
                    side_effect=lambda *args, **kwargs: _make_codex_result(
                        returncode=1, stdout_path=str(codex_log_path)
                    ),
                ),
                patch.object(pipeline, "_detect_changed_files", side_effect=lambda *args, **kwargs: ["file.py"]),
                patch("app.pipeline.load_workflow", return_value={"commands": {}}),
                patch("app.pipeline.workflow_text", return_value="workflow text"),
            ):
                await pipeline.execute_run(
                    chat=adapter,
                    thread_id=THREAD_ID,
                    repo_full_name="owner/repo",
                    issue=make_test_issue(),
                )

            return adapter.messages_for(THREAD_ID), state_store.load_meta(ISSUE_KEY)

        messages, meta = asyncio.run(run_case())
        self.assertTrue(any("Codex 実装で失敗しました" in item for item in messages))
        self.assertEqual("Rework", meta.get("status"))


class TestVerificationGating(unittest.TestCase):
    def test_hard_check_failure_blocks_pr_even_when_verifier_returns_success(self) -> None:
        async def run_case() -> tuple[list[str], dict[str, Any]]:
            tmpdir = tempfile.TemporaryDirectory()
            workspace_dir = tempfile.mkdtemp()
            pipeline, state_store, adapter, codex_log_path = _build_sync_pipeline_case(
                workspace_dir=workspace_dir,
                state_dir=tmpdir.name,
            )
            workflow = {
                "verification": {
                    "required_checks": [{"name": "lint", "command": 'python -c "import sys; sys.exit(1)"'}],
                }
            }
            with (
                patch.object(
                    pipeline.workspace_manager,
                    "prepare",
                    side_effect=lambda *args, **kwargs: _make_workspace_info(workspace_dir),
                ),
                patch.object(
                    pipeline.codex_runner,
                    "run",
                    side_effect=lambda *args, **kwargs: _make_codex_result(
                        returncode=0, stdout_path=str(codex_log_path)
                    ),
                ),
                patch.object(pipeline, "_detect_changed_files", side_effect=lambda *args, **kwargs: ["file.py"]),
                patch("app.pipeline.load_workflow", return_value=workflow),
                patch("app.pipeline.workflow_text", return_value="workflow text"),
                patch.object(
                    pipeline.claude_runner, "verify", side_effect=lambda *args, **kwargs: _make_verification("success")
                ),
            ):
                await pipeline.execute_run(
                    chat=adapter,
                    thread_id=THREAD_ID,
                    repo_full_name="owner/repo",
                    issue=make_test_issue(),
                )
            return adapter.messages_for(THREAD_ID), state_store.load_meta(ISSUE_KEY)

        messages, meta = asyncio.run(run_case())
        self.assertTrue(any("hard check が失敗しました" in item for item in messages))
        self.assertEqual("Rework", meta.get("status"))

    def test_environment_blocked_failure_stops_run_as_blocked(self) -> None:
        async def run_case() -> tuple[list[str], dict[str, Any]]:
            tmpdir = tempfile.TemporaryDirectory()
            workspace_dir = tempfile.mkdtemp()
            pipeline, state_store, adapter, codex_log_path = _build_sync_pipeline_case(
                workspace_dir=workspace_dir,
                state_dir=tmpdir.name,
            )
            with (
                patch.object(
                    pipeline.workspace_manager,
                    "prepare",
                    side_effect=lambda *args, **kwargs: _make_workspace_info(workspace_dir),
                ),
                patch.object(
                    pipeline.codex_runner,
                    "run",
                    side_effect=lambda *args, **kwargs: _make_codex_result(
                        returncode=0, stdout_path=str(codex_log_path)
                    ),
                ),
                patch.object(pipeline, "_detect_changed_files", side_effect=lambda *args, **kwargs: ["file.py"]),
                patch("app.pipeline.load_workflow", return_value={"commands": {}}),
                patch("app.pipeline.workflow_text", return_value="workflow text"),
                patch.object(
                    pipeline,
                    "execute_workflow_commands",
                    return_value={
                        "results": [
                            {
                                "phase": "bootstrap",
                                "category": "bootstrap",
                                "command": "uv sync",
                                "returncode": 1,
                                "output": "failed to download",
                                "success": False,
                                "status": "fail",
                            }
                        ],
                        "failure_type": "environment_blocked",
                    },
                ),
            ):
                await pipeline.execute_run(
                    chat=adapter,
                    thread_id=THREAD_ID,
                    repo_full_name="owner/repo",
                    issue=make_test_issue(),
                )
            return adapter.messages_for(THREAD_ID), state_store.load_meta(ISSUE_KEY)

        messages, meta = asyncio.run(run_case())
        self.assertTrue(any("bootstrap に失敗" in item for item in messages))
        self.assertEqual("Blocked", meta.get("status"))


class TestVerificationPlanFallback(unittest.TestCase):
    def test_verification_plan_fallback_drives_checks_when_workflow_missing(self) -> None:
        async def run_case() -> tuple[dict[str, Any], list[str]]:
            tmpdir = tempfile.TemporaryDirectory()
            state_dir = tmpdir.name
            workspace_dir = tempfile.mkdtemp()

            state_store = FileStateStore(state_dir)
            state_store.create_run(thread_id=THREAD_ID, parent_message_id=1, channel_id=2)
            setup_planning_artifacts(
                THREAD_ID,
                state_store,
                verification_plan={
                    "profile": "generic-minimal",
                    "hard_checks": [{"name": "sanity", "command": "sh -c 'exit 0'"}],
                    "advisory_checks": [{"name": "format", "command": "sh -c 'exit 1'", "allow_not_applicable": True}],
                },
            )
            state_store.bind_issue(THREAD_ID, "owner/repo", 1)

            github_client = MagicMock()
            github_client.get_issue_snapshot.return_value = make_test_issue()
            github_client.create_pull_request.return_value = _make_pr()
            github_client.get_pull_request_status.return_value = {
                "draft": True,
                "mergeable": True,
                "mergeable_state": "clean",
                "head_sha": "headsha123",
            }
            github_client.ready_pull_request_for_review.return_value = {"ready_for_review": True}
            github_client.create_issue_comment.return_value = None
            github_client.update_issue_state.return_value = None
            github_client.upsert_workpad_comment.return_value = None

            pipeline = DevelopmentPipeline(
                settings=make_test_settings(workspace_root=workspace_dir, state_dir=state_dir),
                state_store=state_store,
                github_client=github_client,
                process_registry=ProcessRegistry(state_dir),
                approval_coordinator=ApprovalCoordinator(state_store),
            )

            async def _run_blocking(func, /, *args, **kwargs):
                return func(*args, **kwargs)

            pipeline._run_blocking = _run_blocking  # type: ignore[method-assign]
            adapter = InMemoryAdapter()
            adapter.register_channel(THREAD_ID)
            codex_log_path = Path(workspace_dir) / "codex.log"
            codex_log_path.write_text("codex run log\n", encoding="utf-8")

            with (
                patch.object(
                    pipeline.workspace_manager,
                    "prepare",
                    side_effect=lambda *args, **kwargs: _make_workspace_info(workspace_dir),
                ),
                patch.object(
                    pipeline.codex_runner,
                    "run",
                    side_effect=lambda *args, **kwargs: _make_codex_result(
                        returncode=0, stdout_path=str(codex_log_path)
                    ),
                ),
                patch.object(pipeline, "_detect_changed_files", side_effect=lambda *args, **kwargs: ["file.py"]),
                patch("app.pipeline.load_workflow", return_value={}),
                patch("app.pipeline.workflow_text", return_value="workflow text"),
                patch.object(
                    pipeline.claude_runner, "verify", side_effect=lambda *args, **kwargs: _make_verification()
                ),
                patch.object(pipeline.claude_runner, "review", side_effect=lambda *args, **kwargs: _make_review()),
                patch.object(
                    pipeline, "_capture_git_diff", side_effect=lambda *args, **kwargs: "diff --git a/file.py b/file.py"
                ),
                patch.object(pipeline, "_commit_and_push", side_effect=lambda *args, **kwargs: True),
            ):
                await pipeline.execute_run(
                    chat=adapter,
                    thread_id=THREAD_ID,
                    repo_full_name="owner/repo",
                    issue=make_test_issue(),
                )

            return state_store.load_artifact(ISSUE_KEY, "command_results.json"), adapter.messages_for(THREAD_ID)

        command_results, messages = asyncio.run(run_case())
        self.assertEqual("hard", command_results["results"][0]["category"])
        self.assertEqual("advisory", command_results["results"][1]["category"])
        self.assertEqual("fail", command_results["results"][1]["status"])
        self.assertTrue(any("draft PR" in item for item in messages))

    def test_candidate_refresh_uses_verification_plan_over_repo_root_fallback(self) -> None:
        async def run_case() -> tuple[dict[str, Any], dict[str, Any]]:
            tmpdir = tempfile.TemporaryDirectory()
            state_dir = tmpdir.name
            workspace_dir = tempfile.mkdtemp()

            state_store = FileStateStore(state_dir)
            state_store.create_run(thread_id=THREAD_ID, parent_message_id=1, channel_id=2)
            setup_planning_artifacts(
                THREAD_ID,
                state_store,
                verification_plan={
                    "profile": "generic-minimal",
                    "hard_checks": [{"name": "workspace_sanity", "command": "sh -c 'exit 0'"}],
                    "advisory_checks": [],
                },
            )
            state_store.bind_issue(THREAD_ID, "owner/repo", 1)

            github_client = MagicMock()
            github_client.get_issue_snapshot.return_value = make_test_issue()
            github_client.create_pull_request.return_value = _make_pr()
            github_client.get_pull_request_status.return_value = {
                "draft": True,
                "mergeable": True,
                "mergeable_state": "clean",
                "head_sha": "headsha123",
            }
            github_client.ready_pull_request_for_review.return_value = {"ready_for_review": True}
            github_client.create_issue_comment.return_value = None
            github_client.update_issue_state.return_value = None
            github_client.upsert_workpad_comment.return_value = None

            pipeline = DevelopmentPipeline(
                settings=make_test_settings(workspace_root=workspace_dir, state_dir=state_dir),
                state_store=state_store,
                github_client=github_client,
                process_registry=ProcessRegistry(state_dir),
                approval_coordinator=ApprovalCoordinator(state_store),
            )

            async def _run_blocking(func, /, *args, **kwargs):
                return func(*args, **kwargs)

            pipeline._run_blocking = _run_blocking  # type: ignore[method-assign]
            adapter = InMemoryAdapter()
            adapter.register_channel(THREAD_ID)
            codex_log_path = Path(workspace_dir) / "codex.log"
            codex_log_path.write_text("codex run log\n", encoding="utf-8")

            def _load_workflow(*, workspace: str | None = None, repo_root: str | None = None) -> dict[str, Any]:
                if workspace is not None:
                    return {}
                if repo_root is not None:
                    return {
                        "verification": {
                            "required_checks": [{"name": "lint", "command": "sh -c 'exit 9'"}],
                        }
                    }
                return {}

            refreshed_plan = {
                "profile": "python-basic",
                "bootstrap_commands": ["sh -c 'exit 0'"],
                "hard_checks": [{"name": "lint", "command": "sh -c 'exit 0'"}],
                "advisory_checks": [],
                "repair_checks": [],
            }

            with (
                patch.object(
                    pipeline.workspace_manager,
                    "prepare",
                    side_effect=lambda *args, **kwargs: _make_workspace_info(workspace_dir),
                ),
                patch.object(
                    pipeline.codex_runner,
                    "run",
                    side_effect=lambda *args, **kwargs: _make_codex_result(
                        returncode=0, stdout_path=str(codex_log_path)
                    ),
                ),
                patch.object(pipeline, "_detect_changed_files", side_effect=lambda *args, **kwargs: ["file.py"]),
                patch("app.pipeline.load_workflow", side_effect=_load_workflow),
                patch("app.pipeline.workflow_text", return_value="workflow text"),
                patch("app.pipeline.build_repo_profile", return_value={"languages": ["python"]}),
                patch("app.pipeline.build_verification_plan", return_value=refreshed_plan),
                patch.object(
                    pipeline.claude_runner, "verify", side_effect=lambda *args, **kwargs: _make_verification()
                ),
                patch.object(pipeline.claude_runner, "review", side_effect=lambda *args, **kwargs: _make_review()),
                patch.object(
                    pipeline, "_capture_git_diff", side_effect=lambda *args, **kwargs: "diff --git a/file.py b/file.py"
                ),
                patch.object(pipeline, "_commit_and_push", side_effect=lambda *args, **kwargs: True),
            ):
                await pipeline.execute_run(
                    chat=adapter,
                    thread_id=THREAD_ID,
                    repo_full_name="owner/repo",
                    issue=make_test_issue(),
                )

            return (
                state_store.load_artifact(ISSUE_KEY, "command_results.json"),
                state_store.load_artifact(ISSUE_KEY, "verification_plan.json"),
            )

        command_results, verification_plan = asyncio.run(run_case())
        self.assertEqual("bootstrap", command_results["results"][0]["category"])
        self.assertEqual("sh -c 'exit 0'", command_results["results"][0]["command"])
        self.assertEqual("sh -c 'exit 0'", command_results["results"][1]["command"])
        self.assertEqual("python-basic", verification_plan["profile"])
        self.assertEqual(["sh -c 'exit 0'"], verification_plan["bootstrap_commands"])


class TestVerificationFailure(unittest.TestCase):
    def test_verification_failure_notifies_and_marks_failed(self) -> None:
        async def run_case() -> tuple[list[str], dict[str, Any]]:
            tmpdir = tempfile.TemporaryDirectory()
            workspace_dir = tempfile.mkdtemp()
            pipeline, state_store, adapter, codex_log_path = _build_sync_pipeline_case(
                workspace_dir=workspace_dir,
                state_dir=tmpdir.name,
            )
            with (
                patch.object(
                    pipeline.workspace_manager,
                    "prepare",
                    side_effect=lambda *args, **kwargs: _make_workspace_info(workspace_dir),
                ),
                patch.object(
                    pipeline.codex_runner,
                    "run",
                    side_effect=lambda *args, **kwargs: _make_codex_result(
                        returncode=0, stdout_path=str(codex_log_path)
                    ),
                ),
                patch.object(pipeline, "_detect_changed_files", side_effect=lambda *args, **kwargs: ["file.py"]),
                patch("app.pipeline.load_workflow", return_value={"commands": {}}),
                patch("app.pipeline.workflow_text", return_value="workflow text"),
                patch.object(
                    pipeline.claude_runner, "verify", side_effect=lambda *args, **kwargs: _make_verification("failed")
                ),
            ):
                await pipeline.execute_run(
                    chat=adapter, thread_id=THREAD_ID, repo_full_name="owner/repo", issue=make_test_issue()
                )
            return adapter.messages_for(THREAD_ID), state_store.load_meta(ISSUE_KEY)

        messages, meta = asyncio.run(run_case())
        self.assertTrue(any("verification が失敗しました" in item for item in messages))
        self.assertEqual("Rework", meta.get("status"))


class TestReviewReject(unittest.TestCase):
    def test_review_reject_aborts_pr(self) -> None:
        async def run_case() -> tuple[list[str], dict[str, Any]]:
            tmpdir = tempfile.TemporaryDirectory()
            workspace_dir = tempfile.mkdtemp()
            pipeline, state_store, adapter, codex_log_path = _build_sync_pipeline_case(
                workspace_dir=workspace_dir, state_dir=tmpdir.name
            )
            with (
                patch.object(
                    pipeline.workspace_manager,
                    "prepare",
                    side_effect=lambda *args, **kwargs: _make_workspace_info(workspace_dir),
                ),
                patch.object(
                    pipeline.codex_runner,
                    "run",
                    side_effect=lambda *args, **kwargs: _make_codex_result(
                        returncode=0, stdout_path=str(codex_log_path)
                    ),
                ),
                patch.object(pipeline, "_detect_changed_files", side_effect=lambda *args, **kwargs: ["file.py"]),
                patch("app.pipeline.load_workflow", return_value={"commands": {}}),
                patch("app.pipeline.workflow_text", return_value="workflow text"),
                patch.object(
                    pipeline.claude_runner, "verify", side_effect=lambda *args, **kwargs: _make_verification()
                ),
                patch.object(
                    pipeline.claude_runner, "review", side_effect=lambda *args, **kwargs: _make_review("reject")
                ),
                patch.object(
                    pipeline, "_capture_git_diff", side_effect=lambda *args, **kwargs: "diff --git a/file.py b/file.py"
                ),
            ):
                await pipeline.execute_run(
                    chat=adapter, thread_id=THREAD_ID, repo_full_name="owner/repo", issue=make_test_issue()
                )
            return adapter.messages_for(THREAD_ID), state_store.load_meta(ISSUE_KEY)

        messages, meta = asyncio.run(run_case())
        self.assertTrue(any("review が reject" in item for item in messages))
        self.assertEqual("Rework", meta.get("status"))


class TestNoChanges(unittest.TestCase):
    def test_no_changes_aborts_pr(self) -> None:
        async def run_case() -> tuple[list[str], dict[str, Any]]:
            tmpdir = tempfile.TemporaryDirectory()
            workspace_dir = tempfile.mkdtemp()
            pipeline, state_store, adapter, codex_log_path = _build_sync_pipeline_case(
                workspace_dir=workspace_dir, state_dir=tmpdir.name
            )
            with (
                patch.object(
                    pipeline.workspace_manager,
                    "prepare",
                    side_effect=lambda *args, **kwargs: _make_workspace_info(workspace_dir),
                ),
                patch.object(
                    pipeline.codex_runner,
                    "run",
                    side_effect=lambda *args, **kwargs: _make_codex_result(
                        returncode=0, stdout_path=str(codex_log_path)
                    ),
                ),
                patch.object(pipeline, "_detect_changed_files", side_effect=lambda *args, **kwargs: ["file.py"]),
                patch("app.pipeline.load_workflow", return_value={"commands": {}}),
                patch("app.pipeline.workflow_text", return_value="workflow text"),
                patch.object(
                    pipeline.claude_runner, "verify", side_effect=lambda *args, **kwargs: _make_verification()
                ),
                patch.object(pipeline.claude_runner, "review", side_effect=lambda *args, **kwargs: _make_review()),
                patch.object(
                    pipeline, "_capture_git_diff", side_effect=lambda *args, **kwargs: "diff --git a/file.py b/file.py"
                ),
                patch.object(pipeline, "_commit_and_push", side_effect=lambda *args, **kwargs: False),
            ):
                await pipeline.execute_run(
                    chat=adapter, thread_id=THREAD_ID, repo_full_name="owner/repo", issue=make_test_issue()
                )
            return adapter.messages_for(THREAD_ID), state_store.load_meta(ISSUE_KEY)

        messages, meta = asyncio.run(run_case())
        self.assertTrue(any("変更差分が作られなかった" in item for item in messages))
        self.assertEqual("Rework", meta.get("status"))


class TestMissingArtifacts(PipelineE2EBase):
    async def test_missing_artifacts_aborts_pr(self) -> None:
        # Use a workflow that requires artifacts we won't produce
        workflow = {
            "commands": {},
            "proof_of_work": {
                "required_artifacts": [
                    "issue_snapshot.json",
                    "requirement_summary.json",
                    "plan.json",
                    "test_plan.json",
                    "changed_files.json",
                    "verification.json",
                    "final_summary.json",
                    "run.log",
                    "workpad_updates.jsonl",
                    "runner_metadata.json",
                    "nonexistent_artifact.json",  # This won't be available
                ],
            },
        }
        with (
            self._patch_workspace(),
            self._patch_codex(),
            self._patch_detect_changed(),
            self._patch_workflow(workflow),
            self._patch_workflow_text(),
            self._patch_verify(),
            self._patch_review(),
            self._patch_git_diff(),
            self._patch_commit_push(True),
        ):
            await self.pipeline.execute_run(
                chat=self.adapter,
                thread_id=THREAD_ID,
                repo_full_name="owner/repo",
                issue=make_test_issue(),
            )

        self.adapter.assert_message_contains(THREAD_ID, "proof-of-work artifact が不足")
        self.assertEqual(self.state_store.load_meta(ISSUE_KEY).get("status"), "Rework")


class TestPolicyViolation(PipelineE2EBase):
    async def test_policy_violation_stops_run(self) -> None:
        workflow = {
            "commands": {
                "setup": ["rm -rf /"],  # High risk command
            },
        }
        with (
            self._patch_workspace(),
            patch.object(self.pipeline.codex_runner, "run") as codex_run,
            patch.object(self.pipeline.claude_runner, "verify") as verify,
            self._patch_detect_changed(),
            self._patch_workflow(workflow),
            self._patch_workflow_text(),
        ):
            await self.pipeline.execute_run(
                chat=self.adapter,
                thread_id=THREAD_ID,
                repo_full_name="owner/repo",
                issue=make_test_issue(),
            )

        codex_run.assert_not_called()
        verify.assert_not_called()
        self.adapter.assert_message_contains(THREAD_ID, "禁止または高リスク")
        self.assertEqual(self.state_store.load_meta(ISSUE_KEY).get("status"), "Human Review")
