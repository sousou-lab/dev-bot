from __future__ import annotations

from app.config import ValidationError, load_settings, validate_settings
from app.discord_adapter import build_client
from app.github_client import GitHubIssueClient


def main() -> int:
    try:
        settings = load_settings()
    except ValidationError as exc:
        print(f"Invalid configuration: {exc}")
        return 1

    missing = validate_settings(settings)
    if missing:
        print("Missing environment variables:")
        for key in missing:
            print(f"- {key}")
        return 1

    try:
        settings.ensure_runtime_paths()
    except RuntimeError as exc:
        print(f"Configuration check failed: {exc}")
        return 1

    github_preflight = GitHubIssueClient(
        settings.github_token,
        app_id=getattr(settings, "github_app_id", ""),
        private_key_path=getattr(settings, "github_app_private_key_path", ""),
        installation_id=getattr(settings, "github_app_installation_id", ""),
        project_id=getattr(settings, "github_project_id", ""),
        project_state_field_id=getattr(settings, "github_project_state_field_id", ""),
        project_state_option_ids=getattr(settings, "github_project_state_option_ids", ""),
    ).preflight()
    if github_preflight.get("ok"):
        print(
            "GitHub preflight OK:"
            f" repo_count={github_preflight.get('repo_count', 0)}"
            f" sample={github_preflight.get('sample_repos', [])}"
        )
    else:
        print(
            "GitHub preflight warning:"
            f" error={github_preflight.get('error', 'unknown')}"
            f" fallback={github_preflight.get('fallback_repos', [])}"
        )

    client = build_client(settings)
    client.run(settings.discord_bot_token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
