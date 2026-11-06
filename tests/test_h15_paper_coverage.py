"""H15 PR B: the coverage worker widens opted-in paper profiles, and nothing else. It never creates an order."""

import asyncio
import json
import time
import uuid

import asyncpg
import pytest
from cryptonite_execution.mandate_store import load_mandate
from cryptonite_orchestrator import paper_coverage as pc
from cryptonite_schemas.universe import INGEST_UNIVERSE_KEY
from migrated_db import migrated_database
from test_debate_perp_gate import CapRedis

WLD, WLD_PERP = "WLDUSDT.BINANCE", "WLDUSDT-PERP.BINANCE"
NEW = "MUBARAKUSDT.BINANCE"
LONG_PICKS = ["trend-pullback", "swing-trend-pullback-1h"]
NOW = time.time_ns()
HOUR = 3_600_000_000_000


@pytest.fixture(scope="module")
def db_url():
    with migrated_database("cryptonite_ci_h15b") as url:
        yield url


@pytest.fixture
async def pool(db_url):
    p = await asyncpg.create_pool(db_url, min_size=1, max_size=6)
    await p.execute("TRUNCATE paper_coverage_ledger, ai_profile_coin_locks, execution_commands, paper_positions CASCADE")
    await p.execute("UPDATE ai_mandates SET status = 'ARCHIVED'")
    await p.execute("DELETE FROM system_settings WHERE key = $1", pc.POLICY_KEY)
    for spot, symbol in ((WLD, "WLDUSDT"), (NEW, "MUBARAKUSDT")):
        await p.execute(
            """INSERT INTO instruments (instrument_id, raw_symbol, venue, market_type, base_currency, quote_currency,
                   price_precision, size_precision, price_increment, size_increment, min_notional, is_active, status)
               VALUES ($1, $2, 'BINANCE', 'SPOT', $3, 'USDT', 4, 1, 0.0001, 0.1, 5, true, 'TRADING')
               ON CONFLICT (instrument_id) DO UPDATE SET is_active = true, status = 'TRADING', min_notional = 5""",
            spot, symbol, symbol.removesuffix("USDT"))
        await feed(p, symbol, NOW)
        for i in (0, 1):
            await p.execute(
                """INSERT INTO funding_rates (instrument_id, funding_time_ns, funding_rate, funding_interval_hours, source)
                   VALUES ($1, $2, 0.0001, 8, 'BINANCE_REST') ON CONFLICT DO NOTHING""",
                f"{symbol}-PERP.BINANCE", NOW - i * 8 * HOUR)
    yield p
    await p.close()


async def feed(p, symbol, ts):
    await p.execute("DELETE FROM ingest_checkpoints WHERE symbol = $1", symbol)
    await p.execute(
        """INSERT INTO ingest_checkpoints (venue, market_type, symbol, bar_type, timeframe, first_timestamp_ns,
               last_timestamp_ns, total_bars, last_updated_at)
           VALUES ('BINANCE', 'SPOT', $1, 'KLINE', '1m', 1, $2, 1, now())""", symbol, ts)


async def profile(p, *, direction="LONG", strategies=LONG_PICKS, markets=("SPOT",), level=1, status="ACTIVE",
                  environment="PAPER", instruments=(WLD,), name=None, lock=True):
    mid = await p.fetchval(
        """INSERT INTO ai_mandates (name, capital_budget_usd, instruments, max_position_pct, status, autonomy_level, markets,
               max_leverage, direction, style, allow_perps, strategies, timeframes, environment)
           VALUES ($1, 10000, $2, 10, $3, $4, $5, 1, $6, 'SWING', $7, $8, '{}', $9) RETURNING id""",
        name or f"h15-{uuid.uuid4().hex[:6]}", list(instruments), status, level, list(markets), direction, "PERP" in markets,
        list(strategies), environment)
    if lock:
        await p.execute("INSERT INTO ai_profile_coin_locks (base_asset, mandate_id) VALUES ('WLD', $1)", mid)
    return mid


async def policy(p, *mids, **over):
    async with p.acquire() as conn:
        return await pc.write_policy(conn, pc.Policy(enabled=True, profiles=tuple(str(m) for m in mids), **over), "test")


