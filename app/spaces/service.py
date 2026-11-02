"""Пространства: создание, участники, роли, приглашения.

Инварианты: у активного пространства всегда есть владелец; приглашение не может выдать роль
owner; число участников ограничено (SPACE_MAX_MEMBERS); код погашается атомарно.
"""
from __future__ import annotations

import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from app.core.config import settings as app_settings
from app.core.ext_db import resolve_session_maker
from app.spaces.models import ROLE_RANK, ROLES, Space, SpaceInvite, SpaceMember

RESULT_OK = "ok"
RESULT_ALREADY = "already_member"
RESULT_INVALID = "invalid"
RESULT_REVOKED = "revoked"
RESULT_EXPIRED = "expired"
RESULT_EXHAUSTED = "exhausted"
RESULT_FULL = "space_full"

MAX_USES_LIMIT = 1000
MAX_DAYS_LIMIT = 365
_KIND_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


class SpaceError(ValueError):
    """Нарушено правило пространства (сообщение — для пользователя/администратора)."""


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _int_setting(name: str, default: int) -> int:
    v = getattr(app_settings, name, default)
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else default


def max_members() -> int:
    return _int_setting("SPACE_MAX_MEMBERS", 20)


def invite_days() -> int:
    return max(1, _int_setting("SPACE_INVITE_TTL_DAYS", 7))


# ----------------------------- пространства ------------------------------ #
async def create_space(
    name: str,
    owner_id: int,
    *,
    kind: str = "family",
    timezone_name: str = "UTC",
    currency: str = "RUB",
    locale: str = "ru",
    settings: Optional[Dict[str, Any]] = None,
    owner_name: str = "",
    session_maker=None,
) -> Space:
    name = (name or "").strip()
    if not name:
        raise SpaceError("название не может быть пустым")
    if not _KIND_RE.match(kind or ""):
        raise SpaceError("вид пространства: латиница/цифры/_, до 32 символов")
    sm = resolve_session_maker(session_maker)
    async with sm() as s:
        space = Space(
            name=name[:100], kind=kind, timezone=timezone_name, currency=currency.upper()[:8],
            locale=locale, settings=dict(settings or {}), created_by=owner_id,
        )
        s.add(space)
        await s.flush()
        s.add(SpaceMember(space_id=space.id, user_id=owner_id, role="owner", display_name=owner_name[:100]))
        await s.commit()
        return space


async def get_space(space_id: int, session_maker=None) -> Optional[Space]:
    sm = resolve_session_maker(session_maker)
    async with sm() as s:
        space = await s.get(Space, space_id)
        return space if space is not None and space.is_active else None


PERSONAL_KIND = "personal"  # личное пространство одного человека (напоминания без общего пространства)


async def spaces_of(user_id: int, session_maker=None, include_personal: bool = False) -> List[Space]:
    """Активные пространства пользователя (старые первыми); личное — только по запросу."""
    sm = resolve_session_maker(session_maker)
    async with sm() as s:
        q = (
            select(Space)
            .join(SpaceMember, SpaceMember.space_id == Space.id)
            .where(SpaceMember.user_id == user_id, Space.is_active.is_(True))
            .order_by(SpaceMember.joined_at, Space.id)
        )
        if not include_personal:
            q = q.where(Space.kind != PERSONAL_KIND)
        return list((await s.execute(q)).scalars())


async def update_settings(space_id: int, patch: Dict[str, Any], session_maker=None) -> bool:
    sm = resolve_session_maker(session_maker)
    async with sm() as s:
        space = await s.get(Space, space_id)
        if space is None:
            return False
        merged = dict(space.settings or {})
        merged.update(patch)
        space.settings = merged  # новый объект — иначе JSON-колонка не помечается изменённой
        await s.commit()
        return True


async def replace_settings(
    space_id: int, settings: Dict[str, Any], *, timezone_name: Optional[str] = None,
    currency: Optional[str] = None, session_maker=None,
) -> bool:
    """Заменить settings целиком (после проверки в `config`) и, если заданы, часовой пояс/валюту."""
    sm = resolve_session_maker(session_maker)
    async with sm() as s:
        space = await s.get(Space, space_id)
        if space is None:
            return False
        space.settings = dict(settings)
        if timezone_name:
            space.timezone = timezone_name
        if currency:
            space.currency = currency.upper()[:8]
        await s.commit()
        return True


