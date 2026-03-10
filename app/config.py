from __future__ import annotations

import os
from dataclasses import dataclass

try:
    from dotenv import load_dotenv
except ModuleNotFoundError:  # pragma: no cover - optional in bare test env
    def load_dotenv() -> None:
        return None


load_dotenv()


@dataclass(frozen=True)
class Settings:
    discord_bot_token: str
    discord_guild_id: str
    github_token: str
    anthropic_api_key: str
    requirements_channel_id: str
    workspace_root: str
    runs_root: str
    max_implementation_iterations: int
    max_concurrent_runs: int
    codex_bin: str
    approval_timeout_seconds: int

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            discord_bot_token=os.getenv("DISCORD_BOT_TOKEN", ""),
            discord_guild_id=os.getenv("DISCORD_GUILD_ID", ""),
            github_token=os.getenv("GITHUB_TOKEN", ""),
            anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
            requirements_channel_id=os.getenv("REQUIREMENTS_CHANNEL_ID", ""),
            workspace_root=os.getenv("WORKSPACE_ROOT", "/tmp/dev-bot-workspaces"),
            runs_root=os.getenv("RUNS_ROOT", "./runs"),
            max_implementation_iterations=int(os.getenv("MAX_IMPLEMENTATION_ITERATIONS", "5")),
            max_concurrent_runs=int(os.getenv("MAX_CONCURRENT_RUNS", "5")),
            codex_bin=os.getenv("CODEX_BIN", "codex"),
            approval_timeout_seconds=int(os.getenv("APPROVAL_TIMEOUT_SECONDS", "900")),
        )


def validate_settings(settings: Settings) -> list[str]:
    missing: list[str] = []
    if not settings.discord_bot_token:
        missing.append("DISCORD_BOT_TOKEN")
    if not settings.github_token:
        missing.append("GITHUB_TOKEN")
    if not settings.requirements_channel_id:
        missing.append("REQUIREMENTS_CHANNEL_ID")
    return missing
