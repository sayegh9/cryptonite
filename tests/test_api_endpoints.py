import pytest
from cryptonite_api.main import app, lifespan
from httpx import ASGITransport, AsyncClient
from starlette.testclient import TestClient



@pytest.fixture
async def api_client(operator_auth):
    async with lifespan(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            yield client


@pytest.mark.asyncio
async def test_health_and_ready_endpoints(api_client):
    resp = await api_client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"

    ready_resp = await api_client.get("/ready")
    assert ready_resp.status_code == 200
    ready_data = ready_resp.json()
    assert ready_data["status"] == "ready"
    assert ready_data["checks"]["postgres"] is True
    assert ready_data["checks"]["redis"] is True


@pytest.mark.asyncio
async def test_instruments_endpoint(api_client):
    # Self-contained (LE-9): a fresh test DB has no reference data until ingest syncs instruments
    await app.state.pool.execute(
        """INSERT INTO instruments (instrument_id, raw_symbol, base_currency, quote_currency, price_precision,
                                    size_precision, price_increment, size_increment)
           VALUES ('BTCUSDT.BINANCE', 'BTCUSDT', 'BTC', 'USDT', 2, 5, 0.01, 0.00001)
           ON CONFLICT DO NOTHING""")
    resp = await api_client.get("/api/v1/instruments")
    assert resp.status_code == 200
    instruments = resp.json()
    assert isinstance(instruments, list)
    symbols = [item["symbol"] for item in instruments]
    assert "BTCUSDT" in symbols or "ETHUSDT" in symbols


@pytest.mark.asyncio
async def test_scanners_crud_and_job_enqueue(api_client):
    # 1. List scanners (seeds presets if empty)
    list_resp = await api_client.get("/api/v1/scanners")
    assert list_resp.status_code == 200
    scanners = list_resp.json()
    assert len(scanners) >= 1
    first_spec = scanners[0]
    spec_id = first_spec["id"]

    # 2. Enqueue manual scan run (HTTP 202 Accepted)
    run_resp = await api_client.post(f"/api/v1/scanners/{spec_id}/run")
    assert run_resp.status_code == 202
    run_data = run_resp.json()
    assert run_data["status"] == "ACCEPTED"
    assert "job_id" in run_data
    job_id = run_data["job_id"]

    # 3. Check job status
    job_resp = await api_client.get(f"/api/v1/scanners/jobs/{job_id}")
    assert job_resp.status_code == 200
    job_data = job_resp.json()
    assert job_data["status"] in ["PENDING", "RUNNING", "COMPLETED"]


@pytest.mark.asyncio
async def test_bars_endpoint_and_query_limit(api_client):
    # 1. Fetch bars for BTCUSDT
    resp = await api_client.get("/api/v1/bars?symbol=BTCUSDT&timeframe=1m&limit=10")
    assert resp.status_code == 200
    bars = resp.json()
    assert isinstance(bars, list)
    if bars:
        b = bars[0]
        assert "time" in b
        assert "bar_open_ns" in b
        assert "bar_close_inclusive_ns" in b
        assert "interval_end_exclusive_ns" in b
        assert "open" in b
        assert "close" in b
        assert b["time"] == b["bar_open_ns"] // 1_000_000_000

    # 2. Exceed query limit (> 2000 should be rejected by validation)
    invalid_resp = await api_client.get("/api/v1/bars?symbol=BTCUSDT&limit=2500")
    assert invalid_resp.status_code == 422


def test_websocket_origin_validation_and_subscription():
    with TestClient(app) as client:
        # 1. Unauthorized origin should be rejected with code 1008
        with pytest.raises(Exception):
            with client.websocket_connect("/ws/stream", headers={"Origin": "http://evil-site.com"}):
                pass

        # 2. Authorized origin connects successfully
        with client.websocket_connect("/ws/stream", headers={"Origin": "http://127.0.0.1:3000"}) as ws:
            # Invalid instrument
            ws.send_json({"action": "subscribe", "channel": "kline", "symbol": "INVALIDCOIN", "timeframe": "1m"})
            err_resp = ws.receive_json()
            assert "error" in err_resp

            # Invalid timeframe
            ws.send_json({"action": "subscribe", "channel": "kline", "symbol": "BTCUSDT", "timeframe": "2m"})
            tf_err = ws.receive_json()
            assert "error" in tf_err

            # Valid subscription
            ws.send_json({"action": "subscribe", "channel": "kline", "symbol": "BTCUSDT", "timeframe": "1m"})
            sub_resp = ws.receive_json()
            assert sub_resp["status"] == "subscribed"
            assert sub_resp["instrument_id"] == "BTCUSDT.BINANCE"
