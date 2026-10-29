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
    "app.core.showcase_access",
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
        if engine.dialect.name == "sqlite":
            await conn.run_sync(_add_missing_columns)

    if engine.dialect.name == "sqlite":
        # отдельная транзакция: основной бот и витрина стартуют одновременно и могут
        # обе пытаться пересоздать таблицу — проигравший не должен ронять запуск
        try:
            async with engine.begin() as conn:
                await conn.run_sync(_migrate_operator_sessions_pk)
        except Exception as e:
            logger.error(
                "ext schema: миграция operator_sessions не выполнена (%s: %s) — "
                "вероятно, её выполнил параллельный процесс; перезапустите контейнер",
                type(e).__name__, e,
            )


def _column_default_sql(column) -> str:
    """DEFAULT для ALTER TABLE ADD COLUMN: у NOT NULL колонки он обязателен."""
    from sqlalchemy import Boolean, Integer, Numeric
    from sqlalchemy import Float as _Float

    default = getattr(column.default, "arg", None)
    if default is not None and not callable(default):
        if isinstance(default, bool):
            return str(int(default))
        if isinstance(default, (int, float)):
            return str(default)
        return "'" + str(default).replace("'", "''") + "'"
    if isinstance(column.type, (Boolean, Integer, Numeric, _Float)):
        return "0"
    return "''"


def _add_missing_columns(sync_conn) -> None:
    """create_all не добавляет колонки к уже существующим таблицам.

    Для SQLite дописываем недостающие колонки моделей расширений, иначе после
    обновления кода на старой БД падает «no such column».
    """
    from sqlalchemy import text

    for table in ExtBase.metadata.sorted_tables:
        existing = {
            row[1]
            for row in sync_conn.execute(text(f'PRAGMA table_info("{table.name}")'))
        }
        for column in table.columns:
            if column.name in existing or column.primary_key:
                continue
            ddl = (
                f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" '
                f"{column.type.compile(dialect=sync_conn.dialect)}"
            )
            if not column.nullable:
                ddl += f" NOT NULL DEFAULT {_column_default_sql(column)}"
            logger.warning("ext schema: adding missing column %s.%s", table.name, column.name)
            sync_conn.execute(text(ddl))


def _migrate_operator_sessions_pk(sync_conn) -> None:
    """operator_sessions: ключ client_id -> (tenant, client_id) (раздельные витрины).

    SQLite не умеет менять первичный ключ, поэтому таблица пересоздаётся. DDL в
    SQLite не откатывается транзакцией, поэтому порядок такой, чтобы сбой в
    середине не терял данные: новая таблица создаётся под временным именем,
    данные копируются, и только затем старая удаляется, а новая переименовывается.
    Существующие строки получают пустой tenant (наследная витрина). Вызывается
    после _add_missing_columns — колонка summary уже есть.
    """
    from sqlalchemy import MetaData, text
    from sqlalchemy.schema import CreateTable

    cols = {
        row[1]
        for row in sync_conn.execute(text('PRAGMA table_info("operator_sessions")'))
    }
    if not cols or "tenant" in cols:
        return

    table = ExtBase.metadata.tables["operator_sessions"]
    sync_conn.execute(text("DROP TABLE IF EXISTS operator_sessions_new"))  # хвост сорванной миграции
    # имена индексов старой таблицы нужны новой
    for (name,) in sync_conn.execute(
        text(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND tbl_name='operator_sessions' AND sql IS NOT NULL"
        )
    ).fetchall():
        sync_conn.execute(text(f'DROP INDEX IF EXISTS "{name}"'))

    tmp = table.to_metadata(MetaData(), name="operator_sessions_new")
    sync_conn.execute(CreateTable(tmp))
    sync_conn.execute(
        text(
            "INSERT INTO operator_sessions_new "
            "(tenant, client_id, thread_id, status, client_name, summary, opened_at, updated_at) "
            "SELECT '', client_id, COALESCE(thread_id, 0), COALESCE(status, 'closed'), "
            "COALESCE(client_name, ''), COALESCE(summary, ''), opened_at, updated_at "
            "FROM operator_sessions"
        )
    )
    sync_conn.execute(text("DROP TABLE operator_sessions"))
    sync_conn.execute(text("ALTER TABLE operator_sessions_new RENAME TO operator_sessions"))
    for index in table.indexes:
        index.create(sync_conn)
    logger.warning("ext schema: operator_sessions rebuilt with composite key (tenant, client_id)")
