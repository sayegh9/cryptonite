"""Local embeddings (blueprint §4.7, A6.4): the embed path tags every vector with its model, never falls
back to another embedding model, sends EmbeddingGemma its task prompts, and retrieval never compares
vectors from different models. No network: Ollama is an httpx mock transport."""

import importlib.util
import json
import sys
from pathlib import Path

import asyncpg
import httpx
import pytest
from cryptonite_agents.gateway import BudgetGuard, Gateway, ModelUnavailable
from cryptonite_agents.gateway.local_tier import LocalTierHealth
from cryptonite_agents.gateway.providers import pydantic_ai_backend as backend
from cryptonite_agents.gateway.providers.pydantic_ai_backend import override_model
from cryptonite_agents.gateway.routes import DEFAULT_ROUTES
from migrated_db import migrated_database

LOCAL = "ollama:embeddinggemma"


def test_embed_default_is_the_local_model_not_the_retired_one():
    # The live value is the embed row in ai_model_routes. This default is what the gateway uses when no row
    # loads (missing row, or a failed read), so it must never be the retired text-embedding-004.
    assert DEFAULT_ROUTES["embed"].primary_model == LOCAL


class FakeEmbedder:
    def __init__(self, dims: int = 768):
        self.dims, self.calls = dims, 0

    async def embed_texts(self, texts):
        self.calls += 1
        return [[0.1] * self.dims for _ in texts], 7


def health(up: bool) -> LocalTierHealth:
    def handler(request: httpx.Request) -> httpx.Response:
        if not up:
            raise httpx.ConnectError("refused", request=request)
        return httpx.Response(200, json={"models": [{"name": "embeddinggemma:latest"}]})
    return LocalTierHealth(base_url="http://ollama:11434/v1", transport=httpx.MockTransport(handler))


async def test_embed_tagged_reports_the_model_that_made_the_vectors():
    fake = FakeEmbedder()
    with override_model("test:emb", fake), override_model("test:other", FakeEmbedder()):
        gw = Gateway(budget=BudgetGuard())
        vectors, model = await gw.embed_tagged(["lesson"], model_override="test:emb")
    assert model == "test:emb" and len(vectors[0]) == 768 and fake.calls == 1


async def test_a_down_local_embed_model_fails_fast_and_never_switches_models():
    fake = FakeEmbedder()
    with override_model(LOCAL, fake):
        gw = Gateway(budget=BudgetGuard(), local_tier=health(up=False))
        with pytest.raises(ModelUnavailable):
            await gw.embed_tagged(["lesson"], model_override=LOCAL)
    assert fake.calls == 0


@pytest.mark.parametrize("kind, prefix", [("document", "title: none | text: "), ("query", "task: search result | query: ")])
async def test_embeddinggemma_gets_its_task_prompt(monkeypatch, kind, prefix):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(path=request.url.path, body=json.loads(request.content))
        return httpx.Response(200, json={"embeddings": [[0.5] * 768], "prompt_eval_count": 12})

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    vectors, tokens = await backend.embed(LOCAL, ["funding is extreme"], kind)
    assert seen["path"] == "/api/embed" and seen["body"]["model"] == "embeddinggemma"
    assert seen["body"]["input"] == [prefix + "funding is extreme"]
    assert len(vectors[0]) == 768 and tokens == 12


@pytest.fixture(scope="module")
def db_url():
    with migrated_database("cryptonite_embedtest") as url:
        yield url


class _QueryGateway:
    def __init__(self, model: str):
        self.model = model

    async def embed_tagged(self, texts, kind="document", **_):
        assert kind == "query"
        return [[1.0] + [0.0] * 767], self.model


def _vec(first: float) -> str:
    return "[" + ",".join([str(first), str(1 - first)] + ["0"] * 766) + "]"


@pytest.fixture(scope="module")
def recall_lessons():
    main = Path(__file__).resolve().parents[1] / "services" / "debate_worker" / "main.py"
    sys.path.insert(0, str(main.parent))  # main.py imports its sibling gate module
    spec = importlib.util.spec_from_file_location("debate_worker_main_embed", main)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.recall_lessons


async def test_retrieval_only_compares_vectors_of_the_query_model(db_url, recall_lessons):
    """A reflection embedded by another model must never be ranked against this query, even when its
    vector is the nearest one."""
    pool = await asyncpg.create_pool(db_url, min_size=1, max_size=2)
    try:
        async with pool.acquire() as conn:
            run = await conn.fetchval("INSERT INTO debate_runs (instrument_id, trigger_source) VALUES ('BTCUSDT.BINANCE', 'test') RETURNING id")
            rows = [("other model, nearest vector", 1.0, "google:text-embedding-004", 0.5),
                    ("same model, far vector, big outcome", 0.0, LOCAL, -0.04),
                    ("same model, near vector, small outcome", 0.9, LOCAL, 0.01)]
            for lesson, first, model, alpha in rows:
                await conn.execute(
                    "INSERT INTO reflections (run_id, actual_outcome_pnl, alpha_vs_benchmark, lessons_learned, embedding, embedding_model) "
                    "VALUES ($1, 0, $5, $2, $3, $4)", run, lesson, _vec(first), model, alpha)
        text = await recall_lessons(pool, _QueryGateway(LOCAL), "brief")
        # other-model vectors never appear; among the same model's nearest, the bigger outcome leads
        assert text.split("\n- ") == ["same model, far vector, big outcome", "same model, near vector, small outcome"]
        # a model with no stored vectors yet falls back to the most recent lessons, never to other spaces
        recent = await recall_lessons(pool, _QueryGateway("ollama:brand-new"), "brief")
        assert recent.split("\n- ")[0] == "same model, near vector, small outcome"
    finally:
        await pool.close()
