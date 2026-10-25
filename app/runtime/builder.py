from datetime import datetime, timezone
from typing import Any, Dict, Optional

from .step_result import (
    FSMTransition,
    MemoryWrite,
    OutboxMessage,
    StepMeta,
    StepResult,
)


class StepResultBuilder:
    def __init__(self, step_id: str, action: str):
        self._data = {
            "step_id": step_id,
            "action": action,
            "status": "ok",
            "memory_writes": [],
            "outbox": [],
        }

    def output(self, value: Any):
        self._data["output"] = value
        return self

    def memory_write(self, collection: str, content: str, metadata: Optional[Dict] = None):
        self._data["memory_writes"].append(
            MemoryWrite(
                collection=collection,
                content=content,
                metadata=metadata or {},
            )
        )
        return self

    def transition(self, new_state: str, reason: Optional[str] = None):
        self._data["fsm_transition"] = FSMTransition(
            new_state=new_state,
            reason=reason,
        )
        return self

    def message(self, text: str, meta: Optional[Dict] = None):
        self._data["outbox"].append(
            OutboxMessage(text=text, meta=meta or {})
        )
        return self

    def error(self, error: str):
        self._data["status"] = "error"
        self._data["error"] = error
        return self

    def meta(self, latency_ms: Optional[int] = None, provider: Optional[str] = None):
        self._data["meta"] = StepMeta(
            step_id=self._data["step_id"],
            action=self._data["action"],
            timestamp=datetime.now(timezone.utc),
            latency_ms=latency_ms,
            provider=provider,
        )
        return self

    def build(self) -> StepResult:
        if "meta" not in self._data:
            self.meta()
        return StepResult(**self._data)
