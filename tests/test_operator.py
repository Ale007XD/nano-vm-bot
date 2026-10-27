"""
Эскалация на оператора: хранение сессий, маршрутизация сообщений, антифлуд.
Telegram — fake-бот (AsyncMock), БД — in-memory SQLite.
"""
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core import ext_db
from app.core import operator as store
from app.core.ext_db import ExtBase

# пакет app.handlers тянет планировщик, которому нужна настоящая таймзона
sys.modules["app.core.config"].settings.BOT_TIMEZONE = "UTC"
from app.handlers import operator as h  # noqa: E402

GROUP = -1001234
CLIENT, CLIENT2 = 111, 222


def _bad(msg):
    return TelegramBadRequest(method=MagicMock(), message=msg)


def _forbidden():
    return TelegramForbiddenError(method=MagicMock(), message="Forbidden: bot was blocked")


@pytest_asyncio.fixture
async def sm(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(ExtBase.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(ext_db, "default_session_maker", lambda: maker)
    yield maker
    await engine.dispose()


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    settings = sys.modules["app.core.config"].settings
    monkeypatch.setattr(settings, "OPERATOR_CHAT_ID", GROUP)
    monkeypatch.setattr(settings, "ADMIN_IDS", [999])
    monkeypatch.setattr(settings, "BOT_TIMEZONE", "UTC")
    monkeypatch.setattr(h, "user_allowed", AsyncMock(return_value=True))
    monkeypatch.setattr(h, "_limiter", store.RateLimiter(max_events=3, window=60))
    h._warned_flood.clear()
    store._questions.clear()


def make_bot():
    bot = MagicMock()
    bot.create_forum_topic = AsyncMock(return_value=SimpleNamespace(message_thread_id=77))
    bot.send_message = AsyncMock()
    bot.copy_message = AsyncMock()
    bot.close_forum_topic = AsyncMock()
    return bot


def make_msg(bot, *, uid=CLIENT, chat_id=None, text="привет", thread=None,
             chat_type="private", content_type="text", is_bot=False):
    user = SimpleNamespace(id=uid, first_name="Иван", last_name=None, username="ivan", is_bot=is_bot)
    return SimpleNamespace(
        bot=bot, from_user=user, text=text, message_id=5,
        chat=SimpleNamespace(id=chat_id if chat_id is not None else uid, type=chat_type),
        message_thread_id=thread, content_type=content_type,
        answer=AsyncMock(), reply=AsyncMock(),
    )


# ------------------------------- store ------------------------------------ #
class TestStore:
    @pytest.mark.asyncio
    async def test_open_get_close(self, sm):
        await store.open_session(CLIENT, 10, "Иван")
        assert (await store.get_open_by_client(CLIENT)).thread_id == 10
        assert (await store.get_open_by_thread(10)).client_id == CLIENT
        assert await store.close_session(CLIENT) is True
        assert await store.get_open_by_client(CLIENT) is None
        assert await store.get_open_by_thread(10) is None
        assert await store.close_session(CLIENT) is False

    @pytest.mark.asyncio
    async def test_reopen_replaces_thread(self, sm):
        await store.open_session(CLIENT, 10)
        await store.close_session(CLIENT)
        await store.open_session(CLIENT, 20)
        assert (await store.get_open_by_client(CLIENT)).thread_id == 20
        assert await store.get_open_by_thread(10) is None

    @pytest.mark.asyncio
    async def test_threads_isolated_between_clients(self, sm):
        await store.open_session(CLIENT, 10)
        await store.open_session(CLIENT2, 11)
        assert (await store.get_open_by_thread(11)).client_id == CLIENT2

    def test_rate_limiter_window(self):
        t = [0.0]
        rl = store.RateLimiter(max_events=2, window=10, clock=lambda: t[0])
        assert rl.allow(1) and rl.allow(1)
        assert not rl.allow(1)
        assert rl.allow(2)  # другой клиент не затронут
        t[0] = 11
        assert rl.allow(1)

    def test_question_memory_bounded(self, monkeypatch):
        monkeypatch.setattr(store, "_QUESTIONS_MAX", 3)
        for i in range(5):
            store.remember_question(i, f"q{i}")
        assert store.last_question(0) == "" and store.last_question(4) == "q4"
        store.remember_question(9, "x" * 1000)
        assert len(store.last_question(9)) == 500


# ------------------------------ эскалация --------------------------------- #
class TestEscalation:
    @pytest.mark.asyncio
    async def test_open_creates_topic_and_session(self, sm):
        bot = make_bot()
        store.remember_question(CLIENT, "где мой заказ?")
        msg = make_msg(bot)
        await h.cmd_operator(msg)
        bot.create_forum_topic.assert_awaited_once()
        card = bot.send_message.await_args
        assert card.args[0] == GROUP and card.kwargs["message_thread_id"] == 77
        assert "где мой заказ" in card.args[1]
        assert (await store.get_open_by_client(CLIENT)).thread_id == 77
        assert "оператору" in msg.answer.await_args.args[0]

    @pytest.mark.asyncio
    async def test_card_escapes_html(self, sm):
        bot = make_bot()
        store.remember_question(CLIENT, "<script>alert(1)</script>")
        msg = make_msg(bot)
        msg.from_user.first_name = "<b>Ev</b>"
        await h.cmd_operator(msg)
        card = bot.send_message.await_args.args[1]
        assert "<script>" not in card and "<b>Ev</b>" not in card

    @pytest.mark.asyncio
    async def test_second_call_does_not_duplicate_topic(self, sm):
        bot = make_bot()
        await h.cmd_operator(make_msg(bot))
        await h.cmd_operator(make_msg(bot))
        assert bot.create_forum_topic.await_count == 1

    @pytest.mark.asyncio
    async def test_disabled_when_no_group(self, sm, monkeypatch):
        monkeypatch.setattr(sys.modules["app.core.config"].settings, "OPERATOR_CHAT_ID", 0)
        bot = make_bot()
        msg = make_msg(bot)
        await h.cmd_operator(msg)
        bot.create_forum_topic.assert_not_awaited()
        assert "недоступен" in msg.answer.await_args.args[0]

    @pytest.mark.asyncio
    async def test_banned_user_denied(self, sm, monkeypatch):
        monkeypatch.setattr(h, "user_allowed", AsyncMock(return_value=False))
        bot = make_bot()
        msg = make_msg(bot)
        await h.cmd_operator(msg)
        bot.create_forum_topic.assert_not_awaited()
        assert await store.get_open_by_client(CLIENT) is None

    @pytest.mark.asyncio
    async def test_topic_creation_failure_reported(self, sm):
        bot = make_bot()
        bot.create_forum_topic.side_effect = _bad("not enough rights to create a topic")
        msg = make_msg(bot)
        await h.cmd_operator(msg)
        assert await store.get_open_by_client(CLIENT) is None
        assert "Не удалось" in msg.answer.await_args.args[0]

    @pytest.mark.asyncio
    async def test_callback_button_opens_session(self, sm):
        bot = make_bot()
        inner = make_msg(bot)
        cb = SimpleNamespace(
            bot=bot, from_user=inner.from_user, message=inner, answer=AsyncMock(), data=h.CALL_CALLBACK
        )
        await h.cb_call_operator(cb)
        cb.answer.assert_awaited()
        assert await store.get_open_by_client(CLIENT) is not None


# --------------------------- клиент → оператор ---------------------------- #
class TestClientToOperator:
    @pytest.mark.asyncio
    async def test_filter_only_during_session(self, sm):
        bot = make_bot()
        assert not await h.client_in_session(make_msg(bot))
        await store.open_session(CLIENT, 77)
        assert await h.client_in_session(make_msg(bot))
        assert not await h.client_in_session(make_msg(bot, uid=CLIENT2))

    @pytest.mark.asyncio
    async def test_own_commands_not_swallowed(self, sm):
        await store.open_session(CLIENT, 77)
        bot = make_bot()
        for cmd in ("/endchat", "/help", "/start", "/operator@mybot"):
            assert not await h.client_in_session(make_msg(bot, text=cmd))

    @pytest.mark.asyncio
    async def test_group_messages_never_client_route(self, sm):
        await store.open_session(CLIENT, 77)
        assert not await h.client_in_session(make_msg(make_bot(), chat_type="supergroup"))

    @pytest.mark.asyncio
    async def test_message_copied_into_thread(self, sm):
        await store.open_session(CLIENT, 77)
        bot = make_bot()
        await h.client_message(make_msg(bot, content_type="photo"))
        kw = bot.copy_message.await_args.kwargs
        assert kw == dict(chat_id=GROUP, from_chat_id=CLIENT, message_id=5, message_thread_id=77)

    @pytest.mark.asyncio
    async def test_service_messages_ignored(self, sm):
        await store.open_session(CLIENT, 77)
        bot = make_bot()
        await h.client_message(make_msg(bot, content_type="new_chat_members"))
        bot.copy_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_deleted_topic_is_recreated(self, sm):
        await store.open_session(CLIENT, 77)
        bot = make_bot()
        bot.create_forum_topic.return_value = SimpleNamespace(message_thread_id=88)
        bot.copy_message.side_effect = [_bad("message thread not found"), None]
        await h.client_message(make_msg(bot))
        assert (await store.get_open_by_client(CLIENT)).thread_id == 88
        assert bot.copy_message.await_args.kwargs["message_thread_id"] == 88

    @pytest.mark.asyncio
    async def test_other_errors_do_not_recreate_topic(self, sm):
        await store.open_session(CLIENT, 77)
        bot = make_bot()
        bot.copy_message.side_effect = _bad("chat not found")
        msg = make_msg(bot)
        await h.client_message(msg)
        bot.create_forum_topic.assert_not_awaited()
        assert "Не удалось" in msg.answer.await_args.args[0]

    @pytest.mark.asyncio
    async def test_flood_limited_and_warned_once(self, sm):
        await store.open_session(CLIENT, 77)
        bot = make_bot()
        for _ in range(3):
            await h.client_message(make_msg(bot))
        warn = make_msg(bot)
        await h.client_message(warn)
        again = make_msg(bot)
        await h.client_message(again)
        assert bot.copy_message.await_count == 3
        warn.answer.assert_awaited_once()
        again.answer.assert_not_awaited()


# --------------------------- оператор → клиент ---------------------------- #
class TestOperatorToClient:
    def _op_msg(self, bot, thread=77, **kw):
        return make_msg(bot, uid=900, chat_id=GROUP, thread=thread, chat_type="supergroup", **kw)

    @pytest.mark.asyncio
    async def test_filter_only_configured_group_topics(self, sm):
        bot = make_bot()
        assert await h.in_operator_topic(self._op_msg(bot))
        assert not await h.in_operator_topic(self._op_msg(bot, thread=None))  # General
        other = self._op_msg(bot)
        other.chat.id = -555
        assert not await h.in_operator_topic(other)
        assert not await h.in_operator_topic(self._op_msg(bot, is_bot=True))

    @pytest.mark.asyncio
    async def test_reply_copied_to_client(self, sm):
        await store.open_session(CLIENT, 77)
        bot = make_bot()
        await h.operator_reply(self._op_msg(bot))
        kw = bot.copy_message.await_args.kwargs
        assert kw["chat_id"] == CLIENT and kw["from_chat_id"] == GROUP
        assert "message_thread_id" not in kw

    @pytest.mark.asyncio
    async def test_unknown_or_closed_thread_ignored(self, sm):
        bot = make_bot()
        await h.operator_reply(self._op_msg(bot, thread=500))
        await store.open_session(CLIENT, 77)
        await store.close_session(CLIENT)
        await h.operator_reply(self._op_msg(bot))
        bot.copy_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_blocked_client_closes_session(self, sm):
        await store.open_session(CLIENT, 77)
        bot = make_bot()
        bot.copy_message.side_effect = _forbidden()
        msg = self._op_msg(bot)
        await h.operator_reply(msg)
        assert await store.get_open_by_client(CLIENT) is None
        msg.reply.assert_awaited()

    @pytest.mark.asyncio
    async def test_close_command(self, sm):
        await store.open_session(CLIENT, 77)
        bot = make_bot()
        await h.cmd_close(self._op_msg(bot, text="/close"))
        assert await store.get_open_by_client(CLIENT) is None
        bot.close_forum_topic.assert_awaited_once_with(chat_id=GROUP, message_thread_id=77)
        assert bot.send_message.await_args.args[0] == CLIENT

    @pytest.mark.asyncio
    async def test_client_endchat(self, sm):
        await store.open_session(CLIENT, 77)
        bot = make_bot()
        await h.cmd_endchat(make_msg(bot, text="/endchat"))
        assert await store.get_open_by_client(CLIENT) is None
        bot.close_forum_topic.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_endchat_without_session(self, sm):
        bot = make_bot()
        msg = make_msg(bot, text="/endchat")
        await h.cmd_endchat(msg)
        assert "нет" in msg.answer.await_args.args[0]
        bot.close_forum_topic.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_close_survives_deleted_topic(self, sm):
        await store.open_session(CLIENT, 77)
        bot = make_bot()
        bot.close_forum_topic.side_effect = _bad("message thread not found")
        await h.cmd_close(self._op_msg(bot, text="/close"))
        assert await store.get_open_by_client(CLIENT) is None


class TestWiring:
    def test_operator_router_registered_before_showcase(self):
        from pathlib import Path

        src = Path("app/bot_showcase.py").read_text(encoding="utf-8")
        assert src.index("include_router(operator_router)") < src.index("include_router(showcase_router)")
