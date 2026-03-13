from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class RunSpec:
    run_id: str
    issue_key: str
    candidate_id: str
    cwd: str
    prompt: str
    model: str = "gpt-5.4"
    service_name: str = "dev-bot"
    output_schema_name: str = "implementation_result_v1"
    artifacts_dir: str = ""
    network_access: bool = False
    writable_roots: list[str] = field(default_factory=list)
    read_only_roots: list[str] = field(default_factory=list)
    allow_turn_steer: bool = False
    allow_thread_resume_same_run_only: bool = True


@dataclass(frozen=True, slots=True)
class RunHandle:
    run_id: str
    thread_id: str
    turn_id: str
    process_id: int | None = None


@dataclass(frozen=True, slots=True)
class RunArtifacts:
    implementation_result: dict[str, Any] | None
    changed_files: list[str]
    summary: str
    returncode: int
    mode: str
    implementation_result_path: str
    raw_event_log_path: str


class ExecutionBackend(Protocol):
    async def start_run(self, spec: RunSpec) -> RunHandle: ...
    async def steer(self, handle: RunHandle, message: str) -> None: ...
    async def interrupt(self, handle: RunHandle) -> None: ...
    async def resume_same_run(self, handle: RunHandle) -> RunHandle: ...
    async def collect_outputs(self, handle: RunHandle) -> RunArtifacts: ...
