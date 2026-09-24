"""Lightweight HTTP server providing /health (liveness) and /ready (readiness) endpoints."""

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("cryptonite.server")


class HealthServer:
    """Async HTTP server handling /health and /ready endpoints without external framework overhead."""

    def __init__(
        self,
        recorder: Any = None,
        host: str = "0.0.0.0",
        port: int = 8080,
    ) -> None:
        self.recorder = recorder
        self.host = host
        self.port = port
        self._server: asyncio.Server | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle_client, self.host, self.port
        )
        logger.info("Health server listening on http://%s:%d", self.host, self.port)

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            line = await reader.readline()
            if not line:
                writer.close()
                await writer.wait_closed()
                return

            request_line = line.decode("utf-8").strip()
            parts = request_line.split(" ")
            if len(parts) < 2:
                self._send_response(writer, 400, {"error": "Bad Request"})
                return

            method, path = parts[0], parts[1]

            # Drain remaining headers
            while True:
                h_line = await reader.readline()
                if not h_line or h_line == b"\r\n":
                    break

            if method != "GET":
                self._send_response(writer, 405, {"error": "Method Not Allowed"})
                return

            if path == "/health":
                self._send_response(writer, 200, {"status": "ok", "uptime_s": time.monotonic()})
            elif path == "/ready":
                is_ready, details = await self._check_readiness()
                status_code = 200 if is_ready else 503
                self._send_response(writer, status_code, details)
            else:
                self._send_response(writer, 404, {"error": "Not Found"})

        except Exception as e:
            logger.error("Error in health server handler: %s", e)
            try:
                self._send_response(writer, 500, {"error": "Internal Server Error"})
            except Exception:
                pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    def _send_response(self, writer: asyncio.StreamWriter, status_code: int, data: dict[str, Any]) -> None:
        status_text = {
            200: "OK",
            400: "Bad Request",
            404: "Not Found",
            405: "Method Not Allowed",
            500: "Internal Server Error",
            503: "Service Unavailable",
        }.get(status_code, "Unknown")

        body = json.dumps(data).encode("utf-8")
        header = (
            f"HTTP/1.1 {status_code} {status_text}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n"
            "\r\n"
        ).encode()
        writer.write(header + body)

    async def _check_readiness(self) -> tuple[bool, dict[str, Any]]:
        """Evaluate full readiness conditions."""
        checks = {
            "postgres": False,
            "advisory_lock": False,
            "nautilus_path_writable": False,
            "analytics_path_writable": False,
            "no_failed_batches": False,
        }
        if not self.recorder:
            return True, {"status": "ready (standalone)", "checks": checks}

        # 1. Check PostgreSQL connection
        if self.recorder.pool:
            try:
                val = await self.recorder.pool.fetchval("SELECT 1")
                checks["postgres"] = (val == 1)
            except Exception as e:
                logger.warning("Readiness: Postgres check failed: %s", e)

        # 2. Check advisory lock ownership
        if self.recorder.coordinator:
            checks["advisory_lock"] = self.recorder.coordinator.is_leader

        # 3. Check data paths writable
        n_path = Path(self.recorder.nautilus_catalog_path)
        a_path = Path(self.recorder.analytics_lake_path)
        checks["nautilus_path_writable"] = self._is_writable(n_path)
        checks["analytics_path_writable"] = self._is_writable(a_path)

        # 4. Check no unrecoverable failed batches
        if self.recorder.pool:
            try:
                failed_count = await self.recorder.pool.fetchval(
                    "SELECT count(*) FROM ingest_batches WHERE status = 'failed'"
                )
                checks["no_failed_batches"] = (failed_count == 0)
            except Exception:
                checks["no_failed_batches"] = False

        all_passed = all(checks.values())
        return all_passed, {
            "status": "ready" if all_passed else "unready",
            "checks": checks,
            "last_closed_bar_ts": self.recorder.last_closed_bar_ts,
        }

    def _is_writable(self, path: Path) -> bool:
        try:
            path.mkdir(parents=True, exist_ok=True)
            test_file = path / f".write_test_{time.time_ns()}"
            test_file.write_text("ok")
            test_file.unlink()
            return True
        except Exception:
            return False


async def run_server_standalone():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    from cryptonite_ingest.recorder import IngestRecorder

    symbols = [s.strip() for s in os.getenv("INGEST_SYMBOLS", "BTCUSDT,ETHUSDT").split(",") if s.strip()]
    timeframes = [t.strip() for t in os.getenv("INGEST_TIMEFRAMES", "1m").split(",") if t.strip()]
    catalog_path = os.getenv("NAUTILUS_CATALOG_PATH", "data/nautilus_catalog")
    analytics_path = os.getenv("ANALYTICS_LAKE_PATH", "data/analytics_lake")
    db_url = os.getenv("DATABASE_URL")
    port = int(os.getenv("PORT", "8080"))

    redis_url = os.getenv("REDIS_URL")
    perp_symbols = [s.strip() for s in os.getenv("INGEST_PERP_SYMBOLS", "").split(",") if s.strip()]
    recorder = IngestRecorder(
        symbols=symbols,
        timeframes=timeframes,
        nautilus_catalog_path=catalog_path,
        analytics_lake_path=analytics_path,
        database_url=db_url,
        redis_url=redis_url,
        perp_quote_symbols=perp_symbols,
    )
    srv = HealthServer(recorder=recorder, port=port)
    await srv.start()
    recorder_task = asyncio.create_task(recorder.start())
    try:
        await recorder_task
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await recorder.stop()
        await srv.stop()


if __name__ == "__main__":
    asyncio.run(run_server_standalone())
