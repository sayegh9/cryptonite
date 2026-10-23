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
    conn, m = BreakerConn(), breaker_profile()
    m.breaker_max_loss_streak = m.breaker_max_reject_streak = 2
    conn.loss_trigger = trigger == 'LOSS_STREAK'
    conn.reject_trigger = trigger == 'REJECT_STREAK'
    pause = AsyncMock(side_effect=fake_pause(conn, m))
    log, flatten = AsyncMock(return_value=True), AsyncMock()
    monkeypatch.setattr(monitor, 'pause', pause)
    monkeypatch.setattr(monitor, 'record_decision', log)
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
    assert len(conn.outbox) == 1


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
    rows = [dict(id=10, mandate_id=uuid.uuid4(), name='Alpha',
                 reason='BREAKER_LOSS_STREAK: 2 losing trades')]
    conn.fetch = AsyncMock(side_effect=lambda sql, *args: rows if 'SELECT d.id' in sql else [])
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
            original_log = monitor.record_decision
            monkeypatch.setattr(monitor, 'record_decision', AsyncMock(return_value=False))
            monkeypatch.setattr(monitor, 'alert', AsyncMock())
            health = {'BTCUSDT.BINANCE': {'arrival_ts_ns': int((NOW.timestamp() - 601) * 1e9)}}
            await monitor.track_profile_breakers(conn, Redis(), m, NOW, health)
            paused = await load_mandate(conn, mid)
            assert paused.status is MandateStatus.PAUSED
            assert paused.paused_reason.startswith('BREAKER_STALE_DATA')
            assert await conn.fetchval("SELECT count(*) FROM ai_profile_decisions WHERE mandate_id=$1 AND stage='BREAKER'", mid) == 0
            await monitor.track_profile_breakers(conn, Redis(), m, NOW, health)  # stale ACTIVE snapshot
            await monitor.track_profile_breakers(conn, Redis(), paused, NOW, health)
            monkeypatch.setattr(monitor, 'alert', original)
            monkeypatch.setattr(monitor, 'record_decision', original_log)
            await monitor.notify_profile_breakers(conn, Redis())
            await monitor.notify_profile_breakers(conn, Redis())
            assert await conn.fetchval("SELECT count(*) FROM ai_profile_decisions WHERE mandate_id=$1 AND stage='BREAKER'", mid) == 1
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


class BreakerConn(Conn):
    """Durable lifecycle event store with rollback semantics for independent breaker tests."""
    def __init__(self):
        super().__init__()
        self.status = 'ACTIVE'
        self.events = []
        self.loss_id = 'loss-1'
        self.command_id = 'command-1'
        self.loss_trigger = False
        self.reject_trigger = False
        self.started = NOW - dt.timedelta(minutes=11)
        self.command_sql = ''

    @asynccontextmanager
    async def transaction(self):
        status = self.status
        try:
            yield
        except Exception:
            self.status = status
            raise

    async def fetchrow(self, sql, *args):
        if 'FOR UPDATE' in sql:
            return {'status': self.status, 'paused_reason': None}
        if 'ai_autonomy_events' in sql:
            return self.events[-1] if self.events else None
        return None

    async def fetchval(self, sql, *args):
        if 'AS started_at' in sql:
            return self.started
        if 'ai_autonomy_events' in sql:
            return any(e['detail'].get('trigger_kind') == args[1] and
                       e['detail'].get('trigger_event') == args[2] for e in self.events)
        return await super().fetchval(sql, *args)

    async def fetch(self, sql, *args):
        if 'paper_positions' in sql:
            return ([dict(position_id=self.loss_id, closed_at=NOW, realized_pnl=-1)] * args[1]
                    if self.loss_trigger else [])
        if 'execution_commands' in sql:
            self.command_sql = sql
            return ([dict(command_id=self.command_id, command_outcome='REJECTED')] * args[1]
                    if self.reject_trigger else [])
        if 'SELECT e.id' in sql:
            return self.events
        return await super().fetch(sql, *args)

    async def execute(self, sql, *args):
        if 'UPDATE ai_autonomy_events' in sql:
            self.events[-1]['detail'].update(json.loads(args[1]))
        return await super().execute(sql, *args)


def breaker_profile():
    return SimpleNamespace(id=uuid.uuid4(), name='Alpha', instance_id='AI_MANDATE_x',
                           status=MandateStatus.ACTIVE, instruments=['BTCUSDT.BINANCE'],
                           breaker_max_loss_streak=1, breaker_max_reject_streak=1)


def fake_pause(conn, m):
    async def pause(*args, **kwargs):
        conn.status = 'PAUSED'
        conn.events.append(dict(id=len(conn.events) + 1, mandate_id=m.id, name=m.name,
                                reason=kwargs['reason'], detail={}))
        return m
    return pause


