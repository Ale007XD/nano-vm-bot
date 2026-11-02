"""Личное пространство: напоминания пользователя, не состоящего ни в одном общем пространстве.

Создаётся при первом обращении (kind «personal», один участник-владелец, приглашать в него
нельзя) и даёт «одиночке» те же напоминания, что и общему пространству — с обычной доставкой
текстом без участия LLM. Доступа в закрытого бота личное пространство не даёт.
"""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from sqlalchemy import select

from app.core.ext_db import resolve_session_maker
from app.spaces import reminders, repo, service
from app.spaces.models import Reminder, Space, SpaceMember
from app.spaces.service import PERSONAL_KIND

PERSONAL_NAME = "Личное"
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")  # по именам: числовая нумерация cron неоднозначна


async def personal_scope(user_id: int, tz: str = "UTC", session_maker=None) -> repo.SpaceScope:
    """Scope личного пространства пользователя (создаётся при необходимости)."""
    sm = resolve_session_maker(session_maker)
    async with sm() as s:
        existing = (
            await s.execute(
                select(Space.id)
                .join(SpaceMember, SpaceMember.space_id == Space.id)
                .where(SpaceMember.user_id == user_id, SpaceMember.role == "owner",
                       Space.kind == PERSONAL_KIND, Space.is_active.is_(True))
                .order_by(Space.id).limit(1)
            )
        ).scalar_one_or_none()
    if existing is None:
        space = await service.create_space(PERSONAL_NAME, user_id, kind=PERSONAL_KIND, timezone_name=tz,
                                           session_maker=session_maker)
        existing = space.id
    return await repo.open_scope(existing, user_id, session_maker)


def daily_cron(hour: int, minute: int) -> str:
    return f"{int(minute)} {int(hour)} * * *"


def weekly_cron(weekday: int, hour: int, minute: int) -> str:
    """weekday как в datetime.weekday(): 0 — понедельник."""
    return f"{int(minute)} {int(hour)} * * {DAYS[int(weekday)]}"


async def add_reminder(
    user_id: int, text: str, *, tz: str, run_at: Optional[datetime] = None, cron: Optional[str] = None,
    session_maker=None,
) -> Reminder:
    scope = await personal_scope(user_id, tz, session_maker)
    return await reminders.create(scope, text, tz=tz, run_at=run_at, cron=cron)


async def list_reminders(user_id: int, tz: str = "UTC", session_maker=None) -> List[Reminder]:
    """Напоминания пользователя во всех его пространствах (личном и общих), ближайшие первыми."""
    out: List[Reminder] = []
    for space in await service.spaces_of(user_id, session_maker, include_personal=True):
        scope = await repo.open_scope(space.id, user_id, session_maker)
        out += [r for r in await reminders.list_active(scope)
                if r.created_by == user_id or r.assignee_id == user_id or r.assignee_id is None]
    return sorted(out, key=lambda r: (r.next_run_at, r.id))


async def cancel(user_id: int, reminder_id: int, session_maker=None) -> bool:
    """Отменить напоминание, видимое пользователю (в его пространствах; права — как в reminders.cancel)."""
    for space in await service.spaces_of(user_id, session_maker, include_personal=True):
        scope = await repo.open_scope(space.id, user_id, session_maker)
        try:
            if await reminders.cancel(scope, reminder_id):
                return True
        except repo.SpaceAccessError:
            continue
    return False
