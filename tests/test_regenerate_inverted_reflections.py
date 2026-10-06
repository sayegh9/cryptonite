"""B2 (F-C2): ``scripts/regenerate_inverted_reflections.py`` finds the reflections the inverted scoring
wrote, re-scores them and regenerates their lesson; a second run finds nothing (idempotent).

Selection runs on a throwaway database at Alembic head; the reviewer and the embedder are fakes, so no
model is called. The script is never run against the dev DB here.
"""

import asyncio
import importlib.util
import uuid
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import asyncpg
import pytest
from migrated_db import migrated_database

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def script():
    spec = importlib.util.spec_from_file_location("b2_regen", REPO / "scripts" / "regenerate_inverted_reflections.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _row(rec, ret, pnl, alpha, tag, inst="ETHUSDT.BINANCE"):
    return {"id": uuid.uuid4(), "run_id": uuid.uuid4(), "instrument_id": inst, "recommendation": rec, "reasoning": "r",
            "return_pct": Decimal(str(ret)), "mfe": Decimal("0.01"), "mae": Decimal("-0.01"),
            "actual_outcome_pnl": Decimal(str(pnl)), "alpha_vs_benchmark": Decimal(str(alpha)), "outcome": tag}


def test_the_live_overweight_reflection_is_inverted_and_its_alpha_is_negated(script):
    """Dev DB evidence (F-C2): Overweight +0.985% at 24h stored as LOSS with pnl -0.0099."""
    fix = script.classify(_row("Overweight", 0.00985, -0.00985, -0.0041, "LOSS"))
    assert fix.kind == "inverted" and fix.tag == "WIN"
    assert fix.outcome == pytest.approx(0.00985) and fix.alpha == pytest.approx(0.0041)


@pytest.mark.parametrize(("row", "kind"), [
    (("BUY", 0.02, 0.02, 0.01, "WIN"), None),                # consistent
    (("HOLD", 0.02, -0.02, -0.01, "LOSS"), None),            # N16: HOLD's outcome is -return, unchanged
    (("SELL", -0.01, 0.01, 0.003, "WIN"), None),
    (("Overweight", 0.0, 0.0, 0.0, "FLAT"), None),           # a zero move is the same either way
    (("BUY", 0.02, 0.02, 0.01, "LOSS"), "retag"),            # right outcome, wrong tag
    (("BUY", 0.02, 0.005, 0.01, "WIN"), "unexplained"),      # neither right nor negated: left alone
    (("MAYBE", 0.02, -0.02, 0.0, "LOSS"), "unexplained"),    # unknown verdict
])
def test_classify(script, row, kind):
    fix = script.classify(_row(*row))
    assert (fix.kind if fix else None) == kind


def test_an_inverted_benchmark_reflection_takes_its_outcome_as_alpha(script):
    fix = script.classify(_row("BUY", 0.03, -0.03, -0.03, "LOSS", inst="BTCUSDT.BINANCE"))
    assert fix.kind == "inverted" and fix.alpha == pytest.approx(0.03)


class _Out:
    def __init__(self, output):
        self.output = output


class FakeGateway:
    """Stands in for the gateway: records the pinned models, never calls one."""

    def __init__(self, embed_route="ollama:embeddinggemma"):
        self.embed_route = embed_route
        self.models = []
        self.embed_models = []

    async def route(self, role):
        assert role == "embed"
        return SimpleNamespace(primary_model=self.embed_route)

    async def run(self, role, prompt, *, model_override=None, **_):
        self.models.append(model_override)
        assert "Outcome for this decision: +0." in prompt  # the corrected (positive) outcome reaches the reviewer
        assert "alpha vs BTC: +0.41%" in prompt
        return _Out("Regenerated lesson: the bullish call was right.")

    async def embed_tagged(self, texts, kind="document", model_override=None):
        self.embed_models.append(model_override)
        return [[0.001] * 768 for _ in texts], model_override


async def _seed(url):
    conn = await asyncpg.connect(url)
    ids = {}
    try:
        for name, rec, level, ret, pnl, alpha, tag in [
            ("inverted", "BUY", "Overweight", "0.00985", "-0.00985", "-0.0041", "LOSS"),
            ("consistent", "BUY", "Buy", "0.02", "0.02", "0.01", "WIN"),
            ("hold", "HOLD", "Hold", "0.004", "-0.004", "-0.001", "LOSS"),
            ("unexplained", "SELL", "Sell", "0.01", "0.5", "0.1", "WIN"),
        ]:
            run = await conn.fetchval("INSERT INTO debate_runs (instrument_id, trigger_source, status) "
                                      "VALUES ('ETHUSDT.BINANCE', 'test', 'COMPLETED') RETURNING id")
            dec = await conn.fetchval(
                "INSERT INTO portfolio_decisions (run_id, instrument_id, recommendation, conviction_level, conviction_score, "
                "allocation_pct, reasoning) VALUES ($1, 'ETHUSDT.BINANCE', $2, $3, 0.7, 5, 'why') RETURNING id", run, rec, level)
            await conn.execute("INSERT INTO ai_decision_outcomes (decision_id, horizon, return_pct, mfe, mae, bars) "
                               "VALUES ($1, '24h', $2, 0.012, -0.004, 1440)", dec, Decimal(ret))
            ids[name] = await conn.fetchval(
                "INSERT INTO reflections (run_id, actual_outcome_pnl, alpha_vs_benchmark, lessons_learned, outcome, "
                "instrument_type, timeframe) VALUES ($1, $2, $3, 'old lesson', $4, 'ETHUSDT.BINANCE', '24h') RETURNING id",
                run, Decimal(pnl), Decimal(alpha), tag)
    finally:
        await conn.close()
    return ids


async def _reflections(url):
    conn = await asyncpg.connect(url)
    try:
        return {r["id"]: r for r in await conn.fetch("SELECT * FROM reflections")}
    finally:
        await conn.close()


def test_a_cloud_reviewer_model_is_refused_before_anything_is_read(script, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)  # it must not even need the database
    with pytest.raises(SystemExit, match="local"):
        asyncio.run(script.main(["--apply", "--model", "google:gemini-2.5-flash"]))


@pytest.mark.integration
def test_main_dry_run_then_apply_fixes_only_the_inverted_reflection_and_a_second_run_is_a_no_op(script, monkeypatch, capsys):
    with migrated_database("cryptonite_ci_b2") as url:
        monkeypatch.setenv("DATABASE_URL", url)
        ids = asyncio.run(_seed(url))
        gateways = []

        def make_gateway(_pool, embed_route="ollama:embeddinggemma"):
            gateways.append(FakeGateway(embed_route))
            return gateways[-1]

        assert asyncio.run(script.main([], make_gateway=make_gateway)) == 0  # dry run
        out = capsys.readouterr().out
        assert f"inverted    reflection {ids['inverted']}" in out and "1 to fix, 1 unexplained" in out
        assert "dry run: nothing written" in out
        assert gateways == [] and asyncio.run(_reflections(url))[ids["inverted"]]["lessons_learned"] == "old lesson"

        # the embed route resolves to a cloud model: --apply refuses and writes nothing
        with pytest.raises(SystemExit, match="embed route resolves to google:text-embedding-004"):
            asyncio.run(script.main(["--apply"], make_gateway=lambda pool: make_gateway(pool, "google:text-embedding-004")))
        assert gateways[-1].models == [] and asyncio.run(_reflections(url))[ids["inverted"]]["outcome"] == "LOSS"

        assert asyncio.run(script.main(["--apply"], make_gateway=make_gateway)) == 1
        assert gateways[-1].models == ["ollama:gemma4:12b-it-qat"]  # reviewer pinned to the local tier
        assert gateways[-1].embed_models == ["ollama:embeddinggemma"]  # embedder pinned to the local route model
        stored = asyncio.run(_reflections(url))
        fixed = stored[ids["inverted"]]
        assert (fixed["actual_outcome_pnl"], fixed["alpha_vs_benchmark"], fixed["outcome"]) == (
            Decimal("0.009850"), Decimal("0.004100"), "WIN")
        assert fixed["lessons_learned"].startswith("Regenerated") and fixed["embedding_model"] == "ollama:embeddinggemma"
        for untouched in ("consistent", "hold", "unexplained"):
            assert stored[ids[untouched]]["lessons_learned"] == "old lesson"

        n_gateways = len(gateways)
        assert asyncio.run(script.main(["--apply"], make_gateway=make_gateway)) == 0  # idempotent
        assert "0 to fix, 1 unexplained" in capsys.readouterr().out and len(gateways) == n_gateways


# --- learner robustness: one unmappable verdict cannot stall the reflection batch -----------------

class _LearnerGateway:
    async def run(self, role, prompt, **_):
        return _Out("lesson")

    async def embed_tagged(self, texts, kind="document", model_override=None):
        return [[0.001] * 768 for _ in texts], "ollama:embeddinggemma"


class _Pool:
    """A pool whose connection returns ``rows`` and records inserts."""

    def __init__(self, rows):
        self.rows, self.inserted = rows, []

    def acquire(self):
        pool = self

        class _Ctx:
            async def __aenter__(self):
                return pool

            async def __aexit__(self, *_):
                return False

        return _Ctx()

    async def fetch(self, *_):
        return self.rows

    async def execute(self, _sql, *args):
        self.inserted.append(args)


def _pending(rec, ret):
    return {"run_id": uuid.uuid4(), "instrument_id": "BTCUSDT.BINANCE", "recommendation": rec, "reasoning": "r",
            "created_at": None, "return_pct": Decimal(str(ret)), "mfe": Decimal("0.01"), "mae": Decimal("-0.01")}


def test_learner_logs_and_skips_an_unmappable_row_and_reflects_on_the_rest(script, caplog):
    import reflection

    bad, good = _pending("MAYBE", 0.01), _pending("Overweight", 0.01)
    pool = _Pool([bad, good])
    written = asyncio.run(reflection.ReflectionEngine(pool, None, _LearnerGateway()).process_pending_reflections())
    assert written == 1 and [a[0] for a in pool.inserted] == [good["run_id"]]
    assert pool.inserted[0][1] == pytest.approx(0.01) and pool.inserted[0][-1] == "WIN"  # Overweight is a BUY
    assert f"Reflection skipped for run {bad['run_id']}" in caplog.text


async def _seed_unmappable_then_good(url):
    conn = await asyncpg.connect(url)
    try:
        # rows from before migration 037's CHECK could carry anything; mimic them
        await conn.execute("ALTER TABLE portfolio_decisions DROP CONSTRAINT chk_portfolio_decisions_recommendation")
        good = None
        for i, rec in enumerate(["MAYBE"] * 6 + ["\tOverweight "]):
            run = await conn.fetchval("INSERT INTO debate_runs (instrument_id, trigger_source, status) "
                                      "VALUES ('BTCUSDT.BINANCE', 'test', 'COMPLETED') RETURNING id")
            dec = await conn.fetchval(
                "INSERT INTO portfolio_decisions (run_id, instrument_id, recommendation, conviction_score, allocation_pct, "
                "reasoning, created_at) VALUES ($1, 'BTCUSDT.BINANCE', $2, 0.7, 5, 'why', now() - make_interval(hours => $3)) "
                "RETURNING id", run, rec, 100 - i)  # the unmappable rows are the oldest
            await conn.execute("INSERT INTO ai_decision_outcomes (decision_id, horizon, return_pct, mfe, mae, bars) "
                               "VALUES ($1, '24h', 0.01, 0.012, -0.004, 1440)", dec)
            good = run
        return good
    finally:
        await conn.close()


async def _learn_once(url):
    import reflection

    pool = await asyncpg.create_pool(url, min_size=1, max_size=2)
    try:
        written = await reflection.ReflectionEngine(pool, None, _LearnerGateway()).process_pending_reflections()
        return written, await pool.fetch("SELECT run_id, outcome FROM reflections")
    finally:
        await pool.close()


@pytest.mark.integration
def test_unmappable_rows_do_not_hold_the_learner_batch(script):
    """More unmappable rows than a batch (5) ahead of a good one: the good one is still reflected on."""
    with migrated_database("cryptonite_ci_b2") as url:
        good = asyncio.run(_seed_unmappable_then_good(url))
        written, rows = asyncio.run(_learn_once(url))
        assert written == 1 and [(r["run_id"], r["outcome"]) for r in rows] == [(good, "WIN")]
