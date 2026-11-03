"""H11 TP1 fixes: a spot TP1 is an owner-scoped CLOSE_POSITION (the spot SUBMIT_ORDER SELL had no mandate and was
refused MANDATE_REQUIRED on every attempt since 2026-10-18), and ``exit:tp1:<key>`` is set only once the reduction
filled, so a rejected one is retried (bounded) instead of suppressing TP1 for good."""

import json
import time
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from cryptonite_orchestrator import server
from cryptonite_orchestrator.server import SimpleQuoteCache
from cryptonite_scanner_service import exit_monitor as em
from cryptonite_scanner_service.exit_monitor import ExitAction, Position
from test_orchestrator_perp_wiring import command_outcomes, make_engine
from test_paper_risk_guardrails import make_fresh_dispatcher
from test_partial_perp_close import run

D = Decimal
BTC = "BTCUSDT.BINANCE"
OWNER = f"AI_MANDATE_{uuid.uuid4().hex}"


def spot_long(qty: float = 0.2) -> Position:
    return Position(BTC, "LONG", 50000.0, 52000.0, True, 1_700_000_000, owner_instance=OWNER,
                    take_profit_price=52000.0, quantity=qty)


class Redis:
    def __init__(self):
        self.kv: dict[str, str] = {}
        self.published: list[dict] = []

    async def exists(self, key):
        return int(key in self.kv)

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.kv:
            return None
        self.kv[key] = value
        return True

    async def publish(self, channel, message):
        self.published.append(json.loads(message))


def progress_conn(row=None):
    """A connection whose TP1-progress query returns ``row`` (None: no attempt yet)."""
    conn = AsyncMock()
    conn.fetchrow.return_value = row
    return conn


def attempt(status: str, attempts: int = 1, filled: bool = False, age_s: float = 0.0) -> dict:
    return {"status": status, "created_at": datetime.fromtimestamp(time.time() - age_s, UTC),
            "filled": filled, "attempts": attempts}


async def test_spot_tp1_is_never_a_submit_order_sell():
    conn = progress_conn()
    assert await em.enqueue_tp1_reduce(conn, spot_long())
    sql, _cid, _key, target, payload, _acc = conn.execute.call_args.args
    assert "'CLOSE_POSITION'" in sql and "SUBMIT_ORDER" not in sql
    assert target == OWNER
    assert json.loads(payload) == {"instrument_id": BTC, "quantity": "0.1", "reason": "TAKE_PROFIT_1",
                                   "source": "exit_monitor_tp1"}


async def test_spot_tp1_close_runs_the_risk_reducing_close_path_and_sells_only_the_owners_half(monkeypatch):
    """The orchestrator takes the TP1 close on its close path (no mandate gate, so no MANDATE_REQUIRED) and sells the
    requested half of the mandate's own holding."""
    assert "CLOSE_POSITION" in server.RISK_REDUCING_COMMANDS
    dispatcher, _ = make_fresh_dispatcher()
    dispatcher.positions[BTC] = D("0.2")
    engine = make_engine(dispatcher)
    engine.bridge = MagicMock()

    async def holdings(conn, instance_id):
        assert instance_id == OWNER
        return {BTC: (D("0.2"), None)}

    monkeypatch.setattr(server, "instance_spot_holdings", holdings)
    gate = AsyncMock(side_effect=AssertionError("a TP1 close must not reach the mandate gate"))
    monkeypatch.setattr(server, "gate_ai_order", gate)
    recorded = AsyncMock()  # the profile trade log (its own DB tests); here only what it is told
    monkeypatch.setattr(server, "record_profile_trade", recorded)
    quotes = SimpleQuoteCache()
    quotes.set_quote(BTC, 52000.0, 52010.0)
    payload = em.tp1_payload(spot_long(), "0.1")
    row = {"command_id": uuid.uuid4(), "command_type": "CLOSE_POSITION", "target_instance_id": OWNER,
           "requester_id": "risk:exit_monitor", "payload": json.dumps(payload)}
    conn = await run(engine, [row], quotes)
    assert command_outcomes(conn) == [("COMPLETED", None)]
    evt = engine.bridge.enqueue_event.call_args.args[0]
    assert (evt["side"], evt["quantity"], evt["instance_id"]) == ("SELL", "0.1", OWNER)
    gate.assert_not_called()
    assert recorded.call_args.kwargs["reason"] == "TAKE_PROFIT_1" and recorded.call_args.kwargs["entry"] is False


