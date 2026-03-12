from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
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
        self.drafts_root = self.runs_root / "drafts"
        self.issues_root = self.runs_root / "issues"
        self.bindings_root = self.runs_root / "bindings" / "discord_threads"
        self.legacy_root = self.runs_root
        self.drafts_root.mkdir(parents=True, exist_ok=True)
        self.issues_root.mkdir(parents=True, exist_ok=True)
        self.bindings_root.mkdir(parents=True, exist_ok=True)

    def create_draft(
        self,
        thread_id: int,
        *,
        parent_message_id: int,
        channel_id: int,
        status: str = "collecting_requirements",
    ) -> dict[str, Any]:
        draft_dir = self.draft_dir(thread_id)
        draft_dir.mkdir(parents=True, exist_ok=True)
        meta = {
            "draft_id": str(thread_id),
            "thread_id": str(thread_id),
            "parent_message_id": str(parent_message_id),
            "channel_id": str(channel_id),
            "created_at": datetime.now(UTC).isoformat(),
            "status": status,
            "issue_key": "",
            "current_run_id": "",
            "attempt_count": 0,
        }
        self._write_json(draft_dir / "meta.json", meta)
        return meta

    def create_issue_record(
        self,
        issue_key: str,
        *,
        thread_id: int | None = None,
        parent_message_id: int = 0,
        channel_id: int = 0,
        status: str = "draft",
    ) -> dict[str, Any]:
        issue_dir = self.issue_dir(issue_key)
        issue_dir.mkdir(parents=True, exist_ok=True)
        repo_full_name, issue_number = _split_issue_key(issue_key)
        meta = {
            "issue_key": issue_key,
            "github_repo": repo_full_name,
            "issue_number": issue_number,
            "thread_id": str(thread_id) if thread_id is not None else "",
            "parent_message_id": str(parent_message_id) if parent_message_id else "",
            "channel_id": str(channel_id) if channel_id else "",
            "created_at": datetime.now(UTC).isoformat(),
            "status": status,
            "current_run_id": "",
            "attempt_count": 0,
        }
        self._write_json(issue_dir / "meta.json", meta)
        self.issue_latest_dir(issue_key).mkdir(parents=True, exist_ok=True)
        if thread_id is not None:
            self.bind_thread(thread_id, issue_key)
        return meta

    def create_run(self, thread_id: int, parent_message_id: int, channel_id: int, issue_key: str = "") -> RunMeta:
        meta = self.create_draft(thread_id, parent_message_id=parent_message_id, channel_id=channel_id, status="draft")
        if issue_key:
            self.update_meta(thread_id, issue_key=issue_key)
        return RunMeta(
            thread_id=str(thread_id),
            parent_message_id=str(parent_message_id),
            channel_id=str(channel_id),
            issue_key=issue_key,
            created_at=str(meta["created_at"]),
            status=str(meta["status"]),
        )

    def create_execution_run(self, identifier: str | int) -> str:
        entity_key = self._resolve_entity_key(identifier)
        run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        artifacts_dir = self.execution_artifacts_dir(entity_key, run_id)
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        meta = self.load_meta(entity_key)
        attempt_count = int(meta.get("attempt_count", 0)) + 1
        self.update_meta(entity_key, current_run_id=run_id, attempt_count=attempt_count)
        payload = {
            "run_id": run_id,
            "created_at": datetime.now(UTC).isoformat(),
            "issue_key": entity_key if _is_issue_key(entity_key) else "",
            "thread_id": str(identifier) if isinstance(identifier, int) else str(meta.get("thread_id", "")),
        }
        self._write_json(self.execution_run_dir(entity_key, run_id) / "meta.json", payload)
        return run_id

    def current_run_id(self, identifier: str | int) -> str:
        return str(self.load_meta(identifier).get("current_run_id", ""))

    def draft_dir(self, thread_id: int | str) -> Path:
        return self.drafts_root / str(thread_id)

    def issue_dir(self, issue_key: str) -> Path:
        return self.issues_root / _safe_issue_key(issue_key)

    def issue_latest_dir(self, issue_key: str) -> Path:
        return self.issue_dir(issue_key) / "latest"

    def entity_dir(self, identifier: str | int) -> Path:
        entity_key = self._resolve_entity_key(identifier)
        if _is_issue_key(entity_key):
            return self.issue_dir(entity_key)
        return self.draft_dir(entity_key)

    def attachments_dir(self, identifier: str | int) -> Path:
        path = self.entity_dir(identifier) / "attachments"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def debug_artifacts_dir(self, identifier: str | int) -> Path:
        path = (
            (
                self.issue_latest_dir(self._resolve_entity_key(identifier))
                if _is_issue_key(self._resolve_entity_key(identifier))
                else self.entity_dir(identifier)
            )
            / "debug"
            / "raw"
        )
        path.mkdir(parents=True, exist_ok=True)
        return path

    def execution_run_dir(self, identifier: str | int, run_id: str | None = None) -> Path:
        entity_key = self._resolve_entity_key(identifier)
        resolved = run_id or self.current_run_id(entity_key)
        if _is_issue_key(entity_key):
            return self.issue_dir(entity_key) / "runs" / resolved
        return self.draft_dir(entity_key) / "runs" / resolved

    def execution_artifacts_dir(self, identifier: str | int, run_id: str | None = None) -> Path:
        return self.execution_run_dir(identifier, run_id) / "artifacts"

    def append_message(self, identifier: str | int, role: str, content: str) -> None:
        entity_dir = self.entity_dir(identifier)
        entity_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "timestamp": datetime.now(UTC).isoformat(),
            "role": role,
            "content": content,
        }
        with (entity_dir / "conversation.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def append_log(self, identifier: str | int, message: str) -> None:
        entity_dir = self.entity_dir(identifier)
        entity_dir.mkdir(parents=True, exist_ok=True)
        with (entity_dir / "run.log").open("a", encoding="utf-8") as fh:
            fh.write(message.rstrip() + "\n")

    def record_activity(
        self,
        identifier: str | int,
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
        self.write_artifact(identifier, "current_activity.json", payload)
        history = self.load_artifact(identifier, "activity_history.json")
        items = history.get("items", []) if isinstance(history, dict) else []
        if not isinstance(items, list):
            items = []
        items.append(payload)
        self.write_artifact(identifier, "activity_history.json", {"items": items[-200:]})
        self.append_log(identifier, f"[activity:{phase}:{status}] {summary}")
        return payload

    def clear_activity(self, identifier: str | int) -> None:
        self.write_artifact(identifier, "current_activity.json", {})

    def update_status(self, identifier: str | int, status: str) -> None:
        self.update_meta(identifier, status=status)

    def update_meta(self, identifier: str | int, **fields: object) -> None:
        meta_path = self.entity_dir(identifier) / "meta.json"
        if not meta_path.exists():
            return
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta.update(fields)
        self._write_json(meta_path, meta)

    def has_run(self, thread_id: int) -> bool:
        draft_exists = (self.draft_dir(thread_id) / "meta.json").exists()
        if draft_exists:
            return True
        binding = self.issue_key_for_thread(thread_id)
        return bool(binding and (self.issue_dir(binding) / "meta.json").exists())

    def bind_thread(self, thread_id: int, issue_key: str) -> None:
        current_issue_key = self.issue_key_for_thread(thread_id)
        if current_issue_key and current_issue_key != issue_key:
            raise RuntimeError(f"Thread {thread_id} is already bound to {current_issue_key}")
        issue_meta = self.load_issue_meta(issue_key)
        existing_thread_id = str(issue_meta.get("thread_id", "")).strip()
        if existing_thread_id and existing_thread_id != str(thread_id):
            raise RuntimeError(f"Issue {issue_key} is already bound to thread {existing_thread_id}")
        payload = {
            "thread_id": str(thread_id),
            "issue_key": issue_key,
            "bound_at": datetime.now(UTC).isoformat(),
        }
        self._write_json(self.bindings_root / f"{thread_id}.json", payload)
        draft_meta = self.load_draft_meta(thread_id)
        if not draft_meta:
            self.create_draft(
                thread_id,
                parent_message_id=int(issue_meta.get("parent_message_id", 0) or 0),
                channel_id=int(issue_meta.get("channel_id", 0) or 0),
                status=str(issue_meta.get("status", "collecting_requirements") or "collecting_requirements"),
            )
        if issue_meta:
            self.update_issue_meta(issue_key, thread_id=str(thread_id))
        self.update_draft_meta(thread_id, issue_key=issue_key)

    def bind_issue(self, thread_id: int, repo_full_name: str, issue_number: int) -> str:
        issue_key = f"{repo_full_name}#{issue_number}"
        draft_meta = self.load_draft_meta(thread_id)
        if not self.load_issue_meta(issue_key):
            self.create_issue_record(
                issue_key,
                thread_id=thread_id,
                parent_message_id=int(draft_meta.get("parent_message_id", 0) or 0),
                channel_id=int(draft_meta.get("channel_id", 0) or 0),
                status=str(draft_meta.get("status", "draft")),
            )
        self._promote_draft_artifacts(thread_id, issue_key)
        self.bind_thread(thread_id, issue_key)
        self.update_draft_meta(
            thread_id,
            issue_key=issue_key,
            github_repo=repo_full_name,
            issue_number=str(issue_number),
        )
        return issue_key

    def issue_key_for_thread(self, thread_id: int) -> str:
        path = self.bindings_root / f"{thread_id}.json"
        if path.exists():
            payload = json.loads(path.read_text(encoding="utf-8"))
            return str(payload.get("issue_key", ""))
        meta = self.load_draft_meta(thread_id)
        return str(meta.get("issue_key", ""))

    def thread_id_for_issue(self, issue_key: str) -> str:
        return str(self.load_issue_meta(issue_key).get("thread_id", ""))

    def list_runs_by_status(self, statuses: set[str]) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for meta_path in self.issues_root.glob("*/meta.json"):
            payload = json.loads(meta_path.read_text(encoding="utf-8"))
            if str(payload.get("status", "")) in statuses:
                items.append(payload)
        for meta_path in self.drafts_root.glob("*/meta.json"):
            payload = json.loads(meta_path.read_text(encoding="utf-8"))
            if str(payload.get("status", "")) in statuses:
                items.append(payload)
        return items

    def list_issue_records(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for meta_path in self.issues_root.glob("*/meta.json"):
            items.append(json.loads(meta_path.read_text(encoding="utf-8")))
        return items

    def write_artifact(self, identifier: str | int, filename: str, payload: object) -> None:
        entity_key = self._resolve_entity_key(identifier)
        target_dir = self.issue_latest_dir(entity_key) if _is_issue_key(entity_key) else self.entity_dir(entity_key)
        target_dir.mkdir(parents=True, exist_ok=True)
        self._write_json(target_dir / filename, payload)

    def write_execution_artifact(
        self, identifier: str | int, filename: str, payload: object, run_id: str | None = None
    ) -> None:
        artifacts_dir = self.execution_artifacts_dir(identifier, run_id)
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        self._write_json(artifacts_dir / filename, payload)
        self.write_artifact(identifier, filename, payload)

    def record_failure(
        self,
        identifier: str | int,
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
        self.write_artifact(identifier, "agent_failure.json", payload)
        self.write_artifact(identifier, "last_failure.json", payload)
        self.append_log(identifier, f"[failure:{stage}] {message}")
        if payload["details"]:
            self.append_log(identifier, json.dumps(payload["details"], ensure_ascii=False))
        return payload

    def write_debug_artifact(self, identifier: str | int, filename: str, payload: object) -> str:
        safe_payload, raw_value_types = _json_safe_payload(payload)
        if isinstance(safe_payload, dict) and raw_value_types:
            safe_payload = dict(safe_payload)
            safe_payload.setdefault("raw_value_types", raw_value_types)
        path = self.debug_artifacts_dir(identifier) / filename
        self._write_json(path, safe_payload)
        return str(path)

    def write_debug_text_artifact(self, identifier: str | int, filename: str, content: str) -> str:
        path = self.debug_artifacts_dir(identifier) / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_sanitize_for_log(content), encoding="utf-8")
        return str(path)

    def list_debug_artifacts(self, identifier: str | int) -> list[str]:
        debug_dir = self.debug_artifacts_dir(identifier)
        return [str(path) for path in sorted(debug_dir.rglob("*")) if path.is_file()]

    def clear_debug_artifacts(self, identifier: str | int) -> None:
        debug_dir = self.debug_artifacts_dir(identifier)
        if debug_dir.exists():
            shutil.rmtree(debug_dir)

    def delete_artifact(self, identifier: str | int, filename: str) -> None:
        entity_key = self._resolve_entity_key(identifier)
        target_dir = self.issue_latest_dir(entity_key) if _is_issue_key(entity_key) else self.entity_dir(entity_key)
        path = target_dir / filename
        if path.exists():
            path.unlink()

    def delete_draft_artifact(self, thread_id: int | str, filename: str) -> None:
        path = self.draft_dir(thread_id) / filename
        if path.exists():
            path.unlink()

    def load_meta(self, identifier: str | int) -> dict[str, Any]:
        path = self.entity_dir(identifier) / "meta.json"
        if not path.exists():
            legacy = self._legacy_meta_path(identifier)
            if legacy.exists():
                return json.loads(legacy.read_text(encoding="utf-8"))
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    def load_draft_meta(self, thread_id: int | str) -> dict[str, Any]:
        path = self.draft_dir(thread_id) / "meta.json"
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    def load_issue_meta(self, issue_key: str) -> dict[str, Any]:
        path = self.issue_dir(issue_key) / "meta.json"
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    def update_draft_meta(self, thread_id: int | str, **fields: object) -> None:
        meta_path = self.draft_dir(thread_id) / "meta.json"
        if not meta_path.exists():
            return
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta.update(fields)
        self._write_json(meta_path, meta)

    def update_issue_meta(self, issue_key: str, **fields: object) -> None:
        self.update_meta(issue_key, **fields)

    def load_artifact(self, identifier: str | int, filename: str) -> object:
        entity_key = self._resolve_entity_key(identifier)
        target_dir = self.issue_latest_dir(entity_key) if _is_issue_key(entity_key) else self.entity_dir(entity_key)
        path = target_dir / filename
        if not path.exists():
            legacy = self._legacy_entity_dir(identifier) / filename
            if legacy.exists():
                return json.loads(legacy.read_text(encoding="utf-8"))
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    def load_execution_artifact(self, identifier: str | int, filename: str, run_id: str | None = None) -> object:
        path = self.execution_artifacts_dir(identifier, run_id) / filename
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    def _promote_draft_artifacts(self, thread_id: int, issue_key: str) -> None:
        draft_dir = self.draft_dir(thread_id)
        if not draft_dir.exists():
            return

        for filename in (
            "requirement_summary.json",
            "plan.json",
            "test_plan.json",
            "verification_plan.json",
            "planning_progress.json",
            "issue.json",
        ):
            path = draft_dir / filename
            if not path.exists():
                continue
            self._write_json(self.issue_latest_dir(issue_key) / filename, json.loads(path.read_text(encoding="utf-8")))

        for filename in ("conversation.jsonl", "run.log"):
            source = draft_dir / filename
            if not source.exists():
                continue
            target = self.issue_dir(issue_key) / filename
            if target.exists():
                continue
            target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")

    def write_attachment_text(self, identifier: str | int, filename: str, content: str) -> str:
        path = self.attachments_dir(identifier) / filename
        path.write_text(content, encoding="utf-8")
        return str(path)

    def _resolve_entity_key(self, identifier: str | int) -> str:
        if isinstance(identifier, int):
            bound_issue = self.issue_key_for_thread(identifier)
            return bound_issue or str(identifier)
        text = str(identifier)
        if _is_issue_key(text):
            return text
        if text.isdigit():
            bound_issue = self.issue_key_for_thread(int(text))
            return bound_issue or text
        return text

    def _legacy_entity_dir(self, identifier: str | int) -> Path:
        return self.legacy_root / str(identifier)

    def _legacy_meta_path(self, identifier: str | int) -> Path:
        return self._legacy_entity_dir(identifier) / "meta.json"

    def _write_json(self, path: Path, payload: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _sanitize_payload(value: object) -> object:
    safe_value, _raw_value_types = _json_safe_payload(value)
    return safe_value


def _json_safe_payload(value: object, path: str = "") -> tuple[object, dict[str, str]]:
    key = path or "$"
    if isinstance(value, dict):
        dict_items: dict[str, object] = {}
        raw_value_types: dict[str, str] = {}
        for item_key, item in value.items():
            child_value, child_types = _json_safe_payload(item, f"{key}.{item_key}")
            dict_items[str(item_key)] = child_value
            raw_value_types.update(child_types)
        return dict_items, raw_value_types
    if isinstance(value, list):
        list_items: list[object] = []
        raw_value_types: dict[str, str] = {}
        for index, item in enumerate(value):
            child_value, child_types = _json_safe_payload(item, f"{key}[{index}]")
            list_items.append(child_value)
            raw_value_types.update(child_types)
        return list_items, raw_value_types
    if isinstance(value, tuple):
        tuple_items, raw_value_types = _json_safe_payload(list(value), key)
        raw_value_types.setdefault(key, "tuple")
        return tuple_items, raw_value_types
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace"), {key: "bytes"}
    if isinstance(value, str):
        return _sanitize_for_log(value), {}
    if value is None or isinstance(value, (bool, int, float)):
        return value, {}
    return _sanitize_for_log(repr(value)), {key: type(value).__name__}


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


def _safe_issue_key(issue_key: str) -> str:
    return issue_key.replace("/", "__").replace("#", "__")


def _is_issue_key(value: str) -> bool:
    return "#" in value and "/" in value


def _split_issue_key(issue_key: str) -> tuple[str, str]:
    repo_full_name, issue_number = issue_key.split("#", 1)
    return repo_full_name, issue_number
