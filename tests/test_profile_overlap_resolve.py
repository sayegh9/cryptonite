"""scripts/profile_overlap_resolve.py: coin overlaps among mandates, resolved before migration 050.

Runs on the throwaway database at DATABASE_URL, migrated to 049 (no profile columns). Seeds mandates, the paper
instances they trade under, and positions or orders, then checks the report, the guarded archive and instrument
change, the audit rows, and that the report exits 0 once the overlaps are gone.
"""

import importlib.util
import json
import os
import uuid
from pathlib import Path

import asyncpg
import pytest
from cryptonite_schemas.mandate import mandate_instance_id
from migrated_db import seed_paper_account

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "profile_overlap_resolve.py"
spec = importlib.util.spec_from_file_location("profile_overlap_resolve", SCRIPT)
resolve = importlib.util.module_from_spec(spec)
spec.loader.exec_module(resolve)

ACC = "PAPER-ACCOUNT-001"
SPOT_BTC = "BTCUSDT.BINANCE"
PERP_BTC = "BTCUSDT-PERP.BINANCE"
ETH = "ETHUSDT.BINANCE"
WLD = "WLDUSDT.BINANCE"
MANDATES = {
    "Brain BTCUSDT-PERP.BINANCE": ("ACTIVE", [PERP_BTC]),
    "BTC-ETH spot L1": ("PAUSED", [SPOT_BTC, ETH]),
    "3 assets": ("ACTIVE", [SPOT_BTC, ETH, WLD]),
    "WLDUSDT": ("HALTED", [WLD]),
}


def _url() -> str:
    url = os.environ.get("DATABASE_URL", "")
    name = url.rsplit("/", 1)[-1].split("?")[0]
    if not name or name == "cryptonite" or ("ci" not in name and "test" not in name):
        pytest.skip("needs a throwaway database (name with 'ci' or 'test'), never the dev DB")
    return url


@pytest.fixture
async def conn():
    c = await asyncpg.connect(_url())
    for t in (
        "audit_events",
        "paper_positions",
        "paper_orders",
        "paper_trades",
        "execution_commands",
        "ai_autonomy_events",
        "ai_mandate_equity",
        "ai_mandate_daily_sessions",
    ):
        await c.execute(f"DELETE FROM {t}")
    await c.execute("DELETE FROM paper_strategy_instances")
    await c.execute("DELETE FROM ai_mandates")
    await c.execute("DELETE FROM paper_accounts WHERE account_id = $1", ACC)
    await seed_paper_account(c, ACC)
    yield c
    await c.close()


async def add_mandate(c, name: str, status: str, instruments: list[str]) -> str:
    mid = await c.fetchval(
        """INSERT INTO ai_mandates (name, capital_budget_usd, instruments, max_position_pct, max_drawdown_pct,
               autonomy_level, status)
           VALUES ($1, 10000, $2::text[], 25, 10, 1, $3) RETURNING id""",
        name,
        instruments,
        status,
    )
    instance = mandate_instance_id(mid)
    strat = await c.fetchval(
        "SELECT strategy_version_id FROM paper_strategy_instances WHERE instance_id = 'MANUAL_TRADE'"
    )
    await c.execute(
        """INSERT INTO paper_strategy_instances (instance_id, account_id, strategy_version_id, strategy_source_hash,
               risk_policy_hash, strategy_name, instrument_id, timeframe, resolved_parameters, allocation_usd, status)
           VALUES ($1, $2, $3, 'h', 'h', 'mandate', 'SYSTEM.MANDATE', '1m', '{}'::jsonb, 0, 'SYSTEM')""",
        instance,
        ACC,
        strat,
    )
    return instance


async def add_position(
    c, instance: str, instrument: str, qty: str = "0.01", status: str = "OPEN"
) -> None:
    await c.execute(
        """INSERT INTO paper_positions (position_id, account_id, instance_id, instrument_id, side, entry_price,
               quantity, current_price, status)
           VALUES ($1, $2, $3, $4, 'LONG', 100, $5::numeric, 100, $6)""",
        uuid.uuid4().hex,
        ACC,
        instance,
        instrument,
        qty,
        status,
    )


async def add_order(c, instance: str, instrument: str, status: str = "ACCEPTED") -> None:
    await c.execute(
        """INSERT INTO paper_orders (client_order_id, instance_id, account_id, instrument_id, side, order_type,
               quantity, status)
           VALUES ($1, $2, $3, $4, 'SELL', 'LIMIT', 1, $5)""",
        uuid.uuid4().hex[:20],
        instance,
        ACC,
        instrument,
        status,
    )


