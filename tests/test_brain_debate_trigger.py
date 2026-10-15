"""Brain-started debates (WS-1E): the trigger route tags them and caps them per UTC day; the Brain's
stage_debate_proposal tool says so. The cap lives in Redis on the test index (never DB 0/1)."""

import datetime
import json
import urllib.error
import urllib.request
from unittest.mock import MagicMock

import pytest
import redis as sync_redis
from connections import REDIS_URL
from cryptonite_api.main import create_app
from cryptonite_api.routes import agents as agents_routes
from fastapi.testclient import TestClient
from test_route_auth_guards import OPERATOR_TOKEN, _FakeAuthManager

QUEUE = "stream:debate_queue"
CAP_MESSAGE = "Brain debate cap reached (6 per day); ask the operator to start it from the Debates page"


class _AsyncPipeline:
    """A transactional pipeline whose queued commands run when the route awaits execute()."""

    def __init__(self, pipe):
        self._pipe = pipe

    def __getattr__(self, name):
        return getattr(self._pipe, name)  # incr and expire only queue commands on the sync pipeline

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self._pipe.reset()
        return False

    async def execute(self):
        return self._pipe.execute()


class _AsyncView:
    """The route awaits its Redis calls; the test client is synchronous, so each call runs inline."""

    def __init__(self, client):
        self._client = client

    def pipeline(self, *args, **kwargs):
        return _AsyncPipeline(self._client.pipeline(*args, **kwargs))

    def __getattr__(self, name):
        method = getattr(self._client, name)

        async def call(*args, **kwargs):
            return method(*args, **kwargs)

        return call


@pytest.fixture
def cap_redis():
    client = sync_redis.Redis.from_url(REDIS_URL, decode_responses=True)

    def clear():
        for key in client.scan_iter("brain:debates:*"):
            client.delete(key)
        client.delete(QUEUE)

    clear()
    yield client
    clear()
    client.close()


@pytest.fixture
def client(cap_redis, monkeypatch):
    monkeypatch.setattr(agents_routes, "BRAIN_DEBATE_DAILY_CAP", 6)
    app = create_app()
    app.state.auth_manager = _FakeAuthManager()
    app.state.pool = MagicMock()
    app.state.redis = _AsyncView(cap_redis)
    return TestClient(app)


def _trigger(client, origin=None):
    body = {"symbol": "BTCUSDT"}
    if origin is not None:
        body["origin"] = origin
    return client.post("/api/v1/agents/debates/trigger", json=body, headers={"X-API-Token": OPERATOR_TOKEN})


def _counter(redis) -> int:
    today = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d")
    return int(redis.get(f"brain:debates:{today}") or 0)


def _last_queued(redis) -> dict:
    [(_, fields)] = redis.xrevrange(QUEUE, count=1)
    return fields


def test_brain_origin_queues_a_brain_on_demand_debate(client, cap_redis):
    res = _trigger(client, origin="brain")
    assert res.status_code == 200, res.text
    assert res.json()["payload"]["trigger_source"] == "brain_on_demand"
    assert _last_queued(cap_redis)["trigger_source"] == "brain_on_demand"


@pytest.mark.parametrize("origin", [None, "operator"])
def test_operator_origin_still_queues_a_user_on_demand_debate(client, cap_redis, origin):
    res = _trigger(client, origin=origin)
    assert res.status_code == 200, res.text
    assert _last_queued(cap_redis)["trigger_source"] == "user_on_demand"


def test_unknown_origin_is_rejected(client, cap_redis):
    assert _trigger(client, origin="scanner").status_code == 422
    assert cap_redis.xlen(QUEUE) == 0


def test_seventh_brain_trigger_in_a_utc_day_is_refused_and_not_queued(client, cap_redis):
    for _ in range(6):
        assert _trigger(client, origin="brain").status_code == 200
    queued = cap_redis.xlen(QUEUE)

    res = _trigger(client, origin="brain")

    assert res.status_code == 429
    assert res.json()["detail"] == CAP_MESSAGE
    assert _counter(cap_redis) == 6  # the refused attempt was counted and given back
    assert cap_redis.xlen(QUEUE) == queued