async def deactivate_space(space_id: int, session_maker=None) -> bool:
    """Выключить пространство: участники теряют доступ, данные сохраняются."""
    sm = resolve_session_maker(session_maker)
    async with sm() as s:
        res = await s.execute(update(Space).where(Space.id == space_id).values(is_active=False))
        await s.commit()
        return bool(res.rowcount)


# ----------------------------- участники --------------------------------- #
async def member_role(space_id: int, user_id: int, session_maker=None) -> Optional[str]:
    """Роль пользователя в АКТИВНОМ пространстве, иначе None."""
    sm = resolve_session_maker(session_maker)
    async with sm() as s:
        row = (
            await s.execute(
                select(SpaceMember.role)
                .join(Space, Space.id == SpaceMember.space_id)
                .where(
                    SpaceMember.space_id == space_id,
                    SpaceMember.user_id == user_id,
                    Space.is_active.is_(True),
                )
            )
        ).scalar_one_or_none()
        return row


async def is_member_anywhere(user_id: int, session_maker=None) -> bool:
    """Участник общего пространства. Личное не считается: его создаёт любой уже допущенный
    пользователь, и доступ в закрытого бота оно давать не должно."""
    sm = resolve_session_maker(session_maker)
    async with sm() as s:
        row = (
            await s.execute(
                select(SpaceMember.space_id)
                .join(Space, Space.id == SpaceMember.space_id)
                .where(SpaceMember.user_id == user_id, Space.is_active.is_(True), Space.kind != PERSONAL_KIND)
                .limit(1)
            )
        ).first()
        return row is not None


async def list_members(space_id: int, session_maker=None) -> List[SpaceMember]:
    sm = resolve_session_maker(session_maker)
    async with sm() as s:
        rows = await s.execute(
            select(SpaceMember).where(SpaceMember.space_id == space_id).order_by(SpaceMember.joined_at, SpaceMember.user_id)
        )
        return list(rows.scalars())


async def _member_count(s, space_id: int) -> int:
    return (await s.execute(select(func.count()).select_from(SpaceMember).where(SpaceMember.space_id == space_id))).scalar_one()


async def add_member(
    space_id: int, user_id: int, *, role: str = "member", display_name: str = "",
    invite_code: Optional[str] = None, session_maker=None,
) -> str:
    """Добавить участника напрямую (админом). RESULT_OK | RESULT_ALREADY | RESULT_FULL | RESULT_INVALID."""
    if role not in ROLES:
        raise SpaceError(f"роль должна быть одной из {ROLES}")
    sm = resolve_session_maker(session_maker)
    async with sm() as s:
        space = await s.get(Space, space_id)
        if space is None or not space.is_active or space.kind == PERSONAL_KIND:
            return RESULT_INVALID
        if await s.get(SpaceMember, (space_id, user_id)) is not None:
            return RESULT_ALREADY
        if await _member_count(s, space_id) >= max_members():
            return RESULT_FULL
        s.add(SpaceMember(space_id=space_id, user_id=user_id, role=role,
                          display_name=display_name[:100], invite_code=invite_code))
        try:
            await s.commit()
        except IntegrityError:
            await s.rollback()
            return RESULT_ALREADY
        return RESULT_OK


async def _owner_count(s, space_id: int) -> int:
    return (
        await s.execute(
            select(func.count()).select_from(SpaceMember)
            .where(SpaceMember.space_id == space_id, SpaceMember.role == "owner")
        )
    ).scalar_one()


async def remove_member(space_id: int, user_id: int, session_maker=None) -> bool:
    """Убрать участника. Последнего владельца убрать нельзя (SpaceError)."""
    sm = resolve_session_maker(session_maker)
    async with sm() as s:
        row = await s.get(SpaceMember, (space_id, user_id))
        if row is None:
            return False
        if row.role == "owner" and await _owner_count(s, space_id) <= 1:
            raise SpaceError("нельзя убрать последнего владельца")
        await s.delete(row)
        await s.commit()
        return True


