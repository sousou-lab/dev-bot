from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

from app.approvals import ApprovalCoordinator
from app.contracts.workflow_schema import (
    CandidateModeConfig,
    CodexConfig,
    ImplementationConfig,
    IncidentBundleConfig,
    ReplanningConfig,
    WorkflowConfig,
)
from app.pipeline import CandidateExecutionResult, DevelopmentPipeline
from app.process_registry import ProcessRegistry
from app.state_store import FileStateStore
from tests.helpers import make_test_settings


class PipelineUnitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.state_store = FileStateStore(self.tmpdir.name)
        self.settings = make_test_settings(workspace_root=self.tmpdir.name, state_dir=self.tmpdir.name)
        self.github_client = MagicMock()
        self.pipeline = DevelopmentPipeline(
            settings=self.settings,
            state_store=self.state_store,
            github_client=self.github_client,
            process_registry=ProcessRegistry(self.tmpdir.name),
            approval_coordinator=ApprovalCoordinator(self.state_store),
        )

        async def _run_blocking(func, /, *args, **kwargs):
            return func(*args, **kwargs)

        self.pipeline._run_blocking = _run_blocking  # type: ignore[method-assign]

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def _candidate_result(
        self,
        *,
        candidate_id: str,
        changed_files: list[str] | None = None,
        review: dict[str, object] | None = None,
        scope_analysis: dict[str, object] | None = None,
        proof_result: dict[str, object] | None = None,
        duration_ms: int = 0,
    ) -> CandidateExecutionResult:
        workspace = Path(self.tmpdir.name) / candidate_id
        workspace.mkdir(parents=True, exist_ok=True)
        return CandidateExecutionResult(
            candidate_id=candidate_id,
            workspace_info={
                "workspace": str(workspace),
                "branch_name": f"agent/{candidate_id}",
                "workspace_key": "owner/repo#test",
            },
            codex_result=SimpleNamespace(mode="app-server"),
            codex_log_path=str(workspace / "codex.log"),
            changed_files={"changed_files": changed_files or ["app/x.py"]},
            scope_analysis=scope_analysis
            or {
                "unexpected_file_count": 0,
                "must_not_touch_violations": [],
                "protected_config_violations": [],
            },
            command_results={"results": [{"category": "hard", "status": "pass"}]},
            verification={
                "status": "success",
                "failure_type": "",
                "retry_recommended": False,
                "human_check_recommended": False,
                "notes": [],
            },
            verification_json={"status": "pass"},
            review=review
            or {
                "decision": "approve",
                "risk_items": [],
                "test_gaps": [],
                "unnecessary_changes": [],
                "protected_path_touches": [],
                "postable_findings": [],
            },
            proof_result=proof_result or {"complete": True, "missing_artifacts": []},
            success=True,
            duration_ms=duration_ms,
        )

    def test_commit_and_push_bootstraps_main_when_remote_branch_is_missing(self) -> None:
        completed = [
            SimpleNamespace(stdout=" M README.md\n", returncode=0),
            SimpleNamespace(stdout="", returncode=0),
            SimpleNamespace(stdout="", returncode=0),
            SimpleNamespace(stdout="", returncode=0),
            SimpleNamespace(stdout="", returncode=0),
        ]

        with patch("app.pipeline.subprocess.run", side_effect=completed) as run_mock:
            self.pipeline.workspace_manager.push_branch = MagicMock()  # type: ignore[method-assign]

            pushed = self.pipeline._commit_and_push("/tmp/work", "agent/gh-1-test", 1)

        self.assertTrue(pushed)
        self.pipeline.workspace_manager.push_branch.assert_called_once_with(  # type: ignore[attr-defined]
            "/tmp/work", "agent/gh-1-test", bootstrap_base_branch="main"
        )
        self.assertEqual(
            [
                call(["git", "-C", "/tmp/work", "status", "--porcelain"], capture_output=True, text=True, check=True),
                call(["git", "-C", "/tmp/work", "add", "-A"], check=True),
                call(
                    ["git", "-C", "/tmp/work", "commit", "-m", "feat: automated changes for issue #1"],
                    check=True,
                ),
                call(["git", "-C", "/tmp/work", "rev-parse", "--verify", "main"], capture_output=True),
                call(
                    ["git", "-C", "/tmp/work", "ls-remote", "--heads", "origin", "main"],
                    capture_output=True,
                    text=True,
                    check=True,
                ),
            ],
            run_mock.call_args_list,
        )

    def test_sync_runner_generated_artifacts_imports_implementation_result(self) -> None:
        issue_key = "owner/repo#1"
        self.state_store.create_issue_record(issue_key)
        run_id = self.state_store.create_execution_run(issue_key)
        artifacts_dir = Path(self.tmpdir.name) / "runner-artifacts"
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        (artifacts_dir / "implementation_result.json").write_text(
            '{"candidate_id":"primary","summary":"ok","changed_files":["app/x.py"]}',
            encoding="utf-8",
        )

        self.pipeline._sync_runner_generated_artifacts(issue_key=issue_key, run_id=run_id, artifacts_dir=artifacts_dir)

        payload = self.state_store.load_artifact(issue_key, "implementation_result.json")
        self.assertEqual("ok", payload["summary"])

    def test_materialize_and_finalize_incident_bundle(self) -> None:
        issue_key = "owner/repo#2"
        self.state_store.create_issue_record(issue_key)
        run_id = self.state_store.create_execution_run(issue_key)
        workflow = {"config": WorkflowConfig(incident_bundle=IncidentBundleConfig())}

        bundle_dir = self.pipeline._materialize_incident_bundle(
            workflow=workflow,
            issue_key=issue_key,
            run_id=run_id,
            workspace="/tmp/work",
            issue={"title": "Issue"},
            summary={"goal": "Goal"},
            plan={"steps": ["one"]},
            test_plan={"unit": []},
        )
        assert bundle_dir is not None

        self.pipeline._finalize_incident_bundle(bundle_dir, issue_key, run_id)

        manifest = self.state_store.load_execution_artifact(issue_key, "incident_bundle_manifest.json", run_id)
        self.assertEqual(issue_key, manifest["issue_key"])

    def test_should_allow_turn_steer_forces_true_when_handoff_bundle_exists(self) -> None:
        allowed = self.pipeline._should_allow_turn_steer(
            workflow={"codex": {"allow_turn_steer": False}},
            handoff_bundle={"rollover_id": "handoff-001"},
        )

        self.assertTrue(allowed)

    def test_should_allow_turn_steer_prefers_workflow_config(self) -> None:
        allowed = self.pipeline._should_allow_turn_steer(
            workflow={"config": WorkflowConfig(codex=CodexConfig(allow_turn_steer=True))},
            handoff_bundle=None,
        )

        self.assertTrue(allowed)

    def test_should_allow_thread_resume_same_run_only_prefers_workflow_config(self) -> None:
        allowed = self.pipeline._should_allow_thread_resume_same_run_only(
            workflow={"config": WorkflowConfig(codex=CodexConfig(allow_thread_resume_same_run_only=False))}
        )

        self.assertFalse(allowed)

    def test_should_rollover_session_uses_thresholds(self) -> None:
        self.assertFalse(self.pipeline._should_rollover_session(turn_count=11, steer_count=1, repair_cycles=2))
        self.assertTrue(self.pipeline._should_rollover_session(turn_count=12, steer_count=0, repair_cycles=0))
        self.assertTrue(self.pipeline._should_rollover_session(turn_count=0, steer_count=2, repair_cycles=0))
        self.assertTrue(self.pipeline._should_rollover_session(turn_count=0, steer_count=0, repair_cycles=3))

    def test_should_rollover_session_uses_workflow_compaction_policy(self) -> None:
        workflow = {
            "config": WorkflowConfig(
                codex=CodexConfig(
                    compaction_policy=SimpleNamespace(turn_count_gte=20, steer_count_gte=5, repair_cycles_gte=4)
                )
            )
        }

        self.assertFalse(
            self.pipeline._should_rollover_session(turn_count=12, steer_count=2, repair_cycles=3, workflow=workflow)
        )
        self.assertTrue(
            self.pipeline._should_rollover_session(turn_count=20, steer_count=0, repair_cycles=0, workflow=workflow)
        )

    def test_candidate_parallelism_uses_typed_candidate_mode_config(self) -> None:
        workflow = {
            "config": WorkflowConfig(
                implementation=ImplementationConfig(
                    backend="codex-app-server",
                    candidate_mode=CandidateModeConfig(max_parallel_editors=2),
                )
            )
        }

        parallelism = self.pipeline._candidate_parallelism(workflow=workflow, candidate_count=2)

        self.assertEqual(2, parallelism)

    def test_write_session_handoff_bundle_persists_current_rollover(self) -> None:
        issue_key = "owner/repo#3"
        self.state_store.create_issue_record(issue_key)
        attempt_id = self.state_store.create_attempt(issue_key)

        bundle = self.pipeline._write_session_handoff_bundle(
            issue_key=issue_key,
            attempt_id=attempt_id,
            candidate_id="alt1",
            reason="review_reject",
            branch_name="agent/gh-3-test-att-001-alt1",
            workspace="/tmp/work",
            head_sha="abc123",
            dirty_files=["app/x.py"],
            plan_v2={"goal": "Ship it", "must_not_touch": ["WORKFLOW.md"], "verification_focus": ["tests"]},
            latest_failures=[{"stage": "review", "message": "fix failing tests"}],
            session_snapshot={"session_id": "thread_prev", "turn_count": 4},
        )

        self.assertEqual("handoff-001", bundle["rollover_id"])
        self.assertEqual(4, bundle["current_status"]["session"]["turn_count"])
        self.assertEqual(
            bundle,
            self.state_store.load_candidate_artifact(issue_key, attempt_id, "alt1", "session_handoff_bundle.json"),
        )
        handoff_history = (
            self.state_store.candidate_dir(issue_key, attempt_id, "alt1") / "handoffs" / "handoff-001.json"
        )
        self.assertTrue(handoff_history.exists())

    def test_rollover_same_candidate_preserves_attempt_and_candidate_identity(self) -> None:
        issue_key = "owner/repo#4"
        self.state_store.create_issue_record(issue_key)
        attempt_id = self.state_store.create_attempt(issue_key)
        workspace = Path(self.tmpdir.name) / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        self.state_store.write_candidate_artifact(
            issue_key,
            attempt_id,
            "primary",
            "session_checkpoint.json",
            {
                "issue_key": issue_key,
                "attempt_id": attempt_id,
                "candidate_id": "primary",
                "session_id": "thread_prev",
                "turn_count": 1,
                "steer_count": 0,
                "repair_cycles": 0,
            },
        )

        async def run_case() -> None:
            channel = MagicMock()
            with (
                patch.object(self.pipeline, "_git_head_sha", return_value="abc123"),
                patch.object(self.pipeline, "_detect_changed_files", return_value=["app/x.py"]),
                patch.object(
                    self.pipeline,
                    "_read_session_snapshot",
                    return_value={"session_id": "thread_prev", "turn_count": 2, "status": "active"},
                ),
                patch.object(self.pipeline, "_execute_candidate") as execute_candidate,
            ):
                await self.pipeline._rollover_same_candidate(
                    chat_client=MagicMock(),
                    channel=channel,
                    issue_key=issue_key,
                    run_id="run-1",
                    workflow={"config": WorkflowConfig(codex=CodexConfig(allow_turn_steer=False))},
                    issue={"number": 4},
                    summary={},
                    plan={"goal": "Ship it", "must_not_touch": ["WORKFLOW.md"], "verification_focus": ["tests"]},
                    test_plan={},
                    verification_plan={},
                    attempt_id=attempt_id,
                    workspace_info={
                        "workspace": str(workspace),
                        "branch_name": "agent/gh-4-test-att-001-primary",
                        "workspace_key": issue_key,
                    },
                    candidate_id="primary",
                    reason="session_compaction",
                    latest_failures=[{"stage": "review", "message": "narrow fix"}],
                )

            execute_candidate.assert_awaited_once()
            kwargs = execute_candidate.await_args.kwargs
            self.assertEqual(attempt_id, kwargs["attempt_id"])
            self.assertEqual("primary", kwargs["candidate_id"])
            self.assertEqual("thread_prev", kwargs["session_id"])
            self.assertEqual("handoff-001", kwargs["handoff_bundle"]["rollover_id"])
            self.assertEqual("primary", kwargs["handoff_bundle"]["candidate_id"])

        import asyncio

        asyncio.run(run_case())
        bundle = self.state_store.load_candidate_artifact(
            issue_key, attempt_id, "primary", "session_handoff_bundle.json"
        )
        self.assertEqual(attempt_id, bundle["attempt_id"])
        self.assertEqual("primary", bundle["candidate_id"])
        checkpoint = self.state_store.load_candidate_artifact(
            issue_key, attempt_id, "primary", "session_checkpoint.json"
        )
        self.assertEqual(issue_key, checkpoint["issue_key"])
        self.assertEqual("thread_prev", checkpoint["session_id"])
        self.assertEqual(2, checkpoint["turn_count"])
        self.assertEqual("active", checkpoint["session_snapshot"]["status"])
        self.assertEqual(1, checkpoint["repair_cycles"])

    def test_protected_config_allowlist_required_even_with_label(self) -> None:
        issue_key = "owner/repo#5"
        self.state_store.create_issue_record(issue_key)

        violations, label_present, allowlist = self.pipeline._protected_config_violations(
            changed_files=["WORKFLOW.md"],
            issue={
                "labels": [{"name": "allow-protected-config"}],
                "body": "## Context\nNo allowlist here.\n",
            },
            issue_key=issue_key,
        )

        self.assertEqual(["WORKFLOW.md"], violations)
        self.assertTrue(label_present)
        self.assertEqual([], allowlist)

    def test_protected_config_issue_body_allowlist_allows_listed_path(self) -> None:
        issue_key = "owner/repo#6"
        self.state_store.create_issue_record(issue_key)

        violations, label_present, allowlist = self.pipeline._protected_config_violations(
            changed_files=["WORKFLOW.md"],
            issue={
                "labels": [{"name": "allow-protected-config"}],
                "body": "## 保護設定変更許可リスト\n- WORKFLOW.md\n",
            },
            issue_key=issue_key,
        )

        self.assertEqual([], violations)
        self.assertTrue(label_present)
        self.assertEqual(["WORKFLOW.md"], allowlist)

    def test_protected_config_artifact_allowlist_is_used_as_fallback(self) -> None:
        issue_key = "owner/repo#7"
        self.state_store.create_issue_record(issue_key)
        self.state_store.write_artifact(issue_key, "protected_config_allowlist.json", {"allowlist": ["WORKFLOW.md"]})

        violations, label_present, allowlist = self.pipeline._protected_config_violations(
            changed_files=["WORKFLOW.md"],
            issue={
                "labels": [{"name": "allow-protected-config"}],
                "body": "## Context\nFallback to artifact.\n",
            },
            issue_key=issue_key,
        )

        self.assertEqual([], violations)
        self.assertTrue(label_present)
        self.assertEqual(["WORKFLOW.md"], allowlist)

    def test_build_repair_instructions_and_review_delta_from_review_findings(self) -> None:
        review_result = {
            "decision": "repairable",
            "scope_drift": False,
            "protected_contract_ok": True,
            "reject_reasons": ["fix invalid token path"],
            "severity_counts": {"critical": 0, "high": 1, "medium": 0, "low": 0},
            "verified_finding_count": 1,
            "unverified_finding_count": 0,
        }
        review_findings = {
            "risk_items": ["return 401 instead of 500"],
            "postable_findings": [
                {
                    "id": "R1",
                    "severity": "high",
                    "claim": "invalid token returns 500",
                    "suggested_fix": "return 401 for invalid token",
                }
            ],
        }

        repair = self.pipeline._build_repair_instructions(
            candidate_id="primary",
            review_result=review_result,
            review_findings=review_findings,
        )
        delta = self.pipeline._build_latest_review_delta(
            candidate_id="primary",
            review_result=review_result,
            review_findings=review_findings,
        )
        feedback = self.pipeline._build_latest_repair_feedback(
            candidate_id="primary",
            verification={"failure_type": "", "retry_recommended": True, "notes": ["rerun tests"]},
            repair_instructions=repair,
        )

        self.assertTrue(repair["applicable"])
        self.assertEqual("narrow", repair["scope"])
        self.assertIn("return 401 for invalid token", repair["instructions"])
        self.assertIn("Do not touch protected config or must_not_touch files.", repair["instructions"])
        self.assertEqual(["R1"], delta["finding_ids"])
        self.assertIn("decision=repairable", delta["summary"])
        self.assertTrue(feedback["retry_recommended"])
        self.assertIn("rerun tests", feedback["notes"])
        self.assertEqual("repair_feedback.json", feedback["repair_feedback_ref"])

    def test_build_review_findings_payload_strips_summary_fields(self) -> None:
        payload = self.pipeline._build_review_findings_payload(
            {
                "decision": "approve",
                "risk_items": ["keep scope tight"],
                "postable_findings": [{"id": "R1", "severity": "low"}],
                "findings": [{"id": "R0", "severity": "medium"}],
            }
        )

        self.assertEqual(1, payload["version"])
        self.assertEqual([{"id": "R0", "severity": "medium"}], payload["findings"])
        self.assertEqual([{"id": "R1", "severity": "low"}], payload["postable_findings"])
        self.assertNotIn("decision", payload)
        self.assertNotIn("risk_items", payload)

    def test_build_review_decision_and_postable_findings_payloads(self) -> None:
        review_result = {
            "candidate_id": "primary",
            "decision": "reject",
            "reject_reasons": ["scope drift"],
            "severity_counts": {"high": 1},
            "verified_finding_count": 1,
            "unverified_finding_count": 0,
            "plan_alignment_ok": False,
            "scope_drift": True,
            "protected_contract_ok": True,
        }

        decision = self.pipeline._build_review_decision_payload(review_result)
        postable = self.pipeline._build_postable_findings_payload(
            {"postable_findings": [{"id": "R1", "severity": "high"}]}
        )

        self.assertEqual("reject", decision["decision"])
        self.assertEqual("postable_findings.json", decision["postable_findings_ref"])
        self.assertEqual([{"id": "R1", "severity": "high"}], postable["findings"])

    def test_build_repair_feedback_marks_failed_fast_repair_checks(self) -> None:
        payload = self.pipeline._build_repair_feedback(
            candidate_id="primary",
            repair_results={
                "repair_profile": "python-fast-repair",
                "results": [
                    {"phase": "lint", "status": "fail", "output": "F401 unused import"},
                    {"phase": "format", "status": "pass", "output": ""},
                ],
            },
        )

        self.assertTrue(payload["applicable"])
        self.assertEqual("python-fast-repair", payload["repair_profile"])
        self.assertEqual("lint", payload["issues"][0]["tool"])

    def test_can_fast_repair_reuse_requires_narrow_change_and_known_tools(self) -> None:
        self.assertTrue(
            self.pipeline._can_fast_repair_reuse(
                repair_feedback={"issues": [{"tool": "lint", "message": "fix lint"}]},
                changed_files={"changed_files": ["app/x.py"]},
            )
        )
        self.assertFalse(
            self.pipeline._can_fast_repair_reuse(
                repair_feedback={"issues": [{"tool": "custom", "message": "fix"}]},
                changed_files={"changed_files": ["app/x.py"]},
            )
        )

    def test_build_candidate_strategy_uses_legacy_and_structured_task_sources(self) -> None:
        strategy = self.pipeline._build_candidate_strategy(
            candidate_id="alt1",
            plan={
                "candidate_files": ["app/service.py"],
                "must_not_touch": ["WORKFLOW.md"],
                "verification_focus": ["keep API stable"],
                "tasks": [{"summary": "Wire candidate execution"}, {"summary": "Persist winner artifacts"}],
                "implementation_steps": ["Wire candidate execution", "Update tests"],
                "steps": ["Legacy fallback step"],
            },
            test_plan={"unit": ["tests/test_pipeline.py"], "integration": [], "manual_checks": []},
        )

        self.assertEqual(
            [
                "Wire candidate execution",
                "Persist winner artifacts",
                "Update tests",
                "Legacy fallback step",
            ],
            strategy["tasks"],
        )

    def test_evaluate_attempt_proof_requires_winner_candidate_proof(self) -> None:
        issue_key = "owner/repo#8"
        self.state_store.create_issue_record(issue_key)
        attempt_id = self.state_store.create_attempt(issue_key)
        self.state_store.write_attempt_artifact(issue_key, attempt_id, "attempt_manifest.json", {"status": "running"})
        self.state_store.write_attempt_artifact(issue_key, attempt_id, "scope_contract.json", {"version": 1})
        self.state_store.write_attempt_artifact(
            issue_key, attempt_id, "candidate_decision.json", {"enabled": False, "candidate_ids": ["primary"]}
        )
        self.state_store.write_attempt_artifact(
            issue_key, attempt_id, "winner_selection.json", {"winner_candidate_id": "primary"}
        )
        self.state_store.write_attempt_artifact(
            issue_key,
            attempt_id,
            "final_attempt_summary.json",
            {"winner_candidate_id": "primary", "status": "winner_selected"},
        )
        self.state_store.write_candidate_artifact(
            issue_key, attempt_id, "primary", "proof_result.json", {"complete": True}
        )
        views_dir = self.state_store.views_dir(issue_key)
        for filename in (
            "runner_metadata.json",
            "implementation_result.json",
            "changed_files.json",
            "scope_analysis.json",
            "repair_feedback.json",
            "command_results.json",
            "verification_result.json",
            "verification.json",
            "review_result.json",
            "review_decision.json",
            "review_findings.json",
            "postable_findings.json",
        ):
            (views_dir / filename).write_text("{}", encoding="utf-8")
        candidate_dir = self.state_store.candidate_artifacts_dir(issue_key, attempt_id, "primary")
        candidate_dir.mkdir(parents=True, exist_ok=True)
        (candidate_dir / "repair_feedback.jsonl").write_text("{}\n", encoding="utf-8")

        proof = self.pipeline._evaluate_attempt_proof(
            issue_key=issue_key,
            attempt_id=attempt_id,
            winner_candidate_id="primary",
        )

        self.assertTrue(proof["complete"])
        self.assertIn("candidates/primary/proof_result.json", proof["required_artifacts_present"])
        self.assertIn("final_attempt_summary.json", proof["required_artifacts_present"])
        self.assertIn("views/verification_result.json", proof["required_artifacts_present"])
        self.assertIn("views/review_result.json", proof["required_artifacts_present"])

    def test_select_candidate_result_marks_exact_tie(self) -> None:
        primary = self._candidate_result(candidate_id="primary", review={"postable_findings": []})
        alt1 = self._candidate_result(candidate_id="alt1", review={"postable_findings": []})

        winner, selection = self.pipeline._select_candidate_result(
            plan={"candidate_files": ["app/x.py"]},
            candidate_results=[primary, alt1],
        )

        self.assertIsNotNone(winner)
        self.assertTrue(selection["exact_tie_detected"])
        self.assertEqual(["alt1", "primary"], selection["tied_candidate_ids"])
        self.assertEqual(
            "requires exact tie-break judge after identical deterministic rank", selection["winner_reason"]
        )

    def test_select_candidate_result_prefers_plan_aligned_candidate(self) -> None:
        primary = self._candidate_result(
            candidate_id="primary",
            review={
                "decision": "approve",
                "risk_items": [],
                "test_gaps": [],
                "unnecessary_changes": ["touched extra file"],
                "protected_path_touches": [],
                "postable_findings": [],
            },
        )
        alt1 = self._candidate_result(candidate_id="alt1")

        winner, selection = self.pipeline._select_candidate_result(
            plan={"candidate_files": ["app/x.py"]},
            candidate_results=[primary, alt1],
        )

        self.assertIsNotNone(winner)
        self.assertEqual("alt1", winner.candidate_id)
        self.assertIn("plan_alignment", selection["winner_reason"])

    def test_select_candidate_result_uses_duration_as_final_tiebreak(self) -> None:
        primary = self._candidate_result(candidate_id="primary", duration_ms=2500)
        alt1 = self._candidate_result(candidate_id="alt1", duration_ms=1200)

        winner, selection = self.pipeline._select_candidate_result(
            plan={"candidate_files": ["app/x.py"]},
            candidate_results=[primary, alt1],
        )

        self.assertIsNotNone(winner)
        self.assertEqual("alt1", winner.candidate_id)
        self.assertIn("duration_ms", selection["winner_reason"])

    def test_evaluate_attempt_proof_requires_tiebreak_artifact_for_exact_tie(self) -> None:
        issue_key = "owner/repo#9"
        self.state_store.create_issue_record(issue_key)
        attempt_id = self.state_store.create_attempt(issue_key)
        self.state_store.write_attempt_artifact(issue_key, attempt_id, "attempt_manifest.json", {"status": "running"})
        self.state_store.write_attempt_artifact(issue_key, attempt_id, "scope_contract.json", {"version": 1})
        self.state_store.write_attempt_artifact(
            issue_key, attempt_id, "candidate_decision.json", {"enabled": True, "candidate_ids": ["primary", "alt1"]}
        )
        self.state_store.write_attempt_artifact(
            issue_key,
            attempt_id,
            "winner_selection.json",
            {
                "winner_candidate_id": "primary",
                "exact_tie_detected": True,
                "tied_candidate_ids": ["primary", "alt1"],
            },
        )
        self.state_store.write_attempt_artifact(
            issue_key,
            attempt_id,
            "final_attempt_summary.json",
            {"winner_candidate_id": "primary", "status": "winner_selected"},
        )
        self.state_store.write_candidate_artifact(
            issue_key, attempt_id, "primary", "proof_result.json", {"complete": True}
        )
        views_dir = self.state_store.views_dir(issue_key)
        for filename in (
            "runner_metadata.json",
            "implementation_result.json",
            "changed_files.json",
            "scope_analysis.json",
            "repair_feedback.json",
            "command_results.json",
            "verification_result.json",
            "verification.json",
            "review_result.json",
            "review_decision.json",
            "review_findings.json",
            "postable_findings.json",
        ):
            (views_dir / filename).write_text("{}", encoding="utf-8")
        candidate_dir = self.state_store.candidate_artifacts_dir(issue_key, attempt_id, "primary")
        candidate_dir.mkdir(parents=True, exist_ok=True)
        (candidate_dir / "repair_feedback.jsonl").write_text("{}\n", encoding="utf-8")

        proof = self.pipeline._evaluate_attempt_proof(
            issue_key=issue_key,
            attempt_id=attempt_id,
            winner_candidate_id="primary",
        )

        self.assertFalse(proof["complete"])
        self.assertIn("winner_tiebreak_judge.json", proof["missing_artifacts"])

        self.state_store.write_attempt_artifact(
            issue_key,
            attempt_id,
            "winner_tiebreak_judge.json",
            {"winner_candidate_id": "primary", "tied_candidate_ids": ["primary", "alt1"]},
        )
        proof = self.pipeline._evaluate_attempt_proof(
            issue_key=issue_key,
            attempt_id=attempt_id,
            winner_candidate_id="primary",
        )
        self.assertTrue(proof["complete"])
        self.assertIn("winner_tiebreak_judge.json", proof["required_artifacts_present"])

    def test_resolve_exact_tie_winner_uses_claude_judge_and_persists_artifact(self) -> None:
        issue_key = "owner/repo#10"
        self.state_store.create_issue_record(issue_key)
        run_id = self.state_store.create_execution_run(issue_key)
        attempt_id = self.state_store.create_attempt(issue_key)
        primary = self._candidate_result(
            candidate_id="primary",
            review={"decision": "approve", "risk_items": ["broader change"], "postable_findings": []},
        )
        alt1 = self._candidate_result(
            candidate_id="alt1",
            review={"decision": "approve", "risk_items": ["narrower change"], "postable_findings": []},
        )

        async def run_case() -> None:
            with patch.object(
                self.pipeline.claude_runner,
                "judge_winner_tiebreak",
                return_value={
                    "winner_candidate_id": "alt1",
                    "summary": "alt1 preserves a narrower execution path",
                    "explanation": ["alt1 keeps the same scope with fewer discretionary changes"],
                },
            ):
                winner, selection, payload = await self.pipeline._resolve_exact_tie_winner(
                    issue_key=issue_key,
                    attempt_id=attempt_id,
                    run_id=run_id,
                    plan={"goal": "ship fix", "candidate_files": ["app/x.py"]},
                    candidate_results=[primary, alt1],
                    winner_result=primary,
                    winner_selection={
                        "winner_candidate_id": "primary",
                        "exact_tie_detected": True,
                        "tied_candidate_ids": ["alt1", "primary"],
                    },
                )

            self.assertEqual("alt1", winner.candidate_id)
            self.assertEqual("exact_tie_claude_judge", selection["selection_basis"])
            self.assertEqual("winner_tiebreak_judge.json", selection["winner_tiebreak_artifact"])
            self.assertIsNotNone(payload)
            self.assertEqual(
                "alt1",
                self.state_store.load_attempt_artifact(issue_key, attempt_id, "winner_tiebreak_judge.json")[
                    "winner_candidate_id"
                ],
            )

        import asyncio

        asyncio.run(run_case())

    def test_build_verification_result_normalizes_summary_as_canonical_payload(self) -> None:
        payload = self.pipeline._build_verification_result(
            {"results": [{"phase": "lint", "status": "pass", "category": "hard"}]},
            {
                "status": "success",
                "failure_type": "",
                "retry_recommended": False,
                "human_check_recommended": False,
                "notes": ["looks good"],
            },
        )

        self.assertEqual("success", payload["status"])
        self.assertTrue(payload["hard_checks_pass"])
        self.assertEqual(["looks good"], payload["notes"])

    def test_workflow_commands_include_bootstrap_before_required_checks(self) -> None:
        commands = self.pipeline._workflow_commands(
            {
                "verification": {
                    "bootstrap_commands": ["uv sync"],
                    "required_checks": [{"name": "lint", "command": "uv run ruff check ."}],
                }
            }
        )

        self.assertEqual("bootstrap", commands[0]["category"])
        self.assertEqual("uv sync", commands[0]["command"])
        self.assertEqual("lint", commands[1]["phase"])

    def test_classify_command_failure_marks_missing_tool_as_environment_blocked(self) -> None:
        failure_type = self.pipeline._classify_command_failure(
            phase="lint",
            category="hard",
            returncode=2,
            output="error: Failed to spawn: `ruff`\nCaused by: No such file or directory (os error 2)\n",
        )

        self.assertEqual("environment_blocked", failure_type)

    def test_resolve_workflow_prefers_verification_plan_for_repo_root_fallback(self) -> None:
        resolved = self.pipeline._resolve_workflow(
            {"verification": {"required_checks": [{"name": "lint", "command": "uv run ruff check ."}]}},
            {"hard_checks": [{"name": "tests", "command": "uv run pytest -q"}]},
            prefer_verification_plan=True,
        )

        self.assertEqual("uv run pytest -q", resolved["verification"]["required_checks"][0]["command"])

    def test_should_refresh_verification_plan_when_generic_minimal_can_be_upgraded(self) -> None:
        should_refresh = self.pipeline._should_refresh_verification_plan(
            existing_verification_plan={
                "profile": "generic-minimal",
                "bootstrap_commands": [],
                "hard_checks": [{"name": "workspace_sanity", "command": "python -c 'print(1)'"}],
            },
            refreshed_verification_plan={
                "profile": "python-basic",
                "bootstrap_commands": ["uv pip install -r requirements.txt"],
                "hard_checks": [{"name": "lint", "command": "uv run --with ruff ruff check ."}],
            },
        )

        self.assertTrue(should_refresh)

    def test_can_minor_repair_reuse_requires_narrow_repairable_conditions(self) -> None:
        allowed = self.pipeline._can_minor_repair_reuse(
            review_result={
                "decision": "repairable",
                "scope_drift": False,
                "protected_contract_ok": True,
                "severity_counts": {"critical": 0, "high": 0, "medium": 1, "low": 0},
            },
            verification={"status": "success", "failure_type": ""},
            changed_files={"changed_files": ["app/x.py", "tests/test_x.py"]},
        )
        blocked = self.pipeline._can_minor_repair_reuse(
            review_result={
                "decision": "repairable",
                "scope_drift": False,
                "protected_contract_ok": True,
                "severity_counts": {"critical": 0, "high": 1, "medium": 0, "low": 0},
            },
            verification={"status": "success", "failure_type": ""},
            changed_files={"changed_files": ["app/x.py"]},
        )

        self.assertTrue(allowed)
        self.assertFalse(blocked)

    def test_maybe_write_replan_reason_emits_attempt_artifact_for_plan_misalignment(self) -> None:
        issue_key = "owner/repo#9"
        self.state_store.create_issue_record(issue_key)
        attempt_id = self.state_store.create_attempt(issue_key)

        self.pipeline._maybe_write_replan_reason(
            workflow={"config": WorkflowConfig(replanning=ReplanningConfig(max_replans_per_issue=2))},
            issue_key=issue_key,
            attempt_id=attempt_id,
            review_result={
                "decision": "reject",
                "reject_reasons": ["plan_misalignment"],
                "plan_alignment_ok": False,
                "scope_drift": False,
            },
            scope_analysis={"unexpected_files": ["app/legacy.py"]},
        )

        payload = self.state_store.load_attempt_artifact(issue_key, attempt_id, "replan_reason.json")
        self.assertEqual("att-002", payload["new_attempt_id"])
        self.assertEqual(["plan_misalignment"], payload["reasons"])

    def test_maybe_write_replan_reason_stops_when_max_replans_reached(self) -> None:
        issue_key = "owner/repo#10"
        self.state_store.create_issue_record(issue_key)
        self.state_store.update_issue_meta(issue_key, replan_count=2)
        attempt_id = self.state_store.create_attempt(issue_key)

        self.pipeline._maybe_write_replan_reason(
            workflow={"config": WorkflowConfig(replanning=ReplanningConfig(max_replans_per_issue=2))},
            issue_key=issue_key,
            attempt_id=attempt_id,
            review_result={
                "decision": "reject",
                "reject_reasons": ["plan_misalignment"],
                "plan_alignment_ok": False,
                "scope_drift": False,
            },
            scope_analysis={"unexpected_files": ["app/legacy.py"]},
        )

        payload = self.state_store.load_attempt_artifact(issue_key, attempt_id, "replan_reason.json")
        self.assertEqual({}, payload)
        self.assertEqual(2, self.state_store.load_issue_meta(issue_key)["replan_count"])
