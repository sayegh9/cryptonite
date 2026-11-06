"""H15: an entry on a perp with no usable leverage table goes to spot margin where the profile allows it."""

import json
import time

import pytest
from cryptonite_execution import profile_lifecycle
from cryptonite_execution.brackets import open_entry_market, parse_leverage_brackets
from cryptonite_execution.margin import set_live_brackets
from cryptonite_orchestrator import paper_coverage as pc
from cryptonite_schemas.profile import Market, market_of
from test_setup_proposals import (  # noqa: F401
    SPOT,
    STOP,
    TARGET,
    candidate,
    create,
    db_url,
    h,
    hold,
    profile,
    proposals,
)

pytestmark = pytest.mark.usefixtures("complete_lake_bars")
WLD = "WLDUSDT.BINANCE"
ALL = ("PERP", "SPOT", "SPOT_MARGIN")
TABLE = {"symbol": "WLDUSDT", "brackets": [
    {"bracket": 1, "initialLeverage": 20, "notionalCap": 50000, "notionalFloor": 0, "maintMarginRatio": 0.01, "cum": 0}]}


class NoBrackets:
    async def scan_iter(self, match=None):
        return
        yield


def install_table(fetched_ns=None):
    table = parse_leverage_brackets([TABLE])["WLDUSDT"]
    set_live_brackets("WLDUSDT", table, fetched_ns or time.time_ns())


@pytest.mark.parametrize(("spot", "markets", "expected"), [
    (WLD, ALL, Market.SPOT_MARGIN),
    (WLD, ("PERP",), Market.PERP),  # the profile does not allow margin: the perp route names the refusal
    ("MUBARAKUSDT.BINANCE", ALL, Market.PERP),  # the pair has no margin settings
    ("BTCUSDT.BINANCE", ALL, Market.PERP),  # the reference table can open an entry
])
async def test_a_missing_table_moves_only_what_margin_can_take(spot, markets, expected):
    assert await open_entry_market(NoBrackets(), Market.PERP, spot, markets) is expected


async def test_a_table_keeps_the_perp_and_a_stale_one_moves_to_margin():
    install_table()
    assert await open_entry_market(NoBrackets(), Market.PERP, WLD, ALL) is Market.PERP
    install_table(time.time_ns() - 40 * 24 * 3600 * 1_000_000_000)
    assert await open_entry_market(NoBrackets(), Market.PERP, WLD, ALL) is Market.SPOT_MARGIN


@pytest.mark.parametrize("market", [Market.SPOT, Market.SPOT_MARGIN])
async def test_another_market_is_left_alone(market):
    assert await open_entry_market(NoBrackets(), market, WLD, ALL) is market


async def test_no_redis_reads_nothing_and_keeps_the_perp():
    assert await open_entry_market(None, Market.PERP, WLD, ALL) is Market.PERP


def stale_btc_table():
    """A real refusal on a coin the harness feeds: an old table refuses new perp entries."""
    table = {**TABLE, "symbol": "BTCUSDT"}
    set_live_brackets("BTCUSDT", parse_leverage_brackets([table])["BTCUSDT"], time.time_ns() - 40 * 24 * 3600 * 1_000_000_000)


async def auto_on(h, monkeypatch):  # noqa: F811
    monkeypatch.setenv(proposals.RULE_ENTRIES_FLAG, "1")
    await h.pool.execute("INSERT INTO system_settings (key, value) VALUES ('autotrade.enabled', '1'::jsonb) "
                         "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value")
    profile_lifecycle.reset_switch_cache()


async def commands(h):  # noqa: F811
    return [json.loads(r["payload"]) for r in await h.pool.fetch("SELECT payload FROM execution_commands")]


async def test_a_rule_entry_without_a_usable_table_is_a_margin_order_at_the_scanner_levels(h, monkeypatch):  # noqa: F811
    await auto_on(h, monkeypatch)
    stale_btc_table()
    m = await profile(h, level=3, markets=ALL, max_leverage=3)
    assert await create(h, hold(await candidate(h)), m) is None
    rows = await commands(h)
    assert len(rows) == 1
    payload = rows[0]
    assert market_of(payload["instrument_id"]) is Market.SPOT_MARGIN and payload["margin_mode"] == "ISOLATED"
    assert payload["entry_source"] == proposals.RULE_ENTRY_TAG and payload["side"] == "BUY"
    assert payload["proposed_stop_loss"] == STOP and payload["proposed_take_profit"] == TARGET
    assert await h.pool.fetchval("SELECT count(*) FROM setup_proposals") == 0


async def test_a_usable_table_keeps_the_rule_entry_on_the_perp(h, monkeypatch):  # noqa: F811
    await auto_on(h, monkeypatch)
    m = await profile(h, level=3, markets=ALL, max_leverage=3)
    assert await create(h, hold(await candidate(h)), m) is None
    assert market_of((await commands(h))[0]["instrument_id"]) is Market.PERP


async def test_ask_mode_makes_no_card_it_could_not_approve(h, monkeypatch):  # noqa: F811
    stale_btc_table()
    m = await profile(h, level=1, markets=ALL, max_leverage=3)
    async with h.pool.acquire() as conn:
        reason, _ = await proposals._skip_reason(conn, hold(await candidate(h)), m, h.redis)
    assert reason == "MARKET_NOT_PROPOSABLE: SPOT_MARGIN"
    assert await commands(h) == []


async def test_a_debate_verdict_routes_to_margin_too(h):  # noqa: F811
    stale_btc_table()
    m = await profile(h, level=1, markets=ALL, max_leverage=3)
    async with h.pool.acquire() as conn:
        reason, instrument = await proposals.entry_profile_route(conn, m, SPOT, "BUY", "PERP", h.redis)
    assert reason is None and market_of(instrument) is Market.SPOT_MARGIN


async def test_the_coverage_check_tests_the_margin_route_and_names_a_margin_refusal(h):  # noqa: F811
    stale_btc_table()
    m = await profile(h, level=1, markets=ALL, max_leverage=3)
    assert await pc.route_refusal(h.pool, h.redis, m, SPOT) is None
    await h.pool.execute("UPDATE paper_accounts SET total_equity = 0")
    assert await pc.route_refusal(h.pool, h.redis, m, SPOT) == "ORDER_ROUTE_REFUSED: NON_POSITIVE_EQUITY"


async def test_the_coverage_check_keeps_the_perp_refusal_for_a_perp_only_profile(h):  # noqa: F811
    stale_btc_table()
    m = await profile(h, level=1, markets=("PERP",), max_leverage=3)
    assert await pc.route_refusal(h.pool, h.redis, m, SPOT) == "ORDER_ROUTE_REFUSED: STALE_LEVERAGE_BRACKETS"
