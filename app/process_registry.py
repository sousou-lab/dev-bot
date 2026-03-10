from __future__ import annotations

import json
import os
import signal
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class ProcessRecord:
    thread_id: str
    run_id: str
    pid: int
    pgid: int
    runner_type: str


class ProcessRegistry:
    def __init__(self, runs_root: str) -> None:
        self.root = Path(runs_root)
        self.root.mkdir(parents=True, exist_ok=True)

    def register(self, thread_id: int, run_id: str, pid: int, runner_type: str) -> ProcessRecord:
        try:
            pgid = os.getpgid(pid)
        except OSError:
            pgid = pid
        record = ProcessRecord(
            thread_id=str(thread_id),
            run_id=run_id,
            pid=pid,
            pgid=pgid,
            runner_type=runner_type,
        )
        self._record_path(thread_id).write_text(json.dumps(asdict(record), indent=2), encoding="utf-8")
        return record

    def unregister(self, thread_id: int) -> None:
        path = self._record_path(thread_id)
        if path.exists():
            path.unlink()

    def load(self, thread_id: int) -> dict[str, object]:
        path = self._record_path(thread_id)
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    def terminate(self, thread_id: int) -> bool:
        payload = self.load(thread_id)
        if not payload:
            return False
        pgid = int(payload.get("pgid", 0) or 0)
        pid = int(payload.get("pid", 0) or 0)
        try:
            if pgid > 0:
                os.killpg(pgid, signal.SIGTERM)
            elif pid > 0:
                os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            self.unregister(thread_id)
            return False
        return True

    def _record_path(self, thread_id: int) -> Path:
        return self.root / str(thread_id) / "process.json"
