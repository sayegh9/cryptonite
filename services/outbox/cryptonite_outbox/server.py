"""Lightweight HTTP health and readiness server for Cryptonite Outbox Relay."""

import asyncio
import json
import logging
import os
import time
from typing import Any

from cryptonite_outbox.relay import OutboxRelay

logger = logging.getLogger("cryptonite.outbox.server")


class OutboxHealthServer:
    """Async HTTP server handling /health and /ready endpoints on port 8081."""

    def __init__(self, relay: OutboxRelay, host: str = "0.0.0.0", port: int = 8081) -> None:
        self.relay = relay
        self.host = host
        self.port = port
        self._server: asyncio.Server | None = None
        self._start_time = time.monotonic()

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle_client, self.host, self.port)
        logger.info("Outbox health server listening on http://%s:%d", self.host, self.port)

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
                    "published_count": self.relay.published_count,
                })
            elif path == "/ready":
                is_ready, details = await self.relay.check_readiness()
                status_code = 200 if is_ready else 503
                self._send_response(writer, status_code, details)
            else:
                self._send_response(writer, 404, {"error": "Not Found"})
        except Exception as e:
            logger.error("Error in outbox health server handler: %s", e)
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
            "Connection: close\r\n\r\n"
        ).encode()
        writer.write(header + body)


async def run_outbox_daemon() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    port = int(os.getenv("PORT", "8081"))
    relay = OutboxRelay()
    server = OutboxHealthServer(relay=relay, port=port)

    await relay.start()
    await server.start()

    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await server.stop()
        await relay.stop()


if __name__ == "__main__":
    asyncio.run(run_outbox_daemon())
