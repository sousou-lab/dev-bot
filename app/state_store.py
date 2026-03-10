from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class RunMeta:
    thread_id: str
    parent_message_id: str
    channel_id: str
    issue_key: str
    created_at: str
    status: str
    current_run_id: str = ""
    attempt_count: int = 0


class FileStateStore:
    def __init__(self, runs_root: str = "runs") -> None:
        self.runs_root = Path(runs_root)
        self.runs_root.mkdir(parents=True, exist_ok=True)

    def create_run(self, thread_id: int, parent_message_id: int, channel_id: int, issue_key: str = "") -> RunMeta:
        run_dir = self.thread_dir(thread_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        meta = RunMeta(
            thread_id=str(thread_id),
            parent_message_id=str(parent_message_id),
            channel_id=str(channel_id),
            issue_key=issue_key,
            created_at=datetime.now(UTC).isoformat(),
            status="draft",
        )
        self._write_json(run_dir / "meta.json", asdict(meta))
        return meta

    def create_execution_run(self, thread_id: int) -> str:
        run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        artifacts_dir = self.execution_artifacts_dir(thread_id, run_id)
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        meta = self.load_meta(thread_id)
        attempt_count = int(meta.get("attempt_count", 0)) + 1
        self.update_meta(thread_id, current_run_id=run_id, attempt_count=attempt_count)
        self._write_json(
            self.execution_run_dir(thread_id, run_id) / "meta.json",
            {"run_id": run_id, "created_at": datetime.now(UTC).isoformat(), "thread_id": str(thread_id)},
        )
        return run_id

    def current_run_id(self, thread_id: int) -> str:
        return str(self.load_meta(thread_id).get("current_run_id", ""))

    def thread_dir(self, thread_id: int) -> Path:
        return self.runs_root / str(thread_id)

    def attachments_dir(self, thread_id: int) -> Path:
        path = self.thread_dir(thread_id) / "attachments"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def execution_run_dir(self, thread_id: int, run_id: str | None = None) -> Path:
        resolved = run_id or self.current_run_id(thread_id)
        return self.thread_dir(thread_id) / "runs" / resolved

    def execution_artifacts_dir(self, thread_id: int, run_id: str | None = None) -> Path:
        return self.execution_run_dir(thread_id, run_id) / "artifacts"

    def append_message(self, thread_id: int, role: str, content: str) -> None:
        run_dir = self.thread_dir(thread_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "timestamp": datetime.now(UTC).isoformat(),
            "role": role,
            "content": content,
        }
        with (run_dir / "conversation.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def append_log(self, thread_id: int, message: str) -> None:
        run_dir = self.thread_dir(thread_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        with (run_dir / "run.log").open("a", encoding="utf-8") as fh:
            fh.write(message.rstrip() + "\n")

    def record_activity(
        self,
        thread_id: int,
        *,
        phase: str,
        summary: str,
        status: str,
        run_id: str = "",
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = {
            "timestamp": datetime.now(UTC).isoformat(),
            "phase": phase,
            "summary": summary,
            "status": status,
            "run_id": run_id,
            "details": details or {},
        }
        self.write_artifact(thread_id, "current_activity.json", payload)
        history = self.load_artifact(thread_id, "activity_history.json")
        items = history.get("items", []) if isinstance(history, dict) else []
        if not isinstance(items, list):
            items = []
        items.append(payload)
        self.write_artifact(thread_id, "activity_history.json", {"items": items[-200:]})
        self.append_log(thread_id, f"[activity:{phase}:{status}] {summary}")
        return payload

    def clear_activity(self, thread_id: int) -> None:
        self.write_artifact(thread_id, "current_activity.json", {})

    def update_status(self, thread_id: int, status: str) -> None:
        self.update_meta(thread_id, status=status)

    def update_meta(self, thread_id: int, **fields: object) -> None:
        run_dir = self.thread_dir(thread_id)
        meta_path = run_dir / "meta.json"
        if not meta_path.exists():
            return
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta.update(fields)
        self._write_json(meta_path, meta)

    def has_run(self, thread_id: int) -> bool:
        return (self.thread_dir(thread_id) / "meta.json").exists()

    def bind_issue(self, thread_id: int, repo_full_name: str, issue_number: int) -> str:
        issue_key = f"{repo_full_name}#{issue_number}"
        self.update_meta(thread_id, issue_key=issue_key, github_repo=repo_full_name, issue_number=str(issue_number))
        return issue_key

    def list_runs_by_status(self, statuses: set[str]) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for meta_path in self.runs_root.glob("*/meta.json"):
            payload = json.loads(meta_path.read_text(encoding="utf-8"))
            if str(payload.get("status", "")) in statuses:
                items.append(payload)
        return items

    def write_artifact(self, thread_id: int, filename: str, payload: object) -> None:
        run_dir = self.thread_dir(thread_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        self._write_json(run_dir / filename, payload)

    def write_execution_artifact(self, thread_id: int, filename: str, payload: object, run_id: str | None = None) -> None:
        artifacts_dir = self.execution_artifacts_dir(thread_id, run_id)
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        self._write_json(artifacts_dir / filename, payload)
        self.write_artifact(thread_id, filename, payload)

    def record_failure(
        self,
        thread_id: int,
        *,
        stage: str,
        message: str,
        details: dict[str, Any] | None = None,
        stderr: list[str] | None = None,
    ) -> dict[str, Any]:
        payload = {
            "timestamp": datetime.now(UTC).isoformat(),
            "stage": _sanitize_for_log(stage),
            "message": _sanitize_for_log(message),
            "details": _sanitize_payload(details or {}),
            "stderr": _sanitize_payload(stderr or []),
        }
        self.write_artifact(thread_id, "agent_failure.json", payload)
        self.write_artifact(thread_id, "last_failure.json", payload)
        self.append_log(thread_id, f"[failure:{stage}] {message}")
        if payload["details"]:
            self.append_log(thread_id, json.dumps(payload["details"], ensure_ascii=False))
        return payload

    def delete_artifact(self, thread_id: int, filename: str) -> None:
        path = self.thread_dir(thread_id) / filename
        if path.exists():
            path.unlink()

    def load_meta(self, thread_id: int) -> dict[str, Any]:
        meta_path = self.thread_dir(thread_id) / "meta.json"
        if not meta_path.exists():
            return {}
        return json.loads(meta_path.read_text(encoding="utf-8"))

    def load_artifact(self, thread_id: int, filename: str) -> object:
        path = self.thread_dir(thread_id) / filename
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    def load_execution_artifact(self, thread_id: int, filename: str, run_id: str | None = None) -> object:
        path = self.execution_artifacts_dir(thread_id, run_id) / filename
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    def _write_json(self, path: Path, payload: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def write_attachment_text(self, thread_id: int, filename: str, content: str) -> str:
        path = self.attachments_dir(thread_id) / filename
        path.write_text(content, encoding="utf-8")
        return str(path)


def _sanitize_payload(value: object) -> object:
    if isinstance(value, dict):
        return {key: _sanitize_payload(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_sanitize_payload(item) for item in value]
    if isinstance(value, str):
        return _sanitize_for_log(value)
    return value


def _sanitize_for_log(text: str) -> str:
    masked = text
    patterns = [
        (r"https://[^/\s:@]+:[^@\s]+@github\.com/", "https://[REDACTED]@github.com/"),
        (r"(?i)(authorization\s*[:=]\s*)(.+)", r"\1[REDACTED]"),
        (r"(?i)(bearer\s+)[A-Za-z0-9._\-]+", r"\1[REDACTED]"),
        (r"(?i)(token\s*[:=]\s*)[^\s]+", r"\1[REDACTED]"),
        (r"(?i)(api[_-]?key\s*[:=]\s*)[^\s]+", r"\1[REDACTED]"),
    ]
    for pattern, replacement in patterns:
        masked = re.sub(pattern, replacement, masked)
    return masked
