"""H7a: fresh scanner evidence, structured lake bars, the CRO's limits in code, labelled what-if lessons.

Production 2026-11-02: debates read a manual Scanner Studio run's March-October signals as live, a CRO turned a PM
HOLD into a stored BUY, a Market Analyst spent its turn on DuckDB paths, and lessons from never-filled verdicts were
recalled as trades. The debate-level cases (operator framing, clamps through the worker, the evidence gate) are in
``test_debate_gate.py``.
"""
import asyncio
import datetime
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import asyncpg
import httpx
import polars as pl
import pytest
from cryptonite_agents import tools
from cryptonite_agents.gateway import BudgetGuard, Gateway
from cryptonite_agents.gateway.local_tier import LocalTierHealth
from cryptonite_agents.gateway.providers.pydantic_ai_backend import override_model
from cryptonite_agents.gateway.routes import EMBED, FLASH
from cryptonite_schemas.debate import clamp_risk_verdict
from migrated_db import REPO_ROOT, migrated_database, seed_paper_account
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "debate_worker"))
import reflection  # noqa: E402

NOW = 1_800_000_000.0  # a fixed clock, seconds
UTC = datetime.UTC


def _row(tf: str, age_s: float, trigger: str | None = "COMMITTED_BAR_EVENT", stored_after_s: float = 5,
         entry=100.0, stop=98.0):
    close_s = NOW - age_s
    return {"timeframe": tf, "bar_close_inclusive_ns": int(close_s * 1e9), "suggested_entry": entry,
            "suggested_stop": stop, "suggested_target": 106.0, "metrics": {},
            "created_at": datetime.datetime.fromtimestamp(close_s + stored_after_s, tz=UTC), "trigger_reason": trigger}


# --- 1. fresh scanner evidence ----------------------------------------------------------------------

@pytest.mark.parametrize("tf, age_min, fresh", [
    ("5m", 10, True), ("5m", 16, False), ("5m", 180, False),  # a 3-hour-old 5m signal is never fresh
    ("15m", 40, True), ("15m", 50, False),
    ("1h", 170, True), ("1h", 190, False),
    ("4h", 230, True), ("4h", 250, False),  # 3 bars would be 12 h: the 4 h ceiling applies
])
def test_a_candidate_is_fresh_for_about_three_bars_of_its_timeframe_at_most_four_hours(tf, age_min, fresh):
    got, _ = tools.fresh_scanner_candidates([_row(tf, age_min * 60)], NOW)
    assert bool(got) is fresh


def test_historical_and_manual_scan_rows_are_never_fresh():
    rows = [
        _row("5m", 60, trigger="MANUAL_JOB"),  # a Scanner Studio run over recent bars
        _row("1h", 600, trigger="HISTORICAL_PRIMING"),
        _row("5m", 60, trigger=None, stored_after_s=200 * 86400),  # no provenance: stored months after its bar
    ]
    got, newest_h = tools.fresh_scanner_candidates(rows, NOW)
    assert got == [] and newest_h == pytest.approx(1 / 60)


def test_a_fresh_candidate_carries_age_side_levels_and_source():
    row = _row("15m", 20 * 60, entry=100.0, stop=102.0)
    row["metrics"] = {"spec_slug": "my-short-spec"}
    (c,), _ = tools.fresh_scanner_candidates([row], NOW)
    assert (c["timeframe"], c["side"], c["age_minutes"], c["entry"], c["stop"], c["target"], c["source"]) == (
        "15m", "SHORT", 20.0, 100.0, 102.0, 106.0, "my-short-spec")


def test_the_tool_says_when_nothing_is_fresh(monkeypatch):
    monkeypatch.setattr(tools, "_scanner_rows", lambda inst: [_row("1h", 5 * 3600)])
    monkeypatch.setattr(tools.time, "time", lambda: NOW)
    out = json.loads(tools.get_scanner_context("BTCUSDT.BINANCE"))
    assert out["fresh_candidates"] == []
    assert out["message"] == "no fresh scanner candidate for BTCUSDT.BINANCE (newest is 5.0 h old)"


