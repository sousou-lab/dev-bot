from __future__ import annotations

import json
import os
import signal
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class ProcessRecord:
    issue_key: str
    run_id: str
    pid: int
    pgid: int
    runner_type: str


class ProcessRegistry:
    def __init__(self, runs_root: str) -> None:
        self.root = Path(runs_root) / "processes"
        self.root.mkdir(parents=True, exist_ok=True)

    def register(self, issue_key: str | int, run_id: str, pid: int, runner_type: str) -> ProcessRecord:
        key = str(issue_key)
        try:
            pgid = os.getpgid(pid)
        except OSError:
            pgid = pid
        record = ProcessRecord(
            issue_key=key,
            run_id=run_id,
            pid=pid,
            pgid=pgid,
            runner_type=runner_type,
        )
        self._record_path(key).write_text(json.dumps(asdict(record), indent=2), encoding="utf-8")
        return record

    def unregister(self, issue_key: str | int) -> None:
        path = self._record_path(str(issue_key))
        if path.exists():
            path.unlink()

    def load(self, issue_key: str | int) -> dict[str, object]:
        key = str(issue_key)
        path = self._record_path(key)
        if not path.exists():
            legacy = self._legacy_record_path(key)
            if not legacy.exists():
                return {}
            return json.loads(legacy.read_text(encoding="utf-8"))
        return json.loads(path.read_text(encoding="utf-8"))

    def terminate(self, issue_key: str | int) -> bool:
        payload = self.load(issue_key)
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
            self.unregister(issue_key)
            return False
        return True

    def is_active(self, issue_key: str | int) -> bool:
        payload = self.load(issue_key)
        if not payload:
            return False
        pgid = int(payload.get("pgid", 0) or 0)
        pid = int(payload.get("pid", 0) or 0)
        try:
            if pgid > 0:
                os.killpg(pgid, 0)
                return True
            if pid > 0:
                os.kill(pid, 0)
                return True
        except ProcessLookupError:
            self.unregister(issue_key)
            return False
        except PermissionError:
            return True
        self.unregister(issue_key)
        return False

    def _record_path(self, issue_key: str) -> Path:
        safe_key = issue_key.replace("/", "__").replace("#", "__")
        return self.root / f"{safe_key}.json"

    def _legacy_record_path(self, issue_key: str) -> Path:
        return self.root.parent / issue_key / "process.json"
