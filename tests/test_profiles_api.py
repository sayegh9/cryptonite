"""Focused API tests for the Auto-Trade profile read models and Stop all action."""

from __future__ import annotations

import base64
import datetime as dt
import json
import uuid
from decimal import Decimal

import pytest
from ai_api_harness import close_harness, make_harness
from cryptonite_schemas.mandate import mandate_instance_id
from migrated_db import migrated_database

D = Decimal
UTC = dt.UTC
ACCOUNT = "PAPER-ACCOUNT-001"
BASE = "/api/v1/ai/profiles"
MARGIN = "BTCUSDT-MARGIN.BINANCE"


@pytest.fixture(scope="module")
def db_url():
    with migrated_database("cryptonite_ci_p21_profiles") as url:
        yield url


@pytest.fixture
async def h(db_url):
    harness = await make_harness(db_url)
    await harness.pool.execute("UPDATE ai_mandates SET status = 'ARCHIVED'")
    await harness.pool.execute("DELETE FROM ai_profile_coin_locks")
    await harness.pool.execute("DELETE FROM system_settings WHERE key LIKE 'autotrade.%'")
    await harness.pool.execute("DELETE FROM debate_runs")
    try:
        yield harness
    finally:
        await close_harness(harness)


async def new_profile(h, *, status="ACTIVE", instruments=(MARGIN,), budget=1000):
    return await h.mandate(status=status, instruments=instruments, budget=budget)


@pytest.mark.parametrize(
    "path",
    [
        f"{BASE}/00000000-0000-0000-0000-000000000001/decisions",
        f"{BASE}/00000000-0000-0000-0000-000000000001/summary",
        f"{BASE}/00000000-0000-0000-0000-000000000001/performance",
        f"{BASE}/presets/stats",
        f"{BASE}/estimate?coins=BTCUSDT&style=SWING&direction=LONG",
    ],
)
async def test_every_read_route_requires_an_operator_session(h, path):
    async with h.client() as client:
        client.cookies.clear()
        response = await client.get(path)
    assert response.status_code == 401


async def test_decisions_summary_and_performance_response_shapes(h):
    profile = await new_profile(h)
    await h.pool.execute(
        """INSERT INTO ai_profile_decisions (mandate_id, instrument_id, stage, outcome, reason, detail)
           VALUES ($1, $2, 'GATE', 'ACCEPTED', 'eligible', '{\"source\":\"test\"}'::jsonb)""",
        profile.id, MARGIN,
    )
    async with h.client() as client:
        decisions = await client.get(f"{BASE}/{profile.id}/decisions")
        summary = await client.get(f"{BASE}/{profile.id}/summary")
        performance = await client.get(f"{BASE}/{profile.id}/performance?range=7d")

    assert decisions.status_code == 200
    decision_body = decisions.json()
    assert set(decision_body) == {"profile_id", "decisions", "next_cursor", "has_more"}
    assert decision_body["decisions"][0]["detail"] == {"source": "test"}
    assert summary.status_code == 200
    summary_body = summary.json()
    assert {"today", "open_positions_count", "last_decision", "breaker", "dry_run", "mode",
            "master_switch", "health_score"} <= summary_body.keys()
    components = summary_body["health_score"]["components"]
    assert set(components) == {"freshness", "recent_failures", "breaker"}
    assert components["freshness"]["points"] == 40
    assert components["recent_failures"]["points"] == 30
    assert components["breaker"]["points"] == 30
    assert summary_body["health_score"]["score"] == 100
    assert performance.status_code == 200
    assert {"equity_curve", "fills", "exit_outcomes", "stats"} <= performance.json().keys()


