"""B2 (F-C2, F-M6): one verdict vocabulary. The CRO's ``conviction_level`` (Buy | Overweight | Hold |
Underweight | Sell, any case) and the routing words STRONG_BUY / LONG are BUY-, SELL- or HOLD-like.
Scoring (``score_decision``), the regime tables (``outcomes_by_regime``), the reflection outcome
(``signed_outcome``) and the similar-decisions panel must grade them by that direction, and routing,
shadow scoring and the approve endpoint must agree with ``normalize_verdict()``.

HOLD keeps its continuous semantics (remediation N16): signed outcome = -return, scored WIN when the
asset did not rise beyond the band. Migration 037 runs on a throwaway database at Alembic head.

Test-first: written before the fix; ``normalize_verdict`` is imported inside the tests so its absence
fails those tests and not the module's collection.
"""

import asyncio
import importlib.util
import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import asyncpg
import pytest
from cryptonite_api import shadow, similar
from cryptonite_api.routes import agents
from cryptonite_execution.calibration import outcomes_by_regime, score_decision
from migrated_db import migrated_database

REPO = Path(__file__).resolve().parents[1]
WORKER = REPO / "services" / "debate_worker"
sys.path.insert(0, str(WORKER))
import outcomes  # noqa: E402

# Every CRO conviction literal in title, upper and lower case, plus the routing-only BUY words.
DIRECTION = {
    **{v: "BUY" for v in ("Buy", "BUY", "buy", "Overweight", "OVERWEIGHT", "overweight",
                          "STRONG_BUY", "strong_buy", "LONG", "long")},
    **{v: "SELL" for v in ("Sell", "SELL", "sell", "Underweight", "UNDERWEIGHT", "underweight")},
    **{v: "HOLD" for v in ("Hold", "HOLD", "hold")},
}
VERDICTS = sorted(DIRECTION.items())
ORDER_SIDE = {"BUY": "BUY", "SELL": "SELL", "HOLD": None}


def _normalize_verdict():
    try:
        from cryptonite_schemas.debate import normalize_verdict
    except ImportError as e:
        pytest.fail(f"B2: cryptonite_schemas.debate.normalize_verdict() does not exist yet ({e})")
    return normalize_verdict


