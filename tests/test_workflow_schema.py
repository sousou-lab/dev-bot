from __future__ import annotations

import unittest

from app.contracts.workflow_schema import WorkflowConfig, WorkflowValidationError


class WorkflowSchemaTests(unittest.TestCase):
    def test_from_dict_parses_nested_runtime_sections(self) -> None:
        config = WorkflowConfig.from_dict(
            {
                "planning": {
                    "provider": "claude-agent-sdk",
                    "cwd_source": "plan_workspace",
                    "max_turns": 4,
                    "timeout_seconds": 300,
                    "settings_sources": ["project"],
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
                "implementation": {
                    "backend": "codex-app-server",
                    "optional_backends": ["codex-sdk-sidecar"],
                    "candidate_mode": {"enabled": True, "max_parallel_editors": 2},
                    "push_policy": {
                        "push_only_winner": True,
                        "cleanup_loser_local_branches": True,
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
        assert config.implementation is not None
        assert config.review is not None
        assert config.verification is not None
        assert config.incident_bundle is not None
        assert config.evals is not None
        assert config.telemetry is not None
        self.assertEqual("query", config.planning.committee.roles["merger"].mode)
        self.assertEqual(["project"], config.planning.settings_sources)
        self.assertEqual(["Read", "Grep", "Glob"], config.planning.allowed_tools)
        self.assertEqual(2, config.implementation.candidate_mode.max_parallel_editors)
        self.assertEqual(["codex-sdk-sidecar"], config.implementation.optional_backends)
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

    def test_planning_provider_is_required(self) -> None:
        with self.assertRaises(WorkflowValidationError):
            WorkflowConfig.from_dict({"planning": {"enabled": True}})

    def test_verification_requires_architecture_artifacts(self) -> None:
        with self.assertRaisesRegex(WorkflowValidationError, "verification_plan.json, runner_metadata.json"):
            WorkflowConfig.from_dict(
                {
                    "verification": {
                        "required_artifacts": ["plan.json"],
                    }
                }
            )
