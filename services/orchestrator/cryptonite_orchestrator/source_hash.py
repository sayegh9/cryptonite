"""The source hash a strategy instance is version-locked to (D4, F-H7; replaces ``'dummy_hash'``)."""

from cryptonite_strategies.registry import STRATEGY_REGISTRY, get_strategy_source_hash

UNREGISTERED = "UNREGISTERED"


async def instance_source_hash(conn, strategy_name: str, strategy_version_id) -> str:
    """SHA-256 of the code the instance runs. A registered strategy runs from ``STRATEGY_REGISTRY``, so
    it is the hash of that class's source file as this process loaded it (the same one
    ``get_strategy_meta`` reports as ``code_hash``). Anything else falls back to the hash recorded with
    its stored version (AI-generated code); the loader never runs an unregistered strategy."""
    if strategy_name in STRATEGY_REGISTRY:
        return get_strategy_source_hash(strategy_name)
    recorded = await conn.fetchval("SELECT code_hash FROM strategy_versions WHERE id = $1::uuid", str(strategy_version_id))
    return recorded or UNREGISTERED