async def no_route_refusal(*_):
    return None


async def all_perps():
    return {"WLDUSDT", "MUBARAKUSDT"}


PROBES = pc.Probes(all_perps, no_route_refusal)


async def cycle(p, redis=None, probes=PROBES, **kw):
    return await pc.run_cycle(p, redis or CapRedis(), probes=probes, now_ns=NOW, **kw)


async def scope(p, mid):
    return await p.fetchrow("SELECT direction, strategies, timeframes, markets, instruments, allow_perps, updated_at FROM ai_mandates WHERE id = $1", mid)


def test_a_missing_or_broken_policy_reads_as_disabled():
    assert pc.parse_policy(None).enabled is False
    assert pc.parse_policy("not json").enabled is False
    assert pc.parse_policy({"enabled": "yes"}).enabled is False


async def test_a_long_only_profile_becomes_both_with_short_picks_and_the_perp_market(pool):
    mid = await profile(pool)
    await policy(pool, mid)
    [r] = await cycle(pool)
    assert r.outcome == "APPLIED"
    row = await scope(pool, mid)
    assert row["direction"] == "BOTH" and "PERP" in row["markets"] and row["allow_perps"] is True
    assert WLD_PERP in row["instruments"]
    assert set(LONG_PICKS) <= set(row["strategies"]) and set(pc.DEFAULT_PRESETS) <= set(row["strategies"])
    assert await pool.fetchval("SELECT count(*) FROM audit_events WHERE event_type = 'PAPER_COVERAGE_APPLIED'") >= 1


async def test_a_both_profile_with_long_picks_gains_the_shorts_once(pool):
    mid = await profile(pool, direction="BOTH", markets=("SPOT", "PERP"), instruments=(WLD, WLD_PERP))
    await policy(pool, mid)
    assert (await cycle(pool))[0].outcome == "APPLIED"
    assert (await cycle(pool))[0].outcome == "NO_CHANGE"
    n = await pool.fetchval("SELECT count(*) FROM paper_coverage_ledger WHERE mandate_id = $1", mid)
    assert n == len(pc.DEFAULT_PRESETS)


async def test_nothing_else_on_the_profile_moves_and_no_order_is_created(pool):
    cols = ("autonomy_level, status, environment, dry_run, capital_budget_usd, max_position_pct, max_drawdown_pct, max_leverage, "
            "max_risk_per_trade_pct, daily_loss_limit_pct, max_open_positions, max_debates_per_day, exit_tp_r, exit_trail_pct")
    mid = await profile(pool)
    before = dict(await pool.fetchrow(f"SELECT {cols} FROM ai_mandates WHERE id = $1", mid))
    await policy(pool, mid)
    await cycle(pool)
    assert dict(await pool.fetchrow(f"SELECT {cols} FROM ai_mandates WHERE id = $1", mid)) == before
    assert await pool.fetchval("SELECT count(*) FROM execution_commands") == 0


async def test_an_empty_strategy_list_keeps_what_it_meant(pool):
    from cryptonite_schemas.profile import preset_ids_for
    mid = await profile(pool, strategies=())
    await policy(pool, mid)
    await cycle(pool)
    assert set(preset_ids_for("SWING", "LONG")) <= set((await scope(pool, mid))["strategies"])
    assert await pool.fetchval(
        "SELECT count(*) FROM paper_coverage_ledger WHERE mandate_id = $1 AND old_value = 'style default'", mid) > 0


@pytest.mark.parametrize(("over", "reason"), [
    ({"status": "PAUSED"}, "PROFILE_NOT_ACTIVE"), ({"level": 3}, "MODE_NOT_ALLOWED")])
async def test_an_ineligible_profile_gives_a_named_result_and_no_change(pool, over, reason):
    mid = await profile(pool, **over)
    before = await scope(pool, mid)
    await policy(pool, mid)
    assert (await cycle(pool))[0].outcome == reason
    assert await scope(pool, mid) == before


async def test_a_non_paper_profile_is_refused(pool):
    m = await pool_mandate(pool, await profile(pool))
    assert pc.eligibility_reason(m.model_copy(update={"environment": "LIVE"}), pc.Policy()) == "PROFILE_NOT_PAPER"


