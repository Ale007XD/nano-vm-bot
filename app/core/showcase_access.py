"""
Доступ к закрытым витринам: участники и приглашения.

Витрина с access=invite отвечает только админам и участникам (showcase_members).
Участником становятся по ссылке-приглашению (t.me/<бот>?start=inv_<код>) либо
выдачей админом по ID. Приглашение принадлежит витрине (tenant) и не работает
в других; у него есть лимит использований и срок. Код погашается атомарно —
параллельные /start не превысят лимит.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Integer,
    String,
    select,
    update,
)
from sqlalchemy.exc import IntegrityError

from app.core.ext_db import ExtBase, resolve_session_maker

RESULT_OK = "ok"
RESULT_ALREADY = "already_member"
RESULT_INVALID = "invalid"
RESULT_REVOKED = "revoked"
RESULT_EXPIRED = "expired"
RESULT_EXHAUSTED = "exhausted"

MAX_USES_LIMIT = 1000
MAX_DAYS_LIMIT = 365


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class ShowcaseMember(ExtBase):
    __tablename__ = "showcase_members"

    tenant = Column(String(32), primary_key=True)
    user_id = Column(BigInteger, primary_key=True)
    via = Column(String(16), nullable=False, default="admin")  # admin | invite
    invite_code = Column(String(32), nullable=True)
    added_by = Column(BigInteger, nullable=True)
    created_at = Column(DateTime, default=_now)


class ShowcaseInvite(ExtBase):
    __tablename__ = "showcase_invites"

    code = Column(String(32), primary_key=True)
    tenant = Column(String(32), nullable=False, index=True)
    max_uses = Column(Integer, nullable=False, default=1)  # 0 — без ограничения
    uses = Column(Integer, nullable=False, default=0)
    expires_at = Column(DateTime, nullable=True)
    revoked = Column(Boolean, nullable=False, default=False)
    created_by = Column(BigInteger, nullable=True)
    created_at = Column(DateTime, default=_now)


# ----------------------------- участники --------------------------------- #
async def is_member(tenant: str, user_id: int, session_maker=None) -> bool:
    sm = resolve_session_maker(session_maker)
    async with sm() as s:
        return await s.get(ShowcaseMember, (tenant, user_id)) is not None


async def add_member(
    tenant: str,
    user_id: int,
    *,
    via: str = "admin",
    added_by: Optional[int] = None,
    invite_code: Optional[str] = None,
    session_maker=None,
) -> bool:
    """Добавить участника. False, если он уже был."""
    sm = resolve_session_maker(session_maker)
    async with sm() as s:
        if await s.get(ShowcaseMember, (tenant, user_id)) is not None:
            return False
        s.add(
            ShowcaseMember(
                tenant=tenant, user_id=user_id, via=via,
                added_by=added_by, invite_code=invite_code,
            )
        )
        try:
            await s.commit()
        except IntegrityError:
            await s.rollback()
            return False
        return True


async def remove_member(tenant: str, user_id: int, session_maker=None) -> bool:
    sm = resolve_session_maker(session_maker)
    async with sm() as s:
        row = await s.get(ShowcaseMember, (tenant, user_id))
        if row is None:
            return False
        await s.delete(row)
        await s.commit()
        return True


async def list_members(tenant: str, limit: int = 100, session_maker=None) -> List[ShowcaseMember]:
    sm = resolve_session_maker(session_maker)
    async with sm() as s:
        rows = await s.execute(
            select(ShowcaseMember)
            .where(ShowcaseMember.tenant == tenant)
            .order_by(ShowcaseMember.created_at.desc())
            .limit(limit)
        )
        return list(rows.scalars())


# ----------------------------- приглашения ------------------------------- #
async def create_invite(
    tenant: str,
    created_by: Optional[int] = None,
    max_uses: int = 1,
    days: Optional[int] = None,
    session_maker=None,
) -> str:
    """Создать приглашение; возвращает код (только [A-Za-z0-9_-], годится для deep-link)."""
    max_uses = max(0, min(int(max_uses), MAX_USES_LIMIT))
    expires = None
    if days is not None:
        expires = _now() + timedelta(days=max(1, min(int(days), MAX_DAYS_LIMIT)))
    code = secrets.token_urlsafe(8)
    sm = resolve_session_maker(session_maker)
    async with sm() as s:
        s.add(
            ShowcaseInvite(
                code=code, tenant=tenant, max_uses=max_uses,
                expires_at=expires, created_by=created_by,
            )
        )
        await s.commit()
    return code


async def redeem_invite(
    tenant: str, code: str, user_id: int, session_maker=None
) -> str:
    """Погасить приглашение для пользователя. Возвращает RESULT_*.

    Приглашение другой витрины или несуществующее — RESULT_INVALID (не раскрываем,
    существует ли код где-то ещё). Уже участник — приглашение не расходуется.
    """
    sm = resolve_session_maker(session_maker)
    async with sm() as s:
        invite = await s.get(ShowcaseInvite, code)
        if invite is None or invite.tenant != tenant:
            return RESULT_INVALID
        if await s.get(ShowcaseMember, (tenant, user_id)) is not None:
            return RESULT_ALREADY
        if invite.revoked:
            return RESULT_REVOKED
        if invite.expires_at is not None and invite.expires_at < _now():
            return RESULT_EXPIRED

        # атомарное списание: условие лимита — в самом UPDATE
        res = await s.execute(
            update(ShowcaseInvite)
            .where(
                ShowcaseInvite.code == code,
                ShowcaseInvite.revoked.is_(False),
                (ShowcaseInvite.max_uses == 0) | (ShowcaseInvite.uses < ShowcaseInvite.max_uses),
            )
            .values(uses=ShowcaseInvite.uses + 1)
        )
        if not res.rowcount:
            await s.rollback()
            return RESULT_EXHAUSTED
        s.add(
            ShowcaseMember(tenant=tenant, user_id=user_id, via="invite", invite_code=code)
        )
        try:
            await s.commit()
        except IntegrityError:  # параллельный /start того же пользователя
            await s.rollback()
            return RESULT_ALREADY
        return RESULT_OK


async def revoke_invite(tenant: str, code: str, session_maker=None) -> bool:
    sm = resolve_session_maker(session_maker)
    async with sm() as s:
        res = await s.execute(
            update(ShowcaseInvite)
            .where(ShowcaseInvite.tenant == tenant, ShowcaseInvite.code == code)
            .values(revoked=True)
        )
        await s.commit()
        return bool(res.rowcount)


async def list_invites(tenant: str, limit: int = 20, session_maker=None) -> List[ShowcaseInvite]:
    """Действующие приглашения (не отозваны, не истекли, не исчерпаны)."""
    sm = resolve_session_maker(session_maker)
    async with sm() as s:
        rows = await s.execute(
            select(ShowcaseInvite)
            .where(ShowcaseInvite.tenant == tenant, ShowcaseInvite.revoked.is_(False))
            .order_by(ShowcaseInvite.created_at.desc())
            .limit(limit * 3)
        )
        now = _now()
        active = [
            r for r in rows.scalars()
            if (r.expires_at is None or r.expires_at >= now)
            and (r.max_uses == 0 or r.uses < r.max_uses)
        ]
        return active[:limit]
