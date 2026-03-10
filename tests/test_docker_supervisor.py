from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app.runners.docker_supervisor import DockerSupervisor


class DockerSupervisorTests(unittest.IsolatedAsyncioTestCase):
    def test_available_returns_false_when_docker_not_found(self) -> None:
        with patch("shutil.which", return_value=None):
            supervisor = DockerSupervisor()
            self.assertFalse(supervisor.available())

    def test_available_returns_true_when_docker_found(self) -> None:
        with patch("shutil.which", return_value="/usr/bin/docker"):
            supervisor = DockerSupervisor()
            self.assertTrue(supervisor.available())

    async def test_run_container_returns_result(self) -> None:
        """Test running a command in a container returns stdout and exit code."""
        supervisor = DockerSupervisor(image="python:3.14-slim")

        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"hello world\n", b"")
        mock_proc.returncode = 0

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
            result = await supervisor.run_container(
                command=["echo", "hello world"],
                workspace_dir="/tmp/workspace",
                writable_roots=["/tmp/workspace"],
                network_access=False,
                timeout_ms=60000,
            )

        self.assertEqual(result.returncode, 0)
        self.assertIn("hello world", result.stdout)
        self.assertFalse(result.timed_out)

        # Verify docker was called with correct args
        call_args = mock_exec.call_args[0]
        self.assertEqual(call_args[0], "docker")
        self.assertIn("run", call_args)
        self.assertIn("--rm", call_args)
        self.assertIn("--network", call_args)

    async def test_run_container_applies_network_none(self) -> None:
        """When network_access=False, --network none should be set."""
        supervisor = DockerSupervisor(image="python:3.14-slim")

        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"", b"")
        mock_proc.returncode = 0

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
            await supervisor.run_container(
                command=["true"],
                workspace_dir="/tmp/ws",
                writable_roots=["/tmp/ws"],
                network_access=False,
                timeout_ms=60000,
            )

        call_args = mock_exec.call_args[0]
        # Find --network and its value
        for i, arg in enumerate(call_args):
            if arg == "--network":
                self.assertEqual(call_args[i + 1], "none")
                break
        else:
            self.fail("--network flag not found in docker args")

    async def test_run_container_allows_network(self) -> None:
        """When network_access=True, --network none should NOT be set."""
        supervisor = DockerSupervisor(image="python:3.14-slim")

        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"", b"")
        mock_proc.returncode = 0

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
            await supervisor.run_container(
                command=["true"],
                workspace_dir="/tmp/ws",
                writable_roots=["/tmp/ws"],
                network_access=True,
                timeout_ms=60000,
            )

        call_args = mock_exec.call_args[0]
        self.assertNotIn("none", call_args)

    async def test_run_container_mounts_workspace(self) -> None:
        """Workspace dir should be mounted as read-write."""
        supervisor = DockerSupervisor(image="python:3.14-slim")

        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"", b"")
        mock_proc.returncode = 0

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
            await supervisor.run_container(
                command=["ls"],
                workspace_dir="/tmp/workspace",
                writable_roots=["/tmp/workspace"],
                network_access=False,
                timeout_ms=60000,
            )

        call_args = mock_exec.call_args[0]
        # Find -v mount
        found_mount = False
        for i, arg in enumerate(call_args):
            if arg == "-v" and i + 1 < len(call_args) and "/tmp/workspace" in call_args[i + 1]:
                found_mount = True
                break
        self.assertTrue(found_mount, f"Workspace mount not found in args: {call_args}")

    async def test_run_container_timeout(self) -> None:
        """Container should be killed on timeout."""
        supervisor = DockerSupervisor(image="python:3.14-slim")

        mock_proc = AsyncMock()
        mock_proc.communicate.side_effect = TimeoutError()
        mock_proc.kill = MagicMock()
        mock_proc.wait = AsyncMock()

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await supervisor.run_container(
                command=["sleep", "100"],
                workspace_dir="/tmp/ws",
                writable_roots=["/tmp/ws"],
                network_access=False,
                timeout_ms=100,
            )

        self.assertTrue(result.timed_out)
        self.assertNotEqual(result.returncode, 0)

    async def test_run_container_failure(self) -> None:
        """Non-zero exit code should be captured."""
        supervisor = DockerSupervisor(image="python:3.14-slim")

        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"", b"error msg\n")
        mock_proc.returncode = 1

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await supervisor.run_container(
                command=["false"],
                workspace_dir="/tmp/ws",
                writable_roots=["/tmp/ws"],
                network_access=False,
                timeout_ms=60000,
            )

        self.assertEqual(result.returncode, 1)
        self.assertIn("error msg", result.stderr)