async def test_tp1_flag_waits_for_a_completed_fill():
    pos = spot_long()
    redis = Redis()
    for row in (None, attempt("PENDING"), attempt("REJECTED"), attempt("COMPLETED", filled=False)):
        assert not await em.tp1_done(progress_conn(row), redis, pos)
    assert redis.kv == {}
    assert await em.tp1_done(progress_conn(attempt("COMPLETED", filled=True)), redis, pos)
    assert redis.kv == {f"exit:tp1:{pos.key}": "1"}


async def test_a_rejected_tp1_is_retried_after_the_pause_and_capped():
    pos = spot_long()
    assert not await em.enqueue_tp1_reduce(progress_conn(attempt("PENDING")), pos)
    assert not await em.enqueue_tp1_reduce(progress_conn(attempt("REJECTED", age_s=5)), pos)  # inside the pause
    conn = progress_conn(attempt("REJECTED", attempts=1, age_s=em.CLOSE_RETRY_AFTER_S + 1))
    assert await em.enqueue_tp1_reduce(conn, pos)
    assert conn.execute.call_args.args[2].endswith(":TAKE_PROFIT_1:1")  # the second attempt's key
    exhausted = attempt("FAILED", attempts=em.TP1_MAX_ATTEMPTS, age_s=em.CLOSE_RETRY_AFTER_S + 1)
    assert not await em.enqueue_tp1_reduce(progress_conn(exhausted), pos)


async def test_the_last_failed_tp1_pages_once_and_sets_no_flag():
    pos = spot_long()
    redis = Redis()
    exhausted = attempt("REJECTED", attempts=em.TP1_MAX_ATTEMPTS, age_s=em.CLOSE_RETRY_AFTER_S + 1)
    for _ in range(2):
        await em.act(progress_conn(exhausted), redis, pos, ExitAction("REDUCE_TP1", "TAKE_PROFIT_1"))
    critical = [a for a in redis.published if a.get("reason") == "TP1_FAILED"]
    assert len(critical) == 1 and critical[0]["severity"] == "CRITICAL"
    assert not any(k.startswith("exit:tp1:") for k in redis.kv)


async def test_tp1_progress_counts_only_this_positions_attempts():
    conn = progress_conn(attempt("COMPLETED", attempts=2, filled=True))
    got = await em.tp1_progress(conn, spot_long())
    assert (got.state, got.attempts) == ("DONE", 2)
    query, prefix, legacy = conn.fetchrow.call_args.args
    assert prefix == f"exit_monitor:{spot_long().key}:TAKE_PROFIT_1" and legacy is False
    assert "stage = 'EXIT'" in query


# --- item 3: one enforced TP1 for the monitor and every display ---------------------------------------------------

from cryptonite_schemas.exit_rules import apply_profile_exit_levels, enforced_tp1  # noqa: E402


def test_enforced_tp1_matches_the_monitor_and_the_production_cases():
    short = enforced_tp1("SHORT", 77291.3, 77886.86, 76388.88, D("2.00"), D("50.00"))
    assert round(short.price, 2) == 76100.18
    assert short.text(76388.88) == "TP1 (50% at 2R): 76100.18 from the fill; card target 76388.88 is informational"
    assert round(enforced_tp1("LONG", 1.2579, 1.2541, 1.2686, 2, 50).price, 4) == 1.2655
    pos = Position("BTCUSDT-PERP.BINANCE", "SHORT", 77291.3, 77000.0, True, 1, stop_price=77886.86,
                   take_profit_price=76388.88, profile_tp_r=D("2.00"), profile_tp1_scale_pct=D("50"))
    assert apply_profile_exit_levels(pos).take_profit_price == short.price  # the same number, not just close
    no_r = enforced_tp1("LONG", 100.0, 99.0, 101.5)
    assert (no_r.price, no_r.basis, no_r.scale_pct) == (101.5, "ENTRY_TARGET", 50.0)


