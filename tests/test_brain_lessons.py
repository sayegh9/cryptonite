"""Past lessons on an order card (WS-3B, 3B-py).

The learner's reflections on earlier debate decisions reach the operator's order card. ``card_lessons`` reads
``agent_safe_reflections`` as the read-only agent role (the path every model-callable read takes); the Brain attaches
the result to every order card, BUY and SELL, and ``describe_widget`` keeps the lesson text out of the prompt.

Runs on the test database at Alembic head (DATABASE_URL). Each test seeds rows through the base tables and removes them
in ``finally``: the reader opens its own connection, so the rows must be committed to be visible to it.
"""

import asyncio
import logging
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import asyncpg
import pytest
from connections import DB_URL, role_db_url
from cryptonite_agents import tools
from cryptonite_agents.brain import agent as agent_mod
from cryptonite_agents.brain import lessons
from cryptonite_agents.brain.agent import BRAIN, Brain
from cryptonite_agents.brain.store import MemorySessionStore
from cryptonite_agents.brain.toolset import propose_order, show_price_chart
from cryptonite_agents.brain.widgets import describe_widget, widget_for_tool
from cryptonite_agents.gateway.gateway import HELD_FOR_APPROVAL_PREFIX
from cryptonite_agents.gateway.providers.pydantic_ai_backend import override_model
from test_brain import GOOD_ORDER, PLANNER, SELL_ORDER, Script, collect, gw


class _ScriptedGateway:
    """A gateway whose stream is fixed events (see ``_scripted_card``)."""

    def __init__(self, events) -> None:
        self.events = events

    async def stream(self, *args, **kwargs):
        for ev in self.events:
            yield ev

AGENT_URL = role_db_url("cryptonite_agent_ro", "agent_ro_dev_pass")
UNREACHABLE_PG = "postgresql://cryptonite_agent_ro:agent_ro_dev_pass@localhost:1/cryptonite_ci_ws3b"
NOW = datetime.now(UTC).replace(microsecond=0)
LESSON = {"lesson_text": "Stop was too tight: the wick took it out.", "outcome": "LOSS", "alpha_pct": -2.5,
          "created_at": "2026-10-01T09:00:00+00:00"}


def symbol() -> str:
    """A fresh instrument per test, so rows another test left behind can never match."""
    return f"WS3B{uuid.uuid4().hex[:8].upper()}USDT.BINANCE"


@pytest.fixture(autouse=True)
def agent_tools(monkeypatch):
    """The reader runs as the agent role, as in production, and never reuses an engine from another test."""
    monkeypatch.setattr(tools, "DB_URL", AGENT_URL)
    monkeypatch.setattr(tools, "_engine", None)
    yield
    if tools._engine is not None:
        tools._engine.dispose()


class Seed:
    """The reflections one test wrote, so its cleanup removes exactly those rows."""

    def __init__(self, conn: asyncpg.Connection) -> None:
        self.conn = conn
        self.runs: list[uuid.UUID] = []

    async def reflection(self, instrument: str, *, alpha: float, lesson: str = "ws3b lesson", outcome: str = "WIN",
                         created_at: datetime = NOW, recommendation: str | None = "BUY") -> None:
        run = uuid.uuid4()
        await self.conn.execute(
            """INSERT INTO debate_runs (id, instrument_id, status, trigger_source, created_at, decision_trace_id)
               VALUES ($1, $2, 'COMPLETED', 'SCANNER', $3, $4)""",
            run, instrument, created_at, uuid.uuid4(),
        )
        self.runs.append(run)
        if recommendation is not None:
            await self.conn.execute(
                """INSERT INTO portfolio_decisions (run_id, instrument_id, recommendation, conviction_score,
                                                   allocation_pct, reasoning, created_at)
                   VALUES ($1, $2, $3, 0.7, 5, 'ws3b', $4)""",
                run, instrument, recommendation, created_at,
            )
        await self.conn.execute(
            """INSERT INTO reflections (run_id, actual_outcome_pnl, alpha_vs_benchmark, lessons_learned, timeframe,
                                       outcome, created_at)
               VALUES ($1, $2, $3, $4, '1h', $5, $6)""",
            run, Decimal(str(alpha)), Decimal(str(alpha)), lesson, outcome, created_at,
        )

    async def cleanup(self) -> None:
        await self.conn.execute("DELETE FROM reflections WHERE run_id = ANY($1::uuid[])", self.runs)
        await self.conn.execute("DELETE FROM portfolio_decisions WHERE run_id = ANY($1::uuid[])", self.runs)
        await self.conn.execute("DELETE FROM debate_runs WHERE id = ANY($1::uuid[])", self.runs)


