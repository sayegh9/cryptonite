"""Refuse what cannot be traded (D4, F-H7): mandate and strategy-instance creation check the universe
ingest publishes (``ingest:universe``). An un-fed symbol or timeframe is a 422 naming the reason; an
unknown universe (key missing, stale or Redis down) is a 503, never a silent pass."""

import logging

from cryptonite_schemas.universe import INGEST_UNIVERSE_KEY, UNIVERSE_UNKNOWN, IngestUniverse
from fastapi import HTTPException, Request

logger = logging.getLogger("cryptonite.api.universe")


def _redis(request: Request):
    hub = getattr(request.app.state, "stream_hub", None)
    return getattr(hub, "redis", None) or getattr(request.app.state, "redis", None)


async def load_universe(request: Request) -> IngestUniverse | None:
    redis = _redis(request)
    if redis is None:
        return None
    try:
        return IngestUniverse.parse(await redis.get(INGEST_UNIVERSE_KEY))
    except Exception as e:
        logger.warning("Could not read %s: %s", INGEST_UNIVERSE_KEY, e)
        return None


async def require_universe(request: Request) -> IngestUniverse:
    universe = await load_universe(request)
    if universe is None:
        raise HTTPException(status_code=503, detail=(
            f"{UNIVERSE_UNKNOWN}: ingest has not published which symbols and timeframes it feeds "
            f"({INGEST_UNIVERSE_KEY} missing or expired; is cryptonite_ingest running?). Creation is refused until it does."))
    return universe


async def require_fed(request: Request, instruments: list[str], timeframe: str | None = None) -> None:
    """422 naming the first instrument (or the timeframe) ingest does not feed; 503 if the universe is unknown."""
    universe = await require_universe(request)
    for inst in instruments:
        if why := universe.refusal(inst, timeframe):
            raise HTTPException(status_code=422, detail=f"{why} [{inst}{f' {timeframe}' if timeframe else ''}]")
