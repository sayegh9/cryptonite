"""H8: derivatives evidence, honest tool status, measured bar figures, a stronger evidence gate, operator shorts.

Production acceptance debate d70b8457 (2026-11-02, BTC, operator): the Market Analyst guessed Parquet paths for
funding and open interest, both calls failed yet were traced OK, it overstated "successive lower lows", the gate let a
troubleshooting reply through, and nobody argued a SHORT in a falling market. Models are FunctionModels; no paid call.
"""
import datetime
import json

import pytest
from cryptonite_agents import tools
from cryptonite_agents.debate_fleet import MARKET_ANALYST, SENTIMENT_ANALYST
from cryptonite_agents.gateway.providers.pydantic_ai_backend import _as_tool, tool_result_status
from cryptonite_agents.gateway.registry import RiskClass, ToolSpec
from test_debate_gate import Models, db_url, load, pool, run  # noqa: F401  (fixtures)

UTC = datetime.UTC
NOW = 1_800_000_000.0


@pytest.fixture(scope="module")
def main():
    return load("debate_worker_main_h8", "main.py")


def _bar(i, o, h, lo, c):
    t = datetime.datetime.fromtimestamp(NOW - (10 - i) * 3600, tz=UTC).isoformat(timespec="seconds")
    return {"close_time": t, "open": o, "high": h, "low": lo, "close": c, "volume": 1.0}


BARS = [_bar(0, 100, 105, 99, 104), _bar(1, 104, 106, 101, 102), _bar(2, 102, 103, 97, 98),
        _bar(3, 98, 99, 95, 96), _bar(4, 96, 100, 96, 99), _bar(5, 99, 101, 94, 95)]


# --- 3. measured bar figures -----------------------------------------------------------------------

def test_the_bar_summary_is_exact():
    s = tools.bar_summary(BARS)
    assert (s["first_close"], s["last_close"], s["net_change_pct"]) == (104.0, 95.0, -8.654)
    assert (s["window_high"], s["window_high_bar_close"]) == (106.0, BARS[1]["close_time"])
    assert (s["window_low"], s["window_low_bar_close"]) == (94.0, BARS[5]["close_time"])
    assert s["last_closes"] == [102.0, 98.0, 96.0, 99.0, 95.0]
    # lows 99 101 97 95 96 94: lower at bars 2, 3, 5; highs 105 106 103 99 100 101: higher at bars 1, 4, 5
    assert (s["lower_lows"], s["higher_highs"], s["bar_pairs_compared"]) == (3, 3, 5)
    assert tools.bar_summary([]) is None


def test_recent_bars_carries_the_summary(monkeypatch):
    import cryptonite_storage.queries as queries
    from test_h7a_debate_evidence import MIN_NS, _Lake, _minutes

    now_ns = 1_800_000_000 // 3600 * 3600 * 10**9 + 5 * MIN_NS
    _Lake.frame = _minutes(now_ns - 5 * MIN_NS, 4 * 60)
    monkeypatch.setattr(queries, "AnalyticsQueryEngine", _Lake)
    out = tools.recent_bars("BTCUSDT.BINANCE", "1h", 4, now_ns=now_ns)
    assert out["summary"] == tools.bar_summary(out["bars"]) and out["summary"]["window_high"] == 2.0


# --- 1. derivatives evidence block -----------------------------------------------------------------

def _live(mark=True, oi=True, funding=True):
    ns = int((NOW - 10) * 1e9)
    return json.dumps({
        "symbol": "BTCUSDT", "source": "live feed",
        "mark": {"mark_price": "65010.5", "funding_rate": "0.0001", "ts_event_ns": ns} if mark else None,
        "open_interest": {"open_interest": "81234.5", "ts_event_ns": ns} if oi else None,
        "funding": [{"funding_time": "2027-01-15T08:00:00+00:00", "funding_rate": 0.00008}] if funding else [],
        "note": None if (mark and oi) else "perp:oi:BTCUSDT is missing: the open interest is unknown.",
    })