@pytest.mark.integration
def test_the_agent_role_can_read_the_scan_provenance(monkeypatch):
    """The tool runs as cryptonite_agent_ro: its join on scan_runs must be readable without a new grant."""
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        pytest.skip("DATABASE_URL not set")
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(url)
    agent = urlunsplit(parts._replace(netloc=f"cryptonite_agent_ro:agent_ro_dev_pass@{parts.netloc.rsplit('@', 1)[-1]}"))
    monkeypatch.setattr(tools, "DB_URL", agent)
    monkeypatch.setattr(tools, "_engine", None)
    assert isinstance(tools._scanner_rows("NOSUCHCOIN"), list)
    monkeypatch.setattr(tools, "_engine", None)


# --- 3. the CRO's limits --------------------------------------------------------------------------

@pytest.mark.parametrize("pm, cro, expected", [
    (("HOLD", 0.7, 0.0), ("Buy", 2.0), ("HOLD", "Hold", 0.0, 0.7, True)),
    (("BUY", 0.6, 5.0), ("Sell", 3.0), ("HOLD", "Hold", 0.0, 0.0, True)),
    (("SELL", 0.6, 5.0), ("Overweight", 3.0), ("HOLD", "Hold", 0.0, 0.0, True)),
    (("BUY", 0.6, 5.0), ("Buy", 8.0), ("BUY", "Buy", 5.0, 0.6, True)),
    (("BUY", 0.6, 5.0), ("Overweight", 4.0), ("BUY", "Overweight", 4.0, 0.6, False)),
    (("BUY", 0.6, 5.0), ("Hold", 3.0), ("HOLD", "Hold", 0.0, 0.0, False)),  # a veto: never the PM's BUY conviction
    (("HOLD", 0.8, 0.0), ("Hold", 0.0), ("HOLD", "Hold", 0.0, 0.8, False)),
])
def test_clamp_risk_verdict(pm, cro, expected):
    v = clamp_risk_verdict(*pm, *cro)
    assert (v.recommendation, v.conviction_level, v.allocation_pct, v.conviction_score,
            v.clamp_note is not None) == expected


# --- 4. structured bars ---------------------------------------------------------------------------

MIN_NS = 60 * 10**9


class _Lake:
    """AnalyticsQueryEngine stand-in: 1m bars with bar_open_ns, as query_bars_df returns them."""
    frame: pl.DataFrame

    def __init__(self, base_dir):
        self._con = self

    def close(self):
        pass

    def query_bars_df(self, symbol, timeframe="1m", start_ns=None, end_ns=None):
        f = self.frame
        return f.filter((pl.col("ts_event_ns") > start_ns) & (pl.col("ts_event_ns") <= end_ns))


def _minutes(end_ns: int, count: int, skip: int | None = None) -> pl.DataFrame:
    opens = [end_ns - (count - i) * MIN_NS for i in range(count) if i != skip]
    return pl.DataFrame({"bar_open_ns": opens, "ts_event_ns": [o + MIN_NS for o in opens],
                         "open": [1.0] * len(opens), "high": [2.0] * len(opens), "low": [0.5] * len(opens),
                         "close": [1.5] * len(opens), "volume": [10.0] * len(opens)})


@pytest.mark.parametrize("skip, complete", [(None, True), (30, False)])
def test_recent_bars_resamples_the_lake_and_flags_missing_bars(monkeypatch, skip, complete):
    import cryptonite_storage.queries as queries

    hour_ns = 60 * MIN_NS
    now_ns = 1_800_000_000 // 3600 * 3600 * 10**9 + 5 * MIN_NS  # 5 min past an hour
    end_ns = now_ns - 5 * MIN_NS
    _Lake.frame = _minutes(end_ns, 4 * 60, skip=skip)
    monkeypatch.setattr(queries, "AnalyticsQueryEngine", _Lake)
    out = tools.recent_bars("BTCUSDT.BINANCE", "1h", 4, now_ns=now_ns)
    assert out["complete"] is complete and out["missing"] == (0 if complete else 1)
    assert out["newest_bar_age_minutes"] == 5.0
    assert out["bars"][-1]["close_time"] == datetime.datetime.fromtimestamp(end_ns / 1e9, tz=UTC).isoformat(
        timespec="seconds")
    assert all(b["high"] == 2.0 for b in out["bars"]) and end_ns % hour_ns == 0


