"""
Расширенные таблицы (BYOK, оператор, биллинг) на отдельной декларативной базе.

Почему отдельная ExtBase, а не Base из database.py: модули фич не должны
импортировать database на уровне модуля (циклы, моки в тестах), а сессии
передаются явно (session_maker) — это даёт тестируемость на in-memory SQLite.

init_db() вызывает init_ext_tables(); модули фич подхватываются по списку,
отсутствующие пропускаются.
"""
from __future__ import annotations

import importlib
import logging
from typing import Callable, Optional

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import declarative_base

logger = logging.getLogger(__name__)

ExtBase = declarative_base()

_FEATURE_MODULES = (
    "app.core.byok",
    "app.core.operator",
    "app.core.billing.models",
)


def default_session_maker() -> Callable[[], AsyncSession]:
    """Основная фабрика сессий приложения (импорт лениво)."""
    from app.core.database import async_session_maker

    return async_session_maker


def resolve_session_maker(session_maker: Optional[async_sessionmaker]):
    return session_maker or default_session_maker()


def _import_feature_models() -> None:
    for name in _FEATURE_MODULES:
        try:
            importlib.import_module(name)
        except ModuleNotFoundError as e:
            if e.name != name:
                raise  # модуль есть, но сломан его собственный импорт
            logger.debug("ext model module %s not present", name)


async def init_ext_tables(engine) -> None:
    """Создать таблицы всех подключённых фич (идемпотентно)."""
    _import_feature_models()
    async with engine.begin() as conn:
        await conn.run_sync(ExtBase.metadata.create_all)
