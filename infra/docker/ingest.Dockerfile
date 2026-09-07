FROM python:3.12-slim AS builder

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    build-essential \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Install uv
COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /uvx /bin/

# Copy workspace project definitions
COPY pyproject.toml uv.lock .python-version ./
COPY packages/ packages/
COPY services/ services/

# Sync workspace dependencies
RUN uv sync --frozen --no-dev || uv sync --no-dev

FROM python:3.12-slim AS runner

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user
RUN useradd -m -u 1001 cryptonite && \
    mkdir -p /app/data && \
    chmod -R 777 /app/data && \
    chown -R cryptonite:cryptonite /app

COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /uvx /bin/
COPY --from=builder /app /app

USER cryptonite

EXPOSE 8080

ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONUNBUFFERED=1

CMD ["python", "-m", "cryptonite_ingest.server"]
