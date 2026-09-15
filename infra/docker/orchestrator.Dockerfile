FROM python:3.12-slim

# Install uv
COPY --from=ghcr.io/astral-sh/uv:0.4.5 /uv /bin/uv

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    UV_HTTP_TIMEOUT=300 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_SYSTEM_PYTHON=1

WORKDIR /app

# Copy dependency configuration
COPY pyproject.toml README.md ./
COPY packages/schemas ./packages/schemas
COPY packages/storage ./packages/storage
COPY packages/execution ./packages/execution
COPY packages/strategies ./packages/strategies
COPY services/orchestrator ./services/orchestrator

# Install dependencies (workspaces)
RUN uv pip install --system \
    -e ./packages/schemas \
    -e ./packages/storage \
    -e ./packages/execution \
    -e ./packages/strategies \
    -e ./services/orchestrator

# Ensure it runs as unprivileged user
RUN useradd -m -s /bin/bash cryptonite_orchestrator
USER cryptonite_orchestrator

CMD ["python", "-m", "cryptonite_orchestrator.server"]