@pytest.mark.asyncio
async def test_review_decision_failure_cannot_rollback_pause(monkeypatch):
    conn, m = BreakerConn(), breaker_profile()
    conn.loss_trigger = True
    monkeypatch.setattr(monitor, 'pause', fake_pause(conn, m))
    log = AsyncMock(return_value=False)
    monkeypatch.setattr(monitor, 'record_decision', log)
    await monitor.track_profile_breakers(conn, Redis(), m, NOW, {})
    assert conn.status == 'PAUSED'
    assert len(conn.events) == 1
    # A later pass can recover both the failed decision audit and notification from the lifecycle event.
    log.return_value = True
    await monitor.notify_profile_breakers(conn, Redis())
    assert log.await_count >= 2
    assert len(conn.outbox) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['LOSS_STREAK', 'REJECT_STREAK'])
async def test_review_resume_requires_new_streak_event(monkeypatch, kind):
    conn, m, redis = BreakerConn(), breaker_profile(), Redis()
    conn.loss_trigger = kind == 'LOSS_STREAK'
    conn.reject_trigger = kind == 'REJECT_STREAK'
    monkeypatch.setattr(monitor, 'pause', fake_pause(conn, m))
    monkeypatch.setattr(monitor, 'record_decision', AsyncMock(return_value=True))
    health = {'BTCUSDT.BINANCE': {'arrival_ts_ns': int(NOW.timestamp() * 1e9)}}
    await monitor.track_profile_breakers(conn, redis, m, NOW, health)
    conn.status = 'ACTIVE'  # operator resume retains durable historical event markers
    await monitor.track_profile_breakers(conn, redis, m, NOW, health)
    assert conn.status == 'ACTIVE'
    assert len(conn.events) == 1
    conn.loss_id, conn.command_id = 'loss-2', 'command-2'
    await monitor.track_profile_breakers(conn, redis, m, NOW, health)
    assert conn.status == 'PAUSED'
    assert len(conn.events) == 2
    assert len(conn.outbox) == 2


@pytest.mark.asyncio
async def test_review_reject_query_excludes_lifecycle_and_reductions(monkeypatch):
    conn, m = BreakerConn(), breaker_profile()
    monkeypatch.setattr(monitor, 'pause', fake_pause(conn, m))
    await monitor.track_profile_breakers(conn, Redis(), m, NOW, {
        'BTCUSDT.BINANCE': {'arrival_ts_ns': int(NOW.timestamp() * 1e9)}})
    assert "command_type IN ('SUBMIT_ORDER', 'MANUAL_ORDER')" in conn.command_sql
    assert "payload->>'reduce_only'" in conn.command_sql
    assert "payload->>'side'" in conn.command_sql  # spot SELL is a reduction
    assert conn.status == 'ACTIVE'


@pytest.mark.asyncio
async def test_review_breakers_run_despite_equity_error(monkeypatch):
    conn, m = BreakerConn(), breaker_profile()
    conn.fetch = AsyncMock(return_value=[dict(id=m.id)])
    @asynccontextmanager
    async def acquire():
        yield conn
    monkeypatch.setattr(monitor, 'account_backstop', AsyncMock())
    monkeypatch.setattr(monitor, 'notify_profile_breakers', AsyncMock())
    monkeypatch.setattr(monitor, 'notify_profile_decisions', AsyncMock())
    monkeypatch.setattr(monitor, 'load_mandate', AsyncMock(return_value=m))
    monkeypatch.setattr(monitor, 'track_mandate', AsyncMock(side_effect=RuntimeError('bad equity quote')))
    breaker = AsyncMock()
    monkeypatch.setattr(monitor, 'track_profile_breakers', breaker)
    monkeypatch.setattr(monitor, 'profile_daily_summary', AsyncMock())
    await monitor.mandate_monitor_once(SimpleNamespace(dispatcher=SimpleNamespace()),
                                      SimpleNamespace(acquire=acquire), Redis(), now=NOW)
    breaker.assert_awaited_once()


