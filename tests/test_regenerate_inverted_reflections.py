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
    """Stands in for the gateway: records the pinned model, never calls one."""

    def __init__(self):
        self.models = []

    async def run(self, role, prompt, *, model_override=None, **_):
        self.models.append(model_override)
        assert "Outcome for this decision: +0." in prompt  # the corrected (positive) outcome reaches the reviewer
        assert "alpha vs BTC: +0.41%" in prompt
        return _Out("Regenerated lesson: the bullish call was right.")

    async def embed_tagged(self, texts, kind="document"):
        return [[0.001] * 768 for _ in texts], "ollama:embeddinggemma"


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


async def _run(script, url, apply: bool):
    conn = await asyncpg.connect(url)
    try:
        fixes, rows = await script.find_fixes(conn)
        gateway = FakeGateway()
        written = [await script.apply_fix(conn, gateway, f, rows[f.reflection_id], "ollama:gemma4:12b-it-qat")
                   for f in fixes if apply and f.kind != "unexplained"]
        stored = {r["id"]: r for r in await conn.fetch("SELECT * FROM reflections")}
        return fixes, written, gateway.models, stored
    finally:
        await conn.close()


@pytest.mark.integration
def test_selects_only_the_inverted_reflection_regenerates_it_and_a_second_run_is_a_no_op(script):
    with migrated_database("cryptonite_ci_b2") as url:
        ids = asyncio.run(_seed(url))

        fixes, written, _, stored = asyncio.run(_run(script, url, apply=False))  # dry run writes nothing
        assert {(f.reflection_id, f.kind) for f in fixes} == {(ids["inverted"], "inverted"),
                                                             (ids["unexplained"], "unexplained")}
        assert stored[ids["inverted"]]["lessons_learned"] == "old lesson" and written == []

        _, written, models, stored = asyncio.run(_run(script, url, apply=True))
        assert written == [True] and models == ["ollama:gemma4:12b-it-qat"]  # pinned to the local tier
        fixed = stored[ids["inverted"]]
        assert (fixed["actual_outcome_pnl"], fixed["alpha_vs_benchmark"], fixed["outcome"]) == (
            Decimal("0.009850"), Decimal("0.004100"), "WIN")
        assert fixed["lessons_learned"].startswith("Regenerated") and fixed["embedding_model"] == "ollama:embeddinggemma"
        for untouched in ("consistent", "hold", "unexplained"):
            assert stored[ids[untouched]]["lessons_learned"] == "old lesson"

        fixes, written, models, _ = asyncio.run(_run(script, url, apply=True))  # idempotent
        assert [f.kind for f in fixes] == ["unexplained"] and written == [] and models == []
