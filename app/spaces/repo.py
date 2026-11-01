"""Доступ к данным пространства: SpaceScope фильтрует и проставляет space_id сам.

Любой код фич (списки, расходы, задачи) работает с таблицами только через scope:

    scope = await open_scope(space_id, user_id)       # SpaceAccessError, если не участник
    items = await scope.list(ChecklistItem, ChecklistItem.done.is_(False))
    await scope.add(ChecklistItem(text="молоко"))     # space_id проставится сам

Так забыть `WHERE space_id = …` нельзя, а чужой space_id в записи — ошибка.
"""
from __future__ import annotations

from typing import Any, List, Optional

from sqlalchemy import delete, func, select, update

from app.core.ext_db import resolve_session_maker
from app.spaces import service


class SpaceAccessError(PermissionError):
    """Пользователь не участник пространства (или пространство выключено)."""


def _check_model(model) -> None:
    if not hasattr(model, "space_id"):
        raise TypeError(f"{getattr(model, '__name__', model)} не принадлежит пространству: нет колонки space_id")


class SpaceScope:
    def __init__(self, space_id: int, user_id: int, role: str, session_maker=None):
        self.space_id = space_id
        self.user_id = user_id
        self.role = role
        self._sm = session_maker

    def _maker(self):
        return resolve_session_maker(self._sm)

    def can(self, needed: str) -> bool:
        return service.role_at_least(self.role, needed)

    def require(self, needed: str) -> None:
        if not self.can(needed):
            raise SpaceAccessError(f"нужна роль {needed}")

    async def list(self, model, *where, order_by=None, limit: Optional[int] = None) -> List[Any]:
        _check_model(model)
        q = select(model).where(model.space_id == self.space_id, *where)
        if order_by is not None:
            q = q.order_by(*(order_by if isinstance(order_by, (list, tuple)) else (order_by,)))
        if limit is not None:
            q = q.limit(limit)
        async with self._maker()() as s:
            return list((await s.execute(q)).scalars())

    async def get(self, model, pk) -> Optional[Any]:
        _check_model(model)
        async with self._maker()() as s:
            obj = await s.get(model, pk)
            return obj if obj is not None and obj.space_id == self.space_id else None

    async def count(self, model, *where) -> int:
        _check_model(model)
        q = select(func.count()).select_from(model).where(model.space_id == self.space_id, *where)
        async with self._maker()() as s:
            return int((await s.execute(q)).scalar_one())

    async def add(self, obj) -> Any:
        _check_model(type(obj))
        current = getattr(obj, "space_id", None)
        if current is not None and current != self.space_id:
            raise SpaceAccessError("запись принадлежит другому пространству")
        obj.space_id = self.space_id
        async with self._maker()() as s:
            s.add(obj)
            await s.commit()
            return obj

    async def update(self, model, pk, **values) -> bool:
        _check_model(model)
        if "space_id" in values:
            raise SpaceAccessError("space_id менять нельзя")
        pk_col = model.__mapper__.primary_key[0]
        async with self._maker()() as s:
            res = await s.execute(
                update(model).where(pk_col == pk, model.space_id == self.space_id).values(**values)
            )
            await s.commit()
            return bool(res.rowcount)

    async def delete(self, model, pk) -> bool:
        _check_model(model)
        pk_col = model.__mapper__.primary_key[0]
        async with self._maker()() as s:
            res = await s.execute(delete(model).where(pk_col == pk, model.space_id == self.space_id))
            await s.commit()
            return bool(res.rowcount)


async def open_scope(space_id: int, user_id: int, session_maker=None) -> SpaceScope:
    role = await service.member_role(space_id, user_id, session_maker)
    if role is None:
        raise SpaceAccessError("нет доступа к пространству")
    return SpaceScope(space_id, user_id, role, session_maker)


async def scope_for_user(user_id: int, session_maker=None) -> Optional[SpaceScope]:
    """Scope первого (по дате вступления) пространства пользователя; None, если он ни в одном не состоит."""
    spaces = await service.spaces_of(user_id, session_maker)
    return await open_scope(spaces[0].id, user_id, session_maker) if spaces else None
