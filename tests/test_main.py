from __future__ import annotations

import importlib
import logging
import unittest
from types import SimpleNamespace
from unittest.mock import patch


class MainModuleTests(unittest.TestCase):
    def test_import_succeeds_without_discord_installed(self) -> None:
        module = importlib.import_module("app.main")

        self.assertTrue(hasattr(module, "main"))

    def test_main_reports_missing_discord_dependency_cleanly(self) -> None:
        module = importlib.import_module("app.main")
        settings = SimpleNamespace(
            discord_bot_token="token",
            github_token="token",
            github_app_id="1",
            github_app_private_key_path="/dev/null",
            github_app_installation_id="1",
            github_project_id="",
            github_project_state_field_id="",
            github_project_state_option_ids="",
            github_project_plan_field_id="",
            github_project_plan_option_ids="",
            ensure_runtime_paths=lambda: None,
        )

        with (
            patch.object(module, "load_settings", return_value=settings),
            patch.object(module, "validate_settings", return_value=[]),
            patch.object(
                module.GitHubIssueClient, "preflight", return_value={"ok": True, "repo_count": 0, "sample_repos": []}
            ),
            self.assertLogs("app.main", level=logging.ERROR) as cm,
        ):
            result = module.main()

        self.assertEqual(1, result)
        self.assertTrue(any("Discord dependency is not installed" in msg for msg in cm.output))

    def test_main_exits_when_project_preflight_fails(self) -> None:
        module = importlib.import_module("app.main")
        settings = SimpleNamespace(
            discord_bot_token="token",
            github_token="token",
            github_app_id="1",
            github_app_private_key_path="/dev/null",
            github_app_installation_id="1",
            github_project_id="project-1",
            github_project_state_field_id="field-state",
            github_project_state_option_ids='{"Ready":"opt-ready"}',
            github_project_plan_field_id="field-plan",
            github_project_plan_option_ids='{"Approved":"opt-approved"}',
            ensure_runtime_paths=lambda: None,
        )

        with (
            patch.object(module, "load_settings", return_value=settings),
            patch.object(module, "validate_settings", return_value=[]),
            patch.object(
                module.GitHubIssueClient,
                "preflight",
                return_value={"ok": False, "error": "project field mismatch", "fallback_repos": []},
            ),
            self.assertLogs("app.main", level=logging.ERROR) as cm,
        ):
            result = module.main()

        self.assertEqual(1, result)
        self.assertTrue(any("GitHub preflight failed" in msg for msg in cm.output))