def test_operator_triggers_are_not_counted_against_the_brain_cap(client, cap_redis):
    for _ in range(8):
        assert _trigger(client).status_code == 200
    assert _counter(cap_redis) == 0

    for _ in range(6):
        assert _trigger(client, origin="brain").status_code == 200
    assert _counter(cap_redis) == 6


def test_a_brain_trigger_that_fails_to_queue_gives_its_slot_back(client, cap_redis, monkeypatch):
    def queue_down(*_args, **_kwargs):
        raise sync_redis.ConnectionError("queue down")

    monkeypatch.setattr(cap_redis, "xadd", queue_down)
    res = _trigger(client, origin="brain")
    assert res.status_code == 500
    assert _counter(cap_redis) == 0


def test_a_redis_error_on_the_cap_refuses_the_trigger_and_queues_nothing(client, cap_redis, monkeypatch):
    def redis_down(*_args, **_kwargs):
        raise sync_redis.ConnectionError("redis down")

    monkeypatch.setattr(cap_redis, "pipeline", redis_down)
    res = _trigger(client, origin="brain")
    assert res.status_code == 503
    assert res.json()["detail"] == "Brain debate cap unavailable: Redis error; nothing was queued"
    assert cap_redis.xlen(QUEUE) == 0


def _stage(monkeypatch, outcomes):
    """Runs stage_debate_proposal with urlopen answering each call from `outcomes` (an exception or a body)."""
    from cryptonite_agents import tools

    calls = []

    class _Response:
        def __init__(self, body):
            self._body = body

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return self._body

    def fake_urlopen(req, timeout=None):
        calls.append(req.full_url)
        outcome = outcomes[len(calls) - 1]
        if isinstance(outcome, BaseException):
            raise outcome
        return _Response(outcome)

    monkeypatch.setenv("CRYPTONITE_AGENT_API_TOKEN", "machine-token")
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    result = tools.stage_debate_proposal("BTCUSDT", "1h", "BUY", 0.7, "why")
    return result, calls


def test_a_timed_out_stage_does_not_try_another_url(monkeypatch):
    result, calls = _stage(monkeypatch, [urllib.error.URLError(TimeoutError("timed out"))] * 4)
    assert len(calls) == 1
    assert result == ("Error staging proposal: the API did not answer in time; the debate may or may not have been "
                      "queued. Do not retry this turn; tell the operator.")


def test_a_raw_timeout_while_reading_does_not_try_another_url(monkeypatch):
    result, calls = _stage(monkeypatch, [TimeoutError("read timed out")] * 4)
    assert len(calls) == 1
    assert "did not answer in time" in result


def test_a_refused_connection_falls_through_to_the_next_url(monkeypatch):
    result, calls = _stage(monkeypatch, [urllib.error.URLError(ConnectionRefusedError(111, "refused")),
                                            json.dumps({"id": "1-0"}).encode()])
    assert len(calls) == 2
    assert result == "Success: Proposal staged to Action Inbox (Msg ID: 1-0)"


def test_a_non_connection_failure_does_not_try_another_url(monkeypatch):
    result, calls = _stage(monkeypatch, [b"not json", b'{"id": "1-0"}'])
    assert len(calls) == 1  # the API answered, so the proposal may already be staged
    assert result.startswith("Error staging proposal:")


def test_stage_debate_proposal_sends_origin_brain(monkeypatch):
    from cryptonite_agents import tools

    sent = {}

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps({"id": "1-0"}).encode()

    def fake_urlopen(req, timeout=None):
        sent["body"] = json.loads(req.data.decode())
        return _Response()

    monkeypatch.setenv("CRYPTONITE_AGENT_API_TOKEN", "machine-token")
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    result = tools.stage_debate_proposal("BTCUSDT", "1h", "BUY", 0.7, "why")

    assert result.startswith("Success")
    assert sent["body"]["origin"] == "brain"
    assert sent["body"]["symbol"] == "BTCUSDT"