async def test_auto_is_only_covered_when_the_policy_allows_it(pool):
    mid = await profile(pool, level=3)
    await policy(pool, mid, allowed_modes=("ASK", "AUTO"))
    assert (await cycle(pool))[0].outcome == "APPLIED"


async def test_a_disabled_policy_changes_nothing_and_reads_nothing_else(pool):
    mid = await profile(pool)
    async with pool.acquire() as conn:
        await pc.write_policy(conn, pc.Policy(enabled=False, profiles=(str(mid),)), "test")

    async def boom():
        raise AssertionError("no exchange call while disabled")
    assert await pc.run_cycle(pool, CapRedis(), probes=pc.Probes(boom, no_route_refusal)) == []


async def test_preview_reports_and_writes_nothing(pool):
    mid = await profile(pool)
    before = await scope(pool, mid)
    wanted = pc.Policy(enabled=False, version=1, profiles=(str(mid),))
    [r] = await cycle(pool, apply=False, policy=wanted)
    assert r.outcome == "PLANNED" and r.changes
    assert await scope(pool, mid) == before and await pool.fetchval("SELECT count(*) FROM paper_coverage_ledger") == 0


async def _failed_check(pool, name):
    mid = await profile(pool)
    probes = PROBES
    if name == "INSTRUMENT_METADATA_MISSING":
        await pool.execute("UPDATE instruments SET min_notional = NULL WHERE instrument_id = $1", WLD)
    elif name == "INSTRUMENT_NOT_TRADING":
        await pool.execute("UPDATE instruments SET status = 'BREAK' WHERE instrument_id = $1", WLD)
    elif name == "STALE_FEED":
        await feed(pool, "WLDUSDT", NOW - HOUR)
    elif name == "PERP_NOT_TRADING":
        async def none():
            return set()
        probes = pc.Probes(none, no_route_refusal)
    elif name == "PERP_STATUS_UNKNOWN":
        async def unknown():
            return None
        probes = pc.Probes(unknown, no_route_refusal)
    elif name == "FUNDING_INTERVAL_UNKNOWN":
        await pool.execute("DELETE FROM funding_rates WHERE instrument_id = $1", WLD_PERP)
    elif name == "ORDER_ROUTE_REFUSED: X":
        async def refuse(*_):
            return "ORDER_ROUTE_REFUSED: X"
        probes = pc.Probes(all_perps, refuse)
    elif name == "COIN_LOCKED":
        other = await profile(pool, lock=False)
        await pool.execute("UPDATE ai_profile_coin_locks SET mandate_id = $1 WHERE mandate_id = $2", other, mid)
    return mid, probes


@pytest.mark.parametrize("name", ["INSTRUMENT_METADATA_MISSING", "INSTRUMENT_NOT_TRADING", "STALE_FEED", "PERP_NOT_TRADING",
                                  "PERP_STATUS_UNKNOWN", "FUNDING_INTERVAL_UNKNOWN", "ORDER_ROUTE_REFUSED: X", "COIN_LOCKED"])
async def test_each_failed_coin_check_is_named_and_changes_nothing(pool, name):
    mid, probes = await _failed_check(pool, name)
    before = await scope(pool, mid)
    await policy(pool, mid)
    [r] = await cycle(pool, probes=probes)
    assert r.outcome == f"WLDUSDT: {name}"
    assert await scope(pool, mid) == before


async def test_no_perp_mark_is_named_by_the_real_route_check(pool):
    mid = await profile(pool)
    await policy(pool, mid)
    [r] = await cycle(pool, probes=pc.Probes(all_perps, pc.route_refusal))
    assert r.outcome == "WLDUSDT: NO_PERP_MARK"


