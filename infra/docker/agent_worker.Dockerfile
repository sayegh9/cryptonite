FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app/packages/agents:/app/packages/schemas:/app/services/agent_worker

RUN pip install uv

COPY packages/ ./packages/
COPY services/agent_worker/ ./services/agent_worker/

RUN uv pip install --system "pydantic-ai-slim[google,openai]>=2.50.0" "pydantic>=2.12" "optuna>=5.0.0" duckdb sqlalchemy psycopg2-binary asyncpg httpx fastapi uvicorn pyarrow redis numpy

EXPOSE 8085
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8085"]
