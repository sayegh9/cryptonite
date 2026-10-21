"""Stage 1.2 (leverage plan): live leverage brackets from /fapi/v1/leverageBracket, cached in Redis daily,
REFERENCE_BRACKETS only as the BTC/ETH fallback, NO_LEVERAGE_BRACKETS and STALE_LEVERAGE_BRACKETS for entries.
No Binance call: the fetch is a stub, and Redis is an in-memory fake."""

import asyncio
import fnmatch
import time
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from cryptonite_execution.brackets import (
    BRACKETS_KEY,
    load_brackets,
    parse_leverage_brackets,
    refresh_brackets,
)
from cryptonite_execution.margin import (
    BRACKETS_MAX_AGE_S,
    REFERENCE_BRACKETS,
    bracket_source,
    calculate_liquidation_price,
    get_brackets,
    max_leverage_for_notional,
    set_live_brackets,
)
from test_paper_risk_guardrails import make_perp_dispatcher, perp_intent

D = Decimal
SOL = "SOLUSDT-PERP.BINANCE"

SOL_PAYLOAD = [{
    "symbol": "SOLUSDT",
    "notionalCoef": 1.0,
    "brackets": [
        {"bracket": 1, "initialLeverage": 75, "notionalCap": 5000, "notionalFloor": 0, "maintMarginRatio": 0.0065, "cum": 0.0},
        {"bracket": 2, "initialLeverage": 50, "notionalCap": 50000, "notionalFloor": 5000, "maintMarginRatio": 0.01, "cum": 17.5},
        {"bracket": 3, "initialLeverage": 25, "notionalCap": 250000, "notionalFloor": 50000, "maintMarginRatio": 0.02, "cum": 517.5},
        {"bracket": 4, "initialLeverage": 10, "notionalCap": 1000000, "notionalFloor": 250000, "maintMarginRatio": 0.05, "cum": 8017.5},
    ],
}]


class FakeRedis:
    def __init__(self):
        self.data: dict[str, str] = {}

    async def set(self, key, value, ex=None):
        self.data[key] = value

    async def get(self, key):
        return self.data.get(key)

    async def scan_iter(self, match="*"):
        for k in list(self.data):
            if fnmatch.fnmatch(k, match):
                yield k


def test_parse_keeps_binance_cum_and_tiers():
    parsed = parse_leverage_brackets(SOL_PAYLOAD)
    sol = parsed["SOLUSDT"]
    assert [b.max_leverage for b in sol] == [75, 50, 25, 10]
    assert sol[1].cum == D("17.5") and sol[1].mmr == D("0.01")
    assert sol[-1].notional_cap == D("1000000")


def test_parse_skips_a_malformed_symbol_and_keeps_the_rest():
    bad = {"symbol": "BADUSDT", "brackets": [
        {"bracket": 1, "initialLeverage": 20, "notionalCap": 1000, "notionalFloor": 0, "maintMarginRatio": 0.01, "cum": 0},
        {"bracket": 2, "initialLeverage": 10, "notionalCap": 5000, "notionalFloor": 2000, "maintMarginRatio": 0.02, "cum": 10},
    ]}  # a gap between 1,000 and 2,000
    parsed = parse_leverage_brackets(SOL_PAYLOAD + [bad, {"symbol": "X"}])
    assert set(parsed) == {"SOLUSDT"}


def test_refresh_then_load_installs_live_tables():
    redis = FakeRedis()

    async def fetch():
        return SOL_PAYLOAD

    now_ms = int(time.time() * 1000)
    assert asyncio.run(refresh_brackets(fetch, redis, now_ms=now_ms)) == 1
    assert BRACKETS_KEY.format(symbol="SOLUSDT") in redis.data
    assert bracket_source(SOL).kind == "MISSING"  # cached, not yet loaded
    assert asyncio.run(load_brackets(redis)) == 1
    src = bracket_source(SOL)
    assert src.kind == "LIVE" and not src.stale and src.entry_reject is None
    assert get_brackets(SOL)[0].max_leverage == 75
    assert max_leverage_for_notional(SOL, D("60000")) == 25


def test_failed_fetch_leaves_the_previous_cache():
    redis = FakeRedis()

    async def ok():
        return SOL_PAYLOAD

    async def boom():
        raise RuntimeError("Binance API Error [-1003]: too many requests")

    asyncio.run(refresh_brackets(ok, redis, now_ms=1))
    before = dict(redis.data)
    with pytest.raises(RuntimeError):
        asyncio.run(refresh_brackets(boom, redis))
    assert redis.data == before


