from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from app.state_store import FileStateStore


@dataclass(frozen=True)
class ApprovalRequest:
    thread_id: int
    run_id: str
    tool_name: str
    input_text: str
    reason: str
    requested_at: str
    status: str = "pending"


class ApprovalCoordinator:
    def __init__(self, state_store: FileStateStore) -> None:
        self.state_store = state_store
        self._pending: dict[int, asyncio.Future[bool]] = {}

    def create_request(
        self, thread_id: int, run_id: str, tool_name: str, input_text: str, reason: str
    ) -> ApprovalRequest:
        request = ApprovalRequest(
            thread_id=thread_id,
            run_id=run_id,
            tool_name=tool_name,
            input_text=input_text,
            reason=reason,
            requested_at=datetime.now(UTC).isoformat(),
        )
        self.state_store.write_artifact(thread_id, "pending_approval.json", asdict(request))
        self.state_store.update_meta(thread_id, runtime_status="awaiting_high_risk_approval")
        loop = asyncio.get_running_loop()
        self._pending[thread_id] = loop.create_future()
        return request

    async def wait_for_resolution(self, thread_id: int, timeout_seconds: int | None = None) -> bool:
        future = self._pending.get(thread_id)
        if future is None:
            return False
        try:
            if timeout_seconds is None:
                return await future
            return await asyncio.wait_for(future, timeout=timeout_seconds)
        except TimeoutError:
            payload = self.state_store.load_artifact(thread_id, "pending_approval.json")
            if isinstance(payload, dict) and payload:
                payload["status"] = "expired"
                payload["expired_at"] = datetime.now(UTC).isoformat()
                self.state_store.write_artifact(thread_id, "pending_approval.json", payload)
            self._pending.pop(thread_id, None)
            return False

    def register_restored_request(self, thread_id: int) -> None:
        if thread_id in self._pending:
            return
        loop = asyncio.get_running_loop()
        self._pending[thread_id] = loop.create_future()

    def resolve(self, thread_id: int, approved: bool, actor: str) -> str:
        future = self._pending.get(thread_id)
        payload = self.state_store.load_artifact(thread_id, "pending_approval.json")
        if isinstance(payload, dict) and payload:
            payload["status"] = "approved" if approved else "rejected"
            payload["resolved_at"] = datetime.now(UTC).isoformat()
            payload["resolved_by"] = actor
            self.state_store.write_artifact(thread_id, "pending_approval.json", payload)
        if future is None:
            return "persisted_only"
        if future.done():
            self._pending.pop(thread_id, None)
            return "stale_future"
        future.set_result(approved)
        self._pending.pop(thread_id, None)
        return "resolved"

    def has_pending_request(self, thread_id: int) -> bool:
        payload = self.state_store.load_artifact(thread_id, "pending_approval.json")
        return isinstance(payload, dict) and payload.get("status") == "pending"


def is_high_risk_command(command: str) -> bool:
    patterns = (
        "alembic upgrade head",
        "alembic revision",
        "terraform apply",
        "kubectl apply",
        "rm -rf",
        "git push --force",
    )
    lowered = command.lower()
    return any(pattern in lowered for pattern in patterns)
