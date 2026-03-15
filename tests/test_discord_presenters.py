from __future__ import annotations

import unittest
from unittest.mock import patch

from app.discord_presenters import (
    format_budget_message,
    format_plan_message,
    format_status_message,
    format_why_failed_message,
)


class DiscordPresenterTests(unittest.TestCase):
    def test_format_status_message_includes_runtime_and_links(self) -> None:
        with patch("app.discord_presenters._planning_health", return_value="active"):
            message = format_status_message(
                thread_id=42,
                meta={"status": "running", "attempt_count": 2, "github_repo": "owner/repo"},
                issue={"number": 10, "url": "https://example.test/issues/10"},
                pr={"number": 11, "url": "https://example.test/pulls/11"},
                summary={"goal": "Ship feature"},
                plan={"implementation_steps": ["a", "b"]},
                test_plan={"cases": [{"id": "TC-1"}]},
                verification={"status": "success"},
                review={"decision": "approve"},
                pending_approval={"status": "pending", "tool_name": "Bash", "input_text": "terraform apply"},
                planning_progress={
                    "status": "test_plan_generating",
                    "phase": "plan",
                    "current": 1,
                    "total": 3,
                    "acceptance_criterion": "AC-1",
                    "last_event_kind": "AssistantMessage",
                    "elapsed_ms": 1250,
                    "last_event_at": "2026-03-16T00:00:05Z",
                },
                current_activity={
                    "phase": "workspace",
                    "summary": "workspace ready",
                    "status": "running",
                    "timestamp": "2026-03-11T00:00:00Z",
                },
                process={"pid": 123, "pgid": 456},
                runtime_active=True,
            )

        self.assertIn("status: `running`", message)
        self.assertIn("repo: `owner/repo`", message)
        self.assertIn("issue: [#10](https://example.test/issues/10)", message)
        self.assertIn("pending_approval: `Bash` - terraform apply", message)
        self.assertIn("planning_acceptance: AC-1", message)
        self.assertIn("planning_health: `active`", message)
        self.assertIn("planning_heartbeat: `AssistantMessage 1.2s`", message)
        self.assertIn("planning_updated_at: `2026-03-16T00:00:05Z`", message)
        self.assertIn("process: pid=`123` pgid=`456`", message)

    def test_format_why_failed_message_includes_failure_context(self) -> None:
        with patch("app.discord_presenters._planning_health", return_value="stalled 15m"):
            message = format_why_failed_message(
                last_failure={
                    "stage": "plan_generation",
                    "message": "boom",
                    "details": {
                        "repo": "owner/repo",
                        "planning_progress": {
                            "status": "test_plan_generating",
                            "phase": "plan",
                            "current": 1,
                            "total": 2,
                            "acceptance_criterion": "AC-1",
                            "last_event_kind": "ResultMessage",
                            "elapsed_ms": 65000,
                            "last_event_at": "2026-03-16T00:00:10Z",
                            "last_session_id": "sess_123",
                        },
                    },
                    "stderr": ["line1", "line2"],
                },
                verification={"status": "failed", "failure_type": "test_failure", "notes": ["fix test"]},
                final_result={"success": False, "failure_type": "test_failure"},
            )

        self.assertIn("- stage: `plan_generation`", message)
        self.assertIn("- repo: `owner/repo`", message)
        self.assertIn("- planning_session: `sess_123`", message)
        self.assertIn("- planning_health: `stalled 15m`", message)
        self.assertIn("- planning_heartbeat: `ResultMessage 1m05s`", message)
        self.assertIn("- planning_updated_at: `2026-03-16T00:00:10Z`", message)
        self.assertIn("- failure_type: `test_failure`", message)
        self.assertIn("- final_failure_type: `test_failure`", message)

    def test_format_status_message_marks_stalled_planning_from_timestamp(self) -> None:
        message = format_status_message(
            thread_id=42,
            meta={"status": "running", "attempt_count": 2},
            issue={},
            pr={},
            summary={},
            plan={},
            test_plan={},
            verification={},
            review={},
            pending_approval={},
            planning_progress={
                "status": "test_plan_generating",
                "phase": "acceptance_criterion",
                "current": 13,
                "total": 13,
                "last_event_at": "2020-03-15T23:40:00Z",
            },
            current_activity={},
            process=None,
            runtime_active=True,
        )

        self.assertIn("planning_health: `stalled", message)

    def test_format_budget_message_summarizes_attempts(self) -> None:
        message = format_budget_message(
            attempt_count=3,
            verification={"status": "success"},
            final_result={"success": True},
        )

        self.assertIn("attempts: `3`", message)
        self.assertIn("verification_status: `success`", message)
        self.assertIn("success: `True`", message)

    def test_format_plan_message_lists_scope_steps_and_cases(self) -> None:
        message = format_plan_message(
            "owner/repo",
            {
                "goal": "Ship feature",
                "scope": ["one"],
                "implementation_steps": ["step1"],
                "risks": ["risk1"],
            },
            {
                "cases": [
                    {"id": "TC-1", "name": "works", "category": "unit", "priority": "high"},
                ]
            },
        )

        self.assertIn("- Repo: `owner/repo`", message)
        self.assertIn("Scope\n- one", message)
        self.assertIn("Implementation steps\n- step1", message)
        self.assertIn("TC-1 works [unit/high]", message)
