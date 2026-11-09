"""H15: an entry on a perp with no usable leverage table goes to spot margin where the profile allows it."""

import json
import time
from decimal import Decimal

import pytest
from cryptonite_execution import profile_lifecycle
from cryptonite_execution.brackets import open_entry_market, parse_leverage_brackets
from cryptonite_execution.mandate_store import load_mandate
from cryptonite_execution.margin import set_live_brackets
from cryptonite_orchestrator import paper_coverage as pc
from cryptonite_schemas.marks import quote_tick_key
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


async def test_a_failed_bracket_read_is_not_taken_as_a_missing_table():
    class Down:
        def scan_iter(self, match=None):
            raise ConnectionError("redis down")

    assert await open_entry_market(Down(), Market.PERP, WLD, ALL) is Market.PERP


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


async def set_quote(h, mid):  # noqa: F811
    if mid is None:
        await h.redis.delete(quote_tick_key(SPOT))
        return
    await h.redis.set(quote_tick_key(SPOT), json.dumps({
        "instrument_id": SPOT, "bid_price": str(mid), "ask_price": str(mid), "arrival_ns": time.time_ns()}), ex=60)


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


@pytest.mark.parametrize(("mid", "outcome"), [
    (62000, "RR_BELOW_FLOOR"),  # the price ran during the debate: 3000 of reward on 4000 of risk
    (57000, "RR_BELOW_FLOOR"),  # already through the stop
    (None, "NO_LIVE_PRICE"),
])
async def test_a_margin_rule_entry_is_measured_from_the_price_now(h, monkeypatch, mid, outcome):  # noqa: F811
    await auto_on(h, monkeypatch)
    stale_btc_table()
    m = await profile(h, level=3, markets=ALL, max_leverage=3)
    await set_quote(h, mid)
    assert await create(h, hold(await candidate(h)), m) is None
    assert await commands(h) == []
    async with h.pool.acquire() as conn:
        reason, _ = await proposals._skip_reason(conn, hold(await candidate(h)), m, h.redis)
    assert reason == outcome


async def test_a_margin_rule_entry_routes_from_the_live_price(h, monkeypatch):  # noqa: F811
    await auto_on(h, monkeypatch)
    stale_btc_table()
    m = await profile(h, level=3, markets=ALL, max_leverage=3)
    await set_quote(h, 60500)
    async with h.pool.acquire() as conn:
        reason, f = await proposals._skip_reason(conn, hold(await candidate(h)), m, h.redis)
    assert reason is None and f["entry"] == 60500 and (f["stop"], f["target"]) == (STOP, TARGET)


async def test_one_switch_reading_decides_so_no_margin_card_slips_through(h, monkeypatch):  # noqa: F811
    await auto_on(h, monkeypatch)
    stale_btc_table()
    m = await profile(h, level=3, markets=ALL, max_leverage=3)
    answers = iter([True, False])
    original = proposals.trades_by_rule

    async def flips(conn, mandate):
        await original(conn, mandate)
        return next(answers)

    monkeypatch.setattr(proposals, "trades_by_rule", flips)
    assert await create(h, hold(await candidate(h)), m) is None
    assert await h.pool.fetchval("SELECT count(*) FROM setup_proposals") == 0
    assert len(await commands(h)) == 1


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


async def at_position_pct(h, m, pct):  # noqa: F811
    await h.pool.execute("UPDATE ai_mandates SET max_position_pct = $2 WHERE id = $1", m.id, pct)
    async with h.pool.acquire() as conn:
        return await load_mandate(conn, m.id)


async def test_a_room_below_the_minimum_order_is_refused_before_it_is_queued_with_the_numbers(h, monkeypatch):  # noqa: F811
    """2026-11-08/09: $10,000 at 10% queued margin entries the orchestrator then refused (room $990 after its sizing
    margin, minimum $1,010.10 at $100,000 equity). The route sizes as the orchestrator does, so nothing is queued."""
    await auto_on(h, monkeypatch)
    stale_btc_table()
    m = await at_position_pct(h, await profile(h, level=3, markets=ALL, max_leverage=3), 10)
    assert await create(h, hold(await candidate(h)), m) is None
    assert await commands(h) == []
    why = await h.pool.fetchval(
        "SELECT detail->>'explanation' FROM ai_profile_decisions WHERE mandate_id = $1 AND stage = 'ROUTE'", m.id)
    assert why.startswith("ORDER_BELOW_MIN_NOTIONAL") and "largest order $990.00 set by profile max position" in why
    assert "minimum order $1,010.10 (1% of account equity $100,000.00)" in why
    refusal = await pc.route_refusal(h.pool, h.redis, m, SPOT)
    assert refusal.startswith("ORDER_ROUTE_REFUSED: ORDER_BELOW_MIN_NOTIONAL (largest order $990.00")


async def test_a_room_above_the_minimum_queues_a_plan_the_orchestrator_can_size(h, monkeypatch):  # noqa: F811
    await auto_on(h, monkeypatch)
    stale_btc_table()
    m = await at_position_pct(h, await profile(h, level=3, markets=ALL, max_leverage=3), 15)
    assert await create(h, hold(await candidate(h)), m) is None
    (payload,) = await commands(h)
    notional = Decimal(payload["margin_plan"]["notional_usd"])
    assert Decimal("1010.10") <= notional <= Decimal("1485.00")  # inside both buffered limits
    assert await pc.route_refusal(h.pool, h.redis, m, SPOT) is None