def test_the_derivatives_block_has_each_field_with_its_age(monkeypatch):
    monkeypatch.setattr(tools, "get_derivatives_context", lambda sym: _live())
    d = tools.derivatives_evidence("BTCUSDT.BINANCE", now_s=NOW)
    assert (d["source"], d["perp_instrument"]) == ("live feed", "BTCUSDT-PERP.BINANCE")
    assert d["mark_price"] == {"value": 65010.5, "as_of": tools._ns_iso(int((NOW - 10) * 1e9)), "age_s": 10.0}
    assert (d["open_interest"]["value"], d["open_interest"]["age_s"]) == (81234.5, 10.0)
    assert d["funding_rate"]["value"] == 0.00008
    # H9: labelled as the last 8h settlement with its time, plus the rate in force for the next settlement
    assert d["funding_rate"]["kind"] == "last 8h settlement (settled at 2027-01-15T08:00:00+00:00)"
    assert d["funding_rate"]["predicted_next_rate"] == 0.0001 and "not stale" in d["funding_rate"]["note"]
    assert d["funding_rate"]["as_of"] == "2027-01-15T08:00:00+00:00"


def test_a_missing_field_is_unavailable_with_the_reason(monkeypatch):
    monkeypatch.setattr(tools, "get_derivatives_context", lambda sym: _live(oi=False, funding=False))
    d = tools.derivatives_evidence("BTCUSDT.BINANCE", now_s=NOW)
    assert d["open_interest"] == {"unavailable": "perp:oi:BTCUSDT is missing: the open interest is unknown."}
    assert d["funding_rate"]["kind"].startswith("rate in force") and d["mark_price"]["value"] == 65010.5


@pytest.mark.parametrize("answer, reason", [
    (lambda sym: json.dumps({"symbol": "XUSDT", "perpetual": False, "note": "no USD-M perpetual is listed"}),
     "no USD-M perpetual is listed"),
    (lambda sym: (_ for _ in ()).throw(RuntimeError("down")), "derivatives context could not be read (RuntimeError)"),
])
def test_no_perpetual_or_a_failure_is_unavailable_everywhere(monkeypatch, answer, reason):
    monkeypatch.setattr(tools, "get_derivatives_context", answer)
    d = tools.derivatives_evidence("XUSDT.BINANCE", now_s=NOW)
    assert all(d[f] == {"unavailable": reason} for f in tools.DERIVATIVES_FIELDS)


def test_the_debate_analysts_get_no_lake_sql_and_the_derivatives_tool():
    for persona in (MARKET_ANALYST, SENTIMENT_ANALYST):
        assert tools.QUERY_PARQUET not in persona.tools and tools.DERIVATIVES_CONTEXT in persona.tools
        assert "never guess or write file paths" in persona.instructions
    assert "'Measured:'" in MARKET_ANALYST.instructions and "'Interpretation:'" in MARKET_ANALYST.instructions


# --- 2. honest tool status -------------------------------------------------------------------------

@pytest.mark.parametrize("result, status", [
    ("Security Error: Unauthorized file access attempted outside catalog path.", "ERROR"),
    ("DuckDB Error: IO Error: No files found that match the pattern", "ERROR"),
    ("Database Error: permission denied", "ERROR"),
    ("Error fetching klines data: timeout", "ERROR"),
    ('{"error": "invalid symbol"}', "ERROR"),
    ({"status": "ERROR", "detail": "x"}, "ERROR"),
    ('{"symbol": "BTCUSDT", "mark": null, "note": "error budget"}', "OK"),
    ("No error was found in the bars", "OK"),
    ('[{"close": 1}]', "OK"),
])
def test_tool_result_status(result, status):
    assert tool_result_status(result) == status


def test_a_failing_tool_is_traced_as_error_and_its_text_still_reaches_the_agent():
    calls: list[dict] = []

    def query_parquet_duckdb(query: str) -> str:
        """Query."""
        return "Security Error: Unauthorized file access attempted outside catalog path. Blocked: data/x.parquet"

    def boom(query: str) -> str:
        """Boom."""
        raise RuntimeError("lake down")

    tool = _as_tool(ToolSpec("query_parquet_duckdb", query_parquet_duckdb, RiskClass.READ, "Q."), lambda s: None, calls)
    assert tool.function(query="SELECT 1").startswith("Security Error")
    with pytest.raises(RuntimeError):
        _as_tool(ToolSpec("boom", boom, RiskClass.READ, "B."), lambda s: None, calls).function(query="x")
    assert [(c["tool"], c["status"]) for c in calls] == [("query_parquet_duckdb", "ERROR"), ("boom", "ERROR")]


# --- 4. stronger evidence gate ---------------------------------------------------------------------

COMPLETE = {"complete": True, "timeframe": "1h", "returned": 6, "expected": 6, "bars": BARS,
            "summary": tools.bar_summary(BARS)}
