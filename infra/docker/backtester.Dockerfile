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
COPY services/backtester ./services/backtester

RUN uv pip install --system \
    -e ./packages/schemas \
    -e ./packages/storage \
    -e ./packages/strategies \
    -e ./packages/backtester \
    -e ./services/backtester

USER 1000:1000

CMD ["python", "-m", "cryptonite_backtester_service.server"]
