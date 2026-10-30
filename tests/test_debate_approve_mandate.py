"""Approving a debate from the Inbox: the mandate it trades in, and its size as a share of that mandate's budget.

When more than one ACTIVE mandate allows the instrument, approve is refused as AMBIGUOUS_MANDATE until the operator
names one (``mandate_id``, validated: ACTIVE, allows the instrument, and no other owner holds it), and the Inbox's
dry run (``GET /debates/{id}/sizing``) lists the mandates to choose from, as a Brain card does (#101). A BUY is sized
as a share of the chosen mandate's budget, cut to its room, never of account equity. Runs on a throwaway migrated
database and the Redis at REDIS_URL, as test_debate_mandate_routing does.
"""

import json
import time
import uuid

import pytest
from ai_api_harness import close_harness, make_harness
from cryptonite_execution.mandate_store import load_mandate
from cryptonite_schemas.marks import quote_tick_key
from migrated_db import migrated_database

BTC = "BTCUSDT.BINANCE"
ETH = "ETHUSDT.BINANCE"
WLD = "WLDUSDT.BINANCE"
APPROVE = "/api/v1/agents/debates/{run}/approve"
SIZING = "/api/v1/agents/debates/{run}/sizing"


@pytest.fixture(scope="module")
def db_url():
    with migrated_database("cryptonite_ci_debate_mandate") as url:
        yield url


@pytest.fixture
async def h(db_url):
    harness = await make_harness(db_url)
    # names are unique: an earlier test's mandates give theirs up, so each test can use the case's names
    await harness.pool.execute("UPDATE ai_mandates SET status = 'ARCHIVED', name = id::text")
    await harness.pool.execute(
        "TRUNCATE ai_approvals, notifications_outbox, execution_commands, paper_orders, paper_positions CASCADE")
    await harness.pool.execute("UPDATE paper_accounts SET total_equity = 99800")
    await harness.redis.set("market:24h:ETHUSDT", json.dumps({"last_price": 2500}), ex=60)
    await harness.redis.set("market:24h:BTCUSDT", json.dumps({"last_price": 50000}), ex=60)
    for instrument, mid in ((ETH, 2500), (BTC, 50000)):
        await harness.redis.set(quote_tick_key(instrument), json.dumps({
            "instrument_id": instrument, "bid_price": str(mid - 1), "ask_price": str(mid + 1),
            "arrival_ns": time.time_ns(),
        }), ex=60)
    try:
        yield harness
    finally:
        await harness.redis.delete(quote_tick_key(ETH), quote_tick_key(BTC))
        await close_harness(harness)


async def mandate(h, *, name: str, instruments, limit: int, budget: int = 10000, status: str = "ACTIVE"):
    m = await h.mandate(instruments=instruments, name=name, budget=budget, status=status)
    await h.pool.execute("UPDATE ai_mandates SET max_position_pct = $2 WHERE id = $1", m.id, limit)
    async with h.pool.acquire() as conn:
        return await load_mandate(conn, m.id)


async def hold(h, m, instrument: str, qty: float, price: float) -> None:
    """An open position at its stored price: what the mandate's room counts (entry_room_pct)."""
    await h.pool.execute(
        """INSERT INTO paper_positions (position_id, account_id, instance_id, instrument_id, side, entry_price,
               quantity, current_price, status)
           VALUES ($1, 'PAPER-ACCOUNT-001', $2, $3, 'LONG', $4, $5, $4, 'OPEN')""",
        uuid.uuid4().hex, m.instance_id, instrument, price, qty)


