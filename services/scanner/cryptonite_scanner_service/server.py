"""Lightweight HTTP health and readiness server for Cryptonite Scanner Service."""

import asyncio
import json
import logging
import os
import time
from typing import Any

from cryptonite_scanner_service.worker import ScannerWorker

logger = logging.getLogger("cryptonite.scanner.server")


class ScannerHealthServer:
    """Async HTTP server handling /health and /ready endpoints on port 8082."""

    def __init__(self, worker: ScannerWorker, host: str = "0.0.0.0", port: int = 8082) -> None:
        self.worker = worker
        self.host = host
        self.port = port
        self._server: asyncio.Server | None = None
        self._start_time = time.monotonic()

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle_client, self.host, self.port)
        logger.info("Scanner health server listening on http://%s:%d", self.host, self.port)

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

            parts = line.decode("utf-8").strip().split(" ")
            if len(parts) < 2:
                self._send_response(writer, 400, {"error": "Bad Request"})
                return

            method, path = parts[0], parts[1]
            while True:
                h_line = await reader.readline()
                if not h_line or h_line == b"\r\n":
                    break

            if method != "GET":
                self._send_response(writer, 405, {"error": "Method Not Allowed"})
                return

            if path == "/health":
                self._send_response(writer, 200, {
                    "status": "ok",
                    "uptime_s": time.monotonic() - self._start_time,
                    "processed_events": self.worker.processed_events_count,
                    "executed_runs": self.worker.executed_runs_count,
                    "detected_candidates": self.worker.detected_candidates_count,
                    "queued_candidates": self.worker.queued_candidates_count,
                })
            elif path == "/ready":
                is_ready, details = await self._check_readiness()
                status_code = 200 if is_ready else 503
                self._send_response(writer, status_code, details)
            else:
                self._send_response(writer, 404, {"error": "Not Found"})
        except Exception as e:
            logger.error("Error in scanner health server: %s", e)
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

    async def _check_readiness(self) -> tuple[bool, dict[str, Any]]:
        details: dict[str, Any] = {
            "postgres": False,
            "redis": False,
            "status": "unready",
        }
        if not self.worker.pool or not self.worker.redis:
            return False, details

        try:
            val = await self.worker.pool.fetchval("SELECT 1")
            details["postgres"] = (val == 1)
        except Exception as e:
            details["postgres_error"] = str(e)

        try:
            r_ping = await self.worker.redis.ping()
            details["redis"] = bool(r_ping)
        except Exception as e:
            details["redis_error"] = str(e)

        is_ready = details["postgres"] and details["redis"]
        details["status"] = "ready" if is_ready else "unready"
        return is_ready, details

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
            "Connection: close\r\n\r\n"
        ).encode()
        writer.write(header + body)


async def run_scanner_daemon() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    port = int(os.getenv("PORT", "8082"))
    worker = ScannerWorker()
    server = ScannerHealthServer(worker=worker, port=port)

    await worker.start()
    await server.start()

    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await server.stop()
        await worker.stop()


if __name__ == "__main__":
    asyncio.run(run_scanner_daemon())
