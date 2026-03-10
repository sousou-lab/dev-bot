# Deployment Guide

## Prerequisites

- Docker and Docker Compose
- `.env` file with all required environment variables (see `.env.example`)
- GitHub App configured and private key available

## Quick Start

```bash
# Copy and fill in environment variables
cp .env.example .env
# Edit .env with your values

# Build and start
docker compose up -d

# Check logs
docker compose logs -f

# Check health
curl http://localhost:8080/health
```

## Environment Variables

See `.env.example` for the full list. Key variables:

| Variable | Required | Description |
|----------|----------|-------------|
| `DISCORD_BOT_TOKEN` | Yes | Discord Bot token |
| `DISCORD_GUILD_ID` | Yes | Target server ID |
| `DISCORD_STATUS_CHANNEL_ID` | Yes | Status channel ID |
| `GITHUB_APP_ID` | Yes | GitHub App ID |
| `GITHUB_APP_PRIVATE_KEY_PATH` | Yes | Path to private key (mount into container) |
| `GITHUB_APP_INSTALLATION_ID` | Yes | GitHub App Installation ID |
| `ANTHROPIC_API_KEY` | Yes | For planning lane |
| `HEALTH_CHECK_PORT` | No | Health check port (default: 8080) |
| `LOG_FORMAT` | No | `json` or `text` (default: `json` in Docker) |
| `LOG_LEVEL` | No | `DEBUG`, `INFO`, `WARNING`, `ERROR` (default: `INFO`) |

## Mounting the GitHub App Private Key

```yaml
# docker-compose.override.yml
services:
  dev-bot:
    volumes:
      - ./path/to/private-key.pem:/secrets/github-app.pem:ro
    environment:
      - GITHUB_APP_PRIVATE_KEY_PATH=/secrets/github-app.pem
```

## Persistent Data

| Path | Description |
|------|-------------|
| `/var/lib/dev-bot/runs` | Run artifacts, logs, state |
| `/var/lib/dev-bot/workspaces` | Git bare mirrors and worktrees |

Both are mounted as Docker volumes by default.

## Monitoring

```bash
# Health check
curl -s http://localhost:8080/health | jq .

# Response example:
# {
#   "status": "healthy",
#   "uptime_seconds": 3600.5,
#   "checks": {
#     "orchestrator": { "pending": 0, "active": 1 }
#   }
# }
```

## Stopping

```bash
# Graceful stop (sends SIGTERM, waits 30s)
docker compose down

# Force stop
docker compose down --timeout 0
```

## Updating

```bash
docker compose pull  # or rebuild
docker compose up -d --build
```
