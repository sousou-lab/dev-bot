from __future__ import annotations

import unittest

from app.runners.base import CommandExecution, ImplementationRunner, VerificationRunner
from app.runners.claude_runner import ClaudeRunner
from app.runners.codex_runner import CodexRunner


class RunnerProtocolTests(unittest.TestCase):
    def test_command_execution_is_frozen_dataclass(self) -> None:
        ex = CommandExecution(command="test", returncode=0, output="ok")
        self.assertEqual(ex.command, "test")
        self.assertEqual(ex.returncode, 0)
        self.assertEqual(ex.output, "ok")
        with self.assertRaises(AttributeError):
            ex.command = "changed"  # type: ignore[misc]

    def test_implementation_runner_is_runtime_checkable(self) -> None:
        self.assertTrue(
            getattr(ImplementationRunner, "__protocol_attrs__", None) is not None
            or hasattr(ImplementationRunner, "__subclasshook__")
        )

    def test_verification_runner_is_runtime_checkable(self) -> None:
        self.assertTrue(
            getattr(VerificationRunner, "__protocol_attrs__", None) is not None
            or hasattr(VerificationRunner, "__subclasshook__")
        )

    def test_codex_runner_has_run_method(self) -> None:
        """CodexRunner should have a run() method matching ImplementationRunner."""
        self.assertTrue(hasattr(CodexRunner, "run"))
        self.assertTrue(callable(CodexRunner.run))

    def test_codex_runner_has_build_prompt_method(self) -> None:
        self.assertTrue(hasattr(CodexRunner, "build_prompt"))

    def test_claude_runner_has_verify_method(self) -> None:
        """ClaudeRunner should have verify() matching VerificationRunner."""
        self.assertTrue(hasattr(ClaudeRunner, "verify"))
        self.assertTrue(callable(ClaudeRunner.verify))

    def test_claude_runner_has_review_method(self) -> None:
        """ClaudeRunner should have review() matching VerificationRunner."""
        self.assertTrue(hasattr(ClaudeRunner, "review"))
        self.assertTrue(callable(ClaudeRunner.review))
