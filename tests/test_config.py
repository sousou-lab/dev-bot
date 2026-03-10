from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

from app.config import Settings, validate_settings


class SettingsTests(unittest.TestCase):
    def test_validate_settings_does_not_require_openai_api_key_for_codex_cli_login(self) -> None:
        settings = Settings(
            openai_api_key="",
            discord_bot_token="discord",
            discord_guild_id="guild",
            discord_status_channel_id="status",
            workspace_root="/tmp/workspaces",
            state_dir="/tmp/state",
            github_app_id="1",
            github_app_private_key_path=__file__,
            github_app_installation_id="99",
            anthropic_api_key="",
            planning_lane_enabled=False,
        )

        missing = validate_settings(settings)

        self.assertNotIn("OPENAI_API_KEY", missing)

    def test_validate_settings_does_not_require_anthropic_api_key_when_oauth_login_is_used(self) -> None:
        settings = Settings(
            openai_api_key="",
            discord_bot_token="discord",
            discord_guild_id="guild",
            discord_status_channel_id="status",
            workspace_root="/tmp/workspaces",
            state_dir="/tmp/state",
            github_app_id="1",
            github_app_private_key_path=__file__,
            github_app_installation_id="99",
            anthropic_api_key="",
            planning_lane_enabled=False,
        )

        missing = validate_settings(settings)

        self.assertNotIn("ANTHROPIC_API_KEY", missing)

    def test_from_env_uses_state_dir_and_legacy_runs_root_fallback(self) -> None:
        with tempfile.NamedTemporaryFile() as key_file:
            env = {
                "DISCORD_BOT_TOKEN": "discord",
                "DISCORD_GUILD_ID": "guild",
                "DISCORD_STATUS_CHANNEL_ID": "status",
                "WORKSPACE_ROOT": "/tmp/workspaces",
                "RUNS_ROOT": "/tmp/legacy-runs",
                "GITHUB_APP_ID": "1",
                "GITHUB_APP_PRIVATE_KEY_PATH": key_file.name,
                "GITHUB_APP_INSTALLATION_ID": "99",
                "PLANNING_LANE_ENABLED": "false",
            }
            with patch.dict(os.environ, env, clear=True):
                settings = Settings.from_env()

        self.assertEqual("/tmp/legacy-runs", settings.state_dir)
        self.assertEqual("/tmp/legacy-runs", settings.runs_root)

    def test_from_env_uses_claude_agent_max_buffer_size(self) -> None:
        with tempfile.NamedTemporaryFile() as key_file:
            env = {
                "DISCORD_BOT_TOKEN": "discord",
                "DISCORD_GUILD_ID": "guild",
                "DISCORD_STATUS_CHANNEL_ID": "status",
                "WORKSPACE_ROOT": "/tmp/workspaces",
                "STATE_DIR": "/tmp/state",
                "GITHUB_APP_ID": "1",
                "GITHUB_APP_PRIVATE_KEY_PATH": key_file.name,
                "GITHUB_APP_INSTALLATION_ID": "99",
                "CLAUDE_AGENT_MAX_BUFFER_SIZE": "5242880",
            }
            with patch.dict(os.environ, env, clear=True):
                settings = Settings.from_env()

        self.assertEqual(5242880, settings.claude_agent_max_buffer_size)
