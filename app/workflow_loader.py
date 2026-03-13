from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from app.contracts.workflow_schema import WorkflowConfig, WorkflowValidationError

_FRONT_MATTER_RE = re.compile(r"\A---\n(?P<yaml>.*?)\n---\n?(?P<body>.*)\Z", re.DOTALL)


class WorkflowLoadError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class WorkflowDefinition:
    path: Path
    raw_text: str
    raw_config: dict[str, Any]
    config: WorkflowConfig | None
    prompt_body: str
    config_error: str = ""
    uses_cached_config: bool = False


_LAST_KNOWN_GOOD_DEFINITIONS: dict[Path, WorkflowDefinition] = {}


def _resolve_root(workspace: str | None = None, repo_root: str | None = None) -> Path:
    return Path(workspace or repo_root or ".").resolve()


def _cache_last_known_good(definition: WorkflowDefinition) -> WorkflowDefinition:
    if definition.config is not None:
        _LAST_KNOWN_GOOD_DEFINITIONS[definition.path] = definition
    else:
        _LAST_KNOWN_GOOD_DEFINITIONS.pop(definition.path, None)
    return definition


def _build_definition_with_fallback(
    *,
    path: Path,
    raw_text: str,
    raw_config: dict[str, Any],
    prompt_body: str,
    config_error: str,
) -> WorkflowDefinition:
    cached = _LAST_KNOWN_GOOD_DEFINITIONS.get(path)
    if cached is None:
        return WorkflowDefinition(
            path=path,
            raw_text=raw_text,
            raw_config=raw_config,
            config=None,
            prompt_body=prompt_body,
            config_error=config_error,
        )
    return WorkflowDefinition(
        path=path,
        raw_text=raw_text,
        raw_config=raw_config,
        config=cached.config,
        prompt_body=prompt_body,
        config_error=f"{config_error} (using last known good workflow config)",
        uses_cached_config=True,
    )


def load_workflow_definition(
    workspace: str | None = None,
    repo_root: str | None = None,
    *,
    strict: bool = False,
) -> WorkflowDefinition | None:
    root = _resolve_root(workspace=workspace, repo_root=repo_root)
    path = root / "WORKFLOW.md"
    if not path.exists():
        _LAST_KNOWN_GOOD_DEFINITIONS.pop(path, None)
        return None

    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        return _cache_last_known_good(
            WorkflowDefinition(
                path=path,
                raw_text=text,
                raw_config={},
                config=None,
                prompt_body=text.strip(),
            )
        )

    match = _FRONT_MATTER_RE.match(text)
    if not match:
        if strict:
            raise WorkflowLoadError("WORKFLOW.md must begin with YAML front matter")
        return _build_definition_with_fallback(
            path=path,
            raw_text=text,
            raw_config={},
            prompt_body=text.strip(),
            config_error="WORKFLOW.md must begin with YAML front matter",
        )

    raw_config = yaml.safe_load(match.group("yaml")) or {}
    if not isinstance(raw_config, dict):
        if strict:
            raise WorkflowLoadError("workflow front matter must decode to a mapping")
        return _build_definition_with_fallback(
            path=path,
            raw_text=text,
            raw_config={},
            prompt_body=match.group("body").strip(),
            config_error="workflow front matter must decode to a mapping",
        )

    prompt_body = match.group("body").strip()
    try:
        config = WorkflowConfig.from_dict(raw_config)
        config_error = ""
    except WorkflowValidationError as exc:
        if strict:
            raise
        return _build_definition_with_fallback(
            path=path,
            raw_text=text,
            raw_config=raw_config,
            prompt_body=prompt_body,
            config_error=str(exc),
        )

    return _cache_last_known_good(
        WorkflowDefinition(
            path=path,
            raw_text=text,
            raw_config=raw_config,
            config=config,
            prompt_body=prompt_body,
            config_error=config_error,
        )
    )


def load_workflow(workspace: str | None = None, repo_root: str | None = None) -> dict[str, Any]:
    definition = load_workflow_definition(workspace=workspace, repo_root=repo_root, strict=False)
    if definition is None:
        return {}

    payload = dict(definition.raw_config)
    payload["raw_text"] = definition.raw_text
    payload["contract_body"] = definition.prompt_body
    payload["config"] = definition.config
    payload["config_error"] = definition.config_error
    payload["uses_cached_config"] = definition.uses_cached_config
    payload["workflow_definition"] = definition
    return payload


def workflow_text(workspace: str | None = None, repo_root: str | None = None) -> str:
    payload = load_workflow(workspace=workspace, repo_root=repo_root)
    return str(payload.get("raw_text", ""))
