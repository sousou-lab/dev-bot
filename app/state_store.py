from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path


@dataclass(frozen=True)
class RunMeta:
    thread_id: str
    parent_message_id: str
    channel_id: str
    created_at: str
    status: str


class FileStateStore:
    def __init__(self, runs_root: str = "runs") -> None:
        self.runs_root = Path(runs_root)
        self.runs_root.mkdir(parents=True, exist_ok=True)

    def create_run(self, thread_id: int, parent_message_id: int, channel_id: int) -> RunMeta:
        run_dir = self.runs_root / str(thread_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        meta = RunMeta(
            thread_id=str(thread_id),
            parent_message_id=str(parent_message_id),
            channel_id=str(channel_id),
            created_at=datetime.now(UTC).isoformat(),
            status="thread_created",
        )
        self._write_json(run_dir / "meta.json", asdict(meta))
        return meta

    def append_message(self, thread_id: int, role: str, content: str) -> None:
        run_dir = self.runs_root / str(thread_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "timestamp": datetime.now(UTC).isoformat(),
            "role": role,
            "content": content,
        }
        with (run_dir / "conversation.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def append_log(self, thread_id: int, message: str) -> None:
        run_dir = self.runs_root / str(thread_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        with (run_dir / "run.log").open("a", encoding="utf-8") as fh:
            fh.write(message.rstrip() + "\n")

    def update_status(self, thread_id: int, status: str) -> None:
        self.update_meta(thread_id, status=status)

    def update_meta(self, thread_id: int, **fields: str) -> None:
        run_dir = self.runs_root / str(thread_id)
        meta_path = run_dir / "meta.json"
        if not meta_path.exists():
            return
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta.update(fields)
        self._write_json(meta_path, meta)

    def has_run(self, thread_id: int) -> bool:
        return (self.runs_root / str(thread_id) / "meta.json").exists()

    def write_artifact(self, thread_id: int, filename: str, payload: object) -> None:
        run_dir = self.runs_root / str(thread_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        self._write_json(run_dir / filename, payload)

    def load_meta(self, thread_id: int) -> dict:
        meta_path = self.runs_root / str(thread_id) / "meta.json"
        if not meta_path.exists():
            return {}
        return json.loads(meta_path.read_text(encoding="utf-8"))

    def load_artifact(self, thread_id: int, filename: str) -> object:
        path = self.runs_root / str(thread_id) / filename
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    def _write_json(self, path: Path, payload: object) -> None:
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
