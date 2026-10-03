"""Local model tier health and fallback (blueprint §4.7, A6.1). Ollama is an httpx mock transport and
models are pydantic-ai test doubles: no network, no GPU."""

import json

import httpx
import pytest
from cryptonite_agents.gateway import (
    BudgetGuard,
    BudgetPriority,
    FallbackPolicy,
    Gateway,
    ModelUnavailable,
    RoleRoute,
)
from cryptonite_agents.gateway.local_tier import (
    HEALTH_KEY,
    LocalTierHealth,
    ollama_root,
    read_published,
)
from cryptonite_agents.gateway.providers.pydantic_ai_backend import override_model
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel

LOCAL, CLOUD = "ollama:qwen3.5:9b", "test:cloud"


class FakeRedis:
    def __init__(self):
        self.data: dict[str, str] = {}

    async def set(self, key, value, ex=None):
        self.data[key] = value

    async def get(self, key):
        return self.data.get(key)


class Ollama:
    """``/api/tags`` mock that can be switched off ("pull the plug") and counts probes."""

    def __init__(self, up=True, models=("qwen3.5:9b",)):
        self.up, self.models, self.probes = up, list(models), 0

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            self.probes += 1
            if not self.up:
                raise httpx.ConnectError("connection refused", request=request)
            assert request.url.path == "/api/tags"
            return httpx.Response(200, json={"models": [{"name": m} for m in self.models]})
        return httpx.MockTransport(handler)


def failing_model() -> FunctionModel:
    def fn(messages, info: AgentInfo):
        raise ModelHTTPError(503, "ollama", "connection refused")
    return FunctionModel(fn)


def gateway(ollama: Ollama, policy=FallbackPolicy.DEGRADE, redis=None) -> Gateway:
    route = RoleRoute("triage", LOCAL, CLOUD, policy, BudgetPriority.DEBATE)
    health = LocalTierHealth(redis=redis, base_url="http://ollama:11434/v1", transport=ollama.transport())
    return Gateway(routes={"triage": route}, budget=BudgetGuard(), local_tier=health)


def test_root_url_drops_the_openai_suffix():
    assert ollama_root("http://100.64.0.7:11434/v1") == "http://100.64.0.7:11434"
    assert ollama_root("http://ollama:11434/") == "http://ollama:11434"


async def test_probe_reports_healthy_with_models_and_publishes_it():
    redis = FakeRedis()
    health = LocalTierHealth(redis=redis, base_url="http://ollama:11434/v1", transport=Ollama().transport())
    status = await health.probe()
    assert status.healthy and status.models == ["qwen3.5:9b"]
    published = read_published(redis.data[HEALTH_KEY])
    assert published["healthy"] is True and published["root"] == "http://ollama:11434"


async def test_unreachable_ollama_is_down():
    health = LocalTierHealth(base_url="http://ollama:11434/v1", transport=Ollama(up=False).transport())
    assert await health.is_down()
    assert "ConnectError" in health.status.error


async def test_probe_result_is_cached_within_the_ttl():
    ollama = Ollama()
    health = LocalTierHealth(base_url="http://ollama:11434/v1", transport=ollama.transport())
    for _ in range(5):
        assert not await health.is_down()
    assert ollama.probes == 1


async def test_healthy_local_tier_answers_without_falling_back():
    with override_model(LOCAL, TestModel(custom_output_text="local")), override_model(CLOUD, TestModel()):
        res = await gateway(Ollama()).run("triage", "classify")
    assert (res.output, res.model, res.fell_back) == ("local", LOCAL, False)


async def test_pulling_the_plug_falls_back_and_later_calls_skip_the_local_model():
    """The A6.1 fallback drill: Ollama goes away mid-operation; the failing local call re-probes, finds
    the tier down and falls back; the next call goes straight to the cloud without touching Ollama."""
    calls = {"local": 0}

    def local_fn(messages, info: AgentInfo):
        calls["local"] += 1
        raise ModelHTTPError(503, "ollama", "connection refused")

    redis = FakeRedis()
    ollama = Ollama(up=True)
    gw = gateway(ollama, redis=redis)
    assert not await gw.local_tier.is_down(LOCAL)  # healthy and cached
    ollama.up = False  # pull the plug
    with override_model(LOCAL, FunctionModel(local_fn)), override_model(CLOUD, TestModel(custom_output_text="cloud")):
        first = await gw.run("triage", "classify")
        second = await gw.run("triage", "classify")
    assert (first.model, first.fell_back) == (CLOUD, True)
    assert (second.model, second.fell_back) == (CLOUD, True)  # answered by a model other than the primary
    assert calls["local"] == 1
    published = json.loads(redis.data[HEALTH_KEY])
    # what is published is the probe's own error, never the model call's text (it can carry prompt content)
    assert published["healthy"] is False and published["error"].startswith("ConnectError")
    assert "status_code" not in published["error"]


