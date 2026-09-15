FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app/packages/agents:/app/services/agent_worker

RUN pip install uv

COPY packages/ ./packages/
COPY services/agent_worker/ ./services/agent_worker/

RUN uv pip install --system google-antigravity duckdb sqlalchemy psycopg2-binary fastapi uvicorn pydantic

EXPOSE 8085
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8085"]
