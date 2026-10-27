"""
Эскалация на живого оператора (витрина): хранение сессий «клиент ↔ топик».

Схема: у каждого клиента, позвавшего оператора, в группе поддержки
(OPERATOR_CHAT_ID — супергруппа с включёнными темами) создаётся тема-топик.
Сообщения клиента копируются в тему, ответы операторов в теме — клиенту.
Здесь — только состояние (БД), антифлуд и память последнего вопроса;
Telegram-логика — в app/handlers/operator.py.

Сообщения в БД НЕ хранятся (они живут в Telegram): таблица содержит только
соответствие клиент ↔ тема и статус.
"""
from __future__ import annotations

import time
from collections import OrderedDict, deque
from datetime import datetime, timezone
from typing import Deque, Dict, Optional

from sqlalchemy import BigInteger, Column, DateTime, Integer, String, select, update

from app.core.ext_db import ExtBase, resolve_session_maker

STATUS_OPEN = "open"
STATUS_CLOSED = "closed"


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class OperatorSession(ExtBase):
    __tablename__ = "operator_sessions"

    client_id = Column(BigInteger, primary_key=True)  # telegram id клиента (= id его личного чата)
    thread_id = Column(Integer, nullable=False, index=True)  # message_thread_id темы в группе
    status = Column(String(8), nullable=False, default=STATUS_OPEN, index=True)
    client_name = Column(String(128), nullable=False, default="")
    opened_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)


async def open_session(
    client_id: int, thread_id: int, client_name: str = "", session_maker=None
) -> None:
    """Открыть (или переоткрыть с новой темой) сессию клиента."""
    sm = resolve_session_maker(session_maker)
    async with sm() as s:
        row = await s.get(OperatorSession, client_id)
        if row is None:
            s.add(
                OperatorSession(
                    client_id=client_id,
                    thread_id=thread_id,
                    status=STATUS_OPEN,
                    client_name=client_name[:128],
                )
            )
        else:
            row.thread_id = thread_id
            row.status = STATUS_OPEN
            row.client_name = client_name[:128]
            row.opened_at = _now()
        await s.commit()


async def get_open_by_client(client_id: int, session_maker=None) -> Optional[OperatorSession]:
    sm = resolve_session_maker(session_maker)
    async with sm() as s:
        row = await s.get(OperatorSession, client_id)
        return row if row and row.status == STATUS_OPEN else None


async def get_open_by_thread(thread_id: int, session_maker=None) -> Optional[OperatorSession]:
    sm = resolve_session_maker(session_maker)
    async with sm() as s:
        return (
            await s.execute(
                select(OperatorSession).where(
                    OperatorSession.thread_id == thread_id,
                    OperatorSession.status == STATUS_OPEN,
                )
            )
        ).scalar_one_or_none()


async def close_session(client_id: int, session_maker=None) -> bool:
    """Закрыть сессию клиента. True, если была открыта."""
    sm = resolve_session_maker(session_maker)
    async with sm() as s:
        res = await s.execute(
            update(OperatorSession)
            .where(OperatorSession.client_id == client_id, OperatorSession.status == STATUS_OPEN)
            .values(status=STATUS_CLOSED)
        )
        await s.commit()
        return bool(res.rowcount)


async def set_thread(client_id: int, thread_id: int, session_maker=None) -> None:
    """Заменить тему открытой сессии (старую удалили вручную)."""
    sm = resolve_session_maker(session_maker)
    async with sm() as s:
        await s.execute(
            update(OperatorSession)
            .where(OperatorSession.client_id == client_id)
            .values(thread_id=thread_id)
        )
        await s.commit()


# --------------------------------------------------------------------------- #
# Антифлуд: скользящее окно на клиента (in-memory, процесс витрины один)
# --------------------------------------------------------------------------- #
class RateLimiter:
    def __init__(self, max_events: int = 30, window: float = 60.0, clock=time.monotonic):
        self.max_events = max_events
        self.window = window
        self._clock = clock
        self._events: Dict[int, Deque[float]] = {}

    def allow(self, key: int) -> bool:
        now = self._clock()
        q = self._events.setdefault(key, deque())
        while q and now - q[0] > self.window:
            q.popleft()
        if len(q) >= self.max_events:
            return False
        q.append(now)
        return True


# --------------------------------------------------------------------------- #
# Последний вопрос клиента (для карточки обращения). Ограниченный LRU.
# --------------------------------------------------------------------------- #
_questions: "OrderedDict[int, str]" = OrderedDict()
_QUESTIONS_MAX = 2000


def remember_question(client_id: int, text: str) -> None:
    _questions[client_id] = (text or "")[:500]
    _questions.move_to_end(client_id)
    while len(_questions) > _QUESTIONS_MAX:
        _questions.popitem(last=False)


def last_question(client_id: int) -> str:
    return _questions.get(client_id, "")