@pytest.fixture(scope="module")
def routing():
    spec = importlib.util.spec_from_file_location("b2_mandate_routing", WORKER / "mandate_routing.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def approve_side(recommendation: str) -> str | None:
    """The order side ``POST /debates/{run_id}/approve`` derives (``routes/agents.py`` approve_debate,
    through ``order_side``)."""
    return agents.order_side(recommendation)


# --- scoring (calibration.score_decision, F-C2) --------------------------------------------------

# (asset return after 24h, expected score per direction); the neutral band is 0.2%
SCORE_TABLE = [
    (0.01, {"BUY": "WIN", "SELL": "LOSS", "HOLD": "LOSS"}),     # rise: HOLD missed it
    (-0.01, {"BUY": "LOSS", "SELL": "WIN", "HOLD": "WIN"}),     # fall: HOLD avoided it
    (0.001, {"BUY": "NEUTRAL", "SELL": "NEUTRAL", "HOLD": "WIN"}),  # inside the band
]


@pytest.mark.parametrize(("ret", "by_direction"), SCORE_TABLE, ids=["up1pct", "down1pct", "inside_band"])
@pytest.mark.parametrize(("verdict", "direction"), VERDICTS)
def test_fc2_score_decision_grades_a_verdict_by_its_direction(verdict, direction, ret, by_direction):
    assert score_decision(verdict, ret) == by_direction[direction]


@pytest.mark.parametrize("ret", [0.00985, 0.00823])
def test_fc2_the_two_live_overweight_decisions_are_wins_not_losses(ret):
    """Dev DB evidence (F-C2): two Overweight decisions returned +0.985% / +0.823% at 24h and were
    reflected as LOSS with pnl -0.0099 / -0.0082."""
    assert score_decision("Overweight", ret) == "WIN"
    assert outcomes.signed_outcome("Overweight", ret) == pytest.approx(ret)


# --- reflection outcome (outcomes.signed_outcome) and the similar-decisions panel ----------------

@pytest.mark.parametrize("ret", [0.01, -0.02])
@pytest.mark.parametrize(("verdict", "direction"), VERDICTS)
def test_fc2_signed_outcome_follows_the_verdict_direction(verdict, direction, ret):
    expected = ret if direction == "BUY" else -ret  # SELL and HOLD (N16: continuous, unchanged) gain on a fall
    assert outcomes.signed_outcome(verdict, ret) == pytest.approx(expected)


@pytest.mark.parametrize("ret", [0.01, -0.02])
@pytest.mark.parametrize(("verdict", "direction"), VERDICTS)
def test_fc2_similar_decisions_sign_follows_the_verdict_direction(verdict, direction, ret):
    """``cryptonite_api.similar.signed`` duplicates ``signed_outcome`` and shows on the approval card."""
    expected = ret if direction == "BUY" else -ret
    assert similar.signed(verdict, ret) == pytest.approx(expected)


# --- regime tables (calibration.outcomes_by_regime, F1) ------------------------------------------

REGIME_EXPECTED = {  # rows: +1%, -2%, +0.1% in TREND_UP
    "BUY": {"n": 3, "wins": 1, "losses": 1, "neutral": 1, "mean_signed_return": -0.003},
    "SELL": {"n": 3, "wins": 1, "losses": 1, "neutral": 1, "mean_signed_return": 0.003},
    "HOLD": {"n": 3, "wins": 2, "losses": 1, "neutral": 0, "mean_signed_return": 0.003},
}


@pytest.mark.parametrize(("verdict", "direction"), VERDICTS)
def test_fc2_outcomes_by_regime_counts_and_signs_by_direction(verdict, direction):
    rows = [("TREND_UP", verdict, 0.01), ("TREND_UP", verdict, -0.02), ("TREND_UP", verdict, 0.001)]
    (got,) = outcomes_by_regime(rows)
    want = REGIME_EXPECTED[direction]
    assert got["regime"] == "TREND_UP"
    assert {k: got[k] for k in ("n", "wins", "losses", "neutral")} == {k: want[k] for k in ("n", "wins", "losses", "neutral")}
    assert got["mean_signed_return"] == pytest.approx(want["mean_signed_return"])


def test_fm6_mixed_vocabulary_in_one_regime_aggregates_as_one_direction():
    """The dev DB mixes BUY-like spellings; they must pool as BUY decisions."""
    (got,) = outcomes_by_regime([("CHOP", "Buy", 0.01), ("CHOP", "Overweight", 0.01), ("CHOP", "BUY", 0.01)])
    assert (got["wins"], got["losses"]) == (3, 0)
    assert got["mean_signed_return"] == pytest.approx(0.01)


# --- normalize_verdict() contract (packages/schemas, F-M6) ----------------------------------------

@pytest.mark.parametrize(("verdict", "direction"), VERDICTS)
def test_fm6_normalize_verdict_maps_every_literal_to_buy_sell_or_hold(verdict, direction):
    normalize_verdict = _normalize_verdict()
    assert normalize_verdict(verdict) == direction
    assert normalize_verdict(normalize_verdict(verdict)) == direction  # idempotent on its own output


def test_fm6_normalize_verdict_covers_the_cro_schema_literals():
    """Every value ``RiskDecision.conviction_level`` can take has a mapping (no silent schema drift)."""
    from typing import get_args

    from cryptonite_schemas.debate import RiskDecision

    normalize_verdict = _normalize_verdict()
    literals = get_args(RiskDecision.model_fields["conviction_level"].annotation)
    assert set(literals) == {"Buy", "Overweight", "Hold", "Underweight", "Sell"}
    assert {lit: normalize_verdict(lit) for lit in literals} == {
        "Buy": "BUY", "Overweight": "BUY", "Hold": "HOLD", "Underweight": "SELL", "Sell": "SELL"}


@pytest.mark.parametrize("unknown", ["", "MAYBE", "BUYSELL", "Overweighted", "YOLO"])
def test_fm6_normalize_verdict_raises_on_unknown_values(unknown):
    normalize_verdict = _normalize_verdict()
    with pytest.raises(ValueError):
        normalize_verdict(unknown)


# --- routing, shadow scoring and the approve endpoint agree (plan item B2.6) ----------------------

@pytest.mark.parametrize(("verdict", "direction"), VERDICTS)
def test_fm6_debate_routing_side_matches_the_verdict_direction(routing, verdict, direction):
    assert routing.side_of(verdict) == ORDER_SIDE[direction]


@pytest.mark.parametrize(("verdict", "direction"), VERDICTS)
def test_fm6_shadow_scoring_direction_matches_the_verdict_direction(verdict, direction):
    assert shadow.direction(verdict) == direction


@pytest.mark.parametrize(("verdict", "direction"), VERDICTS)
def test_fm6_approve_endpoint_side_matches_the_verdict_direction(verdict, direction):
    assert approve_side(verdict) == ORDER_SIDE[direction]


@pytest.mark.parametrize("verdict", sorted(DIRECTION))
def test_fm6_every_consumer_agrees_with_normalize_verdict(routing, verdict):
    normalized = _normalize_verdict()(verdict)
    assert routing.side_of(verdict) == ORDER_SIDE[normalized]
    assert shadow.direction(verdict) == normalized
    assert approve_side(verdict) == ORDER_SIDE[normalized]
    assert score_decision(verdict, 0.01) == score_decision(normalized, 0.01)
    assert outcomes.signed_outcome(verdict, 0.01) == outcomes.signed_outcome(normalized, 0.01)


# --- migration 037 (throwaway database at Alembic head) -------------------------------------------

# Legacy spellings in the dev DB (HOLD 53, Hold 2, Buy 3, Overweight 4 on 2026-10-05) plus the other
# literals and cases the CRO and routing allow.
LEGACY_ROWS = ["HOLD", "Hold", "hold", "Buy", "BUY", "buy", "Overweight", "OVERWEIGHT", "Underweight",
               "Sell", "SELL", "STRONG_BUY", "LONG",
               # padded with the characters normalize_verdict() trims: the backfill must trim the same set
               " Overweight ", "\tHold\n", "Sell\r\n", "\n\tbuy \t"]
TRIMMED = " \t\r\n"  # cryptonite_schemas.debate.VERDICT_WHITESPACE


def _alembic(url: str, *args: str) -> None:
    res = subprocess.run([sys.executable, "-m", "alembic", "-c", "infra/migrations/alembic.ini", *args],
                         cwd=REPO, env={**os.environ, "DATABASE_URL": url}, capture_output=True, text=True)
    assert res.returncode == 0, f"alembic {' '.join(args)} failed: {res.stderr.strip().splitlines()[-1:]}"


async def _seed_legacy(url: str) -> None:
    conn = await asyncpg.connect(url)
    try:
        for original in LEGACY_ROWS:
            run = await conn.fetchval("INSERT INTO debate_runs (instrument_id, trigger_source, status) "
                                      "VALUES ('ETHUSDT.BINANCE', 'test', 'COMPLETED') RETURNING id")
            # ``reasoning`` carries the original spelling so each row can be checked after the backfill
            await conn.execute(
                "INSERT INTO portfolio_decisions (run_id, instrument_id, recommendation, conviction_score, allocation_pct, reasoning) "
                "VALUES ($1, 'ETHUSDT.BINANCE', $2, 0.7, 5, $3)", run, original, original)
    finally:
        await conn.close()


async def _decisions(url: str) -> list[asyncpg.Record]:
    conn = await asyncpg.connect(url)
    try:
        return await conn.fetch("SELECT reasoning AS original, recommendation, conviction_level FROM portfolio_decisions")
    finally:
        await conn.close()


async def _insert(url: str, recommendation: str, conviction_level: str | None) -> None:
    conn = await asyncpg.connect(url)
    try:
        run = await conn.fetchval("INSERT INTO debate_runs (instrument_id, trigger_source, status) "
                                  "VALUES ('ETHUSDT.BINANCE', 'test', 'COMPLETED') RETURNING id")
        await conn.execute(
            "INSERT INTO portfolio_decisions (run_id, instrument_id, recommendation, conviction_level, conviction_score, allocation_pct) "
            "VALUES ($1, 'ETHUSDT.BINANCE', $2, $3, 0.7, 5)", run, recommendation, conviction_level)
    finally:
        await conn.close()


async def _has_conviction_level(url: str) -> bool:
    conn = await asyncpg.connect(url)
    try:
        return bool(await conn.fetchval("""SELECT 1 FROM information_schema.columns
                                           WHERE table_name = 'portfolio_decisions' AND column_name = 'conviction_level'"""))
    finally:
        await conn.close()


@pytest.mark.integration
def test_fm6_migration_037_backfills_verdicts_keeps_the_wording_and_constrains_new_rows():
    with migrated_database("cryptonite_ci_b2") as url:
        _alembic(url, "downgrade", "036")
        asyncio.run(_seed_legacy(url))
        _alembic(url, "upgrade", "037")

        rows = asyncio.run(_decisions(url))
        assert len(rows) == len(LEGACY_ROWS)
        for r in rows:
            assert r["recommendation"] == DIRECTION[r["original"].strip(TRIMMED)], repr(r["original"])
            assert r["conviction_level"] == r["original"]  # the CRO's own wording survives for sizing
        assert {r["recommendation"] for r in rows} <= {"BUY", "SELL", "HOLD"}

        asyncio.run(_insert(url, "BUY", "Overweight"))  # the post-fix write shape is accepted
        with pytest.raises(asyncpg.CheckViolationError):
            asyncio.run(_insert(url, "Overweight", "Overweight"))

        _alembic(url, "downgrade", "036")
        assert not asyncio.run(_has_conviction_level(url))
        _alembic(url, "upgrade", "head")
        assert asyncio.run(_has_conviction_level(url))


# --- one mapping: the migration, the extra routing forms, the CRO shadow, the similar filter -----

def _migration_037():
    spec = importlib.util.spec_from_file_location(
        "b2_migration_037", REPO / "infra" / "migrations" / "versions" / "037_verdict_normalization.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_fm6_migration_037_and_normalize_verdict_use_the_same_mapping_and_trim_set():
    from cryptonite_schemas.debate import VERDICT_DIRECTIONS, VERDICT_WHITESPACE

    normalize_verdict = _normalize_verdict()
    migration = _migration_037()
    assert migration.VERDICTS == VERDICT_DIRECTIONS
    assert migration.WHITESPACE == VERDICT_WHITESPACE == TRIMMED
    assert "btrim(recommendation, E' \\t\\r\\n')" in migration._NORMALIZED  # the SQL trims that same set
    for spelling, direction in VERDICT_DIRECTIONS.items():
        assert normalize_verdict(spelling) == direction
        for pad in (" ", "\t", "\r\n", " \t\n\r"):
            assert normalize_verdict(f"{pad}{spelling.lower()}{pad}") == direction
    # what neither side trims is not a verdict (the migration would abort on it)
    for untrimmed in ("\u00a0BUY", "BUY\v", "B UY"):
        with pytest.raises(ValueError):
            normalize_verdict(untrimmed)


@pytest.mark.parametrize(("verdict", "side"), [("strong buy", "BUY"), ("Strong Sell", "SELL"), (" Overweight ", "BUY"),
                                               ("short", "SELL"), ("", None), (None, None), ("MAYBE", None)])
def test_fm6_routing_keeps_its_extra_forms_and_has_no_side_for_unknown_verdicts(routing, verdict, side):
    assert routing.side_of(verdict) == side


@pytest.mark.parametrize("unknown", [None, 7, "REDUCE"])
def test_fm6_normalize_verdict_raises_on_non_verdicts_and_verdict_direction_returns_none(unknown):
    from cryptonite_schemas.debate import verdict_direction

    with pytest.raises(ValueError):
        _normalize_verdict()(unknown)
    assert verdict_direction(unknown) is None


@pytest.mark.parametrize("verdict", ["SMALL_BUY", "Overweighted"])
def test_fc2_scoring_refuses_an_unknown_verdict_instead_of_grading_it_as_a_short(verdict):
    with pytest.raises(ValueError):
        score_decision(verdict, 0.01)
    with pytest.raises(ValueError):
        outcomes.signed_outcome(verdict, 0.01)


def test_fm6_shadow_scoring_keeps_exit_verdicts_as_a_separate_exit_mapping():
    assert shadow.direction("REDUCE") == "SELL" and shadow.direction(" close ") == "SELL"
    assert shadow.direction("MAYBE") is None and shadow.direction(None) is None


@pytest.mark.parametrize(("primary", "candidate", "agrees"), [
    ("Buy", "Overweight", True), ("Overweight", "BUY", True), ("Underweight", "Sell", True),
    ("Hold", "hold", True), ("Buy", "Hold", False), ("Overweight", "Underweight", False)])
def test_fm6_cro_shadow_agreement_compares_directions(primary, candidate, agrees):
    from cryptonite_agents.gateway import shadow as gateway_shadow

    assert gateway_shadow.agreement("debate.cro", {"conviction_level": primary}, {"conviction_level": candidate}) == (
        "conviction_level", agrees)


def test_fm6_exit_shadow_agreement_still_compares_the_wording():
    from cryptonite_agents.gateway import shadow as gateway_shadow

    assert gateway_shadow.agreement("exit.verdict", {"recommendation": "REDUCE"}, {"recommendation": "CLOSE"}) == (
        "recommendation", False)


class _FetchConn:
    def __init__(self):
        self.args = None

    async def fetch(self, _sql, *args):
        self.args = args
        return []


async def _similar_filter(recommendation):
    conn = _FetchConn()
    await similar.similar_decisions(conn, "BTCUSDT.BINANCE", recommendation)
    return conn.args[2] if conn.args else "no query"


@pytest.mark.parametrize(("verdict", "direction"), [("Overweight", "BUY"), ("buy", "BUY"), ("Underweight", "SELL"),
                                                    ("HOLD", "HOLD")])
def test_fc2_similar_decisions_filter_matches_every_spelling_of_the_direction(verdict, direction):
    """Rows written before migration 037 (Overweight, Buy) match a BUY approval, and vice versa."""
    from cryptonite_schemas.debate import VERDICT_DIRECTIONS

    spellings = asyncio.run(_similar_filter(verdict))
    assert set(spellings) == {k for k, v in VERDICT_DIRECTIONS.items() if v == direction}


async def _orchestrate_sideless_order(recommendation: str):
    """One MANUAL_ORDER without ``side`` through the orchestrator's command poller (mocked DB and
    engine, as in tests/test_orchestrator_pipeline.py); returns (engine, executed SQL with args)."""
    import json
    import uuid
    from unittest.mock import AsyncMock, MagicMock

    from cryptonite_execution.engine import PaperTradingEngine
    from cryptonite_orchestrator.server import command_poller

    row = {"command_id": uuid.uuid4(), "command_type": "MANUAL_ORDER", "target_instance_id": "MANUAL_TRADE",
           "payload": json.dumps({"client_order_id": "MANUAL-B2", "instrument_id": "BTCUSDT.BINANCE",
                                  "recommendation": recommendation, "order_type": "MARKET", "quantity": 0.01})}
    calls = 0

    async def fetchrow(*_):
        nonlocal calls
        calls += 1
        if calls == 1:
            return row
        raise asyncio.CancelledError()

    conn = AsyncMock()
    conn.fetchrow = fetchrow
    conn.execute = AsyncMock()
    conn.fetchval = AsyncMock(return_value=None)  # no other owner holds the coin (SELL_EXCEEDS_OWN_HOLDING check)
    pool = MagicMock()
    pool.acquire.return_value.__aenter__.return_value = conn
    engine = MagicMock(spec=PaperTradingEngine)
    engine.dispatcher = MagicMock()
    engine.dispatcher.get_position_qty.return_value = Decimal("1")  # the account holds enough for a SELL of 0.01
    with pytest.raises(asyncio.CancelledError):
        await command_poller(pool, engine)
    return engine, [c.args for c in conn.execute.call_args_list]


@pytest.mark.parametrize(("verdict", "side"), [("Overweight", "BUY"), ("STRONG_BUY", "BUY"), ("long", "BUY"),
                                               ("Underweight", "SELL")])
def test_fm6_orchestrator_side_fallback_follows_the_verdict_direction(verdict, side):
    engine, _ = asyncio.run(_orchestrate_sideless_order(verdict))
    engine.dispatcher.dispatch.assert_called_once()
    assert engine.dispatcher.dispatch.call_args[0][0].side == side


@pytest.mark.parametrize("verdict", ["HOLD", "MAYBE", ""])
def test_fm6_orchestrator_rejects_a_sideless_order_with_no_direction_instead_of_selling(verdict):
    engine, executed = asyncio.run(_orchestrate_sideless_order(verdict))
    engine.dispatcher.dispatch.assert_not_called()
    assert any("status = 'REJECTED'" in args[0] and "NO_ORDER_SIDE" in str(args[2]) for args in executed)


def test_fm6_operator_debates_list_shows_the_direction_and_the_cros_wording():
    import datetime
    import uuid
    from types import SimpleNamespace

    now = datetime.datetime.now(datetime.UTC)
    rows = [{"run_id": uuid.uuid4(), "instrument_id": "BTCUSDT.BINANCE", "status": "PENDING_APPROVAL", "created_at": now,
             "recommendation": rec, "conviction_level": level, "conviction_score": 0.72, "allocation_pct": 6,
             "reasoning": "trend"} for rec, level in [("BUY", "Overweight"), ("HOLD", "HOLD"), ("SELL", None)]]

    class _Conn:
        async def fetch(self, sql, *_):
            assert "d.conviction_level" in sql
            return rows

    class _Acquire:
        async def __aenter__(self):
            return _Conn()

        async def __aexit__(self, *_):
            return False

    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=SimpleNamespace(acquire=_Acquire))))
    got = asyncio.run(agents.list_debates(request, limit=3, _auth=None))
    assert [g["convictionLevel"] for g in got] == ["Overweight", "HOLD", None]
    assert got[0]["decision"].startswith("BUY · Overweight (72.0% Conviction)")
    assert got[1]["decision"].startswith("HOLD (") and got[2]["decision"].startswith("SELL (")


def test_fc2_similar_decisions_has_nothing_to_compare_for_an_unknown_verdict():
    assert asyncio.run(_similar_filter("MAYBE")) == "no query"
    assert asyncio.run(_similar_filter(None)) is None  # no recommendation: no direction filter
