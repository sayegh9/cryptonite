import asyncio
import httpx
import json

async def test():
    async with httpx.AsyncClient() as client:
        # with spaces
        resp1 = await client.get("https://api.binance.com/api/v3/ticker/24hr", params={"symbols": json.dumps(["BTCUSDT", "ETHUSDT"])})
        print("With spaces:", resp1.status_code)
        
        # without spaces
        resp2 = await client.get("https://api.binance.com/api/v3/ticker/24hr", params={"symbols": json.dumps(["BTCUSDT", "ETHUSDT"]).replace(" ", "")})
        print("Without spaces:", resp2.status_code)

asyncio.run(test())