async def debate(h, instrument: str = ETH, *, recommendation: str = "BUY", allocation_pct: float = 10,
                 mandate_id: uuid.UUID | None = None) -> uuid.UUID:
    """A finished debate waiting for the operator, as the debate worker leaves one outside any single mandate."""
    run_id = uuid.uuid4()
    spec_id, version_id, scan_id, candidate_id = (uuid.uuid4() for _ in range(4))
    price = 50000 if instrument == BTC else 2500
    await h.pool.execute("INSERT INTO scan_specs (id, slug, name) VALUES ($1, $2, $3)",
                         spec_id, f"h1-mandate-{spec_id.hex}", "H1 mandate test")
    await h.pool.execute(
        """INSERT INTO scan_spec_versions (id, spec_id, version_num, spec_hash, universe, rules)
           VALUES ($1, $2, 1, 'h1-mandate-test', '{}'::jsonb, '[]'::jsonb)""", version_id, spec_id)
    await h.pool.execute("UPDATE scan_specs SET active_version_id = $2 WHERE id = $1", spec_id, version_id)
    await h.pool.execute(
        """INSERT INTO scan_runs (id, spec_version_id, spec_hash, scanner_version, indicator_version,
               input_watermark_ns, source_partitions, source_partition_checksums, trigger_reason)
           VALUES ($1, $2, 'h1-mandate-test', 'test', 'test', 1, '{}'::jsonb, '{}'::jsonb, 'MANUAL')""",
        scan_id, version_id)
    await h.pool.execute(
        """INSERT INTO signal_candidates (id, scan_run_id, spec_version_id, instrument_id, timeframe,
               bar_open_ns, bar_close_inclusive_ns, interval_end_exclusive_ns, matched_rules, metrics,
               suggested_entry, suggested_stop, suggested_target)
           VALUES ($1, $2, $3, $4, '1h', 1, 2, 3, '[]'::jsonb,
                   '{"stop_type":"atr_fixed","stop_distance":100}'::jsonb, $5, $6, $7)""",
        candidate_id, scan_id, version_id, instrument, price, price * 0.95, price * 1.04)
    await h.pool.execute(
        """INSERT INTO debate_runs (id, instrument_id, status, trigger_source, candidate_id, mandate_id)
           VALUES ($1, $2, 'PENDING_APPROVAL', 'test', $3, $4)""", run_id, instrument, candidate_id, mandate_id)
    await h.pool.execute(
        """INSERT INTO portfolio_decisions (run_id, instrument_id, recommendation, conviction_score, allocation_pct, reasoning)
           VALUES ($1, $2, $3, 0.7, $4, 'r')""", run_id, instrument, recommendation, allocation_pct)
    return run_id


async def approve(h, run_id, **body):
    async with h.client() as c:
        return await c.post(APPROVE.format(run=run_id), headers=h.headers, json=body or None)


async def sizing(h, run_id, mandate_id=None):
    params = {"mandate_id": str(mandate_id)} if mandate_id else None
    async with h.client() as c:
        res = await c.get(SIZING.format(run=run_id), params=params)
    assert res.status_code == 200, res.text
    return res.json()


async def written(h) -> dict:
    return {"approvals": await h.pool.fetchval("SELECT count(*) FROM ai_approvals"),
            "commands": await h.pool.fetchval("SELECT count(*) FROM execution_commands")}


async def command_payload(h) -> dict:
    return json.loads(await h.pool.fetchval("SELECT payload FROM execution_commands WHERE command_type = 'SUBMIT_ORDER'"))


async def the_eth_case(h):
    """2026-10-21: "BTC-ETH spot L1" (10% limit, ~9.7% of its budget in BTC) and "3 assets" (20% limit, empty) both
    allow ETH, so the ETH BUY debate named neither."""
    l1 = await mandate(h, name="BTC-ETH spot L1", instruments=(BTC, ETH), limit=10)
    await hold(h, l1, BTC, 0.0194, 50000)  # $970: 9.7% of $10,000
    three = await mandate(h, name="3 assets", instruments=(BTC, ETH, WLD), limit=20, budget=20000)
    return l1, three


# --- AMBIGUOUS_MANDATE: the Inbox picks the mandate ---------------------------------------------------------------

async def test_two_mandates_on_the_coin_refuse_approve_without_a_choice_and_the_dry_run_lists_both(h):
    l1, three = await the_eth_case(h)
    run_id = await debate(h)

    res = await approve(h, run_id, allocation_pct=10)
    assert res.status_code == 409
    assert res.json()["detail"] == (f"AMBIGUOUS_MANDATE: more than one ACTIVE mandate allows {ETH}; nothing was "
                                    "queued; choose the mandate in the Inbox")
    assert await written(h) == {"approvals": 0, "commands": 0}
    assert await h.pool.fetchval("SELECT status FROM debate_runs WHERE id = $1", run_id) == "PENDING_APPROVAL"

    body = await sizing(h, run_id)
    assert body["ok"] is False and body["code"] == "AMBIGUOUS_MANDATE" and body["detail"] == res.json()["detail"]
    assert body["remedy"] == {"kind": "CHOOSE_MANDATE", "candidates": [
        {"id": str(l1.id), "name": "BTC-ETH spot L1", "autonomy_level": 1, "instruments": [BTC, ETH],
         "max_position_pct": 10.0, "capital_budget_usd": 10000.0, "room_pct": 10.0, "allocation_pct": 10.0,
         "holds": False, "blocked": None, "tidy": "REMOVE_INSTRUMENT"},
        {"id": str(three.id), "name": "3 assets", "autonomy_level": 1, "instruments": [BTC, ETH, WLD],
         "max_position_pct": 20.0, "capital_budget_usd": 20000.0, "room_pct": 20.0, "allocation_pct": 10.0,
         "holds": False, "blocked": None, "tidy": "REMOVE_INSTRUMENT"}]}
    assert await written(h) == {"approvals": 0, "commands": 0}  # the dry run writes nothing


