from __future__ import annotations

import stat
import tempfile
import unittest
from pathlib import Path

from app.hook_runner import HookRunner


class HookRunnerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.scripts_dir = Path(self.tmp) / "scripts"
        self.scripts_dir.mkdir()

    async def test_run_existing_hook_succeeds(self) -> None:
        script = self.scripts_dir / "agent_after_create.sh"
        script.write_text("#!/bin/sh\necho 'hook ran'\n")
        script.chmod(script.stat().st_mode | stat.S_IEXEC)

        runner = HookRunner(scripts_dir=str(self.scripts_dir), timeout_ms=5000)
        result = await runner.run("after_create", env={"DEVBOT_ISSUE_NUMBER": "42"})

        self.assertTrue(result.success)
        self.assertIn("hook ran", result.stdout)
        self.assertEqual(result.returncode, 0)

    async def test_run_missing_hook_is_skipped(self) -> None:
        runner = HookRunner(scripts_dir=str(self.scripts_dir), timeout_ms=5000)
        result = await runner.run("nonexistent_hook")

        self.assertTrue(result.skipped)
        self.assertTrue(result.success)

    async def test_run_failing_hook_reports_failure(self) -> None:
        script = self.scripts_dir / "agent_before_run.sh"
        script.write_text("#!/bin/sh\nexit 1\n")
        script.chmod(script.stat().st_mode | stat.S_IEXEC)

        runner = HookRunner(scripts_dir=str(self.scripts_dir), timeout_ms=5000)
        result = await runner.run("before_run")

        self.assertFalse(result.success)
        self.assertEqual(result.returncode, 1)

    async def test_run_hook_timeout(self) -> None:
        script = self.scripts_dir / "agent_after_run.sh"
        script.write_text("#!/bin/sh\nsleep 10\n")
        script.chmod(script.stat().st_mode | stat.S_IEXEC)

        runner = HookRunner(scripts_dir=str(self.scripts_dir), timeout_ms=100)
        result = await runner.run("after_run")

        self.assertFalse(result.success)
        self.assertTrue(result.timed_out)

    async def test_env_vars_are_passed_to_hook(self) -> None:
        script = self.scripts_dir / "agent_before_remove.sh"
        script.write_text('#!/bin/sh\necho "ISSUE=$DEVBOT_ISSUE_NUMBER REPO=$DEVBOT_REPO"\n')
        script.chmod(script.stat().st_mode | stat.S_IEXEC)

        runner = HookRunner(scripts_dir=str(self.scripts_dir), timeout_ms=5000)
        result = await runner.run(
            "before_remove",
            env={"DEVBOT_ISSUE_NUMBER": "7", "DEVBOT_REPO": "org/repo"},
        )

        self.assertTrue(result.success)
        self.assertIn("ISSUE=7", result.stdout)
        self.assertIn("REPO=org/repo", result.stdout)

    async def test_hook_name_maps_to_script_file(self) -> None:
        """Hook name 'after_create' maps to 'agent_after_create.sh'."""
        runner = HookRunner(scripts_dir=str(self.scripts_dir), timeout_ms=5000)
        expected = self.scripts_dir / "agent_after_create.sh"
        self.assertEqual(runner._script_path("after_create"), expected)