async def run(argv: list[str], capsys) -> tuple[int, str]:
    capsys.readouterr()
    code = await resolve.main(resolve.parse_args(argv))
    return code, capsys.readouterr().out


async def seed_overlaps(c) -> dict[str, str]:
    """The live shape: BTC held by a perp and by two spot mandates, WLD by two. Returns name -> instance."""
    inst = {}
    for name, (status, instruments) in MANDATES.items():
        inst[name] = await add_mandate(c, name, status, instruments)
    return inst


def test_parse_instruments_canonicalises_and_dedupes():
    assert resolve.parse_instruments(" btcusdt , ETHUSDT.BINANCE,btcusdt ") == [SPOT_BTC, ETH]
    assert resolve.parse_instruments(" , ") == []


def test_find_overlaps_counts_spot_and_perp_on_one_base_and_ignores_archived():
    def m(name, status, instruments):
        return resolve.Mandate(uuid.uuid4(), name, status, instruments)

    mandates = [
        m("perp", "ACTIVE", [PERP_BTC]),
        m("spot", "PAUSED", [SPOT_BTC]),
        m("both", "ACTIVE", [SPOT_BTC, PERP_BTC]),
        m("old", "ARCHIVED", [WLD]),
        m("w", "ACTIVE", [WLD]),
    ]
    overlaps = resolve.find_overlaps(mandates)
    assert list(overlaps) == ["BTC"]
    assert {o.name for o in overlaps["BTC"]} == {"perp", "spot", "both"}


async def test_report_lists_overlaps_and_exits_1(conn, capsys):
    await seed_overlaps(conn)
    code, out = await run([], capsys)
    assert code == 1
    assert "\nBTC:\n" in out and "\nWLD:\n" in out
    assert "Brain BTCUSDT-PERP.BINANCE" in out and "BTC-ETH spot L1" in out and "3 assets" in out
    assert "[PAUSED]" in out and "[HALTED]" in out


async def test_report_flags_a_held_coin(conn, capsys):
    inst = await seed_overlaps(conn)
    await add_position(conn, inst["3 assets"], WLD)
    _, out = await run([], capsys)
    assert f"instance {inst['3 assets']}: HOLDS on WLD: {WLD}" in out


async def test_archive_refused_with_an_open_position(conn, capsys):
    inst = await seed_overlaps(conn)
    await add_position(conn, inst["WLDUSDT"], WLD)
    code, out = await run(["--archive", "WLDUSDT", "--confirm", "WLDUSDT"], capsys)
    assert code == 2
    assert f"{WLD} has open position" in out
    assert await conn.fetchval("SELECT status FROM ai_mandates WHERE name = 'WLDUSDT'") == "HALTED"
    assert await conn.fetchval("SELECT count(*) FROM audit_events") == 0


async def test_archive_refused_with_a_working_order(conn, capsys):
    inst = await seed_overlaps(conn)
    await add_order(conn, inst["WLDUSDT"], WLD, status="PARTIALLY_FILLED")
    code, out = await run(["--archive", "WLDUSDT", "--confirm", "WLDUSDT"], capsys)
    assert code == 2 and "working order" in out


async def test_zero_quantity_or_closed_position_does_not_block_archive(conn, capsys):
    inst = await seed_overlaps(conn)
    await add_position(conn, inst["WLDUSDT"], WLD, qty="0", status="OPEN")
    await add_position(conn, inst["WLDUSDT"], WLD, qty="1", status="CLOSED")
    code, _ = await run(["--archive", "WLDUSDT", "--confirm", "WLDUSDT"], capsys)
    assert code == 0


async def test_archive_succeeds_when_flat_and_writes_audit(conn, capsys):
    await seed_overlaps(conn)
    code, out = await run(
        ["--archive", "WLDUSDT", "--confirm", "WLDUSDT", "--reason", "o1 overlap"], capsys
    )
    assert code == 0 and "Applied PROFILE_OVERLAP_ARCHIVED" in out
    assert (
        await conn.fetchval("SELECT status FROM ai_mandates WHERE name = 'WLDUSDT'") == "ARCHIVED"
    )
    row = await conn.fetchrow("SELECT event_type, source, details FROM audit_events")
    details = json.loads(row["details"])
    assert row["event_type"] == "PROFILE_OVERLAP_ARCHIVED" and row["source"] == resolve.SOURCE
    assert details["status_before"] == "HALTED" and details["reason"] == "o1 overlap"
    assert details["actor"] == "operator:admin"