async def test_approve_with_a_chosen_mandate_queues_in_it_sized_by_its_budget(h):
    _, three = await the_eth_case(h)
    run_id = await debate(h)
    pre = await sizing(h, run_id, three.id)
    assert pre["ok"] is True and pre["mandate"]["name"] == "3 assets"
    assert (pre["allocation_pct"], pre["budget_usd"], pre["room_usd"]) == (10.0, 2000.0, 4000.0)

    res = await approve(h, run_id, allocation_pct=10, mandate_id=str(three.id))
    assert res.status_code == 200, res.text
    assert res.json()["mandate_id"] == str(three.id) and res.json()["budget_usd"] == 2000.0
    p = await command_payload(h)
    assert p["mandate_id"] == str(three.id) and p["allocation_pct"] == 10.0
    assert "quantity" not in p  # the orchestrator sizes it from the mandate's budget and room, as a Brain card's
    cmd = await h.pool.fetchrow("SELECT target_instance_id, mandate_id FROM execution_commands")
    assert (cmd["target_instance_id"], cmd["mandate_id"]) == (three.instance_id, three.id)
    summary = await h.pool.fetchval("SELECT summary FROM ai_approvals WHERE source = 'debate'")
    assert summary == f"Debate verdict: BUY {ETH} at 10% of mandate 3 assets ($2,000)"
    assert await h.pool.fetchval("SELECT status FROM debate_runs WHERE id = $1", run_id) == "COMPLETED"


async def test_a_chosen_mandate_that_does_not_allow_the_coin_or_is_not_active_is_refused(h):
    await the_eth_case(h)
    btc_only = await mandate(h, name="BTC only", instruments=(BTC,), limit=25)
    paused = await mandate(h, name="paused ETH", instruments=(ETH,), limit=25, status="PAUSED")
    run_id = await debate(h)

    for chosen, code in ((btc_only, "MANDATE_INSTRUMENT_NOT_ALLOWED"), (paused, "MANDATE_HALTED")):
        res = await approve(h, run_id, mandate_id=str(chosen.id))
        assert res.status_code == 409 and res.json()["detail"].startswith(f"{code}: "), res.text
        assert (await sizing(h, run_id, chosen.id))["code"] == code
    res = await approve(h, run_id, mandate_id=str(uuid.uuid4()))
    assert res.status_code == 409 and res.json()["detail"].startswith("MANDATE_NOT_FOUND: ")
    assert (await approve(h, run_id, mandate_id="not-a-uuid")).status_code == 422
    assert await written(h) == {"approvals": 0, "commands": 0}
    assert await h.pool.fetchval("SELECT status FROM debate_runs WHERE id = $1", run_id) == "PENDING_APPROVAL"


async def test_the_mandate_that_holds_the_coin_is_the_only_choice(h):
    holder = await mandate(h, name="holder", instruments=(BTC, ETH), limit=10)
    other = await mandate(h, name="other", instruments=(ETH,), limit=20)
    await hold(h, holder, ETH, 0.2, 2500)  # $500 of its $1,000 limit
    run_id = await debate(h)

    candidates = (await sizing(h, run_id))["remedy"]["candidates"]
    assert [(c["name"], c["holds"], c["blocked"], c["tidy"], c["allocation_pct"]) for c in candidates] == [
        ("holder", True, None, None, 5.0), ("other", False, f"holder holds {ETH}, so only it can add to it", "PAUSE", 10.0)]
    res = await approve(h, run_id, mandate_id=str(other.id))
    assert res.status_code == 409 and res.json()["detail"] == (
        f"MANDATE_INSTRUMENT_CONFLICT: holder holds {ETH}; mandate other cannot add to it, so nothing was queued; "
        "reject this debate")
    assert await written(h) == {"approvals": 0, "commands": 0}

    res = await approve(h, run_id, mandate_id=str(holder.id))
    assert res.status_code == 200, res.text
    assert (await command_payload(h))["allocation_pct"] == 5.0  # cut to the room left under its 10% limit