def test_recent_bars_from_an_unreadable_or_empty_lake_is_incomplete(tmp_path, monkeypatch):
    monkeypatch.setenv("ANALYTICS_LAKE_PATH", str(tmp_path / "lake"))
    out = json.loads(tools.get_recent_bars("BTCUSDT.BINANCE", "1h", 24))
    assert (out["complete"], out["returned"], out["missing"]) == (False, 0, 24)


def test_the_market_analyst_has_the_bars_tool():
    from cryptonite_agents.debate_fleet import MARKET_ANALYST

    assert tools.RECENT_BARS in MARKET_ANALYST.tools and "get_recent_bars" in MARKET_ANALYST.instructions


# --- 5. what-if vs trade lessons ------------------------------------------------------------------

class _Embedder:
    async def embed_texts(self, texts):
        return [[0.1] * 768 for _ in texts], 1


def _local_tier_up() -> LocalTierHealth:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"models": [{"name": "embeddinggemma:latest"}]})
    return LocalTierHealth(base_url="http://ollama:11434/v1", transport=httpx.MockTransport(handler))


async def _seed_run(conn, filled: bool) -> uuid.UUID:
    run = await conn.fetchval("INSERT INTO debate_runs (instrument_id, trigger_source, status) "
                              "VALUES ('BTCUSDT.BINANCE', 'test', $1) RETURNING id", "COMPLETED" if filled else "NO_TRADE")
    dec = await conn.fetchval(
        "INSERT INTO portfolio_decisions (run_id, instrument_id, recommendation, conviction_score, allocation_pct, "
        "reasoning, created_at) VALUES ($1, 'BTCUSDT.BINANCE', 'BUY', 0.7, 5, 'why', now() - interval '25 hours') "
        "RETURNING id", run)
    await conn.execute("INSERT INTO ai_decision_outcomes (decision_id, horizon, return_pct, mfe, mae, bars) "
                       "VALUES ($1, '24h', 0.01, 0.012, -0.004, 1440)", dec)
    if filled:
        cmd = uuid.uuid4()
        await conn.execute(
            "INSERT INTO execution_commands (command_id, idempotency_key, command_type, payload, status) "
            "VALUES ($1, $2, 'SUBMIT_ORDER', '{}'::jsonb, 'COMPLETED')", cmd, f"debate:{run}")
        coid = "AI-" + cmd.hex[:12].upper()
        await conn.execute(
            "INSERT INTO paper_orders (client_order_id, instance_id, account_id, instrument_id, side, order_type, "
            "quantity, filled_qty, avg_fill_price, status) VALUES ($1, 'AI_DEBATE_WORKER', 'PAPER-ACCOUNT-001', "
            "'BTCUSDT.BINANCE', 'BUY', 'MARKET', 0.01, 0.01, 65000, 'FILLED')", coid)
        await conn.execute(
            "INSERT INTO paper_trades (trade_id, account_id, client_order_id, instance_id, instrument_id, side, price, "
            "quantity, commission) VALUES ($1, 'PAPER-ACCOUNT-001', $2, 'AI_DEBATE_WORKER', 'BTCUSDT.BINANCE', 'BUY', "
            "65000, 0.01, 0.65)", f"T-{run.hex[:8]}", coid)
    return run


@pytest.fixture(scope="module")
def db_url():
    with migrated_database("cryptonite_h7a") as url:
        yield url


