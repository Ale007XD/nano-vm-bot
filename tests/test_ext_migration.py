"""init_ext_tables дописывает недостающие колонки в таблицы старой схемы (SQLite)."""
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine


@pytest.mark.asyncio
async def test_missing_summary_column_is_added(tmp_path):
    from app.core.ext_db import init_ext_tables

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/old.db")
    async with engine.begin() as conn:  # схема до handoff_summary: без summary
        await conn.execute(text(
            "CREATE TABLE operator_sessions (client_id INTEGER PRIMARY KEY, "
            "thread_id INTEGER, status VARCHAR(16) NOT NULL, client_name VARCHAR(255), "
            "opened_at DATETIME, updated_at DATETIME)"
        ))
        await conn.execute(text(
            "INSERT INTO operator_sessions (client_id, status) VALUES (1, 'open')"
        ))

    await init_ext_tables(engine)
    await init_ext_tables(engine)  # идемпотентно

    async with engine.connect() as conn:
        cols = {r[1] for r in await conn.execute(text("PRAGMA table_info(operator_sessions)"))}
        row = (await conn.execute(text("SELECT summary FROM operator_sessions"))).one()
    await engine.dispose()
    assert "summary" in cols
    assert row[0] == ""
