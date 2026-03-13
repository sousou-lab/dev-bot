from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]


def load_manifest() -> dict[str, Any]:
    return yaml.safe_load((ROOT / "docs/policy/manifest.yaml").read_text(encoding="utf-8"))


def build_output(output_name: str, sources: list[str]) -> str:
    chunks: list[str] = []
    for rel in sources:
        chunks.append((ROOT / rel).read_text(encoding="utf-8").rstrip())
    body = "\n\n".join(chunks).rstrip() + "\n"
    checksum = hashlib.sha256(body.encode("utf-8")).hexdigest()
    header = [
        "<!-- GENERATED FILE. DO NOT EDIT.",
        f"output: {output_name}",
        "sources:",
        *[f"  - {src}" for src in sources],
        f"checksum: sha256:{checksum}",
        "-->",
        "",
    ]
    return "\n".join(header) + body


def main() -> None:
    manifest = load_manifest()
    for output_name, sources in manifest["outputs"].items():
        rendered = build_output(output_name, sources)
        (ROOT / output_name).write_text(rendered, encoding="utf-8")


if __name__ == "__main__":
    main()
