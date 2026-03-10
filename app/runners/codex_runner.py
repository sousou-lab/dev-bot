from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CodexRunResult:
    returncode: int
    stdout_path: str
    changed_files: list[str]
    summary: str


class CodexRunner:
    def __init__(self, codex_bin: str = "codex") -> None:
        self.codex_bin = codex_bin

    def build_prompt(
        self,
        *,
        issue: dict,
        requirement_summary: dict,
        plan: dict,
        test_plan: dict,
        workflow_text: str,
    ) -> str:
        return (
            "You are the implementation worker for this repository.\n"
            "Follow the repository workflow contract strictly.\n\n"
            f"[ISSUE]\n{json.dumps(issue, ensure_ascii=False, indent=2)}\n\n"
            f"[REQUIREMENT_SUMMARY]\n{json.dumps(requirement_summary, ensure_ascii=False, indent=2)}\n\n"
            f"[PLAN]\n{json.dumps(plan, ensure_ascii=False, indent=2)}\n\n"
            f"[TEST_PLAN]\n{json.dumps(test_plan, ensure_ascii=False, indent=2)}\n\n"
            f"[WORKFLOW_MD]\n{workflow_text}\n\n"
            "Rules:\n"
            "- Implement only what the plan requires.\n"
            "- Prefer minimal diffs.\n"
            "- Add or update tests when required.\n"
            "- Do not touch protected paths unless explicitly allowed.\n"
            "- At the end, output a short implementation summary.\n"
        )

    def run(
        self,
        *,
        workspace: str,
        run_dir: str,
        issue: dict,
        requirement_summary: dict,
        plan: dict,
        test_plan: dict,
        workflow_text: str,
    ) -> CodexRunResult:
        artifacts = Path(run_dir) / "artifacts"
        artifacts.mkdir(parents=True, exist_ok=True)
        stdout_path = artifacts / "codex_run.log"
        prompt = self.build_prompt(
            issue=issue,
            requirement_summary=requirement_summary,
            plan=plan,
            test_plan=test_plan,
            workflow_text=workflow_text,
        )
        cmd = [
            self.codex_bin,
            "exec",
            "--sandbox",
            "workspace-write",
            "--skip-git-repo-check",
            "-C",
            workspace,
            "-",
        ]
        env = os.environ.copy()
        with stdout_path.open("w", encoding="utf-8") as fh:
            process = subprocess.Popen(
                cmd,
                cwd=workspace,
                env=env,
                stdin=subprocess.PIPE,
                stdout=fh,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            )
            assert process.stdin is not None
            process.stdin.write(prompt)
            process.stdin.close()
            returncode = process.wait()

        changed_files = self._detect_changed_files(workspace)
        summary = f"Codex finished with return code {returncode}"
        (artifacts / "changed_files.json").write_text(
            json.dumps({"changed_files": changed_files}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return CodexRunResult(
            returncode=returncode,
            stdout_path=str(stdout_path),
            changed_files=changed_files,
            summary=summary,
        )

    def start(
        self,
        *,
        workspace: str,
        stdout_path: str,
    ) -> subprocess.Popen[str]:
        return subprocess.Popen(
            [
                self.codex_bin,
                "exec",
                "--sandbox",
                "workspace-write",
                "--skip-git-repo-check",
                "-C",
                workspace,
                "-",
            ],
            cwd=workspace,
            env=os.environ.copy(),
            stdin=subprocess.PIPE,
            stdout=open(stdout_path, "w", encoding="utf-8"),
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )

    def _detect_changed_files(self, workspace: str) -> list[str]:
        try:
            output = subprocess.check_output(["git", "status", "--porcelain"], cwd=workspace, text=True)
        except Exception:
            return []
        changed: list[str] = []
        for line in output.splitlines():
            if len(line) >= 4:
                changed.append(line[3:])
        return changed
