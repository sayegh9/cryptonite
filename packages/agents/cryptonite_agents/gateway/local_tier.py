"""Health of the local model tier (Ollama on the workstation, blueprint §4.7 / A6.1).

The gateway asks ``is_down(model)`` before calling an ``ollama:`` primary. A tier that is down, or
that has not pulled that model, is skipped straight to the route's cloud fallback (DEGRADE roles)
instead of waiting out the request timeout on every call; FAIL_CLOSED roles get no fallback either
way. A failed local call triggers a fresh probe of Ollama's ``/api/tags`` (cheap, lists the pulled
models) rather than marking the whole tier down: a bad answer or a tool error is not an outage.

The last result is published to Redis (``local_tier:health``) so the API can show it.
"""

import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from typing import Any

import httpx

logger = logging.getLogger("cryptonite.agents.gateway.local_tier")

HEALTH_KEY = "local_tier:health"
HEALTH_TTL_S = 15.0  # how long a probe result is trusted
PROBE_TIMEOUT_S = 2.0
HEARTBEAT_EX_S = 120  # the API treats a missing key as "unknown"


def ollama_root(base_url: str | None = None) -> str:
    """``http://host:11434/v1`` (the OpenAI-compatible URL the backend uses) → ``http://host:11434``."""
    url = (base_url or os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")).rstrip("/")
    return url[: -len("/v1")] if url.endswith("/v1") else url


def is_local(model_id: str) -> bool:
    return model_id.startswith("ollama:")


def _tag(name: str) -> str:
    """Ollama lists untagged models as ``name:latest``."""
    return name if ":" in name else f"{name}:latest"


@dataclass
class LocalTierStatus:
    healthy: bool
    checked_at: float  # unix seconds
    models: list[str] = field(default_factory=list)
    error: str | None = None
    source: str = "probe"  # "probe" or "call" (a failed model call)


class LocalTierHealth:
    def __init__(self, redis=None, base_url: str | None = None, transport: httpx.AsyncBaseTransport | None = None,
                 ttl_s: float = HEALTH_TTL_S) -> None:
        self.redis = redis
        self.root = ollama_root(base_url)
        self._transport = transport
        self.ttl_s = ttl_s
        self._status: LocalTierStatus | None = None

    @property
    def status(self) -> LocalTierStatus | None:
        return self._status

    async def probe(self) -> LocalTierStatus:
        try:
            async with httpx.AsyncClient(base_url=self.root, timeout=PROBE_TIMEOUT_S, transport=self._transport) as c:
                resp = await c.get("/api/tags")
                resp.raise_for_status()
                models = sorted(m.get("name", "") for m in resp.json().get("models", []))
            status = LocalTierStatus(True, time.time(), models)
        except Exception as e:  # unreachable, refused, timeout, bad JSON: all mean "down"
            status = LocalTierStatus(False, time.time(), error=f"{type(e).__name__}: {e}"[:300])
        await self._set(status)
        return status

    async def is_down(self, model_id: str | None = None) -> bool:
        """True when the tier is down, or when ``model_id`` (``ollama:<name>``) is not pulled. Probes
        again once the last result is stale."""
        s = self._status
        if s is None or time.time() - s.checked_at >= self.ttl_s:
            s = await self.probe()
        if not s.healthy:
            return True
        if model_id and is_local(model_id) and s.models:
            return _tag(model_id.partition(":")[2]) not in {_tag(m) for m in s.models}
        return False

    async def on_call_failure(self, model_id: str, error: Exception) -> None:
        """A local model call failed: re-probe instead of assuming an outage. Only the error *class*
        is logged; the message can carry prompt or tool content."""
        status = await self.probe()
        logger.warning("Local call to %s failed (%s); tier probe: %s", model_id, type(error).__name__,
                       "healthy" if status.healthy else "DOWN")

    async def mark_up(self) -> None:
        if self._status is not None and not self._status.healthy:
            await self._set(LocalTierStatus(True, time.time(), self._status.models, source="call"))

    async def _set(self, status: LocalTierStatus) -> None:
        changed = self._status is None or self._status.healthy != status.healthy
        self._status = status
        if changed:
            logger.info("Local tier %s (%s)", "healthy" if status.healthy else "DOWN", status.error or self.root)
        if self.redis is not None:
            try:
                await self.redis.set(HEALTH_KEY, json.dumps({**asdict(status), "root": self.root}), ex=HEARTBEAT_EX_S)
            except Exception as e:  # health reporting must never break a model call
                logger.debug("Could not publish local tier health: %s", e)


def read_published(raw: Any) -> dict[str, Any] | None:
    """Decode what ``LocalTierHealth`` published (API side); None when absent or unreadable."""
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None
