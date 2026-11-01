"""Напоминания пространства: создание, отмена, доставка участникам, цикл-исполнитель.

Хранятся в своей таблице (space_reminders), а не в APScheduler: состояние переживает рестарт,
пропущенное во время простоя доставляется после запуска, логика проверяется тестами на
in-memory SQLite и переносится в другие проекты. Исполнитель раз в POLL секунд берёт
«созревшие» напоминания с арендой (lease): если процесс упал между отправкой и фиксацией —
после истечения аренды доставка повторится (лучше дубль, чем потерянное напоминание).
"""
from __future__ import annotations

import asyncio
import html
import logging
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable, List, Optional

import pytz
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select, update

from app.core.ext_db import resolve_session_maker
from app.spaces import service
from app.spaces.models import Reminder, Space, SpaceMember
from app.spaces.repo import SpaceAccessError, SpaceScope

logger = logging.getLogger(__name__)

MAX_TEXT = 500
MAX_ACTIVE = 200
LEASE_SECONDS = 120
MAX_ATTEMPTS = 5
PAST_TOLERANCE = timedelta(seconds=60)

Send = Callable[[int, str], Awaitable[None]]


class ReminderError(ValueError):
    """Некорректное напоминание (сообщение — для пользователя)."""


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _next_cron(cron: str, tz: str, after: datetime) -> datetime:
    """Ближайшее срабатывание cron строго после `after` (UTC naive) в зоне tz → UTC naive."""
    trigger = CronTrigger.from_crontab(cron, timezone=pytz.timezone(tz))
    nxt = trigger.get_next_fire_time(None, after.replace(tzinfo=timezone.utc))
    if nxt is None:
        raise ReminderError("у расписания нет будущих срабатываний")
    return nxt.astimezone(timezone.utc).replace(tzinfo=None)


async def create(
    scope: SpaceScope, text: str, *, tz: str, run_at: Optional[datetime] = None,
    cron: Optional[str] = None, assignee_id: Optional[int] = None, now: Optional[datetime] = None,
) -> Reminder:
    """Создать напоминание: разовое (run_at, UTC naive или aware) или повторяющееся (cron в зоне tz)."""
    now = now or _now()
    text = " ".join((text or "").split())
    if not text:
        raise ReminderError("текст напоминания пуст")
    if (run_at is None) == (cron is None):
        raise ReminderError("нужно указать либо время, либо расписание")
    try:
        pytz.timezone(tz)
    except pytz.UnknownTimeZoneError:
        raise ReminderError("неизвестный часовой пояс") from None
    if run_at is not None:
        if run_at.tzinfo is not None:
            run_at = run_at.astimezone(timezone.utc).replace(tzinfo=None)
        if run_at < now - PAST_TOLERANCE:
            raise ReminderError("это время уже прошло")
        first = run_at
    else:
        try:
            first = _next_cron(cron, tz, now)
        except ReminderError:
            raise
        except Exception:  # noqa: BLE001
            raise ReminderError("не удалось разобрать расписание") from None
    if assignee_id is not None and await service.member_role(scope.space_id, assignee_id) is None:
        raise ReminderError("адресат не состоит в пространстве")
    if await scope.count(Reminder, Reminder.is_active.is_(True)) >= MAX_ACTIVE:
        raise ReminderError(f"слишком много активных напоминаний (максимум {MAX_ACTIVE})")
    return await scope.add(Reminder(
        text=text[:MAX_TEXT], next_run_at=first, cron=cron, tz=tz, assignee_id=assignee_id,
        created_by=scope.user_id))


async def list_active(scope: SpaceScope, limit: int = 50) -> List[Reminder]:
    return await scope.list(Reminder, Reminder.is_active.is_(True), order_by=(Reminder.next_run_at, Reminder.id), limit=limit)


async def cancel(scope: SpaceScope, reminder_id: int) -> bool:
    """Отменить: автор — своё, админ/владелец — любое."""
    r = await scope.get(Reminder, reminder_id)
    if r is None or not r.is_active:
        return False
    if r.created_by != scope.user_id and not scope.can("admin"):
        raise SpaceAccessError("отменять чужие напоминания могут только администраторы")
    return await scope.update(Reminder, reminder_id, is_active=False, leased_until=None)


