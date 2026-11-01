"""Таблицы пространств (на ExtBase, создаются init_ext_tables).

user_id — Telegram ID (BigInteger), а не users.id: модуль не зависит от основной БД.
Роли: owner > admin > member. Владелец всегда есть (последнего убрать нельзя).
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, BigInteger, Boolean, Column, DateTime, Integer, String

from app.core.ext_db import ExtBase

ROLES = ("owner", "admin", "member")
ROLE_RANK = {"member": 1, "admin": 2, "owner": 3}


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Space(ExtBase):
    __tablename__ = "spaces"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), nullable=False)
    kind = Column(String(32), nullable=False, default="family")  # family | company | class | ...
    timezone = Column(String(50), nullable=False, default="UTC")
    currency = Column(String(8), nullable=False, default="RUB")
    locale = Column(String(8), nullable=False, default="ru")
    settings = Column(JSON, nullable=False, default=dict)
    is_active = Column(Boolean, nullable=False, default=True)
    created_by = Column(BigInteger, nullable=True)
    created_at = Column(DateTime, default=_now)


class SpaceMember(ExtBase):
    __tablename__ = "space_members"

    space_id = Column(Integer, primary_key=True)
    user_id = Column(BigInteger, primary_key=True, index=True)
    role = Column(String(16), nullable=False, default="member")
    display_name = Column(String(100), nullable=False, default="")
    invite_code = Column(String(32), nullable=True)
    joined_at = Column(DateTime, default=_now)


class SpaceInvite(ExtBase):
    __tablename__ = "space_invites"

    code = Column(String(32), primary_key=True)
    space_id = Column(Integer, nullable=False, index=True)
    role = Column(String(16), nullable=False, default="member")
    max_uses = Column(Integer, nullable=False, default=1)  # 0 — без ограничения
    uses = Column(Integer, nullable=False, default=0)
    expires_at = Column(DateTime, nullable=True)
    revoked = Column(Boolean, nullable=False, default=False)
    created_by = Column(BigInteger, nullable=True)
    created_at = Column(DateTime, default=_now)


class SpaceOwned:
    """Миксин для таблиц с данными пространства: обязательная колонка space_id."""

    space_id = Column(Integer, nullable=False, index=True)
