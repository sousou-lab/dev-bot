from __future__ import annotations

from app.config import ValidationError, load_settings, validate_settings
from app.github_client import GitHubIssueClient
from app.logging_setup import configure_logging, get_logger

logger = get_logger(__name__)


def main() -> int:
    configure_logging()

    try:
        settings = load_settings()
    except ValidationError as exc:
        logger.error("Invalid configuration: %s", exc)
        return 1

    missing = validate_settings(settings)
    if missing:
        logger.error("Missing environment variables: %s", ", ".join(missing))
        return 1

    try:
        settings.ensure_runtime_paths()
    except RuntimeError as exc:
        logger.error("Configuration check failed: %s", exc)
        return 1

    github_preflight = GitHubIssueClient(
        settings.github_token,
        app_id=getattr(settings, "github_app_id", ""),
        private_key_path=getattr(settings, "github_app_private_key_path", ""),
        installation_id=getattr(settings, "github_app_installation_id", ""),
        project_id=getattr(settings, "github_project_id", ""),
        project_state_field_id=getattr(settings, "github_project_state_field_id", ""),
        project_state_option_ids=getattr(settings, "github_project_state_option_ids", ""),
        project_plan_field_id=getattr(settings, "github_project_plan_field_id", ""),
        project_plan_option_ids=getattr(settings, "github_project_plan_option_ids", ""),
    ).preflight()
    if github_preflight.get("ok"):
        project = github_preflight.get("project", {})
        if isinstance(project, dict) and project.get("id"):
            logger.info(
                "GitHub preflight OK: repo_count=%s sample=%s project=%s",
                github_preflight.get("repo_count", 0),
                github_preflight.get("sample_repos", []),
                project.get("title") or project.get("id"),
            )
        else:
            logger.info(
                "GitHub preflight OK: repo_count=%s sample=%s",
                github_preflight.get("repo_count", 0),
                github_preflight.get("sample_repos", []),
            )
    else:
        if getattr(settings, "github_project_id", "").strip():
            logger.error(
                "GitHub preflight failed: error=%s fallback=%s",
                github_preflight.get("error", "unknown"),
                github_preflight.get("fallback_repos", []),
            )
            return 1
        logger.warning(
            "GitHub preflight warning: error=%s fallback=%s",
            github_preflight.get("error", "unknown"),
            github_preflight.get("fallback_repos", []),
        )

    try:
        from app.discord_adapter import build_client
    except ModuleNotFoundError as exc:
        if exc.name == "discord":
            logger.error("Discord dependency is not installed. Install runtime dependencies before starting the bot.")
            return 1
        raise

    try:
        client = build_client(settings)
    except RuntimeError as exc:
        if "discord.py is not installed" in str(exc):
            logger.error("Discord dependency is not installed. Install runtime dependencies before starting the bot.")
            return 1
        raise
    client.run(settings.discord_bot_token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
