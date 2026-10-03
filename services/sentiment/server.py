"""Sentiment service (AI-first blueprint §4.7, A6.3): CryptoBERT for crypto social posts and FinBERT for
financial news, on CPU.

Models load once from a read-only local directory (``MODELS_DIR``, default ``/models``) with no
network access and no secrets. Labels are normalised to ``bullish`` / ``neutral`` / ``bearish``.

    GET  /health                       -> {"ok": true, "models": {"social": "...", "news": "..."}}
    POST /classify {"texts": [...], "source": "social" | "news"}
                                       -> {"model": "...", "results": [{"label", "score", "scores"}]}
"""

import logging
import os
import threading
from pathlib import Path
from typing import Literal

import torch
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from transformers import AutoModelForSequenceClassification, AutoTokenizer

logger = logging.getLogger("cryptonite.sentiment")
logging.basicConfig(level=logging.INFO)

MODELS_DIR = Path(os.getenv("MODELS_DIR", "/models"))
MODELS = {"social": "ElKulako/cryptobert", "news": "ProsusAI/finbert"}
NORMALISE = {"bullish": "bullish", "positive": "bullish", "bearish": "bearish", "negative": "bearish",
             "neutral": "neutral"}
MAX_TEXTS, MAX_CHARS, MAX_TOKENS = 64, 2000, 256

torch.set_num_threads(int(os.getenv("SENTIMENT_THREADS", "8")))


class Classifier:
    def __init__(self, repo: str) -> None:
        path = MODELS_DIR / repo
        self.name = repo
        self.tokenizer = AutoTokenizer.from_pretrained(path, local_files_only=True)
        # FinBERT ships only a pickle .bin; torch>=2.6 loads it with weights_only=True (no code execution)
        self.model = AutoModelForSequenceClassification.from_pretrained(path, local_files_only=True).eval()
        self.labels = [NORMALISE[self.model.config.id2label[i].lower()] for i in range(self.model.config.num_labels)]
        self._lock = threading.Lock()  # one forward pass at a time per model

    def classify(self, texts: list[str]) -> list[dict]:
        batch = self.tokenizer(texts, padding=True, truncation=True, max_length=MAX_TOKENS, return_tensors="pt")
        with self._lock, torch.inference_mode():
            probs = torch.softmax(self.model(**batch).logits, dim=-1).tolist()
        out = []
        for row in probs:
            scores = {label: round(p, 4) for label, p in zip(self.labels, row, strict=True)}
            best = max(scores, key=scores.get)
            out.append({"label": best, "score": scores[best], "scores": scores})
        return out


class ClassifyIn(BaseModel):
    texts: list[str] = Field(min_length=1, max_length=MAX_TEXTS)
    source: Literal["social", "news"] = "social"


app = FastAPI(title="cryptonite-sentiment")
CLASSIFIERS: dict[str, Classifier] = {}


@app.on_event("startup")
def load() -> None:
    for source, repo in MODELS.items():
        try:
            CLASSIFIERS[source] = Classifier(repo)
            logger.info("Loaded %s for %s", repo, source)
        except Exception as e:  # report unhealthy rather than crash-loop; the gateway falls back to the LLM
            logger.error("Could not load %s: %s", repo, e)


@app.get("/health")
def health() -> dict:
    return {"ok": len(CLASSIFIERS) == len(MODELS), "models": {s: c.name for s, c in CLASSIFIERS.items()}}


@app.post("/classify")
def classify(req: ClassifyIn) -> dict:
    if any(len(t) > MAX_CHARS for t in req.texts):
        raise HTTPException(status_code=422, detail=f"each text must be at most {MAX_CHARS} characters")
    clf = CLASSIFIERS.get(req.source)
    if clf is None:
        raise HTTPException(status_code=503, detail=f"{req.source} model not loaded")
    return {"model": clf.name, "results": clf.classify(req.texts)}
