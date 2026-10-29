"""Точка входа витрин: запуск нескольких ботов, изоляция сбоя одного токена."""
import json
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core import showcases

TOKENS = {"111:AAA": "reklama_demo_bot", "222:BBB": "ai_jurist_demo_bot"}


class FakeBot:
    created = []
    broken = set()

    def __init__(self, token, default=None):
        self.token = token
        self.id = int(token.split(":")[0])
        self.session = SimpleNamespace(close=AsyncMock())
        self.commands = []
        FakeBot.created.append(self)

    async def get_me(self):
        if self.token in FakeBot.broken:
            raise RuntimeError("Unauthorized")
        return SimpleNamespace(username=TOKENS.get(self.token, "x_bot"))

    async def set_my_commands(self, commands, scope=None):
        self.commands.append((len(commands), scope))


class FakeDispatcher:
    last = None

    def __init__(self, storage=None):
        self.middlewares = []
        self.routers = []
        self.update = SimpleNamespace(outer_middleware=self.middlewares.append)
        self.polled = None
        FakeDispatcher.last = self

    def include_router(self, router):
        self.routers.append(router)

    async def start_polling(self, *bots, **kw):
        self.polled = list(bots)


@pytest.fixture
def bs(monkeypatch):
    import logging

    import app.core.logger as app_logger

    sys.modules["app.core.config"].settings.BOT_TIMEZONE = "UTC"  # нужен при импорте обработчиков
    # setup_logging() выполняется при импорте модуля: без заглушки он пишет файл логов
    monkeypatch.setattr(app_logger, "setup_logging", lambda: logging.getLogger("showcase-test"))
    import app.bot_showcase as mod

    FakeBot.created = []
    FakeBot.broken = set()
    FakeDispatcher.last = None
    st = sys.modules["app.core.config"].settings
    monkeypatch.setattr(st, "ADMIN_IDS", [999])
    monkeypatch.setattr(mod, "Bot", FakeBot)
    monkeypatch.setattr(mod, "Dispatcher", FakeDispatcher)
    monkeypatch.setattr(mod, "init_db", AsyncMock())
    monkeypatch.setattr(mod.vector_memory, "initialize", AsyncMock())
    monkeypatch.setattr(mod.vector_memory, "ensure_corpus", AsyncMock())
    monkeypatch.setattr(mod.operator_store, "adopt_legacy_sessions", AsyncMock(return_value=0))
    yield mod
    showcases.set_registry(None)


def configure(monkeypatch, *slugs_tokens, **kw):
    items = [{"slug": s, "token": t, **kw} for s, t in slugs_tokens]
    monkeypatch.setattr(sys.modules["app.core.config"].settings, "SHOWCASES", json.dumps(items))


class TestStartBot:
    @pytest.mark.asyncio
    async def test_ok_sets_menus_and_prepares_corpus(self, bs):
        sc = showcases.parse_showcases(json.dumps([{"slug": "jurist", "token": "222:BBB"}]))[0]
        bot = await bs._start_bot(sc)
        assert bot is not None
        bs.vector_memory.ensure_corpus.assert_awaited_once_with("corpus_jurist")
        assert len(bot.commands) == 2  # общее меню + меню администратора (scope чата)
        assert bot.commands[0][1] is None and bot.commands[1][1] is not None
        assert bot.commands[1][0] > bot.commands[0][0]

    @pytest.mark.asyncio
    async def test_invalid_token_skips_showcase_and_closes_session(self, bs):
        FakeBot.broken.add("222:BBB")
        sc = showcases.parse_showcases(json.dumps([{"slug": "jurist", "token": "222:BBB"}]))[0]
        assert await bs._start_bot(sc) is None
        assert FakeBot.created[0].session.close.await_count == 1


class TestMain:
    @pytest.mark.asyncio
    async def test_all_showcases_polled_in_one_dispatcher(self, bs, monkeypatch):
        configure(monkeypatch, ("reklama", "111:AAA"), ("jurist", "222:BBB"))
        await bs.main()
        dp = FakeDispatcher.last
        assert [b.id for b in dp.polled] == [111, 222]
        assert len(dp.middlewares) == 1 and len(dp.routers) == 2
        assert len(showcases._registry) == 2
        bs.operator_store.adopt_legacy_sessions.assert_awaited_once_with("reklama")
        assert all(b.session.close.await_count == 1 for b in FakeBot.created)

    @pytest.mark.asyncio
    async def test_one_broken_token_does_not_stop_the_other(self, bs, monkeypatch):
        FakeBot.broken.add("111:AAA")
        configure(monkeypatch, ("reklama", "111:AAA"), ("jurist", "222:BBB"))
        await bs.main()
        assert [b.id for b in FakeDispatcher.last.polled] == [222]

    @pytest.mark.asyncio
    async def test_no_working_showcase_exits_with_error(self, bs, monkeypatch):
        FakeBot.broken.update(TOKENS)
        configure(monkeypatch, ("reklama", "111:AAA"), ("jurist", "222:BBB"))
        with pytest.raises(SystemExit) as e:
            await bs.main()
        assert e.value.code == 1
        assert FakeDispatcher.last is None or FakeDispatcher.last.polled is None

    @pytest.mark.asyncio
    async def test_config_error_exits_before_touching_anything(self, bs, monkeypatch):
        monkeypatch.setattr(sys.modules["app.core.config"].settings, "SHOWCASES", "не json")
        with pytest.raises(SystemExit) as e:
            await bs.main()
        assert e.value.code == 2
        bs.init_db.assert_not_awaited()
        assert FakeBot.created == []

    @pytest.mark.asyncio
    async def test_legacy_single_showcase_without_showcases_env(self, bs, monkeypatch):
        st = sys.modules["app.core.config"].settings
        monkeypatch.setattr(st, "SHOWCASES", "")
        monkeypatch.setattr(st, "SHOWCASE_BOT_TOKEN", "111:AAA")
        monkeypatch.setattr(st, "OPERATOR_CHAT_ID", -1001)
        monkeypatch.setattr(st, "SHOWCASE_OPEN_ACCESS", None)
        monkeypatch.setattr(st, "BOT_OPEN_ACCESS", False)
        await bs.main()
        assert [b.id for b in FakeDispatcher.last.polled] == [111]
        (sc,) = list(showcases._registry)
        assert (sc.slug, sc.collection, sc.operator_chat_id) == ("", "corpus", -1001)
        bs.vector_memory.ensure_corpus.assert_awaited_once_with("corpus")
