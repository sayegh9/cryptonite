FROM python:3.12-slim

# Notifier (AI-first blueprint §4.6, A5.4): Telegram long polling + ntfy. Outbound HTTPS only; no port.
WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_SYSTEM_PYTHON=1

RUN pip install --no-cache-dir uv

COPY pyproject.toml README.md ./
COPY packages/schemas ./packages/schemas
COPY services/notifier ./services/notifier

RUN uv pip install --system -e ./packages/schemas -e ./services/notifier

RUN useradd -m -u 1000 -s /bin/bash cryptonite_notifier
USER cryptonite_notifier

CMD ["python", "-m", "cryptonite_notifier"]
