from __future__ import annotations

from pathlib import Path
from typing import Any


def build_verification_plan(
    *,
    workspace: str,
    repo_profile: dict[str, Any],
    plan: dict[str, Any],
) -> dict[str, Any]:
    profile = _select_profile(repo_profile)
    repair_profile = _select_repair_profile(profile)
    scope_paths = _select_scope(plan)
    bootstrap_commands = _commands_for_scope(repo_profile.get("setup_commands", []), scope_paths)
    hard_checks, advisory_checks = _build_checks(profile, repo_profile, scope_paths)
    repair_checks = _build_repair_checks(repair_profile, repo_profile, scope_paths)
    return {
        "profile": profile,
        "repair_profile": repair_profile,
        "scope": {"paths": scope_paths},
        "selection_reason": _selection_reason(profile, repo_profile),
        "confidence": _confidence(profile),
        "profile_patch": {},
        "bootstrap_commands": bootstrap_commands,
        "hard_checks": hard_checks,
        "advisory_checks": advisory_checks,
        "repair_checks": repair_checks,
    }


def workflow_verification_from_plan(plan: dict[str, Any]) -> dict[str, Any]:
    bootstrap_commands = plan.get("bootstrap_commands", []) if isinstance(plan, dict) else []
    hard_checks = plan.get("hard_checks", []) if isinstance(plan, dict) else []
    advisory_checks = plan.get("advisory_checks", []) if isinstance(plan, dict) else []
    return {
        "bootstrap_commands": [str(item).strip() for item in bootstrap_commands if str(item).strip()],
        "required_checks": [_workflow_check(item, "hard") for item in hard_checks if isinstance(item, dict)],
        "advisory_checks": [_workflow_check(item, "advisory") for item in advisory_checks if isinstance(item, dict)],
    }


def _workflow_check(item: dict[str, Any], category: str) -> dict[str, Any]:
    return {
        "name": str(item.get("name", "")).strip() or "check",
        "command": str(item.get("command", "")).strip(),
        "category": category,
        "allow_not_applicable": bool(item.get("allow_not_applicable", False)),
    }


def _select_profile(repo_profile: dict[str, Any]) -> str:
    suggested = str(repo_profile.get("suggested_verification_profile", "")).strip()
    if suggested:
        return suggested
    languages = {str(item).strip() for item in repo_profile.get("languages", []) if str(item).strip()}
    if "python" in languages and "typescript" in languages:
        return "mixed-py-ts"
    if "typescript" in languages:
        return "ts-basic"
    if "javascript" in languages:
        return "node-basic"
    if "python" in languages:
        return "python-basic"
    return "generic-minimal"


def _select_repair_profile(profile: str) -> str:
    if profile == "python-basic":
        return "python-fast-repair"
    if profile == "ts-basic":
        return "ts-fast-repair"
    if profile == "mixed-py-ts":
        return "mixed-py-ts"
    return ""


def _select_scope(plan: dict[str, Any]) -> list[str]:
    candidates = plan.get("candidate_files", []) if isinstance(plan, dict) else []
    top_dirs: set[str] = set()
    root_files = False
    for item in candidates:
        path = str(item).strip()
        if not path:
            continue
        parts = Path(path).parts
        if len(parts) <= 1:
            root_files = True
            continue
        top_dirs.add(parts[0])
    if root_files or not top_dirs or len(top_dirs) > 1:
        return ["."]
    return sorted(top_dirs)