DERIVS = {"funding_rate": {"value": 0.0001}, "open_interest": {"value": 81234.5},
          "mark_price": {"unavailable": "missing"}}
GOOD = ("Measured: last close 95.0, window low 94.0 at the latest bar, net change -8.654%, 3 lower lows in 5 pairs; "
        "funding 0.01% and open interest 81,234.5 BTC.\nInterpretation: a mild downtrend.")


@pytest.mark.parametrize("text, reason", [
    (GOOD, None),
    ("Measured: last close 95.0. Funding rate unavailable; open interest 81,234.5.", None),  # partial is fine
    ("I tried query_parquet_duckdb on data/perp_funding_rates/BTCUSDT-PERP.BINANCE.parquet but got a Security Error.",
     "tool errors or troubleshooting"),
    ("Unable to access the funding history. Last close 95.0.", "tool errors or troubleshooting"),
    ("Price is trending down with successive lower lows and weak momentum.", "cited no measured figure"),
    ("Measured: last close 95.0, low 94.0. Funding and open interest data are unavailable.",
     "funding and open interest unavailable"),
    ("Measured: last close 95.0. Derivatives data is not available for this instrument.",
     "funding and open interest unavailable"),
    ("   ", "returned no analysis"),
])
def test_the_evidence_gate(main, text, reason):
    got = main.evidence_gate(COMPLETE, DERIVS, text)
    assert (got is None) if reason is None else (got.startswith("EVIDENCE_INCOMPLETE") and reason in got)


def test_all_unavailable_is_fine_when_the_block_had_nothing(main):
    none = {f: {"unavailable": "feed down"} for f in tools.DERIVATIVES_FIELDS}
    assert main.evidence_gate(COMPLETE, none, "Last close 95.0. Funding and open interest unavailable.") is None


def test_incomplete_bars_stay_incomplete(main):
    bars = {"complete": False, "timeframe": "1h", "returned": 0, "expected": 48, "reason": "no bars", "bars": []}
    assert "bars incomplete (0/48, no bars)" in main.evidence_gate(bars, DERIVS, GOOD)


@pytest.mark.usefixtures("complete_lake_bars")
async def test_a_troubleshooting_reply_makes_the_debate_non_actionable(main, pool):  # noqa: F811
    text = "query_parquet_duckdb failed: Security Error, blocked path data/exchange_open_interest/BTCUSDT.parquet"
    run_row = await run(main, pool, {"instrument_id": "BTCUSDT.BINANCE", "trigger_source": "user_on_demand"},
                        Models(analyst_text=text))
    d = await pool.fetchrow("SELECT * FROM portfolio_decisions WHERE run_id = $1", run_row["id"])
    assert d["recommendation"] == "HOLD" and "tool errors or troubleshooting" in d["reasoning"]
    assert (run_row["status"], run_row["failure_reason"]) == ("NO_TRADE", "EVIDENCE_INCOMPLETE")


# --- 1 + 5: prompts of an operator debate ----------------------------------------------------------

@pytest.mark.usefixtures("complete_lake_bars")
async def test_an_operator_debate_has_the_derivatives_block_and_the_bull_picks_the_direction(main, pool):  # noqa: F811
    models = Models()
    await run(main, pool, {"instrument_id": "BTCUSDT.BINANCE", "trigger_source": "user_on_demand"}, models)
    texts = models.prompts["texts"]  # analysts (in parallel), then the Bull, then the Bear
    assert sum("--- DERIVATIVES (perpetual" in t for t in texts[:2]) == 2  # both analysts
    assert sum("MEASURED BAR SUMMARY" in t for t in texts[:2]) == 2  # H10: both analysts get the same snapshot
    assert "Direction: LONG" in texts[2] and "Direction: SHORT" in texts[2] and "SELL (open a short)" in texts[2]
    assert "argue for HOLD" in texts[3]
    assert "If you take a SHORT case, the recommendation is SELL" in models.prompts["pm"]


@pytest.mark.usefixtures("complete_lake_bars")
async def test_a_candidate_debate_keeps_the_h6_side_line(main, pool):  # noqa: F811
    models = Models()
    short = {"instrument_id": "BTCUSDT.BINANCE", "trigger_source": "user_on_demand",
             "candidate_payload": json.dumps({"suggested_entry": 100, "suggested_stop": 102, "suggested_target": 95})}
    await run(main, pool, short, models)
    assert "candidate is a SHORT setup" in models.prompts["pm"] and "Direction: LONG" not in "".join(models.prompts["texts"])
