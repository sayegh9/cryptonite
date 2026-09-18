import asyncio
import websockets
import json

async def test():
    async with websockets.connect("ws://127.0.0.1:8000/ws/stream") as ws:
        await ws.send(json.dumps({
            "action": "subscribe",
            "channels": ["open_kline"],
            "instruments": ["BTCUSDT.BINANCE"]
        }))
        for _ in range(2):
            msg = await ws.recv()
            print("MSG:", msg)

asyncio.run(test())
