from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


def load_workflow(workspace: str | None = None, repo_root: str | None = None) -> dict[str, Any]:
    root = Path(workspace or repo_root or ".").resolve()
    path = root / "WORKFLOW.md"
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        return {"raw_text": text}
    _, front_matter, remainder = text.split("---", 2)
    payload = yaml.safe_load(front_matter) or {}
    if not isinstance(payload, dict):
        payload = {}
    payload["raw_text"] = text
    payload["contract_body"] = remainder.strip()
    return payload


def workflow_text(workspace: str | None = None, repo_root: str | None = None) -> str:
    payload = load_workflow(workspace=workspace, repo_root=repo_root)
    return str(payload.get("raw_text", ""))
