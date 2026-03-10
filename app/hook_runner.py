from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path

from app.logging_setup import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class HookResult:
    hook_name: str
    success: bool
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""
    skipped: bool = False
    timed_out: bool = False


class HookRunner:
    """Execute lifecycle hook scripts from a scripts directory."""

    def __init__(self, scripts_dir: str, timeout_ms: int = 60000) -> None:
        self.scripts_dir = Path(scripts_dir)
        self.timeout_seconds = timeout_ms / 1000.0

    def _script_path(self, hook_name: str) -> Path:
        return self.scripts_dir / f"agent_{hook_name}.sh"

    async def run(self, hook_name: str, env: dict[str, str] | None = None) -> HookResult:
        script = self._script_path(hook_name)

        if not script.exists():
            logger.debug("Hook script not found, skipping: %s", script)
            return HookResult(hook_name=hook_name, success=True, skipped=True)

        run_env = {**os.environ, **(env or {})}

        try:
            proc = await asyncio.create_subprocess_exec(
                str(script),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=run_env,
            )
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(),
                timeout=self.timeout_seconds,
            )
            returncode = proc.returncode or 0
            stdout = stdout_bytes.decode("utf-8", errors="replace")
            stderr = stderr_bytes.decode("utf-8", errors="replace")

            if returncode != 0:
                logger.warning("Hook %s failed with exit code %d: %s", hook_name, returncode, stderr.strip())

            return HookResult(
                hook_name=hook_name,
                success=returncode == 0,
                returncode=returncode,
                stdout=stdout,
                stderr=stderr,
            )

        except TimeoutError:
            logger.error("Hook %s timed out after %.1fs", hook_name, self.timeout_seconds)
            proc.kill()
            await proc.wait()
            return HookResult(
                hook_name=hook_name,
                success=False,
                returncode=-1,
                timed_out=True,
            )
        except Exception as exc:
            logger.error("Hook %s raised an exception: %s", hook_name, exc)
            return HookResult(
                hook_name=hook_name,
                success=False,
                returncode=-1,
                stderr=str(exc),
            )
