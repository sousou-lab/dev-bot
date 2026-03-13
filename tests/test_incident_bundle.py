from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from app.debug.bundle_builder import IncidentBundleBuilder


class IncidentBundleTests(unittest.TestCase):
    def test_materialize_and_cleanup_keep_provenance(self) -> None:
        with TemporaryDirectory() as tmpdir:
            builder = IncidentBundleBuilder(Path(tmpdir))
            bundle_dir = builder.materialize("owner/repo#1", "run-1", {"stack.json": {"a": 1}, "summary.md": "ok"})
            builder.freeze(bundle_dir)
            artifacts_dir = Path(tmpdir) / "artifacts"
            builder.cleanup_keep_provenance(bundle_dir, artifacts_dir)

            manifest = json.loads((artifacts_dir / "incident_bundle_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual("owner/repo#1", manifest["issue_key"])
            self.assertEqual("ok", (artifacts_dir / "incident_bundle_summary.md").read_text(encoding="utf-8"))
