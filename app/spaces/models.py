"""Таблицы пространств (на ExtBase, создаются init_ext_tables).

user_id — Telegram ID (BigInteger), а не users.id: модуль не зависит от основной БД.
Роли: owner > admin > member. Владелец всегда есть (последнего убрать нельзя).
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)

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


# ----------------------- данные пространства (SpaceOwned) ------------------ #
class Checklist(SpaceOwned, ExtBase):
    """Список: покупки, сборы в поездку, задания и т. п. (kind — произвольная метка)."""

    __tablename__ = "checklists"

    id = Column(Integer, primary_key=True, autoincrement=True)
    kind = Column(String(32), nullable=False, default="shopping")
    title = Column(String(100), nullable=False, default="")
    archived = Column(Boolean, nullable=False, default=False)
    created_by = Column(BigInteger, nullable=True)
    created_at = Column(DateTime, default=_now)


class ChecklistItem(SpaceOwned, ExtBase):
    __tablename__ = "checklist_items"

    id = Column(Integer, primary_key=True, autoincrement=True)
    checklist_id = Column(Integer, nullable=False, index=True)
    text = Column(String(100), nullable=False)
    qty = Column(String(32), nullable=False, default="")
    done = Column(Boolean, nullable=False, default=False)
    added_by = Column(BigInteger, nullable=True)
    done_by = Column(BigInteger, nullable=True)
    done_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=_now)


class Category(SpaceOwned, ExtBase):
    __tablename__ = "categories"
    __table_args__ = (UniqueConstraint("space_id", "kind", "name_key", name="uq_category"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    kind = Column(String(16), nullable=False, default="expense")  # expense | income | ...
    name = Column(String(50), nullable=False)
    name_key = Column(String(50), nullable=False)  # casefold — для поиска без учёта регистра
    sort = Column(Integer, nullable=False, default=0)


class LedgerEntry(SpaceOwned, ExtBase):
    """Запись журнала (расход/доход). Сумма — целое в минорных единицах (копейки), без float."""

    __tablename__ = "ledger_entries"
    __table_args__ = (Index("ix_ledger_space_time", "space_id", "occurred_at"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    author_id = Column(BigInteger, nullable=False)
    amount_minor = Column(BigInteger, nullable=False)
    currency = Column(String(8), nullable=False, default="RUB")
    category_id = Column(Integer, nullable=True)
    note = Column(String(200), nullable=False, default="")
    occurred_at = Column(DateTime, nullable=False, default=_now)  # UTC, без tzinfo
    source = Column(String(8), nullable=False, default="text")  # text | voice
    raw_text = Column(Text, nullable=False, default="")  # как сказал человек / как распознал Whisper
    created_at = Column(DateTime, default=_now)


class Reminder(SpaceOwned, ExtBase):
    """Напоминание пространства. assignee_id NULL — всем участникам. Время — UTC без tzinfo."""

    __tablename__ = "space_reminders"
    __table_args__ = (Index("ix_reminder_due", "is_active", "next_run_at"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    text = Column(String(500), nullable=False)
    next_run_at = Column(DateTime, nullable=False)
    cron = Column(String(100), nullable=True)  # пятиполевый cron для повторяющихся
    tz = Column(String(50), nullable=False, default="UTC")
    assignee_id = Column(BigInteger, nullable=True)
    created_by = Column(BigInteger, nullable=False)
    is_active = Column(Boolean, nullable=False, default=True)
    leased_until = Column(DateTime, nullable=True)  # «взято в работу»: повторно не берётся до этого времени
    attempts = Column(Integer, nullable=False, default=0)
    run_count = Column(Integer, nullable=False, default=0)
    last_run_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=_now)
