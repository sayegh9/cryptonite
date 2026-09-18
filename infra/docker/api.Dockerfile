FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_SYSTEM_PYTHON=1

RUN pip install --no-cache-dir uv

COPY pyproject.toml README.md ./
COPY packages/schemas ./packages/schemas
COPY packages/storage ./packages/storage
COPY packages/scanner ./packages/scanner
COPY packages/strategies ./packages/strategies
COPY packages/backtester ./packages/backtester
COPY packages/execution ./packages/execution
COPY packages/agents ./packages/agents
COPY services/ingest ./services/ingest
COPY services/outbox ./services/outbox
COPY services/scanner ./services/scanner
COPY services/api ./services/api

RUN uv pip install --system \
    -e ./packages/schemas \
    -e ./packages/storage \
    -e ./packages/scanner \
    -e ./packages/strategies \
    -e ./packages/backtester \
    -e ./packages/execution \
    -e ./packages/agents \
    -e ./services/api

USER 1000:1000

CMD ["uvicorn", "cryptonite_api.main:app", "--host", "0.0.0.0", "--port", "8000"]
