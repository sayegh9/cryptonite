FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app/packages/agents:/app/packages/schemas:/app/services/debate_worker

RUN pip install uv

COPY packages/ ./packages/
COPY services/debate_worker/ ./services/debate_worker/

RUN uv pip install --system "pydantic-ai-slim[google,openai]>=2.50.0" "pydantic>=2.12" duckdb sqlalchemy psycopg2-binary asyncpg httpx fastapi uvicorn pyarrow redis

EXPOSE 8086
CMD ["python", "services/debate_worker/main.py"]
