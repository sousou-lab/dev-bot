from __future__ import annotations

import json
from pathlib import Path


def build_repo_profile(workspace: str) -> dict:
    root = Path(workspace)
    files = sorted(_relative_paths(root))
    package_json = root / "package.json"
    pyproject = root / "pyproject.toml"
    requirements = root / "requirements.txt"

    languages: list[str] = []
    if any(path.endswith(".py") for path in files):
        languages.append("python")
    if any(path.endswith((".ts", ".tsx", ".js", ".jsx")) for path in files):
        languages.append("javascript")

    test_commands: list[str] = []
    setup_commands: list[str] = []
    if pyproject.exists() or requirements.exists():
        test_commands.append("pytest -q")
        if requirements.exists():
            setup_commands.append("pip install -r requirements.txt")
        elif pyproject.exists():
            setup_commands.append("pip install -e .")
    if package_json.exists():
        try:
            package_data = json.loads(package_json.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            package_data = {}
        scripts = package_data.get("scripts", {})
        if "test" in scripts:
            test_commands.append("npm test -- --runInBand")
        setup_commands.append("npm install")

    readme_candidates = [path for path in files if Path(path).name.lower().startswith("readme")]
    sample_files = files[:200]

    return {
        "workspace": workspace,
        "languages": languages,
        "test_commands": _unique(test_commands) or ["pytest -q"],
        "setup_commands": _unique(setup_commands),
        "readme_files": readme_candidates[:5],
        "files": sample_files,
    }


def _relative_paths(root: Path) -> list[str]:
    output: list[str] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in {".git", "node_modules", "__pycache__", ".venv", "venv"} for part in path.parts):
            continue
        output.append(str(path.relative_to(root)))
    return output


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result