@pytest.mark.asyncio
async def test_review_absent_ingest_after_start_grace_and_last_healthy(monkeypatch):
    conn, m, redis = BreakerConn(), breaker_profile(), Redis()
    monkeypatch.setattr(monitor, 'pause', fake_pause(conn, m))
    monkeypatch.setattr(monitor, 'record_decision', AsyncMock(return_value=True))
    conn.started = NOW - dt.timedelta(minutes=9)
    await monitor.track_profile_breakers(conn, redis, m, NOW, {})
    assert conn.status == 'ACTIVE'
    await monitor.track_profile_breakers(conn, redis, m, NOW + dt.timedelta(minutes=2), {})
    assert conn.status == 'PAUSED'
    conn.status = 'ACTIVE'
    conn.started = NOW - dt.timedelta(hours=1)
    await monitor.track_profile_breakers(conn, redis, m, NOW, {
        'BTCUSDT.BINANCE': {'arrival_ts_ns': int(NOW.timestamp() * 1e9)}})
    assert conn.status == 'ACTIVE'
    await monitor.track_profile_breakers(conn, redis, m, NOW + dt.timedelta(minutes=9), {})
    assert conn.status == 'ACTIVE'
    await monitor.track_profile_breakers(conn, redis, m, NOW + dt.timedelta(minutes=11), {})
    assert conn.status == 'PAUSED'


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['LOSS_STREAK', 'REJECT_STREAK'])
async def test_review_real_resume_and_entry_only_rejections(kind):
    """Real lifecycle resume consumes historical episodes; unrelated refusals never trip entries."""
    import os

    import asyncpg
    from cryptonite_execution.mandate_store import load_mandate
    from cryptonite_execution.profile_lifecycle import start
    from migrated_db import seed_paper_account
    from test_mandate_hard_stop import make_mandate

    url = os.environ.get('DATABASE_URL', '')
    if not url.endswith('/cryptonite_ci_p18'):
        pytest.skip('requires the dedicated P1.8 database')
    pool = await asyncpg.create_pool(url, min_size=1, max_size=2)
    mid = None
    previous_equity = None
    try:
        async with pool.acquire() as conn:
            await seed_paper_account(conn)
            previous_equity = await conn.fetchval("SELECT total_equity FROM paper_accounts WHERE account_id='PAPER-ACCOUNT-001'")
            await conn.execute("UPDATE paper_accounts SET total_equity=100000 WHERE account_id='PAPER-ACCOUNT-001'")
        m = await make_mandate(pool, 1)
        mid = m.id
        async with pool.acquire() as conn:
            await conn.execute('UPDATE ai_mandates SET breaker_max_loss_streak=1, breaker_max_reject_streak=1 WHERE id=$1', mid)
            m = await load_mandate(conn, mid)
            sql = """INSERT INTO execution_commands (idempotency_key, command_type, target_instance_id,
                       payload, status, command_outcome, environment)
                     VALUES ($1, $2, $3, $4::jsonb, 'REJECTED', 'REJECTED', 'PAPER')"""
            for command, payload in [('PAUSE', {}), ('STOP', {}), ('RESUME', {}), ('DEPLOY', {}),
                                     ('CLOSE_POSITION', {'instrument_id': 'BTCUSDT.BINANCE', 'side': 'SELL'}),
                                     ('SUBMIT_ORDER', {'instrument_id': 'BTCUSDT.BINANCE', 'side': 'SELL'}),
                                     ('SUBMIT_ORDER', {'instrument_id': 'BTCUSDT-PERP.BINANCE', 'side': 'BUY', 'reduce_only': True})]:
                await conn.execute(sql, uuid.uuid4().hex, command, m.instance_id, json.dumps(payload))
            health = {'BTCUSDT.BINANCE': {'arrival_ts_ns': int(NOW.timestamp() * 1e9)}}
            await monitor.track_profile_breakers(conn, Redis(), m, NOW, health)
            assert (await load_mandate(conn, mid)).status is MandateStatus.ACTIVE
            for episode in range(2):
                if kind == 'REJECT_STREAK':
                    await conn.execute(sql, uuid.uuid4().hex, 'SUBMIT_ORDER', m.instance_id,
                                       json.dumps(dict(instrument_id='BTCUSDT.BINANCE', side='BUY')))
                else:
                    await conn.execute(
                        """INSERT INTO paper_positions (position_id, account_id, instance_id, instrument_id,
                           side, entry_price, quantity, current_price, realized_pnl, status, closed_at)
                           VALUES ($1, 'PAPER-ACCOUNT-001', $2, 'BTCUSDT.BINANCE', 'LONG', 100, 0, 90, -10,
                                   'CLOSED', $3)""", uuid.uuid4().hex, m.instance_id, NOW + dt.timedelta(seconds=episode))
                await monitor.track_profile_breakers(conn, Redis(), m, NOW, health)
                assert (await load_mandate(conn, mid)).status is MandateStatus.PAUSED
                m = await start(conn, mid, actor='operator:test', totp_ok=True, settings=None)
                await monitor.track_profile_breakers(conn, Redis(), m, NOW, health)
                assert (await load_mandate(conn, mid)).status is MandateStatus.ACTIVE
                assert await conn.fetchval("SELECT count(*) FROM ai_autonomy_events WHERE mandate_id=$1 AND actor='risk:breaker'", mid) == episode + 1
                assert await conn.fetchval("SELECT count(*) FROM notifications_outbox WHERE kind='PROFILE_BREAKER' AND payload->>'mandate_id'=$1", str(mid)) == episode + 1
    finally:
        if mid is not None:
            async with pool.acquire() as conn:
                await conn.execute("DELETE FROM notifications_outbox WHERE payload->>'mandate_id'=$1", str(mid))
                await conn.execute('DELETE FROM paper_positions WHERE instance_id=$1', m.instance_id)
                await conn.execute('DELETE FROM execution_commands WHERE target_instance_id=$1', m.instance_id)
                await conn.execute('DELETE FROM ai_profile_coin_locks WHERE mandate_id=$1', mid)
                await conn.execute('DELETE FROM ai_autonomy_events WHERE mandate_id=$1', mid)
                await conn.execute('DELETE FROM ai_mandates WHERE id=$1', mid)
        if previous_equity is not None:
            await pool.execute("UPDATE paper_accounts SET total_equity=$1 WHERE account_id='PAPER-ACCOUNT-001'", previous_equity)
        await pool.close()
