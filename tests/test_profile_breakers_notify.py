"""ADR 0011: profile breaker episodes and durable notification consumption."""
import datetime as dt
import json
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from cryptonite_notifier.bot import OUTBOX_KINDS
from cryptonite_notifier.render import alert_text
from cryptonite_orchestrator import mandate_monitor as monitor
from cryptonite_schemas.mandate import MandateStatus

NOW = dt.datetime(2026, 10, 24, 0, 5, tzinfo=dt.UTC)


class Redis:
    """Persist cursor across consumer invocations."""
    def __init__(self):
        self.values = {}
        self.publish = AsyncMock()

    async def get(self, key):
        return self.values.get(key)

    async def set(self, key, value, **kwargs):
        self.values[key] = value
        return True


class Conn:
    """Outbox with unique keys and an injectable write failure."""
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.outbox = {}
        self.fail = False

    @asynccontextmanager
    async def transaction(self):
        yield

    async def fetch(self, sql, *args):
        if "stage IN ('FILL','EXIT')" in sql:
            return [r for r in self.rows if r['id'] > args[0]]
        return []

    async def fetchval(self, sql, *args):
        if 'notifications_outbox' in sql:
            return args[0] in self.outbox
        return None

    async def execute(self, sql, *args):
        if 'INSERT INTO notifications_outbox' in sql:
            if self.fail:
                raise RuntimeError('outbox unavailable')
            self.outbox.setdefault(args[4], args)
        return 'UPDATE 1'


def decision(i, stage='FILL', level='ALL', mode='AUTO'):
    return dict(id=i, mandate_id=uuid.uuid4(), name='Alpha', notify_level=level,
                instrument_id='BTCUSDT-PERP.BINANCE', stage=stage, outcome='FILLED' if stage == 'FILL' else 'TP',
                detail=json.dumps(dict(side='SHORT', market='PERP', price='100', qty='2',
                                       leverage=3, stop='110', mode=mode, realized_pnl='20', r_multiple='2')))


@pytest.mark.asyncio
async def test_cursor_multiple_fills_restart_and_retry_never_skips():
    conn, redis = Conn([decision(1), decision(2)]), Redis()
    conn.fail = True
    await monitor.notify_profile_decisions(conn, redis)
    assert not await redis.get('profile_notify:cursor')
    conn.fail = False
    await monitor.notify_profile_decisions(conn, redis)
    assert len(conn.outbox) == 2
    assert int(await redis.get('profile_notify:cursor')) == 2
    # Crash after insertion but before cursor persistence replays safely.
    redis.values['profile_notify:cursor'] = 1
    await monitor.notify_profile_decisions(conn, redis)
    assert len(conn.outbox) == 2
    assert redis.publish.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('level,stage,mode,sent', [
    ('NONE', 'FILL', 'AUTO', False), ('IMPORTANT', 'FILL', 'ASK', True),
    ('IMPORTANT', 'EXIT', 'AUTO', False), ('ALL', 'EXIT', 'AUTO', True),
    ('ALL', 'FILL', 'ASK', False), ('ALL', 'FILL', 'VETO', True),
])
async def test_notification_levels(level, stage, mode, sent):
    conn, redis = Conn([decision(1, stage, level, mode)]), Redis()
    await monitor.notify_profile_decisions(conn, redis)
    assert bool(conn.outbox) is sent
    assert int(await redis.get('profile_notify:cursor')) == 1
    if sent:
        payload = json.loads(next(iter(conn.outbox.values()))[3])
        assert payload['instrument_id'] == 'BTCUSDT-PERP.BINANCE'


