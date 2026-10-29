"""
Эскалация на живого оператора (витрина): хранение сессий «клиент ↔ топик».

Схема: у каждого клиента, позвавшего оператора, в группе поддержки
(OPERATOR_CHAT_ID — супергруппа с включёнными темами) создаётся тема-топик.
Сообщения клиента копируются в тему, ответы операторов в теме — клиенту.
Здесь — только состояние (БД), антифлуд и память последнего вопроса;
Telegram-логика — в app/handlers/operator.py.

Витрин несколько: сессия принадлежит витрине (tenant = slug), ключ — пара
(tenant, client_id). Пустой tenant — наследная единственная витрина.

Сообщения в БД НЕ хранятся (они живут в Telegram): таблица содержит только
соответствие клиент ↔ тема и статус.
"""
from __future__ import annotations

import time
from collections import deque
from datetime import datetime, timezone
from typing import Deque, Dict, Hashable, Optional

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Integer,
    String,
    Text,
    select,
    text,
    update,
)

from app.core.ext_db import ExtBase, resolve_session_maker

DEFAULT_TENANT = ""
STATUS_OPEN = "open"
STATUS_CLOSED = "closed"


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class OperatorSession(ExtBase):
    __tablename__ = "operator_sessions"

    tenant = Column(String(32), primary_key=True, default=DEFAULT_TENANT, server_default="")  # slug витрины
    client_id = Column(BigInteger, primary_key=True)  # telegram id клиента (= id его личного чата)
    thread_id = Column(Integer, nullable=False, index=True)  # message_thread_id темы в группе
    status = Column(String(8), nullable=False, default=STATUS_OPEN, index=True)
    client_name = Column(String(128), nullable=False, default="")
    summary = Column(Text, nullable=False, default="")  # резюме для оператора (для веб-интерфейса)
    opened_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)


async def open_session(
    client_id: int, thread_id: int, client_name: str = "", session_maker=None,
    *, tenant: str = DEFAULT_TENANT,
) -> None:
    """Открыть (или переоткрыть с новой темой) сессию клиента."""
    sm = resolve_session_maker(session_maker)
    async with sm() as s:
        row = await s.get(OperatorSession, (tenant, client_id))
        if row is None:
            s.add(
                OperatorSession(
                    tenant=tenant,
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
            row.summary = ""
            row.opened_at = _now()
        await s.commit()


async def set_summary(
    client_id: int, summary: str, session_maker=None, *, tenant: str = DEFAULT_TENANT
) -> None:
    sm = resolve_session_maker(session_maker)
    async with sm() as s:
        await s.execute(
            update(OperatorSession)
            .where(OperatorSession.tenant == tenant, OperatorSession.client_id == client_id)
            .values(summary=summary)
        )
        await s.commit()


async def list_open_sessions(
    limit: int = 100, session_maker=None, *, tenant: Optional[str] = None
) -> list:
    """Открытые обращения (новые первыми) — для веб-интерфейса оператора.

    tenant=None — всех витрин, иначе только указанной.
    """
    sm = resolve_session_maker(session_maker)
    async with sm() as s:
        query = select(OperatorSession).where(OperatorSession.status == STATUS_OPEN)
        if tenant is not None:
            query = query.where(OperatorSession.tenant == tenant)
        rows = await s.execute(query.order_by(OperatorSession.opened_at.desc()).limit(limit))
        return list(rows.scalars())


async def get_open_by_client(
    client_id: int, session_maker=None, *, tenant: str = DEFAULT_TENANT
) -> Optional[OperatorSession]:
    sm = resolve_session_maker(session_maker)
    async with sm() as s:
        row = await s.get(OperatorSession, (tenant, client_id))
        return row if row and row.status == STATUS_OPEN else None


async def get_open_by_thread(
    thread_id: int, session_maker=None, *, tenant: str = DEFAULT_TENANT
) -> Optional[OperatorSession]:
    """Тема принадлежит группе конкретной витрины: номера тем в разных группах совпадают."""
    sm = resolve_session_maker(session_maker)
    async with sm() as s:
        return (
            await s.execute(
                select(OperatorSession).where(
                    OperatorSession.tenant == tenant,
                    OperatorSession.thread_id == thread_id,
                    OperatorSession.status == STATUS_OPEN,
                )
            )
        ).scalar_one_or_none()


async def close_session(
    client_id: int, session_maker=None, *, tenant: str = DEFAULT_TENANT
) -> bool:
    """Закрыть сессию клиента. True, если была открыта."""
    sm = resolve_session_maker(session_maker)
    async with sm() as s:
        res = await s.execute(
            update(OperatorSession)
            .where(
                OperatorSession.tenant == tenant,
                OperatorSession.client_id == client_id,
                OperatorSession.status == STATUS_OPEN,
            )
            .values(status=STATUS_CLOSED)
        )
        await s.commit()
        return bool(res.rowcount)


async def set_thread(
    client_id: int, thread_id: int, session_maker=None, *, tenant: str = DEFAULT_TENANT
) -> None:
    """Заменить тему открытой сессии (старую удалили вручную)."""
    sm = resolve_session_maker(session_maker)
    async with sm() as s:
        await s.execute(
            update(OperatorSession)
            .where(OperatorSession.tenant == tenant, OperatorSession.client_id == client_id)
            .values(thread_id=thread_id)
        )
        await s.commit()


async def adopt_legacy_sessions(tenant: str, session_maker=None) -> int:
    """Сессии с пустым tenant (созданы до разделения витрин) передать витрине tenant.

    Конфликтующие строки (уже есть сессия клиента у этой витрины) пропускаются.
    """
    if tenant == DEFAULT_TENANT:
        return 0
    sm = resolve_session_maker(session_maker)
    async with sm() as s:
        res = await s.execute(
            text("UPDATE OR IGNORE operator_sessions SET tenant = :t WHERE tenant = ''"),
            {"t": tenant},
        )
        await s.commit()
        return int(res.rowcount or 0)


# --------------------------------------------------------------------------- #
# Антифлуд: скользящее окно на клиента (in-memory, процесс витрины один)
# --------------------------------------------------------------------------- #
class RateLimiter:
    def __init__(self, max_events: int = 30, window: float = 60.0, clock=time.monotonic):
        self.max_events = max_events
        self.window = window
        self._clock = clock
        self._events: Dict[Hashable, Deque[float]] = {}

    def allow(self, key: Hashable) -> bool:
        now = self._clock()
        q = self._events.setdefault(key, deque())
        while q and now - q[0] > self.window:
            q.popleft()
        if len(q) >= self.max_events:
            return False
        q.append(now)
        return True