def test_the_fill_note_recomputes_tp1_from_the_fill():
    from cryptonite_api.brain_outcomes import _fill_tp1, outcome_line

    payload = {"instrument_id": "BTCUSDT-PERP.BINANCE", "side": "SELL", "proposed_stop_loss": "77886.86",
               "proposed_take_profit": 76388.88, "tp1_r": 2.0, "tp1_scale_pct": 50.0}
    extra = _fill_tp1(payload, "77291.3")
    assert extra["tp1_text"].startswith("TP1 (50% at 2R): 76100.18 from the fill")
    line = outcome_line({"id": "c1", "side": "SELL", "instrument_id": "BTCUSDT-PERP.BINANCE", "command_id": "x"},
                        {"state": "FILLED", "filled_qty": "0.0258", "avg_fill_price": "77291.3", **extra})
    assert "card target 76388.88 is informational" in line
    assert _fill_tp1({**payload, "instrument_id": "XRPUSDT.BINANCE"}, "1.0") == {}  # a spot SELL is no entry
    assert _fill_tp1({k: v for k, v in payload.items() if k != "tp1_r"}, "77291.3") == {}


def test_the_card_shows_the_enforced_tp1_estimated_from_the_entry():
    from cryptonite_api.routes.brain import _tp1_view, card_tp1

    card = {"side": "BUY", "instrument_id": "XRPUSDT.BINANCE", "limit_price": 1.2579, "stop_loss": 1.2541,
            "take_profit": 1.2686}
    view = _tp1_view(card_tp1(card, SimpleNamespace(exit_tp_r=D("2"), exit_tp1_scale_pct=D("50"))), card)
    assert round(view["price"], 4) == 1.2655 and view["basis"] == "PROFILE_R"
    assert "recomputed from the fill" in view["text"] and "card target 1.2686 is informational" in view["text"]
    assert card_tp1({**card, "side": "SELL"}, SimpleNamespace(exit_tp_r=D("2"), exit_tp1_scale_pct=None)) is None


async def test_debate_evidence_position_rows_carry_the_enforced_tp1():
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "debate_worker"))
    import evidence

    p = {"instrument_id": "XRPUSDT.BINANCE", "side": "LONG", "market": "SPOT", "quantity": 993.7, "entry_price": 1.2579,
         "stop": 1.2541, "take_profit": 1.2686, "leverage": 1, "owner_instance": OWNER, "mandate_id": str(uuid.uuid4()),
         "mandate_name": "Brain XRP", "opened_at": "2026-11-03T11:29:34+00:00"}
    conn = AsyncMock()
    conn.fetchrow.return_value = {"exit_tp_r": D("2.00"), "exit_tp1_scale_pct": D("50.00")}
    await evidence._enforced_tp1(conn, p)
    assert round(p["tp1"], 4) == 1.2655
    assert "enforced TP1 (50% at 2R): 1.2655" in evidence._position_line(p)


# --- item 5: the Brain's debate status --------------------------------------------------------------------------------

def test_the_debate_tool_says_the_worker_starts_it_and_the_card_gets_the_result():
    from cryptonite_agents.brain.widgets import _MSG_ID
    from cryptonite_agents.tools import debate_queued_text

    text = debate_queued_text("1730000000000-0")
    assert text.startswith("Success") and "starts it automatically" in text and "staged" not in text
    assert "appears in the debate card when the run finishes" in text
    assert _MSG_ID.search(text).group(1) == "1730000000000-0"  # the card still follows the run
