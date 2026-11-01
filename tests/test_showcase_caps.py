"""Потолки витрин: счётчик расхода по витрине, лимиты на витрину и на все витрины, /showcase_usage."""
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core import showcase_usage as su
from app.core.ext_db import ExtBase

ADMIN, USER = 1, 999


@pytest_asyncio.fixture
async def sm(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(ExtBase.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(su, "resolve_session_maker", lambda _=None: maker)
    yield maker
    await engine.dispose()


@pytest.fixture
def cfg(monkeypatch):
    st = sys.modules["app.core.config"].settings
    for k, v in dict(ADMIN_IDS=[ADMIN], SHOWCASE_DAILY_LIMIT=0, SHOWCASE_TOTAL_DAILY_LIMIT=0,
                     SHOWCASES_GLOBAL_DAILY_LIMIT=0, BOT_TIMEZONE="UTC").items():
        monkeypatch.setattr(st, k, v)
    db = sys.modules["app.core.database"]
    monkeypatch.setattr(db, "get_today_usage", AsyncMock(return_value=None))
    return st


def showcase(slug="jurist", **kw):
    from app.core.showcases import Showcase

    return Showcase(slug=slug, token="1:x", collection=f"corpus_{slug}", **kw)


class TestCounter:
    @pytest.mark.asyncio
    async def test_accumulates_per_showcase(self, sm):
        await su.add_usage("a", 1, 100)
        await su.add_usage("a", 2, 50)
        await su.add_usage("b", 1, 7)
        assert await su.today("a") == (3, 150)
        assert await su.today("b") == (1, 7)
        assert await su.today("zzz") == (0, 0)
        assert await su.today_all() == {"a": (3, 150), "b": (1, 7)}

    @pytest.mark.asyncio
    async def test_zero_usage_writes_nothing(self, sm):
        await su.add_usage("a", 0, 0)
        assert await su.today_all() == {}

    @pytest.mark.asyncio
    async def test_resets_on_new_utc_day(self, sm, monkeypatch):
        await su.add_usage("a", 5, 5)
        monkeypatch.setattr(su, "_today", lambda: "2099-01-01")
        assert await su.today("a") == (0, 0)
        await su.add_usage("a", 1, 1)
        assert await su.today("a") == (1, 1)

    @pytest.mark.asyncio
    async def test_lost_first_insert_race_is_added_to_existing_row(self, sm, monkeypatch):
        """Параллельный запрос создал строку между UPDATE и INSERT — расход не теряется."""
        from sqlalchemy.ext.asyncio import AsyncSession

        await su.add_usage("a", 1, 10)
        real_execute = AsyncSession.execute
        seen = {"updates": 0}

        async def lying_execute(self, stmt, *a, **kw):
            res = await real_execute(self, stmt, *a, **kw)
            if "UPDATE" in str(stmt):
                seen["updates"] += 1
                if seen["updates"] == 1:
                    res.rowcount = 0  # первый UPDATE «ничего не нашёл» — идём вставлять дубль
            return res

        with monkeypatch.context() as m:
            m.setattr(AsyncSession, "execute", lying_execute)
            await su.add_usage("a", 2, 20)

        assert seen["updates"] == 2  # после IntegrityError расход прибавлен повторным UPDATE
        assert await su.today("a") == (3, 30)


class TestLimits:
    @pytest.mark.asyncio
    async def test_no_showcase_or_no_limits_allows(self, sm, cfg):
        from app.core.access import check_showcase_limit

        await su.add_usage("jurist", 10**6, 0)
        assert (await check_showcase_limit(USER)).allowed  # без витрины потолки не применяются
        assert (await check_showcase_limit(USER, showcase())).allowed  # потолки не заданы

    @pytest.mark.asyncio
    async def test_showcase_total_cap(self, sm, cfg, monkeypatch):
        from app.core.access import check_showcase_limit

        monkeypatch.setattr(cfg, "SHOWCASE_TOTAL_DAILY_LIMIT", 3)
        sc = showcase()
        await su.add_usage("jurist", 2, 0)
        assert (await check_showcase_limit(USER, sc)).allowed
        await su.add_usage("jurist", 1, 0)
        check = await check_showcase_limit(USER, sc)
        assert not check.allowed and "/operator" in check.message
        # другая витрина в своём потолке
        assert (await check_showcase_limit(USER, showcase("beauty"))).allowed
        # админ не ограничен
        assert (await check_showcase_limit(ADMIN, sc)).allowed

    @pytest.mark.asyncio
    async def test_per_showcase_override_beats_setting(self, sm, cfg, monkeypatch):
        from app.core.access import check_showcase_limit

        monkeypatch.setattr(cfg, "SHOWCASE_TOTAL_DAILY_LIMIT", 1)
        await su.add_usage("jurist", 5, 0)
        assert not (await check_showcase_limit(USER, showcase())).allowed
        assert (await check_showcase_limit(USER, showcase(total_limit=0))).allowed  # 0 — без потолка
        assert (await check_showcase_limit(USER, showcase(total_limit=10))).allowed
        assert not (await check_showcase_limit(USER, showcase(total_limit=5))).allowed

    @pytest.mark.asyncio
    async def test_global_cap_sums_all_showcases(self, sm, cfg, monkeypatch):
        from app.core.access import check_showcase_limit

        monkeypatch.setattr(cfg, "SHOWCASES_GLOBAL_DAILY_LIMIT", 5)
        await su.add_usage("a", 3, 0)
        await su.add_usage("b", 1, 0)
        assert (await check_showcase_limit(USER, showcase("a"))).allowed
        await su.add_usage("b", 1, 0)
        assert not (await check_showcase_limit(USER, showcase("a"))).allowed
        assert not (await check_showcase_limit(USER, showcase("c"))).allowed

    @pytest.mark.asyncio
    async def test_per_user_limit_override(self, sm, cfg, monkeypatch):
        from app.core.access import check_showcase_limit

        db = sys.modules["app.core.database"]
        monkeypatch.setattr(db, "get_today_usage", AsyncMock(return_value=SimpleNamespace(requests=4)))
        monkeypatch.setattr(cfg, "SHOWCASE_DAILY_LIMIT", 20)
        assert (await check_showcase_limit(USER, showcase())).allowed
        assert not (await check_showcase_limit(USER, showcase(daily_limit=4))).allowed
        assert (await check_showcase_limit(USER, showcase(daily_limit=0))).allowed

    @pytest.mark.asyncio
    async def test_db_failure_does_not_block(self, cfg, monkeypatch):
        from app.core.access import check_showcase_limit

        monkeypatch.setattr(cfg, "SHOWCASE_TOTAL_DAILY_LIMIT", 1)
        boom = AsyncMock(side_effect=RuntimeError("db down"))
        monkeypatch.setattr(su, "today", boom)
        assert (await check_showcase_limit(USER, showcase())).allowed


class TestSessionScope:
    @pytest.mark.asyncio
    async def test_flush_records_showcase_usage(self, monkeypatch):
        from app.core import usage

        db = sys.modules["app.core.database"]
        monkeypatch.setattr(db, "add_usage_daily", AsyncMock())
        add = AsyncMock()
        monkeypatch.setattr(su, "add_usage", add)

        usage.start_usage_session(USER, scope="jurist")
        usage.record_usage({"prompt_tokens": 10, "completion_tokens": 5})
        usage.record_usage(None)
        await usage.flush_usage()
        add.assert_awaited_once_with("jurist", 2, 15)

    @pytest.mark.asyncio
    async def test_no_scope_no_showcase_row_and_failure_is_swallowed(self, monkeypatch):
        from app.core import usage

        db = sys.modules["app.core.database"]
        monkeypatch.setattr(db, "add_usage_daily", AsyncMock())
        add = AsyncMock(side_effect=RuntimeError("db"))
        monkeypatch.setattr(su, "add_usage", add)

        usage.start_usage_session(USER)
        usage.record_usage({"total_tokens": 3})
        await usage.flush_usage()
        add.assert_not_awaited()

        usage.start_usage_session(USER, scope="x")
        usage.record_usage({"total_tokens": 3})
        await usage.flush_usage()  # исключение add_usage не должно вылететь
        add.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_scope_does_not_leak_into_next_session(self, monkeypatch):
        from app.core import usage

        db = sys.modules["app.core.database"]
        monkeypatch.setattr(db, "add_usage_daily", AsyncMock())
        add = AsyncMock()
        monkeypatch.setattr(su, "add_usage", add)
        usage.start_usage_session(USER, scope="a")
        await usage.flush_usage()
        usage.start_usage_session(USER)
        usage.record_usage({"total_tokens": 1})
        await usage.flush_usage()
        add.assert_not_awaited()


class TestConfig:
    def _parse(self, extra):
        import json

        from app.core.showcases import parse_showcases

        return parse_showcases(json.dumps([{"slug": "jurist", "token": "1:abc", **extra}]))[0]

    def test_defaults_inherit(self):
        sc = self._parse({})
        assert (sc.daily_limit, sc.total_limit) == (-1, -1)

    def test_values_parsed(self):
        sc = self._parse({"daily_limit": 5, "total_limit": 0})
        assert (sc.daily_limit, sc.total_limit) == (5, 0)

    @pytest.mark.parametrize("bad", [{"daily_limit": -5}, {"total_limit": "10"}, {"total_limit": True}])
    def test_invalid(self, bad):
        from app.core.showcases import ShowcaseConfigError

        with pytest.raises(ShowcaseConfigError):
            self._parse(bad)

    def test_usage_key(self):
        from app.core.showcases import LEGACY_SLUG, Showcase

        assert Showcase(slug=LEGACY_SLUG, token="1:x").usage_key == "default"
        assert showcase("jurist").usage_key == "jurist"


class TestUsageCommand:
    @pytest.mark.asyncio
    async def test_admin_sees_numbers_stranger_denied(self, sm, cfg, monkeypatch):
        cfg.BOT_TIMEZONE = "UTC"
        commands = pytest.importorskip("app.handlers.commands")
        from app.core import showcases
        from app.handlers import showcase as sc

        monkeypatch.setattr(commands, "is_admin", lambda uid: uid == ADMIN)
        monkeypatch.setattr(cfg, "SHOWCASES_GLOBAL_DAILY_LIMIT", 100)
        await su.add_usage("jurist", 7, 1234)
        await su.add_usage("beauty", 2, 99)

        def msg(uid):
            return SimpleNamespace(from_user=SimpleNamespace(id=uid), answer=AsyncMock())

        stranger = msg(USER)
        with showcases.use_showcase(showcase(total_limit=50)):
            await sc.cmd_showcase_usage(stranger)
        assert "Только для администраторов" in stranger.answer.await_args.args[0]

        admin = msg(ADMIN)
        with showcases.use_showcase(showcase(total_limit=50)):
            await sc.cmd_showcase_usage(admin)
        text = admin.answer.await_args.args[0]
        assert "<b>7</b>" in text and "потолок: 50" in text and "1234" in text
        assert "<b>9</b>" in text and "потолок: 100" in text and "beauty" in text
