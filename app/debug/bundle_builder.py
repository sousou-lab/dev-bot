from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class IncidentBundleBuilder:
    def __init__(self, bundle_root: Path) -> None:
        self.bundle_root = bundle_root

    def materialize(self, issue_key: str, run_id: str, payload: dict[str, Any]) -> Path:
        bundle_dir = self.bundle_root / "incident_bundle"
        bundle_dir.mkdir(parents=True, exist_ok=True)
        manifest = {
            "issue_key": issue_key,
            "run_id": run_id,
            "created_at": datetime.now(UTC).isoformat(),
            "sanitization_profile": "strict",
            "contains_pii": False,
            "cleanup_policy": "delete_on_terminal_keep_manifest",
            "sources": [],
        }
        for filename, content in payload.items():
            path = bundle_dir / filename
            if isinstance(content, (dict, list)):
                path.write_text(json.dumps(content, ensure_ascii=False, indent=2), encoding="utf-8")
            else:
                path.write_text(str(content), encoding="utf-8")
            manifest["sources"].append({"kind": filename, "path": filename})
        (bundle_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        return bundle_dir

    def freeze(self, bundle_dir: Path) -> None:
        (bundle_dir / ".frozen").write_text("true\n", encoding="utf-8")

    def cleanup_keep_provenance(self, bundle_dir: Path, artifacts_dir: Path) -> None:
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        manifest = bundle_dir / "manifest.json"
        summary = bundle_dir / "summary.md"
        if manifest.exists():
            (artifacts_dir / "incident_bundle_manifest.json").write_text(
                manifest.read_text(encoding="utf-8"), encoding="utf-8"
            )
        if summary.exists():
            (artifacts_dir / "incident_bundle_summary.md").write_text(
                summary.read_text(encoding="utf-8"), encoding="utf-8"
            )
        for child in bundle_dir.iterdir():
            child.unlink()
        bundle_dir.rmdir()
