FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app/packages/agents:/app/packages/schemas:/app/services/debate_worker

RUN pip install uv

COPY packages/ ./packages/
COPY services/debate_worker/ ./services/debate_worker/

RUN uv pip install --system google-antigravity duckdb sqlalchemy psycopg2-binary asyncpg httpx fastapi uvicorn pydantic pyarrow redis

EXPOSE 8086
CMD ["python", "services/debate_worker/main.py"]
