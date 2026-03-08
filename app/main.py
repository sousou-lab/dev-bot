from __future__ import annotations

from app.config import Settings, validate_settings
from app.discord_adapter import build_client


def main() -> int:
    settings = Settings.from_env()
    missing = validate_settings(settings)

    # if missing:
    #     print("Missing environment variables:")
    #     for key in missing:
    #         print(f"- {key}")
    #     return 1

    print("Configuration loaded successfully.")
    print(f"Requirements channel ID: {settings.requirements_channel_id}")
    client = build_client(settings)
    client.run(settings.discord_bot_token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