async def test_a_failed_call_on_a_healthy_tier_does_not_take_it_down():
    """A tool error or bad request is not an outage: the re-probe keeps the tier up, so the next call
    is local again (no flapping)."""
    def local_fn(messages, info: AgentInfo):
        raise ModelHTTPError(400, "ollama", "bad request")

    gw = gateway(Ollama(up=True))
    with override_model(LOCAL, FunctionModel(local_fn)), override_model(CLOUD, TestModel(custom_output_text="cloud")):
        first = await gw.run("triage", "classify")
    assert first.model == CLOUD
    assert gw.local_tier.status.healthy
    with override_model(LOCAL, TestModel(custom_output_text="local")), override_model(CLOUD, TestModel()):
        second = await gw.run("triage", "classify")
    assert second.model == LOCAL


async def test_a_model_that_is_not_pulled_is_skipped_without_a_call():
    calls = {"local": 0}

    def local_fn(messages, info: AgentInfo):
        calls["local"] += 1
        raise AssertionError("must not be called")

    gw = gateway(Ollama(models=("gemma4:12b-it-qat",)))
    with override_model(LOCAL, FunctionModel(local_fn)), override_model(CLOUD, TestModel(custom_output_text="cloud")):
        res = await gw.run("triage", "classify")
    assert (res.model, calls["local"]) == (CLOUD, 0)


async def test_a_pinned_local_model_is_never_swapped_for_the_cloud():
    cloud_calls = {"n": 0}

    def cloud_fn(messages, info: AgentInfo):
        cloud_calls["n"] += 1
        raise AssertionError("must not be called")

    with override_model(LOCAL, failing_model()), override_model(CLOUD, FunctionModel(cloud_fn)):
        with pytest.raises(ModelUnavailable):
            await gateway(Ollama(up=False)).run("triage", "classify", model_override=LOCAL)
    assert cloud_calls["n"] == 0


async def test_tier_comes_back_once_the_probe_succeeds():
    ollama = Ollama(up=False)
    gw = gateway(ollama)
    gw.local_tier.ttl_s = 0  # every call re-probes
    with override_model(LOCAL, TestModel(custom_output_text="local")), override_model(CLOUD, TestModel(custom_output_text="cloud")):
        down = await gw.run("triage", "classify")
        ollama.up = True
        up = await gw.run("triage", "classify")
    assert (down.model, up.model) == (CLOUD, LOCAL)


async def test_fail_closed_role_does_not_switch_models_when_the_tier_is_down():
    cloud_calls = {"n": 0}

    def cloud_fn(messages, info: AgentInfo):
        cloud_calls["n"] += 1
        raise AssertionError("must not be called")

    with override_model(LOCAL, failing_model()), override_model(CLOUD, FunctionModel(cloud_fn)):
        with pytest.raises(ModelUnavailable):
            await gateway(Ollama(up=False), FallbackPolicy.FAIL_CLOSED).run("triage", "decide")
    assert cloud_calls["n"] == 0


async def test_cloud_only_routes_never_probe_ollama():
    ollama = Ollama()
    route = RoleRoute("summarize", "test:a", "test:b", FallbackPolicy.DEGRADE, BudgetPriority.OPTIONAL)
    gw = Gateway(routes={"summarize": route}, budget=BudgetGuard(),
                 local_tier=LocalTierHealth(base_url="http://ollama:11434/v1", transport=ollama.transport()))
    with override_model("test:a", TestModel(custom_output_text="a")):
        await gw.run("summarize", "x")
    assert ollama.probes == 0


async def test_streaming_skips_a_down_local_tier():
    with override_model(LOCAL, TestModel(custom_output_text="local")), override_model(CLOUD, TestModel(custom_output_text="cloud")):
        events = [e async for e in gateway(Ollama(up=False)).stream("triage", "classify")]
    done = [e for e in events if e.kind == "done"][0]
    assert done.result.model == CLOUD


def test_published_health_tolerates_garbage():
    assert read_published(None) is None
    assert read_published("{not json") is None