async def test_decision_cursor_pages_by_timestamp_and_id(h):
    profile = await new_profile(h)
    timestamp = dt.datetime.now(UTC) - dt.timedelta(minutes=1)
    await h.pool.executemany(
        """INSERT INTO ai_profile_decisions (mandate_id, ts, stage, outcome, detail)
           VALUES ($1, $2, 'GATE', 'NO_TRADE', '{}'::jsonb)""",
        [(profile.id, timestamp + dt.timedelta(seconds=offset)) for offset in range(3)],
    )
    async with h.client() as client:
        first = await client.get(f"{BASE}/{profile.id}/decisions?limit=2")
        second = await client.get(f"{BASE}/{profile.id}/decisions?limit=2&cursor={first.json()['next_cursor']}")
    assert first.status_code == second.status_code == 200
    assert len(first.json()["decisions"]) == 2 and first.json()["has_more"] is True
    assert len(second.json()["decisions"]) == 1 and second.json()["has_more"] is False


async def test_decision_cursor_rejects_ids_outside_postgres_bigint_range(h):
    profile = await new_profile(h)
    raw = f"{dt.datetime.now(UTC).isoformat()}.9223372036854775808".encode()
    cursor = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    async with h.client() as client:
        response = await client.get(f"{BASE}/{profile.id}/decisions", params={"cursor": cursor})
    assert response.status_code == 422


async def test_stop_all_requires_csrf_and_only_stops_active_or_paused(h):
    active = await new_profile(h, status="ACTIVE")
    paused = await new_profile(h, status="PAUSED", instruments=("ETHUSDT-MARGIN.BINANCE",))
    stopped = await new_profile(h, status="STOPPED", instruments=("XRPUSDT-MARGIN.BINANCE",))
    archived = await new_profile(h, status="ARCHIVED", instruments=("WLDUSDT-MARGIN.BINANCE",))
    # Keeping an open position proves that Stop all requests the established exit path rather than rejecting it.
    await h.pool.execute(
        """INSERT INTO paper_positions (position_id, account_id, instance_id, instrument_id, side,
               entry_price, quantity, current_price, unrealized_pnl, status, opened_at, leverage, margin_mode)
           VALUES ($1, $2, $3, $4, 'LONG', 100, 1, 101, 1, 'OPEN', now(), 2, 'ISOLATED')""",
        str(uuid.uuid4()), ACCOUNT, mandate_instance_id(active.id), MARGIN,
    )

    async with h.client() as client:
        client.cookies.clear()
        unauthenticated = await client.post(f"{BASE}/stop-all")
        assert unauthenticated.status_code == 401
    async with h.client() as client:
        csrf_missing = await client.post(f"{BASE}/stop-all")
        assert csrf_missing.status_code == 403
        accepted = await client.post(f"{BASE}/stop-all", headers=h.headers)
    assert accepted.status_code == 200
    result = accepted.json()
    assert result["selected_count"] == 2
    assert result["requested_count"] == 2 and result["refused_count"] == 0
    rows = {row["id"]: row for row in result["profiles"]}
    assert rows[str(active.id)]["status"] == "STOPPING"
    assert rows[str(paused.id)]["status"] == "STOPPED"
    assert await h.pool.fetchval("SELECT status FROM ai_mandates WHERE id=$1", stopped.id) == "STOPPED"
    assert await h.pool.fetchval("SELECT status FROM ai_mandates WHERE id=$1", archived.id) == "ARCHIVED"
    assert await h.pool.fetchval(
        "SELECT count(*) FROM paper_positions WHERE instance_id=$1 AND status='OPEN'",
        mandate_instance_id(active.id),
    ) == 1


