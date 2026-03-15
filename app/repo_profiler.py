from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml


def build_repo_profile(workspace: str) -> dict[str, Any]:
    root = Path(workspace)
    files = sorted(_relative_paths(root))
    package_json = root / "package.json"
    pyproject = root / "pyproject.toml"
    requirements = root / "requirements.txt"
    repo_config = _load_repo_config(root)
    python_detected = any(path.endswith(".py") for path in files)
    js_detected = any(path.endswith((".ts", ".tsx", ".js", ".jsx")) for path in files)
    ts_detected = any(path.endswith((".ts", ".tsx")) for path in files)

    languages: list[str] = []
    if python_detected:
        languages.append("python")
    if js_detected:
        languages.append("javascript")
    if ts_detected:
        languages.append("typescript")
    if repo_config.get("language"):
        configured = str(repo_config["language"]).strip()
        if configured and configured not in languages:
            languages.append(configured)

    test_commands: list[str] = []
    setup_commands: list[str] = []
    lint_commands: list[str] = []
    typecheck_commands: list[str] = []
    format_commands: list[str] = []
    build_commands: list[str] = []
    migration = _detect_migration(root, files)
    if python_detected:
        lint_commands.append("python -m compileall .")
        lint_commands.append("uv run --with ruff ruff check .")
        typecheck_commands.append("uv run --with pyright pyright .")
        format_commands.append("uv run --with ruff ruff format --check .")
        if any(Path(path).name.startswith("test_") or "/tests/" in f"/{path}" for path in files):
            test_commands.append("uv run --with pytest pytest -q")
    if pyproject.exists() or requirements.exists():
        if requirements.exists():
            setup_commands.append("uv pip install -r requirements.txt")
        elif pyproject.exists():
            setup_commands.append("uv sync")
    if package_json.exists():
        try:
            package_data = json.loads(package_json.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            package_data = {}
        scripts = package_data.get("scripts", {})
        if "test" in scripts:
            test_commands.append("npm test -- --runInBand")
        if "lint" in scripts:
            lint_commands.append("npm run lint")
        if "format" in scripts:
            format_commands.append("npm run format -- --check")
        if "check-format" in scripts:
            format_commands.append("npm run check-format")
        if "build" in scripts:
            build_commands.append("npm run build")
        if "typecheck" in scripts:
            typecheck_commands.append("npm run typecheck")
        elif (root / "tsconfig.json").exists():
            typecheck_commands.append("npx tsc --noEmit")
        if "lint" not in scripts and _has_eslint_config(root, files):
            lint_commands.append("npx eslint .")
        setup_commands.append("npm install --no-audit --no-fund")
    if any(path.endswith(".html") for path in files) and not package_json.exists() and not pyproject.exists():
        languages.append("html")

    setup_commands = _override_list(repo_config, "setup_cmds", setup_commands)
    test_commands = _override_list(repo_config, "test_cmds", test_commands)
    lint_commands = _override_list(repo_config, "lint_cmds", lint_commands)
    typecheck_commands = _override_list(repo_config, "typecheck_cmds", typecheck_commands)
    format_commands = _override_list(repo_config, "format_cmds", format_commands)
    build_commands = _override_list(repo_config, "build_cmds", build_commands)
    migration = _merge_migration(migration, repo_config.get("migration"))

    readme_candidates = [path for path in files if Path(path).name.lower().startswith("readme")]
    sample_files = files[:200]

    return {
        "workspace": workspace,
        "languages": languages,
        "test_commands": _unique(test_commands),
        "setup_commands": _unique(setup_commands),
        "lint_commands": _unique(lint_commands),
        "typecheck_commands": _unique(typecheck_commands),
        "format_commands": _unique(format_commands),
        "build_commands": _unique(build_commands),
        "suggested_verification_profile": _suggest_verification_profile(
            languages=languages,
            files=files,
            package_json=package_json.exists(),
            pyproject=pyproject.exists(),
        ),
        "migration": migration,
        "readme_files": readme_candidates[:5],
        "files": sample_files,
        "repo_config_loaded": bool(repo_config),
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


def _load_repo_config(root: Path) -> dict[str, Any]:
    path = root / ".devbot.yml"
    if not path.exists():
        return {}
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise RuntimeError(f"Failed to parse .devbot.yml: {exc}") from exc
    return payload if isinstance(payload, dict) else {}


def _override_list(repo_config: dict[str, Any], key: str, default: list[str]) -> list[str]:
    value = repo_config.get(key)
    if not isinstance(value, list):
        return default
    result = [str(item).strip() for item in value if str(item).strip()]
    return result or default


def _detect_migration(root: Path, files: list[str]) -> dict[str, Any]:
    if (root / "alembic.ini").exists() or any(path.startswith("alembic/versions/") for path in files):
        return {
            "engine": "alembic",
            "apply_cmds": ["alembic upgrade head"],
            "rollback_cmds": ["alembic downgrade -1"],
            "notes": ["DATABASE_URL などの接続先はテストDBに向けておくこと"],
        }
    if (root / "manage.py").exists() and any(
        "/migrations/" in path or path.endswith("/migrations/__init__.py") for path in files
    ):
        return {
            "engine": "django",
            "apply_cmds": ["python manage.py migrate"],
            "rollback_cmds": [],
            "notes": ["rollback は .devbot.yml で明示しない限り自動化しません"],
        }
    return {
        "engine": "",
        "apply_cmds": [],
        "rollback_cmds": [],
        "notes": [],
    }


def _merge_migration(base: dict[str, Any], configured: Any) -> dict[str, Any]:
    if not isinstance(configured, dict):
        return base
    merged = dict(base)
    for key in ("engine", "database_url_env"):
        value = configured.get(key)
        if value is not None:
            merged[key] = str(value)
    for key in ("apply_cmds", "rollback_cmds", "notes"):
        value = configured.get(key)
        if isinstance(value, list):
            merged[key] = [str(item).strip() for item in value if str(item).strip()]
    for key in ("apply_cmd", "rollback_cmd"):
        value = configured.get(key)
        if isinstance(value, str) and value.strip():
            list_key = "apply_cmds" if key == "apply_cmd" else "rollback_cmds"
            merged[list_key] = [value.strip()]
    return merged


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _suggest_verification_profile(
    *, languages: list[str], files: list[str], package_json: bool, pyproject: bool
) -> str:
    language_set = set(languages)
    if "html" in language_set and not package_json and not pyproject:
        return "static-web"
    if "python" in language_set and "typescript" in language_set:
        return "mixed-py-ts"
    if "typescript" in language_set:
        return "ts-basic"
    if "javascript" in language_set:
        return "node-basic"
    if "python" in language_set:
        if any(path.endswith("pyrightconfig.json") for path in files):
            return "python-typecheck"
        return "python-basic"
    return "generic-minimal"


def _has_eslint_config(root: Path, files: list[str]) -> bool:
    config_names = {
        ".eslintrc",
        ".eslintrc.js",
        ".eslintrc.cjs",
        ".eslintrc.json",
        ".eslintrc.yaml",
        ".eslintrc.yml",
        "eslint.config.js",
        "eslint.config.cjs",
        "eslint.config.mjs",
        "eslint.config.ts",
    }
    file_names = {Path(path).name for path in files}
    if file_names & config_names:
        return True
    package_json = root / "package.json"
    if not package_json.exists():
        return False
    try:
        package_data = json.loads(package_json.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False
    return "eslintConfig" in package_data
