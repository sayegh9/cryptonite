"""The hashes a strategy instance is version-locked to: the source of the code it runs (D4, F-H7) and the risk
policy it was deployed under (both replace ``'dummy_hash'``)."""

import hashlib
import json

from cryptonite_strategies.registry import STRATEGY_REGISTRY, get_strategy_source_hash

from cryptonite_orchestrator.ops_monitor import effective_limits

UNREGISTERED = "UNREGISTERED"


def risk_policy_hash(dispatcher) -> str:
    """SHA-256 of the risk limits the dispatcher enforces at deploy time: the same figures the Operator page shows
    (``effective_limits``' paper block: daily loss, order size band, fee, backstop, cross-mandate cap), in a canonical
    form. Two instances deployed under the same limits carry the same hash; a changed limit changes it."""
    limits = effective_limits(dispatcher)["paper"]
    return hashlib.sha256(json.dumps(limits, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


async def instance_source_hash(conn, strategy_name: str, strategy_version_id) -> str:
    """SHA-256 of the code the instance runs. A registered strategy runs from ``STRATEGY_REGISTRY``, so
    it is the hash of that class's source file as this process loaded it (the same one
    ``get_strategy_meta`` reports as ``code_hash``). Anything else falls back to the hash recorded with
    its stored version (AI-generated code); the loader never runs an unregistered strategy."""
    if strategy_name in STRATEGY_REGISTRY:
        return get_strategy_source_hash(strategy_name)
    recorded = await conn.fetchval("SELECT code_hash FROM strategy_versions WHERE id = $1::uuid", str(strategy_version_id))
    return recorded or UNREGISTERED
