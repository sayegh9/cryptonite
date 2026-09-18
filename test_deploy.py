import urllib.request, json
payload = {
  "strategy_name": "ema-cross",
  "strategy_version_id": "v1.0.0",
  "instrument_id": "BTCUSDT.BINANCE",
  "timeframe": "BTCUSDT.BINANCE-1-MINUTE-LAST-EXTERNAL",
  "allocation_usd": 10000.0,
  "parameters": {}
}
req = urllib.request.Request('http://127.0.0.1:8000/api/v1/paper/strategies/deploy', data=json.dumps(payload).encode('utf-8'), headers={'Content-Type': 'application/json'})
try:
    with urllib.request.urlopen(req) as response:
        print("Success!", response.read().decode())
except Exception as e:
    print("Error:", e.read().decode())