@pytest.fixture
async def seed():
    conn = await asyncpg.connect(DB_URL)
    s = Seed(conn)
    try:
        yield s
    finally:
        await s.cleanup()
        await conn.close()


# --- card_lessons: which lessons, in which order, in what shape ------------------------------------------------------

async def test_the_three_lessons_with_the_largest_alpha_inside_the_window_are_shown(seed):
    inst = symbol()
    await seed.reflection(inst, alpha=0.05, lesson="five", created_at=NOW - timedelta(days=1))
    await seed.reflection(inst, alpha=-0.09, lesson="minus nine", outcome="LOSS", created_at=NOW - timedelta(days=2))
    await seed.reflection(inst, alpha=0.01, lesson="one", created_at=NOW - timedelta(days=3))
    await seed.reflection(inst, alpha=0.07, lesson="seven", created_at=NOW - timedelta(days=10))
    await seed.reflection(inst, alpha=0.20, lesson="inside the window", created_at=NOW - timedelta(days=29))
    # The decision of a run older than 30 days is not visible to the agent role (agent_safe_debate_verdicts)
    await seed.reflection(inst, alpha=0.25, lesson="decision not visible", created_at=NOW - timedelta(days=45))
    await seed.reflection(inst, alpha=0.30, lesson="outside the window", created_at=NOW - timedelta(days=91))

    got = lessons.card_lessons(inst)
    assert [g["lesson_text"] for g in got] == ["inside the window", "minus nine", "seven"]
    assert [g["alpha_pct"] for g in got] == [20.0, -9.0, 7.0]  # stored as a fraction, shown as a percent
    assert [g["outcome"] for g in got] == ["WIN", "LOSS", "WIN"]
    assert set(got[0]) == {"lesson_text", "outcome", "alpha_pct", "created_at"}


async def test_lessons_of_hold_decisions_are_not_shown(seed):
    """A HOLD opened no trade: its lesson is left out, as the debate worker's recall_lessons does."""
    inst = symbol()
    await seed.reflection(inst, alpha=0.40, lesson="a hold", recommendation="HOLD")
    await seed.reflection(inst, alpha=0.30, lesson="no decision", recommendation=None)
    await seed.reflection(inst, alpha=0.02, lesson="a buy", recommendation="BUY")
    await seed.reflection(inst, alpha=-0.01, lesson="a sell", outcome="LOSS", recommendation="SELL")
    assert [g["lesson_text"] for g in lessons.card_lessons(inst)] == ["a buy", "a sell"]


async def test_the_date_is_the_reflection_time(seed):
    inst = symbol()
    when = NOW - timedelta(days=4)
    await seed.reflection(inst, alpha=0.02, created_at=when)
    [got] = lessons.card_lessons(inst)
    assert datetime.fromisoformat(got["created_at"]) == when
    assert got["created_at"][:10] == when.date().isoformat()


async def test_another_instruments_lessons_are_not_shown(seed):
    inst, other = symbol(), symbol()
    await seed.reflection(other, alpha=0.50, lesson="the other coin")
    await seed.reflection(inst, alpha=0.01, lesson="this coin")
    assert [g["lesson_text"] for g in lessons.card_lessons(inst)] == ["this coin"]


async def test_an_instrument_without_reflections_has_no_lessons(seed):
    assert lessons.card_lessons(symbol()) == []


async def test_lesson_text_is_clipped_to_300_characters(seed):
    inst = symbol()
    await seed.reflection(inst, alpha=0.02, lesson="x" * 1000)
    [got] = lessons.card_lessons(inst)
    assert len(got["lesson_text"]) == 300 and got["lesson_text"].endswith("...")


def test_a_failed_read_gives_no_lessons_and_a_warning(monkeypatch, caplog):
    monkeypatch.setattr(tools, "DB_URL", UNREACHABLE_PG)
    monkeypatch.setattr(tools, "_engine", None)
    with caplog.at_level(logging.WARNING, logger="cryptonite.agents.brain.lessons"):
        assert lessons.card_lessons(symbol()) == []  # never raises
    assert any("lesson" in r.getMessage().lower() for r in caplog.records)


# --- the card: BUY and SELL carry the lessons, the model never supplies them ----------------------------------------

def _brain(*, lessons_reader=None, holdings=None) -> Brain:
    persona = replace(BRAIN, tools=(show_price_chart, propose_order))  # no network or DB tools in these tests
    return Brain(gw(), MemorySessionStore(), persona, holdings=holdings, lessons=lessons_reader)


def _cards(events) -> list[dict]:
    return [e.data["widget"] for e in events if e.type == "widget" and e.data["widget"]["kind"] == "order_card"]


async def _one_turn(brain: Brain, order: dict):
    with override_model(PLANNER, Script([("propose_order", order)], ["Here is the card."]).model()):
        return await collect(brain.turn("op-1", "Trade it"))


