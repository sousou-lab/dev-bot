from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass

from app.logging_setup import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class ContainerResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False


class DockerSupervisor:
    """Manage Docker containers for isolated agent execution."""

    def __init__(self, image: str = "python:3.14-slim") -> None:
        self.image = image

    def available(self) -> bool:
        """Check if Docker is available on the system."""
        return shutil.which("docker") is not None

    async def run_container(
        self,
        *,
        command: list[str],
        workspace_dir: str,
        writable_roots: list[str],
        network_access: bool = False,
        timeout_ms: int = 3600000,
        env: dict[str, str] | None = None,
    ) -> ContainerResult:
        """Run a command inside a Docker container with isolation."""
        docker_args = ["docker", "run", "--rm"]

        # Network isolation
        if not network_access:
            docker_args.extend(["--network", "none"])

        # Mount workspace directories
        for root in writable_roots:
            docker_args.extend(["-v", f"{root}:{root}"])

        # Working directory
        docker_args.extend(["-w", workspace_dir])

        # Environment variables
        for key, value in (env or {}).items():
            docker_args.extend(["-e", f"{key}={value}"])

        # Image and command
        docker_args.append(self.image)
        docker_args.extend(command)

        timeout_seconds = timeout_ms / 1000.0
        logger.info("Starting container: %s", " ".join(docker_args[:10]))

        try:
            proc = await asyncio.create_subprocess_exec(
                *docker_args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(),
                timeout=timeout_seconds,
            )
            return ContainerResult(
                returncode=proc.returncode or 0,
                stdout=stdout_bytes.decode("utf-8", errors="replace"),
                stderr=stderr_bytes.decode("utf-8", errors="replace"),
            )
        except TimeoutError:
            logger.error("Container timed out after %.1fs", timeout_seconds)
            proc.kill()
            await proc.wait()
            return ContainerResult(
                returncode=-1,
                stdout="",
                stderr="Container execution timed out",
                timed_out=True,
            )
        except Exception as exc:
            logger.error("Container execution failed: %s", exc)
            return ContainerResult(
                returncode=-1,
                stdout="",
                stderr=str(exc),
            )
