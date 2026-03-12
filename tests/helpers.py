from __future__ import annotations

from typing import Any

from app.config import Settings
from app.state_store import FileStateStore


def make_test_settings(**overrides: Any) -> Settings:
    """Create a minimal Settings instance for testing (non-Pydantic fallback)."""
    defaults = {
        "discord_bot_token": "test-token",
        "discord_guild_id": "12345",
        "discord_status_channel_id": "67890",
        "workspace_root": "/tmp/dev-bot-test-workspaces",
        "state_dir": "/tmp/dev-bot-test-runs",
        "github_app_id": "1",
        "github_app_private_key_path": "/dev/null",
        "github_app_installation_id": "1",
        "anthropic_api_key": "sk-test",
    }
    defaults.update(overrides)
    return Settings(**defaults)


def make_test_issue(number: int = 1, **overrides: Any) -> dict[str, Any]:
    """Create a minimal Issue dict for testing."""
    issue: dict[str, Any] = {
        "number": number,
        "title": f"Test issue #{number}",
        "url": f"https://github.com/owner/repo/issues/{number}",
        "body": "Test issue body",
    }
    issue.update(overrides)
    return issue


def setup_planning_artifacts(
    thread_id: int,
    state_store: FileStateStore,
    *,
    plan: dict[str, Any] | None = None,
    test_plan: dict[str, Any] | None = None,
    verification_plan: dict[str, Any] | None = None,
    requirement_summary: dict[str, Any] | None = None,
) -> None:
    """Write the minimum planning artifacts required by ``execute_run``."""
    state_store.write_artifact(
        thread_id,
        "requirement_summary.json",
        requirement_summary or {"goal": "Test goal", "acceptance_criteria": [], "constraints": []},
    )
    state_store.write_artifact(
        thread_id,
        "plan.json",
        plan or {"steps": ["step1"]},
    )
    state_store.write_artifact(
        thread_id,
        "test_plan.json",
        test_plan or {"unit": ["test1"], "integration": [], "manual_checks": []},
    )
    state_store.write_artifact(
        thread_id,
        "verification_plan.json",
        verification_plan or {},
    )
