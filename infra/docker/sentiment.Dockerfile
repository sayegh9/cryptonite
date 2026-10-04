FROM python:3.12-slim

# Sentiment service (AI-first blueprint §4.7, A6.3): CryptoBERT + FinBERT on CPU. Models are mounted
# read-only at /models; the service never downloads anything and receives no secrets.
WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1

COPY services/sentiment/requirements.txt ./requirements.txt
RUN --mount=type=cache,target=/root/.cache/pip pip install -r requirements.txt

COPY services/sentiment/server.py ./server.py

RUN useradd -m -u 1000 -s /bin/bash cryptonite_sentiment
USER cryptonite_sentiment

EXPOSE 8090
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8090"]
