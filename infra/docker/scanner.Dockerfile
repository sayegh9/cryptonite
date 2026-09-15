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
COPY services/ingest ./services/ingest
COPY services/outbox ./services/outbox
COPY services/scanner ./services/scanner
COPY services/api ./services/api

RUN uv pip install --system -e ./packages/schemas -e ./packages/storage -e ./packages/scanner -e ./services/scanner

USER 1000:1000

CMD ["python", "-m", "cryptonite_scanner_service.server"]