def test_reference_snapshot_only_for_btc_and_eth():
    assert set(REFERENCE_BRACKETS) == {"BTCUSDT", "ETHUSDT"}
    assert bracket_source("BTCUSDT-PERP.BINANCE").kind == "REFERENCE"
    assert bracket_source("ETHUSDT-PERP.BINANCE").entry_reject is None
    assert bracket_source(SOL).entry_reject == "NO_LEVERAGE_BRACKETS"
    with pytest.raises(ValueError):
        get_brackets(SOL)


def test_live_table_wins_over_the_reference_and_ages_into_stale():
    live = parse_leverage_brackets([{**SOL_PAYLOAD[0], "symbol": "BTCUSDT"}])["BTCUSDT"]
    now = time.time_ns()
    set_live_brackets("BTCUSDT", live, now)
    assert get_brackets("BTCUSDT-PERP.BINANCE")[0].max_leverage == 75  # not the reference 125
    src = bracket_source("BTCUSDT-PERP.BINANCE", now_ns=now + (BRACKETS_MAX_AGE_S + 1) * 1_000_000_000)
    assert src.kind == "LIVE" and src.stale and src.entry_reject == "STALE_LEVERAGE_BRACKETS"


def test_liquidation_math_uses_the_live_table():
    set_live_brackets("SOLUSDT", parse_leverage_brackets(SOL_PAYLOAD)["SOLUSDT"], time.time_ns())
    # 10 SOL long at 150, isolated margin 300 (5x): tier 1 (notional 1,500), mmr 0.65%, cum 0
    lp = calculate_liquidation_price(SOL, "BUY", D("10"), D("150"), D("300"), "ISOLATED")
    assert lp == (D("1500") - D("300")) / (D("10") * (1 - D("0.0065")))


def _sol_dispatcher():
    d = make_perp_dispatcher()
    d.update_mark_price(SOL, D("150"))
    return d


def _sol_intent(side, qty, **flags):
    i = perp_intent(side, qty, **flags)
    i.instrument_id = SOL
    return i


def test_dispatcher_refuses_an_entry_without_brackets():
    d = _sol_dispatcher()
    ok, reason, _ = d.evaluate_intent(_sol_intent("BUY", "2", leverage=2, margin_mode="ISOLATED"))
    assert not ok and reason.startswith("NO_LEVERAGE_BRACKETS")


def test_dispatcher_refuses_an_entry_on_stale_brackets():
    d = _sol_dispatcher()
    set_live_brackets("SOLUSDT", parse_leverage_brackets(SOL_PAYLOAD)["SOLUSDT"],
                      time.time_ns() - (BRACKETS_MAX_AGE_S + 60) * 1_000_000_000)
    ok, reason, _ = d.evaluate_intent(_sol_intent("SELL", "2", leverage=2, margin_mode="ISOLATED"))
    assert not ok and reason.startswith("STALE_LEVERAGE_BRACKETS")


def test_dispatcher_accepts_an_entry_on_fresh_live_brackets():
    d = _sol_dispatcher()
    set_live_brackets("SOLUSDT", parse_leverage_brackets(SOL_PAYLOAD)["SOLUSDT"], time.time_ns())
    ok, reason, _ = d.evaluate_intent(_sol_intent("SELL", "2", leverage=2, margin_mode="ISOLATED"))
    assert ok, reason


def test_reduce_only_is_never_refused_for_brackets():
    d = _sol_dispatcher()
    d.set_perp_position(SOL, D("-2"), D("150"), leverage=2, margin_mode="ISOLATED", instance_id="INST-001")
    ok, reason, _ = d.evaluate_intent(_sol_intent("BUY", "2", reduce_only=True))
    assert ok, reason
    assert d.dispatch(_sol_intent("BUY", "1", cid="R-2", reduce_only=True), MagicMock(), MagicMock())[0]


def test_derivatives_feed_refresh_loop_caches_and_survives_a_failure():
    from cryptonite_scanner_service.derivatives_feed import DerivativesFeed

    redis = FakeRedis()
    feed = DerivativesFeed(redis, None, ["SOLUSDT"])
    calls = {"n": 0}

    async def fetch():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("Binance Network/Timeout Error")
        return SOL_PAYLOAD

    async def run():
        stop = asyncio.Event()
        task = asyncio.create_task(feed.run_bracket_refresh(stop, fetch, interval_s=60, retry_s=0.01))
        for _ in range(200):
            if BRACKETS_KEY.format(symbol="SOLUSDT") in redis.data:
                break
            await asyncio.sleep(0.01)
        stop.set()
        await task

    asyncio.run(run())
    assert calls["n"] == 2 and BRACKETS_KEY.format(symbol="SOLUSDT") in redis.data
