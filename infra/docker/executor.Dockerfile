FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_SYSTEM_PYTHON=1

RUN pip install --no-cache-dir uv

COPY pyproject.toml README.md ./
COPY packages/schemas ./packages/schemas
COPY packages/storage ./packages/storage
COPY packages/strategies ./packages/strategies
COPY packages/execution ./packages/execution
COPY services/executor ./services/executor

RUN uv pip install --system \
    -e ./packages/schemas \
    -e ./packages/storage \
    -e ./packages/strategies \
    -e ./packages/execution \
    -e ./services/executor

USER 1000:1000

CMD ["python", "-m", "cryptonite_executor.server"]
