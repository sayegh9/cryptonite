"""Market data routes: 24h ticker prices, symbol search, coin profiles, market overview."""

import json
import logging

import httpx
from cryptonite_api.routes.auth import require_operator, require_csrf
from fastapi import APIRouter, HTTPException, Query, Request

logger = logging.getLogger("cryptonite.api.market")
router = APIRouter(prefix="/api/v1/market", tags=["market"])

BINANCE_BASE = "https://api.binance.com"
COINGECKO_BASE = "https://api.coingecko.com/api/v3"
HEADERS = {"User-Agent": "Cryptonite/0.2.0"}


@router.get("/prices")
async def get_market_prices(
    request: Request,
    symbols: str = Query(..., description="Comma-separated symbols e.g. BTCUSDT,ETHUSDT"),
) -> list[dict]:
    """Fetch 24h ticker stats for given symbols. Cached 10s per symbol in Redis."""
    redis = request.app.state.stream_hub.redis
    sym_list = [s.strip().upper() for s in symbols.split(",") if s.strip()]
    if not sym_list:
        return []

    results: list[dict] = []
    uncached: list[str] = []

    for sym in sym_list:
        cached = await redis.get(f"market:24h:{sym}")
        if cached:
            results.append(json.loads(cached))
        else:
            uncached.append(sym)

    if uncached:
        try:
            # Binance accepts list of symbols as JSON array string WITHOUT SPACES
            syms_param = '[' + ','.join(f'"{s}"' for s in uncached) + ']'
            async with httpx.AsyncClient(timeout=6.0, headers=HEADERS) as client:
                resp = await client.get(
                    f"{BINANCE_BASE}/api/v3/ticker/24hr",
                    params={"symbols": syms_param},
                )
                resp.raise_for_status()
            for t in resp.json():
                row = {
                    "symbol": t["symbol"],
                    "last_price": float(t["lastPrice"]),
                    "price_change_pct": float(t["priceChangePercent"]),
                    "high_24h": float(t["highPrice"]),
                    "low_24h": float(t["lowPrice"]),
                    "volume": float(t["volume"]),
                    "quote_volume": float(t["quoteVolume"]),
                }
                await redis.setex(f"market:24h:{t['symbol']}", 10, json.dumps(row))
                results.append(row)
        except Exception as exc:
            logger.warning("Binance ticker fetch failed for %s: %s", uncached, exc)

    return results


@router.get("/search")
async def search_symbols(
    request: Request,
    q: str = Query(..., min_length=1, description="Search query, e.g. BTC"),
) -> list[dict]:
    """Search tradeable Binance USDT symbols. Exchange info cached 1h."""
    redis = request.app.state.stream_hub.redis
    cache_key = "binance:exchange_info:symbols"
    cached = await redis.get(cache_key)

    if cached:
        all_symbols: list[dict] = json.loads(cached)
    else:
        try:
            async with httpx.AsyncClient(timeout=10.0, headers=HEADERS) as client:
                resp = await client.get(f"{BINANCE_BASE}/api/v3/exchangeInfo")
                resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"Binance exchange info unavailable: {exc}") from exc

        all_symbols = [
            {"symbol": s["symbol"], "base": s["baseAsset"], "quote": s["quoteAsset"]}
            for s in data["symbols"]
            if s["status"] == "TRADING" and s["quoteAsset"] == "USDT"
        ]
        await redis.setex(cache_key, 3600, json.dumps(all_symbols))

    q_upper = q.strip().upper()
    return [
        s for s in all_symbols
        if q_upper in s["symbol"] or q_upper in s["base"]
    ][:20]