async def test_reflections_on_unfilled_runs_are_labelled_what_if_and_filled_runs_trade(db_url):
    prompts: dict[str, str] = {}

    def reviewer(messages, info: AgentInfo):
        text = str(messages)
        prompts["trade" if "the entry filled" in text else "what_if"] = text
        return ModelResponse(parts=[TextPart("Buying into strength worked.")])

    conn = await asyncpg.connect(db_url)
    try:
        await seed_paper_account(conn)
        what_if, trade = await _seed_run(conn, filled=False), await _seed_run(conn, filled=True)
    finally:
        await conn.close()
    pool = await asyncpg.create_pool(db_url, min_size=1, max_size=2)
    try:
        gw = Gateway(budget=BudgetGuard(), local_tier=_local_tier_up())
        with override_model(FLASH, FunctionModel(reviewer)), override_model(EMBED, _Embedder()):
            assert await reflection.ReflectionEngine(pool, None, gw).process_pending_reflections() == 2
        rows = {r["run_id"]: r for r in await pool.fetch("SELECT * FROM reflections")}
    finally:
        await pool.close()
    assert rows[what_if]["kind"] == "WHAT_IF" and rows[trade]["kind"] == "TRADE"
    assert rows[what_if]["lessons_learned"].startswith(reflection.WHAT_IF_LABEL)
    assert not rows[trade]["lessons_learned"].startswith(reflection.WHAT_IF_LABEL)
    assert not rows[what_if]["recall_excluded"] and not rows[trade]["recall_excluded"]  # labelled: recallable
    assert "NO trade was executed" in prompts["what_if"]
    assert "the entry filled at 65000" in prompts["trade"] and "commission paid 0.65" in prompts["trade"]


def _alembic(url: str, *args: str) -> None:
    subprocess.run([sys.executable, "-m", "alembic", "-c", "infra/migrations/alembic.ini", *args],
                   cwd=REPO_ROOT, env={**os.environ, "DATABASE_URL": url}, check=True, capture_output=True)


async def _seed_legacy(url: str) -> tuple[uuid.UUID, uuid.UUID]:
    conn = await asyncpg.connect(url)
    try:
        await seed_paper_account(conn)
        runs = []
        for filled in (False, True):
            run = await _seed_run(conn, filled)
            await conn.execute("INSERT INTO reflections (run_id, actual_outcome_pnl, alpha_vs_benchmark, "
                               "lessons_learned) VALUES ($1, 0.01, 0.01, 'The trade worked.')", run)
            runs.append(run)
        return runs[0], runs[1]
    finally:
        await conn.close()


async def _recall(url: str):
    import importlib.util

    spec = importlib.util.spec_from_file_location("debate_worker_main_h7a", REPO_ROOT / "services/debate_worker/main.py")
    main = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(main)
    pool = await asyncpg.create_pool(url, min_size=1, max_size=2)
    try:
        conn = await asyncpg.connect(url)
        kinds = {r["run_id"]: (r["kind"], r["recall_excluded"])
                 for r in await conn.fetch("SELECT run_id, kind, recall_excluded FROM agent_safe_reflections")}
        await conn.close()
        return kinds, [r["lessons_learned"] for r in await main._recent_lessons(pool)]
    finally:
        await pool.close()


@pytest.mark.integration
def test_migration_060_labels_legacy_reflections_and_keeps_what_ifs_out_of_recall():
    with migrated_database("cryptonite_h7a_mig") as url:
        _alembic(url, "downgrade", "059")
        what_if, trade = asyncio.run(_seed_legacy(url))
        _alembic(url, "upgrade", "head")
        kinds, recalled = asyncio.run(_recall(url))
        assert kinds == {what_if: ("WHAT_IF", True), trade: ("TRADE", False)}
        assert recalled == ["The trade worked."]  # only the filled run's lesson
        _alembic(url, "downgrade", "059")  # and back: the 046 view and table as they were
        _alembic(url, "upgrade", "head")
