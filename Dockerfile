FROM python:3.14-slim AS base

# Install Node.js (required for claude-agent-sdk) and git
RUN apt-get update && \
    apt-get install -y --no-install-recommends nodejs npm git && \
    rm -rf /var/lib/apt/lists/*

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Install Python dependencies
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# Install Node dependencies
COPY package.json package-lock.json* ./
RUN npm ci --omit=dev 2>/dev/null || npm install --omit=dev

# Copy application code
COPY app/ app/
COPY scripts/ scripts/
COPY WORKFLOW.md AGENTS.md ./
COPY .claude/ .claude/
COPY .agents/ .agents/

# Create directories for persistent data
RUN mkdir -p /var/lib/dev-bot/workspaces /var/lib/dev-bot/runs

ENV STATE_DIR=/var/lib/dev-bot/runs
ENV WORKSPACE_ROOT=/var/lib/dev-bot/workspaces
ENV LOG_FORMAT=json
ENV PYTHONUNBUFFERED=1

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')" || exit 1

ENTRYPOINT ["uv", "run", "python", "-m", "app.main"]