async def _insert_exit_fill(h, profile, *, r_multiple, realized_pnl, order_suffix):
    command_id = uuid.uuid4()
    client_order_id = f"p21-{order_suffix}-{command_id.hex[:12]}"
    await h.pool.execute(
        """INSERT INTO execution_commands (command_id, idempotency_key, command_type, mandate_id, payload)
           VALUES ($1, $2, 'SUBMIT_ORDER', $3, $4::jsonb)""",
        command_id, str(command_id), profile.id, json.dumps({"client_order_id": client_order_id}),
    )
    await h.pool.execute(
        """INSERT INTO paper_orders (client_order_id, instance_id, account_id, instrument_id, side,
                   order_type, quantity, price, filled_qty, avg_fill_price, status)
           VALUES ($1, $2, $3, $4, 'SELL', 'MARKET', 1, 110, 1, 110, 'FILLED')""",
        client_order_id, mandate_instance_id(profile.id), ACCOUNT, MARGIN,
    )
    await h.pool.execute(
        """INSERT INTO paper_trades (trade_id, account_id, client_order_id, instance_id, instrument_id,
                   side, price, quantity, commission, commission_currency, executed_at)
           VALUES ($1, $2, $3, $4, $5, 'SELL', 110, 1, 0.10, 'USDT', now())""",
        f"TRADE-{order_suffix}-{command_id.hex[:12]}", ACCOUNT, client_order_id,
        mandate_instance_id(profile.id), MARGIN,
    )
    detail = {"realized_pnl": realized_pnl, "r_multiple": r_multiple}
    await h.pool.execute(
        """INSERT INTO ai_profile_decisions (mandate_id, instrument_id, stage, outcome, detail, command_id)
           VALUES ($1, $2, 'EXIT', 'TAKE_PROFIT', $3::jsonb, $4)""",
        profile.id, MARGIN, json.dumps(detail), command_id,
    )


async def test_performance_counts_null_r_and_includes_margin_interest(h):
    profile = await new_profile(h, instruments=(MARGIN,))
    await _insert_exit_fill(h, profile, r_multiple=None, realized_pnl="-2.00", order_suffix="unknown")
    await _insert_exit_fill(h, profile, r_multiple="1.50", realized_pnl="3.00", order_suffix="known")
    now = dt.datetime.now(UTC)
    margin_id = await h.pool.fetchval(
        """INSERT INTO paper_margin_accounts (account_id, pair, collateral_usdt, borrowed_quote, status)
           VALUES ($1, 'BTCUSDT', 100, 10, 'OPEN') RETURNING id""",
        ACCOUNT,
    )
    position_id = await h.pool.fetchval(
        """INSERT INTO paper_positions (position_id, account_id, instance_id, instrument_id, side,
                   entry_price, quantity, current_price, status, opened_at, leverage, margin_mode)
           VALUES ($1, $2, $3, $4, 'LONG', 100, 1, 101, 'OPEN', $5, 2, 'ISOLATED') RETURNING position_id""",
        str(uuid.uuid4()), ACCOUNT, mandate_instance_id(profile.id), MARGIN, now - dt.timedelta(hours=2),
    )
    await h.pool.execute(
        """INSERT INTO paper_margin_interest
               (margin_account_id, period_start, period_end, asset, principal, hourly_rate, amount)
           VALUES ($1, $2, $3, 'USDT', 10, 0.001, 0.05),
                  ($1, $2, $3, 'BTC', 0.1, 0.001, 0.00005)""",
        margin_id, now - dt.timedelta(hours=1), now - dt.timedelta(minutes=30),
    )
    # Historical costs remain attributable after the profile's current instrument selection changes.
    await h.pool.execute(
        "UPDATE ai_mandates SET instruments = ARRAY['ETHUSDT-MARGIN.BINANCE']::text[] WHERE id = $1",
        profile.id,
    )
    assert position_id

    async with h.client() as client:
        response = await client.get(f"{BASE}/{profile.id}/performance?range=30d")
    assert response.status_code == 200
    stats = response.json()["stats"]
    assert stats["audited_exit_count"] == 2
    assert stats["r_unknown"] == 1 and stats["r_known"] == 1
    assert stats["avg_r"] == "1.5"
    assert stats["margin_interest_by_asset"] == {"BTC": "0.00005", "USDT": "0.05"}
    assert stats["fees"]["total_amount"] == "0.2"
    assert len(response.json()["fills"]) == 2