async def test_a_concurrent_edit_aborts_the_plan_and_the_operator_wins(pool):
    mid = await profile(pool)
    await policy(pool, mid)

    async def operator_edits(*_):
        await pool.execute("UPDATE ai_mandates SET strategies = '{trend-pullback}', updated_at = now() WHERE id = $1", mid)
    [r] = await cycle(pool, probes=pc.Probes(all_perps, operator_edits))
    assert r.outcome == "ABORTED_CONCURRENT_EDIT"
    row = await scope(pool, mid)
    assert row["direction"] == "LONG" and row["strategies"] == ["trend-pullback"]
    assert await pool.fetchval("SELECT count(*) FROM paper_coverage_ledger") == 0


async def test_a_hand_removed_item_is_not_added_back(pool):
    mid = await profile(pool, direction="BOTH", markets=("SPOT", "PERP"), instruments=(WLD, WLD_PERP))
    await policy(pool, mid)
    await cycle(pool)
    await pool.execute("UPDATE ai_mandates SET strategies = array_remove(strategies, 'trend-pullback-short'), updated_at = now() WHERE id = $1", mid)
    assert (await cycle(pool))[0].outcome == "NO_CHANGE"
    assert "trend-pullback-short" not in (await scope(pool, mid))["strategies"]


async def test_disable_then_enable_keeps_the_version_so_nothing_is_reapplied(pool):
    mid = await profile(pool)
    one = await policy(pool, mid)
    async with pool.acquire() as conn:
        off = await pc.write_policy(conn, pc.Policy(enabled=False, profiles=(str(mid),)), "test")
    again = await policy(pool, mid)
    assert one.version == off.version == again.version


async def _new_coin_redis():
    r = CapRedis()
    r.kv[INGEST_UNIVERSE_KEY] = json.dumps({"v": 1, "venue": "BINANCE", "symbols": ["MUBARAKUSDT", "WLDUSDT"], "timeframes": ["1m"]})
    return r


async def test_a_new_fed_coin_goes_to_the_first_eligible_profile_exactly_once_under_concurrent_workers(pool):
    first = await profile(pool, direction="BOTH", markets=("SPOT", "PERP"), instruments=(WLD, WLD_PERP), strategies=pc.DEFAULT_PRESETS)
    await policy(pool, first, allow_new_coins=True)
    redis = await _new_coin_redis()
    await asyncio.gather(*(cycle(pool, redis) for _ in range(4)))
    row = await scope(pool, first)
    assert NEW in row["instruments"] and "MUBARAKUSDT-PERP.BINANCE" in row["instruments"]
    assert await pool.fetchval("SELECT count(*) FROM ai_profile_coin_locks WHERE base_asset = 'MUBARAK'") == 1
    assert await pool.fetchval("SELECT count(*) FROM paper_coverage_ledger WHERE item_kind = 'COIN'") == 1


async def test_no_eligible_profile_for_a_new_coin_is_named(pool):
    mid = await profile(pool, status="PAUSED")
    await policy(pool, mid, allow_new_coins=True)
    results = await cycle(pool, await _new_coin_redis())
    assert ("MUBARAKUSDT", "NO_ELIGIBLE_PAPER_PROFILE") in {(r.subject, r.outcome) for r in results}


async def test_an_invalid_profile_is_not_offered_new_coins_and_the_next_valid_one_gets_them(pool):
    bad = await profile(pool, direction="LONG", strategies=["trend-pullback-short"], lock=False, instruments=("XRPUSDT.BINANCE",))
    good = await profile(pool, direction="BOTH", markets=("SPOT", "PERP"), instruments=(WLD, WLD_PERP), strategies=pc.DEFAULT_PRESETS)
    await policy(pool, bad, good, allow_new_coins=True)
    results = await cycle(pool, await _new_coin_redis())
    assert any(r.outcome.startswith("INVALID_SELECTION") for r in results)
    assert NEW in (await scope(pool, good))["instruments"]
    assert NEW not in (await scope(pool, bad))["instruments"]


async def test_a_plan_that_only_adds_a_market_nothing_needs_is_dropped(pool):
    picks = ["trend-pullback"]
    before_id = await profile(pool, strategies=picks)
    await policy(pool, before_id, allowed_presets=tuple(picks))
    before = await scope(pool, before_id)
    assert [r.outcome for r in await cycle(pool)] == ["NO_CHANGE"]
    assert await scope(pool, before_id) == before


