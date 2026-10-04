FROM python:3.12-slim

WORKDIR /app

# LE-7: every Python service installs the uv.lock environment the tests run against (no unpinned
# installs), from a BuildKit cache shared by all images so rebuilds do not re-download.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_LOCK_TIMEOUT=3600 \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:$PATH"

RUN --mount=type=cache,target=/root/.cache/pip pip install uv==0.12.17

COPY pyproject.toml uv.lock README.md ./
COPY packages ./packages
COPY services ./services

RUN --mount=type=cache,target=/root/.cache/uv,id=cryptonite-uv,sharing=locked \
    uv sync --frozen --no-dev --all-packages

RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/* \
    && useradd -m -u 1001 cryptonite && mkdir -p /app/data && chmod -R 777 /app/data && chown -R cryptonite:cryptonite /app
USER cryptonite

EXPOSE 8080
CMD ["python", "-m", "cryptonite_ingest.server"]