def _build_checks(
    profile: str, repo_profile: dict[str, Any], scope_paths: list[str]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if profile == "static-web":
        return _static_web_checks(scope_paths)
    if profile == "generic-minimal":
        return _generic_minimal_checks(scope_paths)

    hard_checks: list[dict[str, Any]] = []
    advisory_checks: list[dict[str, Any]] = []

    for command in _commands_for_scope(repo_profile.get("lint_commands", []), scope_paths):
        hard_checks.append({"name": "lint", "command": command, "allow_not_applicable": False})
    for command in _commands_for_scope(repo_profile.get("test_commands", []), scope_paths):
        hard_checks.append({"name": "tests", "command": command, "allow_not_applicable": False})

    if profile in {"python-basic", "python-typecheck", "ts-basic", "node-ts", "mixed-py-ts"}:
        typecheck_commands = _commands_for_scope(repo_profile.get("typecheck_commands", []), scope_paths)
        if typecheck_commands:
            for command in typecheck_commands:
                hard_checks.append({"name": "typecheck", "command": command, "allow_not_applicable": False})

    for command in _commands_for_scope(repo_profile.get("format_commands", []), scope_paths):
        advisory_checks.append({"name": "format", "command": command, "allow_not_applicable": True})
    for command in _commands_for_scope(repo_profile.get("build_commands", []), scope_paths):
        advisory_checks.append({"name": "build", "command": command, "allow_not_applicable": True})

    if not hard_checks:
        return _generic_minimal_checks(scope_paths)
    return hard_checks, advisory_checks


def _build_repair_checks(
    repair_profile: str,
    repo_profile: dict[str, Any],
    scope_paths: list[str],
) -> list[dict[str, Any]]:
    if repair_profile not in {"python-fast-repair", "ts-fast-repair", "mixed-py-ts"}:
        return []
    checks: list[dict[str, Any]] = []
    for command in _commands_for_scope(repo_profile.get("format_commands", []), scope_paths):
        checks.append({"name": "format", "command": command, "allow_not_applicable": True})
    for command in _commands_for_scope(repo_profile.get("lint_commands", []), scope_paths):
        checks.append({"name": "lint", "command": command, "allow_not_applicable": True})
    for command in _commands_for_scope(repo_profile.get("typecheck_commands", []), scope_paths):
        checks.append({"name": "typecheck", "command": command, "allow_not_applicable": True})
    test_commands = _commands_for_scope(repo_profile.get("test_commands", []), scope_paths)
    if test_commands:
        checks.append({"name": "tests", "command": test_commands[0], "allow_not_applicable": True})
    return checks


def _commands_for_scope(commands: Any, scope_paths: list[str]) -> list[str]:
    if not isinstance(commands, list):
        return []
    if scope_paths == ["."]:
        return [str(item).strip() for item in commands if str(item).strip()]
    scoped: list[str] = []
    for item in commands:
        command = str(item).strip()
        if not command:
            continue
        path = scope_paths[0]
        if (
            command.startswith("npm ")
            or command.startswith("uv run ")
            or command.startswith("pytest ")
            or command.startswith("python ")
        ):
            scoped.append(f"cd {path} && {command}")
        else:
            scoped.append(f"cd {path} && {command}")
    return scoped


def _static_web_checks(scope_paths: list[str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    target = scope_paths[0] if scope_paths and scope_paths[0] != "." else "."
    hard_checks = [
        {
            "name": "html_static_smoke",
            "command": (
                'python -c "from pathlib import Path; '
                f"root=Path(r'{target}'); "
                "html=sorted(root.rglob('*.html')); "
                "assert html, 'no html files found'; "
                "text=html[0].read_text(encoding='utf-8', errors='replace'); "
                "assert '<html' in text.lower(), 'missing html root'; "
                "assert '<script' in text.lower() or '.js' in text.lower(), 'missing interactive script'; "
                'print(html[0])"'
            ),
            "allow_not_applicable": False,
        },
        {
            "name": "xss_static_scan",
            "command": (
                'python -c "from pathlib import Path; '
                f"root=Path(r'{target}'); "
                "files=list(root.rglob('*.html'))+list(root.rglob('*.js')); "
                "assert files, 'no html/js files found'; "
                "joined='\\n'.join(p.read_text(encoding='utf-8', errors='replace') for p in files); "
                "assert 'innerHTML' not in joined, 'innerHTML usage detected'; "
                "print('ok')\""
            ),
            "allow_not_applicable": False,
        },
    ]
    return hard_checks, []


def _generic_minimal_checks(scope_paths: list[str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    target = scope_paths[0] if scope_paths and scope_paths[0] != "." else "."
    hard_checks = [
        {
            "name": "workspace_sanity",
            "command": (
                'python -c "from pathlib import Path; '
                f"root=Path(r'{target}'); "
                "assert root.exists(), 'scope does not exist'; "
                "files=[p for p in root.rglob('*') if p.is_file()]; "
                "assert files, 'no files found in scope'; "
                'print(len(files))"'
            ),
            "allow_not_applicable": False,
        }
    ]
    return hard_checks, []


def _selection_reason(profile: str, repo_profile: dict[str, Any]) -> str:
    languages = [str(item).strip() for item in repo_profile.get("languages", []) if str(item).strip()]
    if profile == "static-web":
        return "html-centric repository without Python or Node project metadata"
    if profile == "generic-minimal":
        return "no trusted repository workflow and no strongly matched catalog profile"
    return f"repo profile languages={languages}"


def _confidence(profile: str) -> str:
    if profile == "generic-minimal":
        return "low"
    if profile == "static-web":
        return "medium"
    return "high"