async def test_a_policy_write_waits_for_an_apply_in_progress(pool):
    async with pool.acquire() as holder, holder.transaction():
        await holder.execute("SELECT pg_advisory_xact_lock(hashtext($1))", pc.POLICY_KEY)
        write = asyncio.create_task(policy(pool, uuid.uuid4()))
        await asyncio.sleep(0.2)
        assert not write.done()
    assert (await write).enabled


@pytest.mark.parametrize(("flags", "stored", "expected"), [
    ([], True, True), ([], False, False), (["--new-coins"], False, True), (["--no-new-coins"], True, False),
])
def test_new_coins_can_be_turned_off_and_stays_put_when_left_out(flags, stored, expected):
    args = pc._parser().parse_args(["enable", *flags])
    assert pc.wanted_policy(pc.Policy(allow_new_coins=stored), args, ("p",)).allow_new_coins is expected


async def test_a_failed_lock_write_rolls_back_the_scope_change(pool, monkeypatch):
    mid = await profile(pool)
    await policy(pool, mid)
    before = await scope(pool, mid)

    async def fail(*_):
        raise asyncpg.PostgresError("lock write failed")
    monkeypatch.setattr(pc.profile_lifecycle, "sync_locks", fail)
    [r] = await cycle(pool)
    assert r.outcome.startswith("APPLY_FAILED")
    assert await scope(pool, mid) == before and await pool.fetchval("SELECT count(*) FROM paper_coverage_ledger") == 0


async def test_the_cycle_limit_holds(pool):
    a = await profile(pool)
    b = await profile(pool, lock=False, instruments=(NEW,))
    await policy(pool, a, b, max_changes_per_cycle=1)
    outcomes = [r.outcome for r in await cycle(pool)]
    assert outcomes == ["APPLIED", "CYCLE_LIMIT"]


async def test_undo_lists_removes_and_keeps_a_lock_with_an_open_position(pool, monkeypatch):
    first = await profile(pool, direction="BOTH", markets=("SPOT", "PERP"), instruments=(WLD, WLD_PERP), strategies=pc.DEFAULT_PRESETS)
    await policy(pool, first, allow_new_coins=True)
    await cycle(pool, await _new_coin_redis())
    async with pool.acquire() as conn:
        items = await pc.items_in_place(conn)
    coin = next(i for i in items if i["item_kind"] == "COIN")

    async def in_use(*_):
        return {"MUBARAK"}
    monkeypatch.setattr(pc.profile_lifecycle, "_coins_in_use", in_use)
    report = await pc.undo(pool, {coin["id"]})
    assert "kept" in report[0]
    assert await pool.fetchval("SELECT count(*) FROM ai_profile_coin_locks WHERE base_asset = 'MUBARAK'") == 1
    monkeypatch.undo()
    report = await pc.undo(pool, {coin["id"]})
    assert "removed" in report[0]
    assert NEW not in (await scope(pool, first))["instruments"]
    assert await pool.fetchval("SELECT count(*) FROM ai_profile_coin_locks WHERE base_asset = 'MUBARAK'") == 0


async def pool_mandate(pool, mid):
    async with pool.acquire() as conn:
        return await load_mandate(conn, mid)


async def test_undo_leaves_what_a_later_manual_edit_changed(pool):
    mid = await profile(pool, direction="BOTH", markets=("SPOT", "PERP"), instruments=(WLD, WLD_PERP))
    await policy(pool, mid)
    await cycle(pool)
    await pool.execute("UPDATE ai_mandates SET strategies = array_remove(strategies, 'trend-pullback-short') WHERE id = $1", mid)
    async with pool.acquire() as conn:
        items = await pc.items_in_place(conn)
    assert "trend-pullback-short" not in {i["item_value"] for i in items}


def test_the_public_api_has_no_coverage_route_and_touches_no_notifier_or_approval():
    source = open(pc.__file__).read()
    assert "ai_approvals" not in source and "notifications_outbox" not in source and "execution_commands" not in source
    from pathlib import Path
    routes = Path(pc.__file__).parents[2] / "api" / "cryptonite_api" / "routes"
    assert not [f for f in routes.glob("*.py") if pc.POLICY_KEY in f.read_text()]
