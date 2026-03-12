"""Pipeline E2E tests using InMemoryAdapter — no Discord server required."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from app.approvals import ApprovalCoordinator
from app.pipeline import DevelopmentPipeline
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


def _make_codex_result(returncode: int = 0, stdout_path: str = "/dev/null", mode: str = "app-server") -> CodexRunResult:
    return CodexRunResult(
        returncode=returncode,
        stdout_path=stdout_path,
        changed_files=["file.py"],
        summary="done",
        mode=mode,
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
            [("lint", "ruff check ."), ("tests", "pytest -q")],
            commands,
        )

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
        self.github_client.ready_pull_request_for_review.assert_called_with("owner/repo", 99)
        self.assertFalse(self.state_store.load_artifact(ISSUE_KEY, "pr.json")["draft"])

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


class TestCodexFailure(PipelineE2EBase):
    async def test_codex_failure_notifies_and_marks_failed(self) -> None:
        with (
            self._patch_workspace(),
            self._patch_codex(returncode=1),
            self._patch_detect_changed(),
            self._patch_workflow(),
            self._patch_workflow_text(),
        ):
            await self.pipeline.execute_run(
                chat=self.adapter,
                thread_id=THREAD_ID,
                repo_full_name="owner/repo",
                issue=make_test_issue(),
            )

        self.adapter.assert_message_contains(THREAD_ID, "Codex 実装で失敗しました")
        self.assertEqual(self.state_store.load_meta(ISSUE_KEY).get("status"), "Rework")


class TestVerificationFailure(PipelineE2EBase):
    async def test_verification_failure_notifies_and_marks_failed(self) -> None:
        with (
            self._patch_workspace(),
            self._patch_codex(),
            self._patch_detect_changed(),
            self._patch_workflow(),
            self._patch_workflow_text(),
            self._patch_verify(status="failed"),
        ):
            await self.pipeline.execute_run(
                chat=self.adapter,
                thread_id=THREAD_ID,
                repo_full_name="owner/repo",
                issue=make_test_issue(),
            )

        self.adapter.assert_message_contains(THREAD_ID, "verification が失敗しました")
        self.assertEqual(self.state_store.load_meta(ISSUE_KEY).get("status"), "Rework")


class TestReviewReject(PipelineE2EBase):
    async def test_review_reject_aborts_pr(self) -> None:
        with (
            self._patch_workspace(),
            self._patch_codex(),
            self._patch_detect_changed(),
            self._patch_workflow(),
            self._patch_workflow_text(),
            self._patch_verify(),
            self._patch_review(decision="reject"),
            self._patch_git_diff(),
        ):
            await self.pipeline.execute_run(
                chat=self.adapter,
                thread_id=THREAD_ID,
                repo_full_name="owner/repo",
                issue=make_test_issue(),
            )

        self.adapter.assert_message_contains(THREAD_ID, "review が reject")
        self.assertEqual(self.state_store.load_meta(ISSUE_KEY).get("status"), "Rework")


class TestNoChanges(PipelineE2EBase):
    async def test_no_changes_aborts_pr(self) -> None:
        with (
            self._patch_workspace(),
            self._patch_codex(),
            self._patch_detect_changed(),
            self._patch_workflow(),
            self._patch_workflow_text(),
            self._patch_verify(),
            self._patch_review(),
            self._patch_git_diff(),
            self._patch_commit_push(False),
        ):
            await self.pipeline.execute_run(
                chat=self.adapter,
                thread_id=THREAD_ID,
                repo_full_name="owner/repo",
                issue=make_test_issue(),
            )

        self.adapter.assert_message_contains(THREAD_ID, "変更差分が作られなかった")
        self.assertEqual(self.state_store.load_meta(ISSUE_KEY).get("status"), "Rework")


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
            self._patch_codex(),
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

        self.adapter.assert_message_contains(THREAD_ID, "禁止または高リスク")
        self.assertEqual(self.state_store.load_meta(ISSUE_KEY).get("status"), "Human Review")