async def test_performance_keeps_profile_fills_without_exit_audit_attribution(h):
    profile = await new_profile(h, instruments=("BTCUSDT-PERP.BINANCE",))
    client_order_id = f"CLOSE-{uuid.uuid4().hex[:8].upper()}"
    await h.pool.execute(
        """INSERT INTO paper_orders (client_order_id, instance_id, account_id, instrument_id, side,
                   order_type, quantity, price, filled_qty, avg_fill_price, status)
           VALUES ($1, $2, $3, 'BTCUSDT-PERP.BINANCE', 'SELL', 'MARKET', 1, 90, 1, 90, 'FILLED')""",
        client_order_id, mandate_instance_id(profile.id), ACCOUNT,
    )
    await h.pool.execute(
        """INSERT INTO paper_trades (trade_id, account_id, client_order_id, instance_id, instrument_id,
                   side, price, quantity, commission, commission_currency, executed_at)
           VALUES ('P2-CLOSE-FILL', $1, $2, $3, 'BTCUSDT-PERP.BINANCE', 'SELL', 90, 1, 0.1, 'USDT', now())""",
        ACCOUNT, client_order_id, mandate_instance_id(profile.id),
    )
    async with h.client() as client:
        response = await client.get(f"{BASE}/{profile.id}/performance?range=7d")
    assert response.status_code == 200
    body = response.json()
    assert [row["trade_id"] for row in body["fills"]] == ["P2-CLOSE-FILL"]
    assert body["exit_outcomes"] == []
    assert body["stats"]["audited_exit_count"] == 0
    assert body["stats"]["outcome_coverage"] == {
        "scope": "EXIT audit decisions only",
        "complete": False,
        "profile_fill_count": 1,
        "audited_exit_count": 0,
        "note": (
            "All profile-owned fills are listed separately. Some close paths do not persist a durable one-to-one "
            "link between a fill and its EXIT audit row, so outcome metrics cover only audited exits."
        ),
    }


async def test_performance_downsamples_full_equity_range_and_marks_curve(h):
    profile = await new_profile(h)
    start = dt.datetime.now(UTC) - dt.timedelta(days=10)
    await h.pool.executemany(
        """INSERT INTO ai_mandate_equity (mandate_id, ts, equity, hwm, drawdown_pct)
           VALUES ($1, $2, $3, 100, $4)""",
        [
            (profile.id, start + dt.timedelta(seconds=index), D("100") - D(index % 10), D(index % 10))
            for index in range(5005)
        ],
    )
    async with h.client() as client:
        response = await client.get(f"{BASE}/{profile.id}/performance?range=all")
    assert response.status_code == 200
    body = response.json()
    assert len(body["equity_curve"]) == 5000
    assert body["equity_curve_sampled"] is True
    assert body["equity_curve"][0]["ts"] == start.isoformat()
    assert body["equity_curve"][-1]["ts"] == (start + dt.timedelta(seconds=5004)).isoformat()


async def test_performance_includes_funding_from_profile_instance_events(h):
    profile = await new_profile(h, instruments=("BTCUSDT-PERP.BINANCE",))
    ts_ns = int(dt.datetime.now(UTC).timestamp() * 1_000_000_000)
    event_id = uuid.uuid4()
    payload = {"instance_id": mandate_instance_id(profile.id), "cash_flow": "-1.25"}
    await h.pool.execute(
        """INSERT INTO paper_execution_events
               (engine_run_id, native_event_id, idempotency_key, event_id, client_order_id,
                event_type, payload, occurred_at_ns)
           VALUES ('p21', $1, $2, $3, $4, 'FUNDING_SETTLED', $5::jsonb, $6)""",
        str(event_id), f"p21:{event_id}", event_id, f"FUND-{event_id.hex}", json.dumps(payload), ts_ns,
    )
    async with h.client() as client:
        response = await client.get(f"{BASE}/{profile.id}/performance?range=7d")
    assert response.status_code == 200
    assert response.json()["stats"]["funding"] == "-1.25"


