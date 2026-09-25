FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app/packages/agents:/app/packages/schemas:/app/services/agent_worker

RUN pip install uv

COPY packages/ ./packages/
COPY services/agent_worker/ ./services/agent_worker/

RUN uv pip install --system google-antigravity duckdb sqlalchemy psycopg2-binary asyncpg httpx fastapi uvicorn pydantic pyarrow redis numpy

EXPOSE 8085
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8085"]
