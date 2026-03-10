from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CommandExecution:
    command: str
    returncode: int
    output: str

