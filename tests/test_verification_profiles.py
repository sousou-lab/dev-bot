from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.repo_profiler import build_repo_profile
from app.verification_profiles import build_verification_plan, workflow_verification_from_plan


class VerificationProfileTests(unittest.TestCase):
    def test_repo_profiler_suggests_static_web_for_html_only_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, "index.html").write_text(
                "<!doctype html><html><body><script></script></body></html>", encoding="utf-8"
            )

            profile = build_repo_profile(tmpdir)

        self.assertEqual("static-web", profile["suggested_verification_profile"])
        self.assertIn("html", profile["languages"])

    def test_build_verification_plan_for_static_web_uses_catalog_checks(self) -> None:
        repo_profile = {
            "languages": ["html"],
            "suggested_verification_profile": "static-web",
            "lint_commands": [],
            "test_commands": [],
            "typecheck_commands": [],
            "format_commands": [],
            "build_commands": [],
        }
        plan = {"candidate_files": ["index.html"]}

        verification_plan = build_verification_plan(workspace=".", repo_profile=repo_profile, plan=plan)

        self.assertEqual("static-web", verification_plan["profile"])
        self.assertEqual(["."], verification_plan["scope"]["paths"])
        self.assertEqual(
            ["html_static_smoke", "xss_static_scan"], [item["name"] for item in verification_plan["hard_checks"]]
        )

    def test_workflow_verification_from_plan_emits_hard_and_advisory_checks(self) -> None:
        verification = workflow_verification_from_plan(
            {
                "hard_checks": [{"name": "lint", "command": "ruff check ."}],
                "advisory_checks": [{"name": "format", "command": "ruff format --check ."}],
            }
        )

        self.assertEqual("hard", verification["required_checks"][0]["category"])
        self.assertEqual("advisory", verification["advisory_checks"][0]["category"])
