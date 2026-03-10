from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from app.logging_setup import get_logger

logger = get_logger(__name__)


class HealthCheckServer:
    """Lightweight HTTP health check server using asyncio."""

    def __init__(self, orchestrator: Any, port: int = 8080) -> None:
        self.orchestrator = orchestrator
        self.port = port
        self._start_time = time.monotonic()
        self._server: asyncio.Server | None = None

    def build_health_response(self) -> str:
        uptime = time.monotonic() - self._start_time
        return json.dumps(
            {
                "status": "healthy",
                "uptime_seconds": round(uptime, 1),
                "checks": {
                    "orchestrator": {
                        "pending": self.orchestrator.pending_count(),
                        "active": self.orchestrator.active_count(),
                    },
                },
            },
            ensure_ascii=False,
        )

    async def _handle_request(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await reader.readline()  # Read the request line
            body = self.build_health_response()
            response = f"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n{body}"
            writer.write(response.encode())
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle_request, "0.0.0.0", self.port)
        addrs = self._server.sockets
        if addrs:
            actual_port = addrs[0].getsockname()[1]
            self.port = actual_port
        logger.info("Health check server started on port %d", self.port)

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            logger.info("Health check server stopped")
