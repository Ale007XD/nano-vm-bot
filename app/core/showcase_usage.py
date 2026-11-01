"""Суточный расход витрин (UTC): LLM-запросы и токены по каждой витрине.

usage_daily ключуется по пользователю и не знает, какая витрина потратила запрос, поэтому
потолок «на витрину» и «на все витрины» считается по отдельной таблице. Сумма по витрине —
всех пользователей вместе. Проверка лимита и запись расхода не атомарны: параллельные
запросы могут превысить потолок на число одновременно идущих запросов — для защиты от
разорения этого достаточно.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Dict, Optional, Tuple

from sqlalchemy import Column, Integer, String, select, update
from sqlalchemy.exc import IntegrityError

from app.core.ext_db import ExtBase, resolve_session_maker

logger = logging.getLogger(__name__)


class ShowcaseUsageDaily(ExtBase):
    __tablename__ = "showcase_usage_daily"

    showcase = Column(String(64), primary_key=True)  # slug; у наследной витрины — «default»
    date = Column(String(10), primary_key=True)  # YYYY-MM-DD (UTC)
    requests = Column(Integer, nullable=False, default=0)
    total_tokens = Column(Integer, nullable=False, default=0)


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


async def add_usage(showcase: str, requests: int, tokens: int, session_maker=None) -> None:
    """Прибавить расход витрины за текущие сутки (UPSERT, устойчив к гонке первой вставки)."""
    requests, tokens = int(requests or 0), int(tokens or 0)
    if not (requests or tokens):
        return
    day = _today()
    sm = resolve_session_maker(session_maker)

    def _inc():
        return (
            update(ShowcaseUsageDaily)
            .where(ShowcaseUsageDaily.showcase == showcase, ShowcaseUsageDaily.date == day)
            .values(
                requests=ShowcaseUsageDaily.requests + requests,
                total_tokens=ShowcaseUsageDaily.total_tokens + tokens,
            )
        )

    async with sm() as session:
        result = await session.execute(_inc())
        if result.rowcount == 0:
            session.add(
                ShowcaseUsageDaily(showcase=showcase, date=day, requests=requests, total_tokens=tokens)
            )
            try:
                await session.commit()
                return
            except IntegrityError:
                await session.rollback()  # параллельный запрос первым создал строку суток
                await session.execute(_inc())
        await session.commit()


async def today(showcase: str, session_maker=None) -> Tuple[int, int]:
    """(запросы, токены) витрины за текущие сутки."""
    sm = resolve_session_maker(session_maker)
    async with sm() as session:
        row = (
            await session.execute(
                select(ShowcaseUsageDaily).where(
                    ShowcaseUsageDaily.showcase == showcase, ShowcaseUsageDaily.date == _today()
                )
            )
        ).scalar_one_or_none()
        return (int(row.requests), int(row.total_tokens)) if row else (0, 0)


async def today_all(session_maker=None) -> Dict[str, Tuple[int, int]]:
    """{витрина: (запросы, токены)} за текущие сутки по всем витринам."""
    sm = resolve_session_maker(session_maker)
    async with sm() as session:
        rows = (
            await session.execute(select(ShowcaseUsageDaily).where(ShowcaseUsageDaily.date == _today()))
        ).scalars().all()
        return {r.showcase: (int(r.requests), int(r.total_tokens)) for r in rows}


def usage_key(slug: Optional[str]) -> str:
    return slug or "default"