# ----------------------------- исполнитель --------------------------------- #
async def recipients(reminder: Reminder, session_maker=None) -> List[int]:
    """Кому слать: адресату, а если он вышел из пространства — автору; без адресата — всем участникам."""
    sm = resolve_session_maker(session_maker)
    async with sm() as s:
        members = [r[0] for r in (await s.execute(
            select(SpaceMember.user_id).where(SpaceMember.space_id == reminder.space_id)
            .order_by(SpaceMember.joined_at, SpaceMember.user_id))).all()]
    if reminder.assignee_id is None:
        return members
    if reminder.assignee_id in members:
        return [reminder.assignee_id]
    return [reminder.created_by] if reminder.created_by in members else []


async def _claim_due(now: datetime, limit: int, session_maker=None) -> List[Reminder]:
    sm = resolve_session_maker(session_maker)
    claimed: List[Reminder] = []
    async with sm() as s:
        rows = (await s.execute(
            select(Reminder).where(
                Reminder.is_active.is_(True), Reminder.next_run_at <= now,
                Reminder.space_id.in_(select(Space.id).where(Space.is_active.is_(True))),  # выключенное пространство молчит
                (Reminder.leased_until.is_(None)) | (Reminder.leased_until < now))
            .order_by(Reminder.next_run_at).limit(limit))).scalars().all()
        for r in rows:
            # атомарно: аренду получает только тот, кто увидел прежнее значение leased_until
            res = await s.execute(
                update(Reminder).where(Reminder.id == r.id, Reminder.is_active.is_(True),
                                       Reminder.leased_until.is_(None) if r.leased_until is None
                                       else Reminder.leased_until == r.leased_until)
                .values(leased_until=now + timedelta(seconds=LEASE_SECONDS)))
            if res.rowcount:
                claimed.append(r)
        await s.commit()
    return claimed


async def _finish(r: Reminder, now: datetime, session_maker=None) -> None:
    sm = resolve_session_maker(session_maker)
    values = dict(leased_until=None, attempts=0, run_count=Reminder.run_count + 1, last_run_at=now)
    if r.cron:
        try:
            values["next_run_at"] = _next_cron(r.cron, r.tz, now)
        except Exception as e:  # noqa: BLE001
            logger.error(f"reminder {r.id}: cron {r.cron!r} stopped: {e}")
            values["is_active"] = False
    else:
        values["is_active"] = False
    async with sm() as s:
        await s.execute(update(Reminder).where(Reminder.id == r.id).values(**values))
        await s.commit()


async def _fail(r: Reminder, now: datetime, session_maker=None) -> None:
    sm = resolve_session_maker(session_maker)
    attempts = (r.attempts or 0) + 1
    async with sm() as s:
        # аренда остаётся: повтор через LEASE_SECONDS; после MAX_ATTEMPTS напоминание выключается
        await s.execute(update(Reminder).where(Reminder.id == r.id).values(
            attempts=attempts, **({"is_active": False} if attempts >= MAX_ATTEMPTS else {})))
        await s.commit()
    if attempts >= MAX_ATTEMPTS:
        logger.error(f"reminder {r.id}: not delivered after {attempts} attempts, deactivated")


def _message(r: Reminder) -> str:
    return f"⏰ <b>Напоминание</b>\n\n{html.escape(r.text)}"


async def run_once(send: Send, now: Optional[datetime] = None, limit: int = 50, session_maker=None) -> int:
    """Доставить созревшие напоминания; возвращает число доставленных. Не бросает исключений."""
    now = now or _now()
    done = 0
    try:
        due = await _claim_due(now, limit, session_maker)
    except Exception as e:  # noqa: BLE001
        logger.error(f"reminders: claim failed: {e}")
        return 0
    for r in due:
        try:
            targets = await recipients(r, session_maker)
            if not targets:
                logger.warning(f"reminder {r.id}: no recipients left, deactivated")
                await _finish(r, now, session_maker)
                continue
            delivered = 0
            for chat_id in targets:
                try:
                    await send(chat_id, _message(r))
                    delivered += 1
                except Exception as e:  # noqa: BLE001 — один недоступный адресат не должен срывать остальных
                    logger.warning(f"reminder {r.id}: send to {chat_id} failed: {e}")
            if delivered:
                await _finish(r, now, session_maker)
                done += 1
            else:
                await _fail(r, now, session_maker)
        except Exception as e:  # noqa: BLE001
            logger.error(f"reminder {r.id}: {e}")
    return done


async def run_loop(send: Send, interval: float = 15.0, stop: Optional[asyncio.Event] = None) -> None:
    """Бесконечный цикл исполнителя (останавливается stop.set() или отменой задачи)."""
    stop = stop or asyncio.Event()
    logger.info(f"space reminders loop started (every {interval}s)")
    while not stop.is_set():
        await run_once(send)
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