@pytest.mark.asyncio
@pytest.mark.parametrize('trigger', ['LOSS_STREAK', 'REJECT_STREAK', 'STALE_DATA'])
async def test_breaker_pauses_once_without_flattening(monkeypatch, trigger):
    m = SimpleNamespace(id=uuid.uuid4(), name='Alpha', instance_id='AI_MANDATE_x',
                        status=MandateStatus.ACTIVE, instruments=['BTCUSDT.BINANCE'],
                        breaker_max_loss_streak=2, breaker_max_reject_streak=2, notify_level='NONE')
    conn = Conn()
    conn.fetchrow = AsyncMock(return_value={'status': 'ACTIVE', 'paused_reason': None})
    conn.fetch = AsyncMock(side_effect=lambda sql, *args: (
        [{'realized_pnl': -1}, {'realized_pnl': -2}] if 'paper_positions' in sql and trigger == 'LOSS_STREAK'
        else [{'command_outcome': 'REJECTED'}] * 2 if 'execution_commands' in sql and trigger == 'REJECT_STREAK' else []))
    pause, log, flatten = AsyncMock(), AsyncMock(return_value=True), AsyncMock()
    monkeypatch.setattr(monitor, 'pause', pause, raising=False)
    monkeypatch.setattr(monitor, 'record_decision', log, raising=False)
    monkeypatch.setattr(monitor, 'flatten_instance', flatten)
    health = {'BTCUSDT.BINANCE': {'arrival_ts_ns': int((NOW.timestamp() - (601 if trigger == 'STALE_DATA' else 1)) * 1e9)}}
    await monitor.track_profile_breakers(conn, Redis(), m, NOW, health)
    m.status = MandateStatus.PAUSED
    await monitor.track_profile_breakers(conn, Redis(), m, NOW, health)
    assert pause.await_count == 1
    assert pause.call_args.kwargs['actor'] == 'risk:breaker'
    assert f'BREAKER_{trigger}' in pause.call_args.kwargs['reason']
    assert log.await_count == 1
    flatten.assert_not_awaited()
    # Delivery uses the separately committed BREAKER decision reconciliation.
    assert not conn.outbox


@pytest.mark.asyncio
async def test_daily_once_per_day_and_refusal_digest():
    conn, redis = Conn(), Redis()
    m = SimpleNamespace(id=uuid.uuid4(), name='Alpha', notify_level='ALL')
    conn.fetch = AsyncMock(return_value=[{'stage': 'EXIT', 'outcome': 'TP', 'reason': None,
                                         'detail': {'realized_pnl': '12'}},
                                        {'stage': 'GATE', 'outcome': 'REFUSED', 'reason': 'NO_MARKET', 'detail': {}},
                                        {'stage': 'BREAKER', 'outcome': 'PAUSED', 'reason': 'STALE', 'detail': {}}])
    await monitor.profile_daily_summary(conn, redis, m, NOW - dt.timedelta(minutes=1))
    assert not conn.outbox
    await monitor.profile_daily_summary(conn, redis, m, NOW)
    await monitor.profile_daily_summary(conn, redis, m, NOW)
    assert len(conn.outbox) == 1
    payload = json.loads(next(iter(conn.outbox.values()))[3])
    assert payload['pnl'] == '12' and payload['refusals'] == {'NO_MARKET': 1}
    assert payload['wins'] == 1 and payload['breaker_events'] == 1
    await monitor.profile_daily_summary(conn, redis, m, NOW + dt.timedelta(days=1))
    assert len(conn.outbox) == 2


@pytest.mark.parametrize('kind,expected', [('PROFILE_FILL', 'SHORT'), ('PROFILE_EXIT', '2R'),
    ('PROFILE_BREAKER', 'paused'), ('PROFILE_DAILY_SUMMARY', 'P&L')])
def test_profile_render_and_outbox_kind(kind, expected):
    title, body = alert_text(dict(kind=kind, mandate='Alpha', side='SHORT', market='PERP',
        instrument_id='BTCUSDT-PERP.BINANCE', leverage=3, price=100, qty=2, stop=110,
        r_multiple=2, realized_pnl=20, reason='BREAKER_LOSS_STREAK', pnl=20,
        trades=2, wins=1, losses=1, refusals={'NO_MARKET': 1}, breaker_events=1))
    assert expected in body
    assert kind in OUTBOX_KINDS
    assert 'Alpha' in title


@pytest.mark.asyncio
async def test_daily_none_and_important_digest_filter():
    """NONE suppresses summary; IMPORTANT omits routine refusals."""
    conn, redis = Conn(), Redis()
    m = SimpleNamespace(id=uuid.uuid4(), name='Alpha', notify_level='NONE')
    await monitor.profile_daily_summary(conn, redis, m, NOW)
    assert not conn.outbox
    m.notify_level = 'IMPORTANT'
    await monitor.profile_daily_summary(conn, redis, m, NOW)
    payload = json.loads(next(iter(conn.outbox.values()))[3])
    assert 'refusals' not in payload


@pytest.mark.asyncio
async def test_breaker_success_or_flat_trade_interrupts_streak(monkeypatch):
    """A streak includes the most recent completed outcomes, including non-losses."""
    conn = Conn()
    conn.fetch = AsyncMock(side_effect=[[{'realized_pnl': -1}, {'realized_pnl': 0}],
                                      [{'command_outcome': 'REJECTED'}, {'command_outcome': 'SUCCESS'}]])
    m = SimpleNamespace(id=uuid.uuid4(), name='Alpha', instance_id='AI_MANDATE_x',
                        status=MandateStatus.ACTIVE, instruments=['BTCUSDT.BINANCE'],
                        breaker_max_loss_streak=2, breaker_max_reject_streak=2)
    pause = AsyncMock()
    monkeypatch.setattr(monitor, 'pause', pause)
    await monitor.track_profile_breakers(conn, Redis(), m, NOW, {})
    pause.assert_not_awaited()