async def test_set_instruments_refuses_dropping_a_held_coin(conn, capsys):
    inst = await seed_overlaps(conn)
    await add_position(conn, inst["BTC-ETH spot L1"], SPOT_BTC)
    code, out = await run(
        ["--set-instruments", "BTC-ETH spot L1", "ETHUSDT", "--confirm", "BTC-ETH spot L1"], capsys
    )
    assert code == 2 and f"{SPOT_BTC} is dropped but held" in out
    assert await conn.fetchval(
        "SELECT instruments FROM ai_mandates WHERE name = 'BTC-ETH spot L1'"
    ) == [SPOT_BTC, ETH]


async def test_set_instruments_refuses_an_empty_list(conn, capsys):
    await seed_overlaps(conn)
    code, out = await run(
        ["--set-instruments", "BTC-ETH spot L1", " , ", "--confirm", "BTC-ETH spot L1"], capsys
    )
    assert code == 2 and "empty" in out


async def test_set_instruments_refuses_a_coin_another_mandate_owns(conn, capsys):
    await seed_overlaps(conn)
    code, out = await run(
        ["--set-instruments", "BTC-ETH spot L1", "ETHUSDT,WLDUSDT", "--confirm", "BTC-ETH spot L1"],
        capsys,
    )
    assert code == 2 and "WLD is already owned by" in out


async def test_set_instruments_applies_and_audits(conn, capsys):
    await seed_overlaps(conn)
    # ETH is also "3 assets"'s coin: that owner goes first (runbook order), then the spot L1 change applies
    await run(["--archive", "3 assets", "--confirm", "3 assets"], capsys)
    code, out = await run(
        ["--set-instruments", "BTC-ETH spot L1", "ETHUSDT", "--confirm", "BTC-ETH spot L1"], capsys
    )
    assert code == 0
    assert await conn.fetchval(
        "SELECT instruments FROM ai_mandates WHERE name = 'BTC-ETH spot L1'"
    ) == [ETH]
    details = json.loads(
        await conn.fetchval(
            "SELECT details FROM audit_events WHERE event_type = 'PROFILE_OVERLAP_INSTRUMENTS_SET'"
        )
    )
    assert details["instruments_before"] == [SPOT_BTC, ETH]
    assert details["instruments_after"] == [ETH]


async def test_confirm_mismatch_changes_nothing(conn, capsys):
    await seed_overlaps(conn)
    before = await conn.fetch("SELECT name, status, instruments FROM ai_mandates ORDER BY name")
    code, out = await run(["--archive", "WLDUSDT", "--confirm", "wldusdt"], capsys)
    assert code == 2 and "does not match" in out
    code, _ = await run(
        ["--set-instruments", "BTC-ETH spot L1", "ETHUSDT", "--confirm", "BTC"], capsys
    )
    assert code == 2
    assert (
        await conn.fetch("SELECT name, status, instruments FROM ai_mandates ORDER BY name")
        == before
    )
    assert await conn.fetchval("SELECT count(*) FROM audit_events") == 0


async def test_dry_run_prints_and_changes_nothing(conn, capsys):
    await seed_overlaps(conn)
    code, out = await run(["--archive", "WLDUSDT"], capsys)
    assert code == 0 and "Dry run: nothing changed" in out
    assert await conn.fetchval("SELECT status FROM ai_mandates WHERE name = 'WLDUSDT'") == "HALTED"


async def test_after_resolving_the_report_exits_0(conn, capsys):
    await seed_overlaps(conn)
    assert (await run([], capsys))[0] == 1
    # the runbook's resolution: keep the perp for BTC, spot L1 keeps ETH only, archive "3 assets" and WLDUSDT
    await run(["--archive", "3 assets", "--confirm", "3 assets"], capsys)
    await run(["--archive", "WLDUSDT", "--confirm", "WLDUSDT"], capsys)
    code, _ = await run(
        ["--set-instruments", "BTC-ETH spot L1", "ETHUSDT", "--confirm", "BTC-ETH spot L1"], capsys
    )
    assert code == 0
    code, out = await run([], capsys)
    assert code == 0 and "passes" in out
