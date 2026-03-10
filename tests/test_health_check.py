from __future__ import annotations

import json
import unittest
from unittest.mock import MagicMock

from app.health_check import HealthCheckServer


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
        await server.start()
        self.assertIsNotNone(server.port)
        self.assertTrue(server.port > 0)
        await server.stop()
