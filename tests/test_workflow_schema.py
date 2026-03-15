from __future__ import annotations

import unittest

from app.contracts.workflow_schema import WorkflowConfig, WorkflowValidationError


class WorkflowSchemaTests(unittest.TestCase):
    def test_from_dict_parses_nested_runtime_sections(self) -> None:
        config = WorkflowConfig.from_dict(
            {
                "planning": {
                    "provider": "claude-agent-sdk",
                    "test_plan_max_parallelism": 4,
                    "cwd_source": "plan_workspace",
                    "max_turns": 4,
                    "timeout_seconds": 300,
                    "settings_sources": ["project"],
                    "legacy_fallback": {"enabled": True, "use_only_on_committee_failure": True},
                    "autoselect_committee": {
                        "enabled": True,
                        "min_acceptance_criteria": 10,
                        "min_acceptance_criteria_when_complex": 6,
                        "min_summary_chars_when_complex": 2500,
                        "min_repo_files": 100,
                        "min_acceptance_criteria_with_large_repo": 5,
                    },
                    "allowed_tools": ["Read", "Grep", "Glob"],
                    "skill_mode": "explicit_project_filesystem",
                    "committee": {
                        "roles": {
                            "merger": {
                                "mode": "query",
                                "allowed_tools": ["Read"],
                                "disallowed_tools": ["Write"],
                                "output_schema": "plan_v2",
                            }
                        }
                    },
                },
                "codex": {
                    "command": "codex app-server",
                    "allow_turn_steer": True,
                    "allow_thread_resume_same_run_only": True,
                    "compaction_policy": {
                        "turn_count_gte": 8,
                        "steer_count_gte": 1,
                        "repair_cycles_gte": 2,
                    },
                },
                "implementation": {
                    "backend": "codex-app-server",
                    "optional_backends": ["codex-sdk-sidecar"],
                    "candidate_mode": {
                        "enabled": True,
                        "max_parallel_editors": 2,
                        "triggers": {"require_clear_design_branches": False},
                    },
                    "push_policy": {
                        "push_only_winner": True,
                        "cleanup_loser_local_branches": True,
                    },
                },
                "replanning": {
                    "enabled": True,
                    "auto_replan_on_reject_reasons": ["plan_misalignment", "scope_drift"],
                    "max_replans_per_issue": 2,
                    "emit_replan_reason_artifact": True,
                    "create_new_attempt_on_replan": True,
                },
                "protected_config": {
                    "default": "deny",
                    "allow_label": "allow-protected-config",
                    "protected_paths": ["WORKFLOW.md", "docs/policy/**"],
                    "allowlist_source": {
                        "priority": [
                            {"issue_body_section": "保護設定変更許可リスト"},
                            {"artifact": "protected_config_allowlist.json"},
                        ]
                    },
                },
                "review": {
                    "provider": "claude-agent-sdk",
                    "rules_file": "REVIEW.md",
                    "post_inline_to_github": True,
                    "thresholds": {"min_confidence_to_report": 0.9, "verifier_required": True},
                    "roles": {"diff_reviewer": {}, "ranker": {}},
                },
                "verification": {
                    "required_artifacts": [
                        "plan.json",
                        "verification_plan.json",
                        "runner_metadata.json",
                    ],
                    "required_checks": [{"name": "tests", "command": "uv run pytest -q"}],
                    "advisory_checks": [{"name": "typecheck", "command": "uv run pyright app"}],
                },
                "debug": {"incident_bundle": {"enabled": True, "cleanup_on_terminal": False}},
                "evals": {
                    "enabled": True,
                    "strategy": "synthetic_first",
                    "fixtures_root": "tests/agent_evals/fixtures/python",
                    "graders": {"planning": "mechanical_plus_judge"},
                },
                "telemetry": {"sink": "jsonl", "otel_compatible_fields": True},
            }
        )

        assert config.planning is not None
        assert config.codex is not None
        assert config.implementation is not None
        assert config.replanning is not None
        assert config.protected_config is not None
        assert config.review is not None
        assert config.verification is not None
        assert config.incident_bundle is not None
        assert config.evals is not None
        assert config.telemetry is not None
        self.assertEqual("query", config.planning.committee.roles["merger"].mode)
        self.assertEqual(4, config.planning.test_plan_max_parallelism)
        self.assertEqual("committee", config.planning.mode)
        self.assertEqual(["project"], config.planning.settings_sources)
        self.assertTrue(config.planning.legacy_fallback.enabled)
        self.assertTrue(config.planning.legacy_fallback.use_only_on_committee_failure)
        self.assertEqual(10, config.planning.autoselect_committee.min_acceptance_criteria)
        self.assertEqual(6, config.planning.autoselect_committee.min_acceptance_criteria_when_complex)
        self.assertEqual(["Read", "Grep", "Glob"], config.planning.allowed_tools)
        self.assertTrue(config.codex.allow_turn_steer)
        self.assertEqual(8, config.codex.compaction_policy.turn_count_gte)
        self.assertEqual(2, config.implementation.candidate_mode.max_parallel_editors)
        self.assertFalse(config.implementation.candidate_mode.triggers.require_clear_design_branches)
        self.assertEqual(["codex-sdk-sidecar"], config.implementation.optional_backends)
        self.assertEqual(2, config.replanning.max_replans_per_issue)
        self.assertEqual("allow-protected-config", config.protected_config.allow_label)
        self.assertEqual(["WORKFLOW.md", "docs/policy/**"], config.protected_config.protected_paths)
        self.assertEqual("保護設定変更許可リスト", config.protected_config.allowlist_source.issue_body_section)
        self.assertEqual(0.9, config.review.thresholds.min_confidence_to_report)
        self.assertTrue(config.review.post_inline_to_github)
        self.assertIn("diff_reviewer", config.review.roles)
        self.assertEqual("uv run pytest -q", config.verification.required_checks[0].command)
        self.assertFalse(config.incident_bundle.cleanup_on_terminal)
        self.assertEqual("synthetic_first", config.evals.strategy)
        self.assertEqual("jsonl", config.telemetry.sink)

    def test_candidate_mode_rejects_more_than_two_parallel_editors(self) -> None:
        with self.assertRaises(WorkflowValidationError):
            WorkflowConfig.from_dict(
                {
                    "implementation": {
                        "backend": "codex-app-server",
                        "candidate_mode": {"max_parallel_editors": 3},
                    }
                }
            )

    def test_codex_config_parses_allow_turn_steer(self) -> None:
        config = WorkflowConfig.from_dict(
            {
                "codex": {
                    "command": "codex app-server",
                    "allow_turn_steer": True,
                    "allow_thread_resume_same_run_only": True,
                    "compaction_policy": {"turn_count_gte": 10},
                }
            }
        )

        assert config.codex is not None
        self.assertTrue(config.codex.allow_turn_steer)
        self.assertEqual(10, config.codex.compaction_policy.turn_count_gte)

    def test_replanning_rejects_negative_max_replans(self) -> None:
        with self.assertRaisesRegex(WorkflowValidationError, "max_replans_per_issue must be >= 0"):
            WorkflowConfig.from_dict({"replanning": {"max_replans_per_issue": -1}})

    def test_planning_provider_is_required(self) -> None:
        with self.assertRaises(WorkflowValidationError):
            WorkflowConfig.from_dict({"planning": {"enabled": True}})

    def test_planning_mode_defaults_to_committee_when_omitted(self) -> None:
        config = WorkflowConfig.from_dict({"planning": {"provider": "claude-agent-sdk"}})

        assert config.planning is not None
        self.assertEqual("committee", config.planning.mode)

    def test_verification_requires_architecture_artifacts(self) -> None:
        with self.assertRaisesRegex(WorkflowValidationError, "verification_plan.json, runner_metadata.json"):
            WorkflowConfig.from_dict(
                {
                    "verification": {
                        "required_artifacts": ["plan.json"],
                    }
                }
            )
