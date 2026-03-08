from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from app.config import Settings


class LocalRunner:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def prepare_run(self, workspace: str, run_dir: str, requirement_summary: dict, issue: dict) -> tuple[list[str], dict[str, str], str]:
        artifacts_dir = Path(run_dir) / "artifacts"
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        summary_path = artifacts_dir / "requirement_summary.json"
        issue_path = artifacts_dir / "issue.json"
        summary_path.write_text(json.dumps(requirement_summary, ensure_ascii=False, indent=2), encoding="utf-8")
        issue_path.write_text(json.dumps(issue, ensure_ascii=False, indent=2), encoding="utf-8")

        env = os.environ.copy()
        if self.settings.anthropic_api_key:
            env["ANTHROPIC_API_KEY"] = self.settings.anthropic_api_key
        else:
            env.pop("ANTHROPIC_API_KEY", None)
        env["MAX_IMPLEMENTATION_ITERATIONS"] = str(self.settings.max_implementation_iterations)

        cmd = [
            sys.executable,
            "-m",
            "app.container_runner",
            "--workspace",
            workspace,
            "--artifacts-dir",
            str(artifacts_dir),
        ]
        return cmd, env, str(artifacts_dir)

    def load_final_result(self, artifacts_dir: str) -> dict:
        final_result_path = Path(artifacts_dir) / "final_result.json"
        if not final_result_path.exists():
            raise RuntimeError("Local runner finished without final_result.json")
        return json.loads(final_result_path.read_text(encoding="utf-8"))

    def load_optional_artifact(self, artifacts_dir: str, filename: str) -> dict:
        path = Path(artifacts_dir) / filename
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    def start_process(self, cmd: list[str], env: dict[str, str]) -> subprocess.Popen[str]:
        return subprocess.Popen(
            cmd,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
