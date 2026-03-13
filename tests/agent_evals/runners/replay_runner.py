from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class ReplayEvalRunner:
    def load_capture(self, capture_path: str | Path) -> dict[str, Any]:
        path = Path(capture_path)
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("replay capture must decode to a mapping")
        return payload
