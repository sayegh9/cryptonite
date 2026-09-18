import urllib.request, json, time
payload = {
    'strategy_slug': 'ema-cross',
    'instrument_id': 'BTCUSDT.BINANCE',
    'bar_type': 'BTCUSDT.BINANCE-1-MINUTE-LAST-EXTERNAL',
    'timeframe': '1m',
    'parameters': {},
    'warmup_start_ns': int((time.time() - 12*3600)*1_000_000_000),
    'evaluation_start_ns': int((time.time() - 6*3600)*1_000_000_000),
    'evaluation_end_ns': int(time.time()*1_000_000_000),
    'execution_profile': 'optimistic'
}
req = urllib.request.Request('http://127.0.0.1:8000/api/v1/backtests', data=json.dumps(payload).encode('utf-8'), headers={'Content-Type': 'application/json'})
try:
    with urllib.request.urlopen(req) as response:
        print("Success!", response.read().decode())
except Exception as e:
    print("Error:", e.read().decode())