async def test_a_debate_routed_into_its_mandate_cannot_be_moved_to_another(h):
    l1, three = await the_eth_case(h)
    run_id = await debate(h, mandate_id=l1.id)
    res = await approve(h, run_id, mandate_id=str(three.id))
    assert res.status_code == 409 and res.json()["detail"].startswith("MANDATE_MISMATCH: ")
    assert (await approve(h, run_id, mandate_id=str(l1.id))).status_code == 200


# --- Sizing: a share of the mandate's budget, not of account equity -------------------------------------------------

async def test_a_debate_buy_is_sized_by_the_mandate_budget_not_by_equity(h):
    """Account equity $99,800; mandate budget $10,000. 10% is $1,000 of the mandate, not $9,980 of equity."""
    m = await mandate(h, name="ETH", instruments=(ETH,), limit=25)
    run_id = await debate(h, allocation_pct=10)
    pre = await sizing(h, run_id)
    assert pre == {"ok": True, "side": "BUY",
                   "mandate": {"id": str(m.id), "name": "ETH", "autonomy_level": 1, "capital_budget_usd": 10000.0,
                               "max_position_pct": 25.0},
                   "room_pct": 25.0, "room_usd": 2500.0, "requested_pct": 10.0, "allocation_pct": 10.0,
                   "budget_usd": 1000.0, "sell_quantity": None}

    res = await approve(h, run_id)
    assert res.status_code == 200, res.text
    assert res.json()["budget_usd"] == 1000.0 and "quantity" not in res.json()
    p = await command_payload(h)
    assert (p["allocation_pct"], p["mandate_id"], p["order_type"], p["price"]) == (10.0, str(m.id), "LIMIT", 2500.0)


async def test_the_allocation_is_cut_to_the_mandate_room_and_kept_within_1_to_25_percent(h):
    m = await mandate(h, name="ETH", instruments=(ETH,), limit=25)
    await hold(h, m, ETH, 0.6, 2500)  # $1,500 of the $2,500 limit: 10% of the budget left
    run_id = await debate(h, allocation_pct=40)  # the model asked for more than the 25% ceiling
    pre = await sizing(h, run_id)
    assert (pre["requested_pct"], pre["room_pct"], pre["allocation_pct"], pre["room_usd"]) == (25.0, 10.0, 10.0, 1000.0)
    assert (await sizing(h, run_id))["budget_usd"] == 1000.0

    assert (await approve(h, run_id, allocation_pct=30)).status_code == 422  # the operator's slider stops at 25%
    assert (await approve(h, run_id, allocation_pct=0.5)).status_code == 422
    res = await approve(h, run_id, allocation_pct=20)
    assert res.status_code == 200, res.text
    assert (await command_payload(h))["allocation_pct"] == 10.0


async def test_a_mandate_at_its_limit_refuses_the_buy(h):
    m = await mandate(h, name="ETH", instruments=(ETH,), limit=10)
    await hold(h, m, ETH, 0.4, 2500)  # $1,000: the whole 10% limit
    run_id = await debate(h)
    res = await approve(h, run_id)
    assert res.status_code == 409 and res.json()["detail"].startswith("MANDATE_POSITION_LIMIT_REACHED: ")
    assert (await sizing(h, run_id))["code"] == "MANDATE_POSITION_LIMIT_REACHED"
    assert await written(h) == {"approvals": 0, "commands": 0}


async def test_a_debate_sell_still_sells_the_mandate_holding(h):
    m = await mandate(h, name="ETH", instruments=(ETH,), limit=25)
    await h.pool.execute(
        """INSERT INTO paper_orders (client_order_id, instance_id, account_id, instrument_id, side, order_type,
               quantity, filled_qty, avg_fill_price, status)
           VALUES ('held-1', $1, 'PAPER-ACCOUNT-001', $2, 'BUY', 'MARKET', 0.3, 0.3, 2500, 'FILLED')""",
        m.instance_id, ETH)
    run_id = await debate(h, recommendation="SELL")
    pre = await sizing(h, run_id)
    assert (pre["ok"], pre["side"], pre["room_pct"], pre["sell_quantity"]) == (True, "SELL", None, "0.3")
    res = await approve(h, run_id)
    assert res.status_code == 200, res.text
    p = await command_payload(h)
    assert (p["side"], p["quantity"], p["order_type"]) == ("SELL", 0.3, "MARKET")
    assert "allocation_pct" not in p and "price" not in p