@pytest.mark.asyncio
async def test_cursor_reads_committed_prefix_before_delivery():
    """The table lock must be released before alerts can perform I/O."""
    conn, redis = Conn([decision(1)]), Redis()
    events = []
    original = conn.execute

    @asynccontextmanager
    async def transaction():
        events.append('begin')
        yield
        events.append('commit')

    async def execute(sql, *args):
        events.append(sql)
        return await original(sql, *args)

    conn.transaction, conn.execute = transaction, execute
    await monitor.notify_profile_decisions(conn, redis)
    lock = events.index('LOCK TABLE ai_profile_decisions IN SHARE MODE')
    assert events[lock + 1] == 'commit'
    insert = next(i for i, e in enumerate(events) if 'INSERT INTO notifications_outbox' in e)
    assert insert > lock + 1


@pytest.mark.asyncio
async def test_cursor_waits_for_late_lower_id_commit():
    """Real Postgres: a higher committed ID cannot make an earlier uncommitted fill disappear."""
    import asyncio
    import os

    import asyncpg

    url = os.environ.get('DATABASE_URL', '')
    if not url.endswith('/cryptonite_ci_p18'):
        pytest.skip('requires the dedicated P1.8 database')
    writer = await asyncpg.connect(url)
    consumer = await asyncpg.connect(url)
    later = await asyncpg.connect(url)
    mid = None
    redis = Redis()
    try:
        mid = await later.fetchval(
            """INSERT INTO ai_mandates (name, capital_budget_usd, instruments, max_position_pct,
                       max_drawdown_pct, autonomy_level, notify_level)
               VALUES ('p18 cursor', 1000, ARRAY['BTCUSDT.BINANCE'], 25, 10, 3, 'ALL') RETURNING id""")
        # Historical rows in this dedicated test DB are outside the synthetic episode.
        redis.values['profile_notify:cursor'] = await later.fetchval('SELECT COALESCE(MAX(id), 0) FROM ai_profile_decisions')
        tx = writer.transaction()
        await tx.start()
        sql = """INSERT INTO ai_profile_decisions (mandate_id, instrument_id, stage, outcome, detail)
                 VALUES ($1, 'BTCUSDT.BINANCE', 'FILL', 'FILLED', '{"mode":"AUTO"}') RETURNING id"""
        lower = await writer.fetchval(sql, mid)
        higher = await later.fetchval(sql, mid)
        task = asyncio.create_task(monitor.notify_profile_decisions(consumer, redis))
        await asyncio.sleep(0.05)
        assert not task.done()
        await tx.commit()
        await task
        assert int(redis.values['profile_notify:cursor']) == higher
        assert await later.fetchval(
            'SELECT count(*) FROM notifications_outbox WHERE dedupe_key = ANY($1::text[])',
            [f'profile-decision:{lower}', f'profile-decision:{higher}']) == 2
        # Simulate restart before cursor write: durable dedupe still leaves two rows.
        redis.values['profile_notify:cursor'] = lower - 1
        await monitor.notify_profile_decisions(consumer, redis)
        assert await later.fetchval(
            'SELECT count(*) FROM notifications_outbox WHERE dedupe_key = ANY($1::text[])',
            [f'profile-decision:{lower}', f'profile-decision:{higher}']) == 2
    finally:
        await writer.close()
        await consumer.close()
        if mid is not None:
            await later.execute('DELETE FROM notifications_outbox WHERE payload->>\'mandate_id\' = $1', str(mid))
            await later.execute('DELETE FROM ai_mandates WHERE id = $1', mid)
        await later.close()


@pytest.mark.asyncio
async def test_durable_delivery_survives_pubsub_failure():
    """Redis pubsub failure cannot discard an already queued outbox message."""
    conn, redis = Conn([decision(1)]), Redis()
    redis.publish.side_effect = RuntimeError('redis publish down')
    await monitor.notify_profile_decisions(conn, redis)
    assert len(conn.outbox) == 1
    assert int(redis.values['profile_notify:cursor']) == 1