async def test_preset_stats_computes_quality_labels_on_server(h):
    async with h.client() as client:
        response = await client.get(f"{BASE}/presets/stats")
    assert response.status_code == 200
    document = response.json()
    assert document["generated_at"] and document["source_command"]
    assert len(document["results"]) == 56
    for row in document["results"]:
        expected = "thin" if row["trades"] < 20 else "negative" if row["avg_r"] < 0 else "ok"
        assert row["quality"] == expected
    assert {row["quality"] for row in document["results"]} == {"thin", "negative", "ok"}


async def test_estimate_reports_not_in_feed_and_existing_onboarding_action(h):
    now = dt.datetime.now(UTC)
    run_rows = [
        (uuid.uuid4(), uuid.uuid4(), f"{symbol}.BINANCE", now - dt.timedelta(hours=hour))
        for symbol, count in (("BTCUSDT", 14), ("ETHUSDT", 7))
        for hour in range(count)
    ]
    await h.pool.executemany(
        """INSERT INTO debate_runs (id, candidate_id, instrument_id, status, trigger_source, created_at)
           VALUES ($1, $2, $3, 'NO_TRADE', 'profile', $4)""",
        run_rows,
    )
    async with h.client() as client:
        response = await client.get(f"{BASE}/estimate?coins=BTCUSDT,DOGEUSDT&style=SWING&direction=LONG")
    assert response.status_code == 200
    body = response.json()
    assert body["feed"]["ingest_max_symbols"] >= 1
    assert "BTCUSDT" in body["feed"]["coins_in_feed"]
    assert body["feed"]["not_in_feed"] == [{
        "coin": "DOGEUSDT", "code": "NOT_IN_FEED",
        "onboarding_action": {"method": "POST", "path": "/api/v1/instruments/onboard",
                              "body": {"symbol": "DOGEUSDT"}},
    }]
    assert body["estimate"]["debates_per_day"] == "3"
    assert body["estimate"]["llm_usd_per_day"] is not None
    assert "last 7 UTC days" in body["estimate"]["method"]
    assert "observed coin" in body["estimate"]["method"]


async def test_summary_recent_failures_require_profile_accepted_gate_attribution(h):
    profile = await new_profile(h)
    other = await new_profile(h, instruments=("ETHUSDT-MARGIN.BINANCE",))
    failed_run = uuid.uuid4()
    other_failed_run = uuid.uuid4()
    await h.pool.execute(
        """INSERT INTO debate_runs (id, instrument_id, status, trigger_source, created_at, completed_at, mandate_id)
           VALUES ($1, 'BTCUSDT.BINANCE', 'FAILED', 'profile', now(), now(), NULL),
                  ($2, 'ETHUSDT.BINANCE', 'FAILED', 'profile', now(), now(), NULL),
                  ($3, 'BTCUSDT.BINANCE', 'NO_TRADE', 'profile', now(), now(), $4),
                  ($5, 'BTCUSDT.BINANCE', 'FAILED', 'profile', now(), now(), $4)""",
        failed_run, other_failed_run, uuid.uuid4(), profile.id, uuid.uuid4(),
    )
    await h.pool.executemany(
        """INSERT INTO ai_profile_decisions (mandate_id, stage, outcome, run_id, detail)
           VALUES ($1, 'GATE', 'ACCEPTED', $2, '{}'::jsonb)""",
        [(profile.id, failed_run), (other.id, other_failed_run)],
    )
    async with h.client() as client:
        response = await client.get(f"{BASE}/{profile.id}/summary")
    assert response.status_code == 200
    recent = response.json()["health_score"]["components"]["recent_failures"]
    assert recent["failed_debates_24h"] == 1
    assert recent["points"] == 20
