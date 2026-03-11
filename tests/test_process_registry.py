from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.process_registry import ProcessRegistry


class ProcessRegistryTests(unittest.TestCase):
    def test_load_reads_legacy_thread_process_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            legacy_dir = Path(tmpdir) / "123"
            legacy_dir.mkdir(parents=True, exist_ok=True)
            payload = {
                "issue_key": "123",
                "run_id": "run-1",
                "pid": 999,
                "pgid": 999,
                "runner_type": "codex",
            }
            (legacy_dir / "process.json").write_text(json.dumps(payload), encoding="utf-8")

            registry = ProcessRegistry(tmpdir)

            self.assertEqual(payload, registry.load(123))
