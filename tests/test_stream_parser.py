"""Tests for WebSocket stream parser, open-kline buffering, and closed-bar dispatch."""

import json

import pytest
from cryptonite_ingest.stream import BinanceStreamClient


@pytest.mark.asyncio
async def test_stream_parser_open_vs_closed():
    closed_bars_received = []

    async def mock_callback(symbol, tf, kline, arrival_ns):
        closed_bars_received.append((symbol, tf, kline))

    client = BinanceStreamClient(
        symbols=["BTCUSDT"],
        timeframes=["1m"],
        on_closed_bar=mock_callback,
    )

    # 1. Simulate an open-kline update (x = False)
    open_msg = json.dumps({
        "stream": "btcusdt@kline_1m",
        "data": {
            "e": "kline",
            "E": 1725700030000,
            "s": "BTCUSDT",
            "k": {
                "t": 1725700000000,
                "T": 1725700059999,
                "s": "BTCUSDT",
                "i": "1m",
                "f": 100,
                "L": 200,
                "o": "54000.00",
                "c": "54020.00",
                "h": "54050.00",
                "l": "53990.00",
                "v": "5.500",
                "n": 50,
                "x": False,  # Open bar!
                "q": "297110.00",
                "V": "2.500",
                "Q": "135050.00",
                "B": "0",
            },
        },
    })

    await client._handle_message(open_msg, arrival_ns=1725700030000000000)

    # Verify buffered in memory
    assert "BTCUSDT:1m" in client.open_kline_buffer
    buf_kline = client.open_kline_buffer["BTCUSDT:1m"]
    assert buf_kline.close == "54020.00"
    assert not buf_kline.is_closed
    # Verify no closed bar dispatched
    assert len(closed_bars_received) == 0

    # 2. Simulate final closed-kline event (x = True)
    closed_msg = json.dumps({
        "stream": "btcusdt@kline_1m",
        "data": {
            "e": "kline",
            "E": 1725700060000,
            "s": "BTCUSDT",
            "k": {
                "t": 1725700000000,
                "T": 1725700059999,
                "s": "BTCUSDT",
                "i": "1m",
                "f": 100,
                "L": 300,
                "o": "54000.00",
                "c": "54050.00",
                "h": "54100.00",
                "l": "53990.00",
                "v": "10.500",
                "n": 100,
                "x": True,  # Final closed bar!
                "q": "567525.00",
                "V": "5.000",
                "Q": "270250.00",
                "B": "0",
            },
        },
    })

    await client._handle_message(closed_msg, arrival_ns=1725700060000000000)

    # Verify buffered updated
    assert client.open_kline_buffer["BTCUSDT:1m"].is_closed
    # Verify closed bar dispatched
    assert len(closed_bars_received) == 1
    sym, tf, k_rec = closed_bars_received[0]
    assert sym == "BTCUSDT"
    assert tf == "1m"
    assert k_rec.close == "54050.00"
    assert k_rec.is_closed