async def set_role(space_id: int, user_id: int, role: str, session_maker=None) -> bool:
    """Сменить роль. Понизить последнего владельца нельзя (SpaceError)."""
    if role not in ROLES:
        raise SpaceError(f"роль должна быть одной из {ROLES}")
    sm = resolve_session_maker(session_maker)
    async with sm() as s:
        row = await s.get(SpaceMember, (space_id, user_id))
        if row is None:
            return False
        if row.role == "owner" and role != "owner" and await _owner_count(s, space_id) <= 1:
            raise SpaceError("нельзя понизить последнего владельца")
        row.role = role
        await s.commit()
        return True


def ROLE_RANK_OF(role: Optional[str]) -> int:
    return ROLE_RANK.get(role or "", 0)


def role_at_least(role: Optional[str], needed: str) -> bool:
    return ROLE_RANK_OF(role) >= ROLE_RANK[needed]


# ----------------------------- приглашения ------------------------------- #
async def create_invite(
    space_id: int, created_by: Optional[int] = None, *, role: str = "member",
    max_uses: int = 1, days: Optional[int] = None, session_maker=None,
) -> str:
    """Создать приглашение; возвращает код (подходит для deep-link). Роль owner приглашением не выдаётся."""
    if role not in ROLES or role == "owner":
        raise SpaceError("приглашением можно выдать роль admin или member")
    days = invite_days() if days is None else days
    expires = _now() + timedelta(days=max(1, min(int(days), MAX_DAYS_LIMIT)))
    code = secrets.token_urlsafe(8)
    sm = resolve_session_maker(session_maker)
    async with sm() as s:
        space = await s.get(Space, space_id)
        if space is None or not space.is_active or space.kind == PERSONAL_KIND:
            raise SpaceError("пространство не найдено")
        s.add(SpaceInvite(code=code, space_id=space_id, role=role,
                          max_uses=max(0, min(int(max_uses), MAX_USES_LIMIT)),
                          expires_at=expires, created_by=created_by))
        await s.commit()
    return code


async def redeem_invite(code: str, user_id: int, *, display_name: str = "", session_maker=None):
    """Погасить приглашение. Возвращает (RESULT_*, space_id | None).

    Несуществующий код и код неактивного пространства — RESULT_INVALID. Уже участник —
    приглашение не расходуется. Лимит и «пространство не переполнено» проверяются до списания.
    """
    sm = resolve_session_maker(session_maker)
    async with sm() as s:
        invite = await s.get(SpaceInvite, code)
        if invite is None:
            return RESULT_INVALID, None
        space = await s.get(Space, invite.space_id)
        if space is None or not space.is_active:
            return RESULT_INVALID, None
        sid = invite.space_id
        if await s.get(SpaceMember, (sid, user_id)) is not None:
            return RESULT_ALREADY, sid
        if invite.revoked:
            return RESULT_REVOKED, sid
        if invite.expires_at is not None and invite.expires_at < _now():
            return RESULT_EXPIRED, sid
        if await _member_count(s, sid) >= max_members():
            return RESULT_FULL, sid

        res = await s.execute(
            update(SpaceInvite)
            .where(
                SpaceInvite.code == code,
                SpaceInvite.revoked.is_(False),
                (SpaceInvite.max_uses == 0) | (SpaceInvite.uses < SpaceInvite.max_uses),
            )
            .values(uses=SpaceInvite.uses + 1)
        )
        if not res.rowcount:
            await s.rollback()
            return RESULT_EXHAUSTED, sid
        s.add(SpaceMember(space_id=sid, user_id=user_id, role=invite.role,
                          display_name=display_name[:100], invite_code=code))
        try:
            await s.commit()
        except IntegrityError:  # параллельный переход по той же ссылке
            await s.rollback()
            return RESULT_ALREADY, sid
        return RESULT_OK, sid


async def revoke_invite(space_id: int, code: str, session_maker=None) -> bool:
    sm = resolve_session_maker(session_maker)
    async with sm() as s:
        res = await s.execute(
            update(SpaceInvite)
            .where(SpaceInvite.space_id == space_id, SpaceInvite.code == code)
            .values(revoked=True)
        )
        await s.commit()
        return bool(res.rowcount)
