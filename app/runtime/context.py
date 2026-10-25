"""
VMContext — контейнер зависимостей для ExecutionVM.

Все типы явные. VM и instructions зависят от протоколов,
не от конкретных реализаций.
"""
from __future__ import annotations

from typing import Any, Dict, TYPE_CHECKING

from app.runtime.state_context import StateContext
from app.runtime.llm_adapter import LLMProtocol

if TYPE_CHECKING:
    from app.core.memory import VectorMemory


class VMContext:
    """
    Передаётся в каждую Instruction.execute().

    Поля:
        state       — текущий StateContext (иммутабельный)
        llm         — LLMProtocol-адаптер
        memory      — VectorMemory (ChromaDB)
        tools       — реестр вызываемых инструментов (ToolRegistry или dict)
        variables   — переменные прогона: {step_id: output}
                      мутируется VM во время run(), не виден снаружи
    """

    def __init__(
        self,
        state: StateContext,
        llm: LLMProtocol,
        memory: "VectorMemory",
        tools: Any,
    ):
        self.state = state
        self.llm = llm
        self.memory = memory
        self.tools = tools
        self.variables: Dict[str, Any] = {}

    @property
    def user_id(self) -> int:
        return self.state.user_id
