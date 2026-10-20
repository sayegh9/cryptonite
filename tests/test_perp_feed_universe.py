"""Stage 1.3 (leverage plan): perp quotes and marks for every fed symbol with a USD-M perpetual (same list and
cap as spot ingest), and the futures bookTicker reconnect recovery hook. Binance is never called: the USD-M
symbol list and the REST book ticker are stubbed."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from cryptonite_ingest.recorder import IngestRecorder
from cryptonite_scanner_service.derivatives_feed import DerivativesFeed, mark_stream_url
from cryptonite_schemas.universe import INGEST_UNIVERSE_KEY, IngestUniverse


def recorder(tmp_path, symbols, perp_env=(), max_symbols=12, monkeypatch=None):
    if monkeypatch is not None:
        monkeypatch.setenv("INGEST_MAX_SYMBOLS", str(max_symbols))
    return IngestRecorder(symbols=list(symbols), timeframes=["1m"], nautilus_catalog_path=str(tmp_path / "cat"),
                          analytics_lake_path=str(tmp_path / "lake"), perp_quote_symbols=list(perp_env))


async def test_fed_symbols_with_a_usdm_perpetual_get_perp_quotes(tmp_path, monkeypatch):
    r = recorder(tmp_path, ["BTCUSDT", "XRPUSDT", "WLDUSDT", "SPOTONLYUSDT"], perp_env=["BTCUSDT", "ETHUSDT"],
                 monkeypatch=monkeypatch)

    async def listed(self):
        return {"BTCUSDT", "ETHUSDT", "XRPUSDT", "WLDUSDT"}

    monkeypatch.setattr(IngestRecorder, "_fetch_usdm_perpetuals", listed)
    await r._apply_perp_quote_symbols()
    # env perps first and never dropped (ETH has no spot feed here but stays), then the spot symbols that list
    assert r.perp_quote_symbols == ["BTCUSDT", "ETHUSDT", "XRPUSDT", "WLDUSDT"]
    assert set(r.universe().perp_symbols) == {"BTCUSDT", "ETHUSDT", "XRPUSDT", "WLDUSDT"}


async def test_perp_list_respects_the_ingest_cap(tmp_path, monkeypatch):
    r = recorder(tmp_path, ["BTCUSDT", "XRPUSDT", "WLDUSDT"], perp_env=["BTCUSDT", "ETHUSDT"], max_symbols=3,
                 monkeypatch=monkeypatch)

    async def listed(self):
        return {"BTCUSDT", "ETHUSDT", "XRPUSDT", "WLDUSDT"}

    monkeypatch.setattr(IngestRecorder, "_fetch_usdm_perpetuals", listed)
    await r._apply_perp_quote_symbols()
    assert r.perp_quote_symbols == ["BTCUSDT", "ETHUSDT", "XRPUSDT"]


async def test_unknown_usdm_list_keeps_the_env_perps_only(tmp_path, monkeypatch):
    r = recorder(tmp_path, ["BTCUSDT", "XRPUSDT"], perp_env=["BTCUSDT"], monkeypatch=monkeypatch)
    await r._apply_perp_quote_symbols()  # conftest stubs the list as unknown
    assert r.perp_quote_symbols == ["BTCUSDT"]


async def test_switch_off_keeps_env_behaviour(tmp_path, monkeypatch):
    monkeypatch.setenv("INGEST_PERP_FROM_ONBOARDED", "0")
    r = recorder(tmp_path, ["BTCUSDT", "XRPUSDT"], perp_env=["BTCUSDT"], monkeypatch=monkeypatch)

    async def listed(self):
        raise AssertionError("must not be called when switched off")

    monkeypatch.setattr(IngestRecorder, "_fetch_usdm_perpetuals", listed)
    await r._apply_perp_quote_symbols()
    assert r.perp_quote_symbols == ["BTCUSDT"]


async def test_perp_reconnect_publishes_rest_book_tickers(tmp_path, monkeypatch):
    r = recorder(tmp_path, ["BTCUSDT", "XRPUSDT"], perp_env=["BTCUSDT", "XRPUSDT"], monkeypatch=monkeypatch)
    client = MagicMock()
    client._last_published_quote = {"BTCUSDT": (("1", "1", "1", "1"), 1)}
    client._handle_book_ticker = AsyncMock()
    r.perp_quote_client = client
    rows = [
        {"symbol": "BTCUSDT", "bidPrice": "60000.1", "bidQty": "2", "askPrice": "60000.2", "askQty": "3"},
        {"symbol": "XRPUSDT", "bidPrice": "0.51", "bidQty": "100", "askPrice": "0.5101", "askQty": "200"},
        {"symbol": "DOGEUSDT", "bidPrice": "0.1", "bidQty": "1", "askPrice": "0.11", "askQty": "1"},
    ]
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return httpx.Response(200, json=rows)

    real_client = httpx.AsyncClient
    monkeypatch.setattr("cryptonite_ingest.recorder.httpx.AsyncClient",
                        lambda **kw: real_client(transport=httpx.MockTransport(handler)))
    await r._on_perp_stream_reconnect()
    assert seen["url"].endswith("/fapi/v1/ticker/bookTicker")
    published = [c.args[0] for c in client._handle_book_ticker.await_args_list]
    assert [p["s"] for p in published] == ["BTCUSDT", "XRPUSDT"]  # not DOGE: not fed
    assert published[0] == {"s": "BTCUSDT", "b": "60000.1", "B": "2", "a": "60000.2", "A": "3"}
    assert "BTCUSDT" not in client._last_published_quote  # coalescing bypassed so the quote goes out


async def test_perp_reconnect_recovery_failure_is_quiet(tmp_path, monkeypatch):
    r = recorder(tmp_path, ["BTCUSDT"], perp_env=["BTCUSDT"], monkeypatch=monkeypatch)
    r.perp_quote_client = MagicMock(_handle_book_ticker=AsyncMock(), _last_published_quote={})
    real_client = httpx.AsyncClient
    monkeypatch.setattr("cryptonite_ingest.recorder.httpx.AsyncClient",
                        lambda **kw: real_client(transport=httpx.MockTransport(lambda req: httpx.Response(503))))
    await r._on_perp_stream_reconnect()
    r.perp_quote_client._handle_book_ticker.assert_not_awaited()


class _Redis:
    def __init__(self, value=None):
        self.value = value

    async def get(self, key):
        assert key == INGEST_UNIVERSE_KEY
        return self.value


def test_mark_feed_follows_the_ingest_universe():
    universe = IngestUniverse.build(["BTCUSDT", "XRPUSDT"], ["1m"], ["BTCUSDT", "XRPUSDT", "WLDUSDT"])
    feed = DerivativesFeed(_Redis(universe.model_dump_json()), None, ["BTCUSDT", "ETHUSDT"])
    assert asyncio.run(feed.follow_universe_once()) is True
    assert feed.symbols == ["BTCUSDT", "ETHUSDT", "WLDUSDT", "XRPUSDT"]
    assert feed.ws_url == mark_stream_url(feed.symbols)
    assert feed._resubscribe
    feed._resubscribe = False
    assert asyncio.run(feed.follow_universe_once()) is False  # unchanged: no reconnect
    assert not feed._resubscribe


def test_unknown_universe_keeps_the_current_marks():
    feed = DerivativesFeed(_Redis(None), None, ["BTCUSDT", "ETHUSDT"])
    assert asyncio.run(feed.follow_universe_once()) is False
    assert feed.symbols == ["BTCUSDT", "ETHUSDT"]


@pytest.mark.parametrize("perps", [[], ["BTCUSDT"]])
def test_base_symbols_are_never_dropped(perps):
    universe = IngestUniverse.build(["BTCUSDT"], ["1m"], perps)
    feed = DerivativesFeed(_Redis(json.dumps(universe.model_dump())), None, ["BTCUSDT", "ETHUSDT"])
    asyncio.run(feed.follow_universe_once())
    assert feed.symbols[:2] == ["BTCUSDT", "ETHUSDT"]


def test_mark_stream_reconnects_when_the_symbols_change():
    """After a frame, a changed list closes the socket and reconnects on the new URL."""
    urls = []

    class _WS:
        def __init__(self, url):
            self.url = url

        async def __aenter__(self):
            urls.append(self.url)
            return self

        async def __aexit__(self, *a):
            return False

        async def recv(self):
            await asyncio.sleep(0)
            return json.dumps({"data": []})

    async def run():
        feed = DerivativesFeed(_Redis(None), None, ["BTCUSDT"], connect=lambda url, **kw: _WS(url))
        stop = asyncio.Event()
        task = asyncio.create_task(feed.run_mark_stream(stop))
        await asyncio.sleep(0.01)
        feed.update_symbols(["XRPUSDT"])
        for _ in range(100):
            if len(urls) >= 2:
                break
            await asyncio.sleep(0.01)
        stop.set()
        await asyncio.wait_for(task, 2)

    asyncio.run(run())
    assert "btcusdt@markPrice" in urls[0] and "xrpusdt@markPrice" not in urls[0]
    assert "xrpusdt@markPrice" in urls[-1]
