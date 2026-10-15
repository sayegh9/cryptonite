"""Brain-started debates (WS-1E): the trigger route tags them and caps them per UTC day; the Brain's
stage_debate_proposal tool says so. The cap lives in Redis on the test index (never DB 0/1)."""

import datetime
import json
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


class _AsyncView:
    """The route awaits its Redis calls; the test client is synchronous, so each call runs inline."""

    def __init__(self, client):
        self._client = client

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