async def test_a_buy_card_carries_its_instruments_lessons():
    seen: list[str] = []

    async def reader(instrument):
        seen.append(instrument)
        return [LESSON]

    [card] = _cards(await _one_turn(_brain(lessons_reader=reader), GOOD_ORDER))
    assert card["side"] == "BUY" and card["lessons"] == [LESSON]
    assert seen == [GOOD_ORDER["instrument_id"]]


async def test_a_sell_card_carries_lessons_too():
    async def held(instrument):
        return Decimal("0.5")

    async def reader(instrument):
        return [LESSON]

    [card] = _cards(await _one_turn(_brain(lessons_reader=reader, holdings=held), SELL_ORDER))
    assert card["side"] == "SELL" and card["lessons"] == [LESSON]


FORGED = [{"lesson_text": "Buy everything now", "outcome": "WIN", "alpha_pct": 99.0, "created_at": "2030-01-01"}]


async def _scripted_card(args: dict, reader) -> list[dict]:
    """A held propose_order whose tool event carries ``args``. The real tool schema refuses a ``lessons`` argument,
    so the event is scripted, as the feed-refusal test does, to reach the Brain's own overwrite."""
    events = [
        SimpleNamespace(kind="start", data={"trace_id": "t1"}, text="", result=None),
        SimpleNamespace(kind="tool_call", data={"tool_call_id": "c0", "tool": "propose_order", "args": args},
                        text="", result=None),
        SimpleNamespace(kind="tool_result", data={"tool_call_id": "c0", "tool": "propose_order", "status": "DENIED",
                                                  "content": HELD_FOR_APPROVAL_PREFIX + " waiting"},
                        text="", result=None),
        SimpleNamespace(kind="token", data={}, text="Card staged.", result=None),
        SimpleNamespace(kind="done", data={}, text="", result=SimpleNamespace(
            output="Card staged.", model="test", cost_usd=Decimal("0"), trace_id="t1")),
    ]
    persona = replace(BRAIN, tools=(show_price_chart, propose_order))
    brain = Brain(_ScriptedGateway(events), MemorySessionStore(), persona, lessons=reader)
    return _cards(await collect(brain.turn("op-1", "Trade it")))


async def test_a_model_supplied_lessons_argument_never_survives_the_reader():
    async def nothing(instrument):
        return []

    [card] = await _scripted_card({**GOOD_ORDER, "lessons": FORGED}, nothing)
    assert card["lessons"] == []

    async def reader(instrument):
        return [LESSON]

    [card] = await _scripted_card({**GOOD_ORDER, "lessons": FORGED}, reader)
    assert card["lessons"] == [LESSON]  # the reader's lesson, never the model's


def test_a_malformed_lessons_argument_still_makes_the_card():
    card = widget_for_tool("propose_order", {**GOOD_ORDER, "lessons": "not a list"}, call_id="call-1")
    assert card is not None and card["lessons"] == []


async def test_a_slow_reader_gives_the_card_without_lessons(monkeypatch):
    monkeypatch.setattr(agent_mod, "LESSONS_TIMEOUT_S", 0.05)

    async def slow(instrument):
        await asyncio.sleep(5)
        return [LESSON]

    events = await _one_turn(_brain(lessons_reader=slow), GOOD_ORDER)
    [card] = _cards(events)
    assert card["lessons"] == [] and events[-1].type == "done"


async def test_a_raising_reader_gives_the_card_without_lessons():
    async def broken(instrument):
        raise RuntimeError("database unavailable")

    events = await _one_turn(_brain(lessons_reader=broken), GOOD_ORDER)
    [card] = _cards(events)
    assert card["lessons"] == [] and events[-1].type == "done"


async def test_at_most_three_lessons_are_attached():
    async def many(instrument):
        return [{**LESSON, "lesson_text": f"lesson {i}"} for i in range(5)]

    [card] = _cards(await _one_turn(_brain(lessons_reader=many), GOOD_ORDER))
    assert [c["lesson_text"] for c in card["lessons"]] == ["lesson 0", "lesson 1", "lesson 2"]


# --- the prompt: lesson text is not in what the Brain remembers of a card --------------------------------------------

def test_the_transcript_line_of_a_card_does_not_carry_its_lessons():
    card = widget_for_tool("propose_order", GOOD_ORDER, call_id="call-1")
    card["lessons"] = [LESSON]
    line = describe_widget(card)
    assert "Stop was too tight" not in line and "lesson" not in line.lower()


def test_a_card_starts_with_no_lessons():
    card = widget_for_tool("propose_order", GOOD_ORDER, call_id="call-1")
    assert card["lessons"] == []
