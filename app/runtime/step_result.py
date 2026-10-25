from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class MemoryWrite(BaseModel):
    collection: Literal["episodic", "semantic", "procedural"]
    content: str
    metadata: Dict[str, Any] = Field(default_factory=dict)


class FSMTransition(BaseModel):
    new_state: str
    reason: Optional[str] = None


class OutboxMessage(BaseModel):
    text: str
    meta: Dict[str, Any] = Field(default_factory=dict)


class StepMeta(BaseModel):
    step_id: str
    action: str
    timestamp: datetime
    latency_ms: Optional[int] = None
    provider: Optional[str] = None


class StepResult(BaseModel):
    step_id: str
    action: str

    status: Literal["ok", "error"]

    output: Optional[Any] = None

    memory_writes: List[MemoryWrite] = Field(default_factory=list)
    fsm_transition: Optional[FSMTransition] = None
    outbox: List[OutboxMessage] = Field(default_factory=list)

    error: Optional[str] = None

    meta: StepMeta

    model_config = ConfigDict(frozen=True)
