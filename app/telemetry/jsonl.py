from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class JsonlTelemetrySink:
    def __init__(self, path: Path) -> None:
        self.path = path

    def write_event(
        self,
        *,
        event: str,
        issue_key: str,
        run_id: str,
        status: str,
        candidate_id: str | None = None,
        role: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        duration_ms: int | None = None,
        tokens_in: int | None = None,
        tokens_out: int | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(),
            "event": event,
            "issue_key": issue_key,
            "run_id": run_id,
            "candidate_id": candidate_id,
            "role": role,
            "provider": provider,
            "model": model,
            "duration_ms": duration_ms,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "status": status,
        }
        if extra:
            payload.update(extra)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