@pytest.mark.asyncio
async def test_breaker_notification_outage_retry_and_none_bypass():
    """Paused profiles retain an unsent critical decision until outbox recovery."""
    conn, redis = Conn(), Redis()
    conn.fetch = AsyncMock(return_value=[dict(id=10, mandate_id=uuid.uuid4(), name='Alpha',
                                            reason='BREAKER_LOSS_STREAK: 2 losing trades')])
    conn.fail = True
    await monitor.notify_profile_breakers(conn, redis)
    assert not conn.outbox
    conn.fail = False
    await monitor.notify_profile_breakers(conn, redis)
    await monitor.notify_profile_breakers(conn, redis)
    assert len(conn.outbox) == 1
    assert redis.publish.await_count == 1
    assert next(iter(conn.outbox.values()))[-1] == 'CRITICAL'


@pytest.mark.asyncio
async def test_real_breaker_pause_survives_outbox_outage_and_exit_runs(monkeypatch):
    """Commit the safety pause, recover its alert, and gate an existing holding's exit."""
    import os
    from decimal import Decimal

    import asyncpg
    from cryptonite_execution.mandate_store import load_mandate
    from cryptonite_execution.risk import OrderIntent
    from cryptonite_orchestrator.mandate_gate import gate_ai_order
    from migrated_db import seed_paper_account
    from test_mandate_hard_stop import make_mandate

    url = os.environ.get('DATABASE_URL', '')
    if not url.endswith('/cryptonite_ci_p18'):
        pytest.skip('requires the dedicated P1.8 database')
    pool = await asyncpg.create_pool(url, min_size=1, max_size=2)
    mid = None
    try:
        async with pool.acquire() as conn:
            await seed_paper_account(conn)
        m = await make_mandate(pool, 1)
        mid = m.id
        async with pool.acquire() as conn:
            original = monitor.alert
            monkeypatch.setattr(monitor, 'alert', AsyncMock())
            health = {'BTCUSDT.BINANCE': {'arrival_ts_ns': int((NOW.timestamp() - 601) * 1e9)}}
            await monitor.track_profile_breakers(conn, Redis(), m, NOW, health)
            paused = await load_mandate(conn, mid)
            assert paused.status is MandateStatus.PAUSED
            assert paused.paused_reason.startswith('BREAKER_STALE_DATA')
            assert await conn.fetchval("SELECT count(*) FROM ai_profile_decisions WHERE mandate_id=$1 AND stage='BREAKER'", mid) == 1
            await monitor.track_profile_breakers(conn, Redis(), m, NOW, health)  # stale ACTIVE snapshot
            await monitor.track_profile_breakers(conn, Redis(), paused, NOW, health)
            monkeypatch.setattr(monitor, 'alert', original)
            await monitor.notify_profile_breakers(conn, Redis())
            await monitor.notify_profile_breakers(conn, Redis())
            assert await conn.fetchval("SELECT count(*) FROM notifications_outbox WHERE kind='PROFILE_BREAKER' AND payload->>'mandate_id'=$1", str(mid)) == 1
            await conn.execute(
                """INSERT INTO paper_orders (client_order_id, instance_id, account_id, instrument_id, side,
                       order_type, quantity, filled_qty, avg_fill_price, status)
                   VALUES ($1, $2, 'PAPER-ACCOUNT-001', 'BTCUSDT.BINANCE', 'BUY', 'MARKET', 0.01, 0.01, 50000, 'FILLED')""",
                uuid.uuid4().hex, m.instance_id)
            engine = SimpleNamespace(dispatcher=SimpleNamespace(perp_positions={}, mark_prices={}, open_orders={}),
                                     account_id='PAPER-ACCOUNT-001')
            intent = OrderIntent(client_order_id=uuid.uuid4().hex, instance_id=m.instance_id,
                                 instrument_id='BTCUSDT.BINANCE', side='SELL', order_type='MARKET',
                                 quantity=Decimal('0.01'), price=Decimal('50000'))
            assert await gate_ai_order(conn, engine, {'command_id': uuid.uuid4()}, {}, intent, paused, None) is None
    finally:
        if mid is not None:
            async with pool.acquire() as conn:
                await conn.execute("DELETE FROM notifications_outbox WHERE payload->>'mandate_id'=$1", str(mid))
                await conn.execute('DELETE FROM paper_orders WHERE instance_id=$1', m.instance_id)
                await conn.execute('DELETE FROM ai_autonomy_events WHERE mandate_id=$1', mid)
                await conn.execute('DELETE FROM ai_mandates WHERE id=$1', mid)
        await pool.close()
