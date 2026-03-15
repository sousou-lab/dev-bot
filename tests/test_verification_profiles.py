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
                "bootstrap_commands": ["uv sync"],
                "hard_checks": [{"name": "lint", "command": "ruff check ."}],
                "advisory_checks": [{"name": "format", "command": "ruff format --check ."}],
            }
        )

        self.assertEqual(["uv sync"], verification["bootstrap_commands"])
        self.assertEqual("hard", verification["required_checks"][0]["category"])
        self.assertEqual("advisory", verification["advisory_checks"][0]["category"])

    def test_repo_profiler_bootstraps_python_verification_tools_without_repo_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, "app.py").write_text("print('ok')\n", encoding="utf-8")
            Path(tmpdir, "tests").mkdir()
            Path(tmpdir, "tests", "test_app.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")

            profile = build_repo_profile(tmpdir)

        self.assertEqual("python-basic", profile["suggested_verification_profile"])
        self.assertIn("uv run --with ruff ruff check .", profile["lint_commands"])
        self.assertIn("uv run --with pyright pyright .", profile["typecheck_commands"])
        self.assertIn("uv run --with pytest pytest -q", profile["test_commands"])

    def test_repo_profiler_reports_full_file_count_while_sampling_file_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            for index in range(205):
                Path(tmpdir, f"file_{index:03d}.txt").write_text("x\n", encoding="utf-8")

            profile = build_repo_profile(tmpdir)

        self.assertEqual(205, profile["file_count"])
        self.assertEqual(200, len(profile["files"]))

    def test_build_verification_plan_for_python_includes_fast_repair_profile(self) -> None:
        repo_profile = {
            "languages": ["python"],
            "lint_commands": ["uv run ruff check ."],
            "test_commands": ["uv run python -m pytest -q"],
            "typecheck_commands": ["uv run pyright app"],
            "format_commands": ["uv run ruff format --check app/ tests/"],
            "build_commands": [],
        }

        verification_plan = build_verification_plan(
            workspace=".",
            repo_profile=repo_profile,
            plan={"candidate_files": ["app/service.py"]},
        )

        self.assertEqual("python-basic", verification_plan["profile"])
        self.assertEqual("python-fast-repair", verification_plan["repair_profile"])
        self.assertEqual(
            ["format", "lint", "typecheck", "tests"],
            [item["name"] for item in verification_plan["repair_checks"]],
        )

    def test_build_verification_plan_for_mixed_repo_uses_mixed_profile(self) -> None:
        repo_profile = {
            "languages": ["python", "typescript"],
            "lint_commands": ["uv run ruff check .", "npm run lint"],
            "test_commands": ["uv run python -m pytest -q", "npm test"],
            "typecheck_commands": ["uv run pyright app", "tsc --noEmit"],
            "format_commands": ["uv run ruff format --check app/ tests/", "biome format --check ."],
            "build_commands": [],
        }

        verification_plan = build_verification_plan(
            workspace=".",
            repo_profile=repo_profile,
            plan={"candidate_files": ["app/service.py", "web/index.ts"]},
        )

        self.assertEqual("mixed-py-ts", verification_plan["profile"])
        self.assertEqual("mixed-py-ts", verification_plan["repair_profile"])
        self.assertGreaterEqual(len(verification_plan["repair_checks"]), 4)
