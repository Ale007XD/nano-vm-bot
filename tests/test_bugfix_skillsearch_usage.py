"""Регрессии: поиск навыков в SkillBot, гонка первой записи usage_daily, RAG_MEMORIES."""
import importlib.util
import os
import sys

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.skills_loader import SkillInfo, SkillLoader

_APP = os.path.join(os.path.dirname(__file__), "..", "app")


def _loader(*infos):
    ld = SkillLoader()
    for i in infos:
        ld._skills[i.name] = i
    return ld


CALC = SkillInfo(name="calculator", description="Считает выражения", category="math", commands=["calc"])
WEATHER = SkillInfo(name="weather", description="Погода по городу", category="info")


class TestSkillSearch:
    def test_matches_name_description_category_command(self):
        ld = _loader(CALC, WEATHER)
        assert [s.name for s in ld.search_skills("calculator")] == ["calculator"]
        assert [s.name for s in ld.search_skills("погода")] == ["weather"]
        assert [s.name for s in ld.search_skills("math")] == ["calculator"]
        assert [s.name for s in ld.search_skills("calc")] == ["calculator"]

    def test_service_words_are_ignored(self):
        ld = _loader(CALC, WEATHER)
        assert [s.name for s in ld.search_skills("найди навык калькулятор погода")] == ["weather"]
        assert [s.name for s in ld.search_skills("search for calculator")] == ["calculator"]

    def test_empty_and_service_only_query(self):
        ld = _loader(CALC)
        assert ld.search_skills("") == []
        assert ld.search_skills("найди навык") == []

    def test_more_hits_rank_first(self):
        a = SkillInfo(name="a", description="погода город")
        b = SkillInfo(name="b", description="погода")
        assert [s.name for s in _loader(b, a).search_skills("погода город")] == ["a", "b"]

    @pytest.mark.asyncio
    async def test_skillbot_handler_does_not_crash(self, monkeypatch):
        from app.agents import skillbot as sb

        monkeypatch.setattr(sb, "skill_loader", _loader(CALC))

        async def no_vec(*a, **k):
            return []

        monkeypatch.setattr(sb.vector_memory, "search_skills", no_vec, raising=False)
        text = await sb.skillbot._handle_skill_search("найди навык calculator")
        assert "calculator" in text


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, os.path.join(_APP, path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
async def real_db(tmp_path, monkeypatch):
    """Настоящий database.py на временном SQLite (conftest подменяет модуль заглушкой)."""
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path}/u.db")
    cfg = _load("_real_config_usage", "core/config.py")
    monkeypatch.setitem(sys.modules, "app.core.config", cfg)
    db = _load("_real_database_usage", "core/database.py")
    async with db.engine.begin() as conn:
        await conn.run_sync(db.Base.metadata.create_all)
    async with db.async_session_maker() as s:
        s.add(db.User(telegram_id=111))
        await s.commit()
    yield db, db.async_session_maker
    await db.engine.dispose()


class TestUsageRace:
    @pytest.mark.asyncio
    async def test_lost_insert_race_falls_back_to_update(self, real_db, monkeypatch):
        """Конкурент создал строку суток между нашим UPDATE и INSERT: расход не теряется.

        (SQLite сериализует записи сам, поэтому гонку моделируем: первый UPDATE
        «не нашёл» строку, хотя она уже есть — как на Postgres при параллельных сессиях.)
        """
        db, _ = real_db
        u = {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5, "requests": 1}
        await db.add_usage_daily(111, u)  # строку «создал конкурент»

        orig = AsyncSession.execute
        faked = False

        async def execute(self, stmt, *a, **k):
            nonlocal faked
            res = await orig(self, stmt, *a, **k)
            if getattr(stmt, "is_update", False) and not faked:
                faked = True

                class _Zero:
                    rowcount = 0

                return _Zero()
            return res

        monkeypatch.setattr(AsyncSession, "execute", execute)
        await db.add_usage_daily(111, u)  # без фикса: IntegrityError
        monkeypatch.setattr(AsyncSession, "execute", orig)
        row = await db.get_today_usage(111)
        assert faked
        assert row.requests == 2 and row.total_tokens == 10

    @pytest.mark.asyncio
    async def test_sequential_accumulates(self, real_db):
        db, _ = real_db
        await db.add_usage_daily(111, {"requests": 1, "total_tokens": 4})
        await db.add_usage_daily(111, {"requests": 1, "total_tokens": 6})
        row = await db.get_today_usage(111)
        assert row.requests == 2 and row.total_tokens == 10

    @pytest.mark.asyncio
    async def test_unknown_user_is_noop(self, real_db):
        db, _ = real_db
        await db.add_usage_daily(999, {"requests": 1})
        assert await db.get_today_usage(999) is None


class TestRagMemoriesSetting:
    def test_default_is_ten(self):
        cfg = _load("_real_config_rag", "core/config.py")
        assert cfg.Settings.model_fields["RAG_MEMORIES"].default == 10

    def test_handlers_use_setting(self):
        import pathlib

        src = pathlib.Path("app/handlers/messages.py").read_text()
        assert "n_memories=10" not in src
        assert src.count("n_memories=settings.RAG_MEMORIES") == 2