@router.get("/profile/{symbol}")
async def get_coin_profile(symbol: str, request: Request) -> dict:
    """Merge Binance 24h ticker + CoinGecko metadata. Cached 15 min."""
    sym = symbol.upper()
    redis = request.app.state.stream_hub.redis
    pool = request.app.state.pool

    cache_key = f"market:profile:{sym}"
    cached = await redis.get(cache_key)
    if cached:
        return json.loads(cached)

    # Resolve base asset from DB (e.g. BTCUSDT -> BTC)
    row = await pool.fetchrow(
        "SELECT base_currency, coingecko_id FROM instruments WHERE instrument_id = $1",
        f"{sym}.BINANCE",
    )
    if not row:
        raise HTTPException(status_code=404, detail=f"{sym} not onboarded in Cryptonite")

    base = row["base_currency"].lower()
    coingecko_id = row["coingecko_id"] or base  # fall back to lowercase base asset

    result: dict = {"symbol": sym}

    async with httpx.AsyncClient(timeout=8.0, headers=HEADERS) as client:
        # CoinGecko free — no API key needed
        try:
            cg_resp = await client.get(
                f"{COINGECKO_BASE}/coins/{coingecko_id}",
                params={
                    "localization": "false",
                    "tickers": "false",
                    "community_data": "false",
                    "developer_data": "false",
                },
            )
            if cg_resp.status_code == 200:
                cg = cg_resp.json()
                market_data = cg.get("market_data", {})
                result.update({
                    "name": cg.get("name"),
                    "description": (cg.get("description") or {}).get("en", ""),
                    "categories": cg.get("categories", [])[:5],
                    "image_url": (cg.get("image") or {}).get("large"),
                    "market_cap_usd": (market_data.get("market_cap") or {}).get("usd"),
                    "circulating_supply": market_data.get("circulating_supply"),
                    "max_supply": market_data.get("max_supply"),
                    "ath_usd": (market_data.get("ath") or {}).get("usd"),
                    "ath_date": (market_data.get("ath_date") or {}).get("usd"),
                    "links": {
                        "homepage": ((cg.get("links") or {}).get("homepage") or [""])[0],
                        "twitter": (cg.get("links") or {}).get("twitter_screen_name"),
                        "github": ((cg.get("links") or {}).get("repos_url", {}).get("github") or [""])[0],
                    },
                })
        except Exception as exc:
            logger.warning("CoinGecko profile fetch failed for %s: %s", coingecko_id, exc)

        # Always fetch Binance 24h stats
        try:
            bn_resp = await client.get(
                f"{BINANCE_BASE}/api/v3/ticker/24hr",
                params={"symbol": sym},
            )
            if bn_resp.status_code == 200:
                bn = bn_resp.json()
                result.update({
                    "last_price": float(bn["lastPrice"]),
                    "change_24h_pct": float(bn["priceChangePercent"]),
                    "high_24h": float(bn["highPrice"]),
                    "low_24h": float(bn["lowPrice"]),
                    "volume_24h_quote": float(bn["quoteVolume"]),
                })
        except Exception as exc:
            logger.warning("Binance 24h fetch failed for %s: %s", sym, exc)

    if len(result) <= 1:  # only has 'symbol'
        raise HTTPException(status_code=503, detail="Unable to fetch market data from upstream providers")

    await redis.setex(cache_key, 900, json.dumps(result))
    return result


@router.get("/overview")
async def get_market_overview(request: Request) -> list[dict]:
    """Top 30 USDT pairs by quote volume. Cached 15s."""
    redis = request.app.state.stream_hub.redis
    cache_key = "market:overview"
    cached = await redis.get(cache_key)
    if cached:
        return json.loads(cached)

    try:
        async with httpx.AsyncClient(timeout=8.0, headers=HEADERS) as client:
            resp = await client.get(f"{BINANCE_BASE}/api/v3/ticker/24hr")
            resp.raise_for_status()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Binance overview unavailable: {exc}") from exc

    usdt_pairs = [
        t for t in resp.json()
        if str(t.get("symbol", "")).endswith("USDT")
        and float(t.get("quoteVolume", 0)) > 0
    ]
    usdt_pairs.sort(key=lambda t: float(t["quoteVolume"]), reverse=True)

    top30 = [
        {
            "symbol": t["symbol"],
            "last_price": float(t["lastPrice"]),
            "change_pct": float(t["priceChangePercent"]),
            "volume_quote": float(t["quoteVolume"]),
        }
        for t in usdt_pairs[:30]
    ]
    await redis.setex(cache_key, 15, json.dumps(top30))
    return top30
