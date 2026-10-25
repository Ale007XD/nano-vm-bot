"""
StateContext — runtime state model.

Намеренно НЕ является ORM-объектом.
UserState (database.py) = persistence layer.
StateContext = runtime model для δ(S, Program) → S'.

Жизненный цикл:
    ctx = StateContext.from_db(user_state_row)
    ctx2 = ctx.apply(step_result)
    await persist(ctx2, session)          # опционально
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field
from pydantic import ConfigDict


# ---------------------------------------------------------------------------
# Вспомогательные модели
# ---------------------------------------------------------------------------

class OutboxEntry(BaseModel):
    """Сообщение, ожидающее отправки через Telegram."""
    text: str
    meta: Dict[str, Any] = Field(default_factory=dict)


class MemorySnapshot(BaseModel):
    """
    Легковесный снимок памяти, нужный VM во время прогона программы.
    Не хранит весь ChromaDB — только то, что уже извлечено retrieve_memory.
    """
    entries: List[Dict[str, Any]] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# StateContext
# ---------------------------------------------------------------------------

class StateContext(BaseModel):
    """
    Иммутабельный runtime-state одного пользователя.

    Поля:
        user_id       — Telegram user ID (не internal DB id)
        fsm_state     — текущее состояние FSM ('idle', 'awaiting_input', ...)
        agent_mode    — активный агент ('fastbot', 'planbot', 'skillbot', 'runtime')
        memory        — снимок памяти для текущего прогона VM
        outbox        — очередь исходящих сообщений
        extra         — произвольные данные (skill_name, pending_question и т.п.)
    """
    user_id: int
    fsm_state: str = "idle"
    agent_mode: str = "fastbot"
    memory: MemorySnapshot = Field(default_factory=MemorySnapshot)
    outbox: List[OutboxEntry] = Field(default_factory=list)
    extra: Dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(frozen=True)

    # ------------------------------------------------------------------
    # Фабрика: создать из ORM-объекта UserState
    # ------------------------------------------------------------------
    @classmethod
    def from_db(cls, user_state) -> "StateContext":
        """
        Создать StateContext из ORM UserState (database.py).
        user_state может быть None (новый пользователь).
        """
        if user_state is None:
            raise ValueError("user_state is None — передай telegram_id явно через from_defaults()")

        extra: Dict[str, Any] = {}
        if user_state.context and isinstance(user_state.context, dict):
            extra = user_state.context

        return cls(
            user_id=user_state.user_id,          # ForeignKey users.id
            fsm_state=extra.get("fsm_state", "idle"),
            agent_mode=user_state.current_agent or "fastbot",
            extra=extra,
        )

    @classmethod
    def from_defaults(cls, user_id: int, agent_mode: str = "fastbot") -> "StateContext":
        """Создать для нового пользователя или без DB."""
        return cls(user_id=user_id, agent_mode=agent_mode)

    # ------------------------------------------------------------------
    # apply(StepResult) → StateContext'
    # ------------------------------------------------------------------
    def apply(self, result) -> "StateContext":
        """
        Применить StepResult к текущему состоянию.
        Возвращает НОВЫЙ StateContext — оригинал не изменяется (frozen).

        Обрабатывает:
            result.outbox           → добавляет в self.outbox
            result.fsm_transition   → обновляет fsm_state
            result.memory_writes    → добавляет в memory.entries (снимок)

        memory_writes НЕ персистируются здесь — это задача store_memory instruction.
        apply() только фиксирует факт записи в снимке для последующего replay.
        """
        # --- outbox ---
        new_outbox = list(self.outbox)
        for msg in result.outbox:
            new_outbox.append(OutboxEntry(text=msg.text, meta=msg.meta))

        # --- fsm_transition ---
        new_fsm_state = self.fsm_state
        if result.fsm_transition is not None:
            new_fsm_state = result.fsm_transition.new_state

        # --- memory snapshot ---
        new_entries = list(self.memory.entries)
        for mw in result.memory_writes:
            new_entries.append({
                "collection": mw.collection,
                "content": mw.content,
                "metadata": mw.metadata,
            })
        new_memory = MemorySnapshot(entries=new_entries)

        return self.model_copy(update={
            "fsm_state": new_fsm_state,
            "outbox": new_outbox,
            "memory": new_memory,
        })

    # ------------------------------------------------------------------
    # Сериализация для персистентности → UserState.context (JSON)
    # ------------------------------------------------------------------
    def to_db_context(self) -> Dict[str, Any]:
        """
        Сериализовать в dict для сохранения в UserState.context (JSON Column).
        outbox и memory НЕ персистируются — они ephemeral.
        """
        return {
            **self.extra,
            "fsm_state": self.fsm_state,
        }
