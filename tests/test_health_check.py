from __future__ import annotations

import json
import unittest
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

from app.health_check import HealthCheckServer, _should_retry_on_loopback


class HealthCheckServerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.orchestrator = MagicMock()
        self.orchestrator.pending_count.return_value = 2
        self.orchestrator.active_count.return_value = 1

    async def test_health_response_is_valid_json(self) -> None:
        server = HealthCheckServer(orchestrator=self.orchestrator, port=0)
        body = server.build_health_response()
        parsed = json.loads(body)
        self.assertIn("status", parsed)
        self.assertIn("checks", parsed)
        self.assertIn("uptime_seconds", parsed)

    async def test_health_response_includes_orchestrator_stats(self) -> None:
        server = HealthCheckServer(orchestrator=self.orchestrator, port=0)
        body = server.build_health_response()
        parsed = json.loads(body)
        self.assertEqual(parsed["checks"]["orchestrator"]["pending"], 2)
        self.assertEqual(parsed["checks"]["orchestrator"]["active"], 1)

    async def test_health_status_is_healthy(self) -> None:
        server = HealthCheckServer(orchestrator=self.orchestrator, port=0)
        body = server.build_health_response()
        parsed = json.loads(body)
        self.assertEqual(parsed["status"], "healthy")

    async def test_uptime_is_non_negative(self) -> None:
        server = HealthCheckServer(orchestrator=self.orchestrator, port=0)
        body = server.build_health_response()
        parsed = json.loads(body)
        self.assertGreaterEqual(parsed["uptime_seconds"], 0)

    async def test_start_and_stop_lifecycle(self) -> None:
        server = HealthCheckServer(orchestrator=self.orchestrator, port=0)
        fake_socket = MagicMock()
        fake_socket.getsockname.return_value = ("127.0.0.1", 18080)
        fake_server = AsyncMock()
        fake_server.sockets = [fake_socket]
        fake_server.close = MagicMock()

        with patch("app.health_check.asyncio.start_server", new=AsyncMock(return_value=fake_server)):
            await server.start()
            self.assertEqual(18080, server.port)
            await server.stop()

        fake_server.close.assert_called_once()
        fake_server.wait_closed.assert_awaited_once()

    async def test_start_retries_on_loopback_when_bind_is_not_permitted(self) -> None:
        server = HealthCheckServer(orchestrator=self.orchestrator, port=0, host="0.0.0.0")
        fake_server = AsyncMock()
        fake_server.sockets = []
        err = OSError(1, "Operation not permitted")

        with patch(
            "app.health_check.asyncio.start_server",
            side_effect=[err, fake_server],
        ) as start_server:
            await server.start()

        self.assertEqual("127.0.0.1", server.host)
        self.assertEqual(2, start_server.await_count)
        first_call = start_server.await_args_list[0]
        second_call = start_server.await_args_list[1]
        self.assertEqual("0.0.0.0", first_call.args[1])
        self.assertEqual("127.0.0.1", second_call.args[1])

    def test_wildcard_ephemeral_bind_failure_retries_on_loopback(self) -> None:
        self.assertTrue(
            _should_retry_on_loopback(
                host="0.0.0.0",
                port=0,
                exc=OSError("could not bind on any address out of [('0.0.0.0', 0)]"),
            )
        )
