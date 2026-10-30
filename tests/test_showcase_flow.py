"""
Раздельные витрины, сквозные сценарии обработчиков: приглашения, изоляция
корпусов/диалогов/эскалаций между витринами, команды администратора.
Каждое обновление обрабатывается в контексте витрины своего бота (как в middleware).
"""
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core import ext_db, handoff, showcase_access, showcases
from app.core import operator as store
from app.core.access import LimitCheck
from app.core.dialog import history
from app.core.ext_db import ExtBase

sys.modules["app.core.config"].settings.BOT_TIMEZONE = "UTC"
from app.handlers import operator as h  # noqa: E402
from app.handlers import showcase as sc  # noqa: E402

ADMIN = 999
GROUP_A, GROUP_B = -1001, -1002


def build_registry():
    import json

    items = [
        {"slug": "reklama", "token": "111:AAA", "collection": "corpus", "operator_chat_id": GROUP_A,
         "name": "Наружная реклама", "system_prompt": "ПРОМПТ-РЕКЛАМА", "model": "model-a"},
        {"slug": "jurist", "token": "222:BBB", "operator_chat_id": GROUP_B,
         "system_prompt": "ПРОМПТ-ЮРИСТ", "model": "model-b", "welcome": "Здравствуйте, юрист на связи."},
    ]
    return showcases.ShowcaseRegistry(showcases.parse_showcases(json.dumps(items)))


@pytest.fixture
def reg():
    registry = build_registry()
    showcases.set_registry(registry)
    yield registry
    showcases.set_registry(None)


@pytest.fixture
def A(reg):
    return reg.get("reklama")


@pytest.fixture
def B(reg):
    return reg.get("jurist")


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
def env(monkeypatch, sm):
    st = sys.modules["app.core.config"].settings
    monkeypatch.setattr(st, "ADMIN_IDS", [ADMIN])
    monkeypatch.setattr(st, "BOT_TIMEZONE", "UTC")
    monkeypatch.setattr(st, "SHOWCASE_SYSTEM_PROMPT", "ПРОМПТ-ПО-УМОЛЧАНИЮ")
    monkeypatch.setattr(st, "SHOWCASE_MODEL", "model-default")
    monkeypatch.setattr(st, "CORPUS_TOP_K", 5)
    db = sys.modules["app.core.database"]
    monkeypatch.setattr(db, "is_user_banned", AsyncMock(return_value=False))
    monkeypatch.setattr(sc, "start_typing", MagicMock())
    monkeypatch.setattr(sc, "stop_typing", MagicMock())
    monkeypatch.setattr(sc, "start_usage_session", MagicMock())
    monkeypatch.setattr(sc, "flush_usage", AsyncMock())
    monkeypatch.setattr(sc, "deliver_via_message", AsyncMock())
    monkeypatch.setattr(sc, "_ensure_user", AsyncMock())
    monkeypatch.setattr(sc, "check_showcase_limit", AsyncMock(return_value=LimitCheck(True)))
    monkeypatch.setattr(handoff, "build_summary", AsyncMock(return_value=None))
    history._data.clear()
    h._warned_flood.clear()
    monkeypatch.setattr(h, "_limiter", store.RateLimiter(max_events=100, window=60))


def make_bot(username="demo_bot"):
    bot = MagicMock()
    bot.me = AsyncMock(return_value=SimpleNamespace(username=username))
    bot.create_forum_topic = AsyncMock(return_value=SimpleNamespace(message_thread_id=77))
    bot.send_message = AsyncMock()
    bot.copy_message = AsyncMock()
    bot.close_forum_topic = AsyncMock()
    return bot


def msg(uid, text="вопрос", *, bot=None, chat_id=None, chat_type="private", thread=None):
    processing = SimpleNamespace(edit_text=AsyncMock(), delete=AsyncMock(), answer=AsyncMock())
    user = SimpleNamespace(id=uid, first_name="Иван", last_name=None, username="ivan", is_bot=False)
    m = SimpleNamespace(
        bot=bot or make_bot(), from_user=user, text=text, message_id=5,
        chat=SimpleNamespace(id=chat_id if chat_id is not None else uid, type=chat_type),
        message_thread_id=thread, content_type="text",
        answer=AsyncMock(return_value=processing), reply=AsyncMock(),
    )
    m.processing = processing
    return m


def cmd(args=None):
    return SimpleNamespace(args=args)


def said(m):
    return " ".join(str(c.args[0]) for c in m.answer.await_args_list)


# ----------------------------- приглашения ------------------------------- #
class TestStartAndInvites:
    @pytest.mark.asyncio
    async def test_stranger_without_code_gets_invite_notice(self, B):
        m = msg(50)
        with showcases.use_showcase(B):
            await sc.cmd_start(m, cmd())
        assert "по приглашению" in said(m)
        assert not await showcase_access.is_member("jurist", 50)

    @pytest.mark.asyncio
    async def test_valid_code_opens_access_and_greets(self, B):
        code = await showcase_access.create_invite("jurist", ADMIN)
        m = msg(50)
        with showcases.use_showcase(B):
            await sc.cmd_start(m, cmd(f"inv_{code}"))
        out = said(m)
        assert "Доступ открыт" in out and "юрист на связи" in out
        assert await showcase_access.is_member("jurist", 50)

    @pytest.mark.asyncio
    async def test_member_can_ask_questions_after_joining(self, B, monkeypatch):
        code = await showcase_access.create_invite("jurist", ADMIN)
        with showcases.use_showcase(B):
            await sc.cmd_start(msg(50), cmd(f"inv_{code}"))
            monkeypatch.setattr(sc.vector_memory, "search_corpus", AsyncMock(return_value=[]))
            m = msg(50)
            await sc.handle_message(m)
        assert "не нашёл" in m.processing.edit_text.await_args.args[0]

    @pytest.mark.asyncio
    async def test_code_of_other_showcase_does_not_work(self, A, B):
        code = await showcase_access.create_invite("jurist", ADMIN)
        m = msg(50)
        with showcases.use_showcase(A):
            await sc.cmd_start(m, cmd(f"inv_{code}"))
        assert "недействительно" in said(m)
        assert not await showcase_access.is_member("reklama", 50)
        assert not await showcase_access.is_member("jurist", 50)

    @pytest.mark.asyncio
    async def test_exhausted_and_revoked_codes(self, B):
        code = await showcase_access.create_invite("jurist", ADMIN, max_uses=1)
        with showcases.use_showcase(B):
            await sc.cmd_start(msg(50), cmd(f"inv_{code}"))
            late = msg(51)
            await sc.cmd_start(late, cmd(f"inv_{code}"))
            assert "уже использовано" in said(late)
            revoked = await showcase_access.create_invite("jurist", ADMIN)
            await showcase_access.revoke_invite("jurist", revoked)
            m = msg(52)
            await sc.cmd_start(m, cmd(f"inv_{revoked}"))
            assert "отозвано" in said(m)
        assert not await showcase_access.is_member("jurist", 51)
        assert not await showcase_access.is_member("jurist", 52)

    @pytest.mark.asyncio
    async def test_banned_user_does_not_consume_invite(self, B):
        sys.modules["app.core.database"].is_user_banned = AsyncMock(return_value=True)
        code = await showcase_access.create_invite("jurist", ADMIN, max_uses=1)
        m = msg(50)
        with showcases.use_showcase(B):
            await sc.cmd_start(m, cmd(f"inv_{code}"))
        assert not await showcase_access.is_member("jurist", 50)
        assert (await showcase_access.list_invites("jurist"))[0].uses == 0

    @pytest.mark.asyncio
    async def test_malformed_payload_is_ignored(self, B):
        m = msg(50)
        with showcases.use_showcase(B):
            await sc.cmd_start(m, cmd("../../etc/passwd"))
        assert "по приглашению" in said(m)

    @pytest.mark.asyncio
    async def test_existing_member_with_code_is_not_charged(self, B):
        code = await showcase_access.create_invite("jurist", ADMIN, max_uses=2)
        with showcases.use_showcase(B):
            await sc.cmd_start(msg(50), cmd(f"inv_{code}"))
            m = msg(50)
            await sc.cmd_start(m, cmd(f"inv_{code}"))
        assert "Доступ открыт" not in said(m)
        assert (await showcase_access.list_invites("jurist"))[0].uses == 1

    @pytest.mark.asyncio
    async def test_open_showcase_ignores_code_and_greets_everyone(self, monkeypatch):
        import json

        (open_sc,) = showcases.parse_showcases(
            json.dumps([{"slug": "demo", "token": "333:CCC", "access": "open"}])
        )
        m = msg(50)
        with showcases.use_showcase(open_sc):
            await sc.cmd_start(m, cmd())
        assert "Добро пожаловать" in said(m)

    @pytest.mark.asyncio
    async def test_name_in_default_welcome(self, A):
        await showcase_access.add_member("reklama", 50)
        m = msg(50)
        with showcases.use_showcase(A):
            await sc.cmd_start(m, cmd())
        assert "Наружная реклама" in said(m)


# ----------------------------- вопросы ----------------------------------- #
def doc(name):
    return {"content": f"текст {name}", "metadata": {"source_id": name, "source_name": name, "chunk_index": 0}}


class TestQuestions:
    @pytest.mark.asyncio
    async def test_each_showcase_searches_its_own_corpus_with_its_own_prompt_and_model(
        self, A, B, monkeypatch
    ):
        await showcase_access.add_member("reklama", 50)
        await showcase_access.add_member("jurist", 50)
        search = AsyncMock(return_value=[doc("faq")])
        llm = AsyncMock(return_value=SimpleNamespace(content="ответ"))
        monkeypatch.setattr(sc.vector_memory, "search_corpus", search)
        monkeypatch.setattr(sc.llm_client, "chat_with_fallback", llm)

        with showcases.use_showcase(A):
            await sc.handle_message(msg(50, "вопрос A"))
        with showcases.use_showcase(B):
            await sc.handle_message(msg(50, "вопрос B"))

        assert [c.kwargs["corpus"] for c in search.await_args_list] == ["corpus", "corpus_jurist"]
        first, second = llm.await_args_list
        assert first.kwargs["model"] == "model-a" and "ПРОМПТ-РЕКЛАМА" in first.args[0][0].content
        assert second.kwargs["model"] == "model-b" and "ПРОМПТ-ЮРИСТ" in second.args[0][0].content
        assert "ПРОМПТ-РЕКЛАМА" not in second.args[0][0].content

    @pytest.mark.asyncio
    async def test_non_member_is_refused_before_any_search(self, B, monkeypatch):
        search = AsyncMock(return_value=[doc("faq")])
        monkeypatch.setattr(sc.vector_memory, "search_corpus", search)
        m = msg(50)
        with showcases.use_showcase(B):
            await sc.handle_message(m)
        search.assert_not_awaited()
        assert "по приглашению" in said(m)

    @pytest.mark.asyncio
    async def test_member_of_one_showcase_is_a_stranger_in_the_other(self, A, B, monkeypatch):
        await showcase_access.add_member("jurist", 50)
        search = AsyncMock(return_value=[])
        monkeypatch.setattr(sc.vector_memory, "search_corpus", search)
        with showcases.use_showcase(A):
            await sc.handle_message(msg(50))
        search.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_dialog_history_is_separate_per_showcase(self, A, B, monkeypatch):
        await showcase_access.add_member("reklama", 50)
        await showcase_access.add_member("jurist", 50)
        monkeypatch.setattr(sc.vector_memory, "search_corpus", AsyncMock(return_value=[]))
        with showcases.use_showcase(A):
            await sc.handle_message(msg(50, "про баннеры"))
        with showcases.use_showcase(B):
            await sc.handle_message(msg(50, "про банкротство"))
        assert history.last_client_text(A.client_key(50)) == "про баннеры"
        assert history.last_client_text(B.client_key(50)) == "про банкротство"

    @pytest.mark.asyncio
    async def test_limit_message_offers_operator_of_current_showcase(self, B, monkeypatch):
        await showcase_access.add_member("jurist", 50)
        monkeypatch.setattr(sc, "check_showcase_limit", AsyncMock(return_value=LimitCheck(False, "лимит")))
        m = msg(50)
        with showcases.use_showcase(B):
            await sc.handle_message(m)
        assert m.answer.await_args.args[0] == "лимит"
        assert m.answer.await_args.kwargs["reply_markup"] is not None


# ----------------------------- эскалации --------------------------------- #
class TestOperatorPerShowcase:
    @pytest.mark.asyncio
    async def test_topic_goes_to_the_group_of_that_showcase(self, A, B):
        await showcase_access.add_member("reklama", 50)
        await showcase_access.add_member("jurist", 50)
        bot_a, bot_b = make_bot(), make_bot()
        with showcases.use_showcase(A):
            await h.cmd_operator(msg(50, "/operator", bot=bot_a))
        with showcases.use_showcase(B):
            await h.cmd_operator(msg(50, "/operator", bot=bot_b))
        assert bot_a.create_forum_topic.await_args.kwargs["chat_id"] == GROUP_A
        assert bot_b.create_forum_topic.await_args.kwargs["chat_id"] == GROUP_B
        assert await store.get_open_by_client(50, tenant="reklama")
        assert await store.get_open_by_client(50, tenant="jurist")

    @pytest.mark.asyncio
    async def test_summary_uses_the_showcase_model(self, A, B, monkeypatch):
        await showcase_access.add_member("jurist", 50)
        build = AsyncMock(return_value="резюме")
        monkeypatch.setattr(handoff, "build_summary", build)
        history.add(B.client_key(50), "client", "вопрос", "question")
        with showcases.use_showcase(B):
            await h.cmd_operator(msg(50, "/operator"))
        assert build.await_args.kwargs["model"] == "model-b"

    @pytest.mark.asyncio
    async def test_operator_reply_in_other_group_with_same_thread_id_goes_nowhere(self, A, B):
        """В обеих группах тема №77: ответ в группе B не должен попасть клиенту витрины A."""
        await showcase_access.add_member("reklama", 50)
        await showcase_access.add_member("jurist", 60)
        with showcases.use_showcase(A):
            await h.cmd_operator(msg(50, "/operator"))
        with showcases.use_showcase(B):
            await h.cmd_operator(msg(60, "/operator"))

        bot = make_bot()
        reply_b = msg(ADMIN, "ответ", bot=bot, chat_id=GROUP_B, chat_type="supergroup", thread=77)
        with showcases.use_showcase(B):
            assert await h.in_operator_topic(reply_b)
            await h.operator_reply(reply_b)
        assert [c.kwargs["chat_id"] for c in bot.copy_message.await_args_list] == [60]

        # то же сообщение, пришедшее боту A, — не из его группы
        with showcases.use_showcase(A):
            assert not await h.in_operator_topic(reply_b)

    @pytest.mark.asyncio
    async def test_card_and_cleanup_use_only_this_showcases_history(self, A, B):
        await showcase_access.add_member("reklama", 50)
        await showcase_access.add_member("jurist", 50)
        history.add(A.client_key(50), "client", "про баннеры", "question")
        history.add(B.client_key(50), "client", "про банкротство", "question")
        bot = make_bot()
        with showcases.use_showcase(B):
            await h.cmd_operator(msg(50, "/operator", bot=bot))
        card = bot.send_message.await_args_list[0].args[1]
        assert "банкротство" in card and "баннеров" not in card

        with showcases.use_showcase(B):
            await h.cmd_endchat(msg(50, "/endchat"))
        assert history.recent(B.client_key(50)) == []
        assert [t.text for t in history.recent(A.client_key(50))] == ["про баннеры"]

    @pytest.mark.asyncio
    async def test_close_in_one_showcase_keeps_the_other_session(self, A, B):
        await showcase_access.add_member("reklama", 50)
        await showcase_access.add_member("jurist", 50)
        with showcases.use_showcase(A):
            await h.cmd_operator(msg(50, "/operator"))
        with showcases.use_showcase(B):
            await h.cmd_operator(msg(50, "/operator"))
            await h.cmd_endchat(msg(50, "/endchat"))
        assert await store.get_open_by_client(50, tenant="jurist") is None
        assert await store.get_open_by_client(50, tenant="reklama") is not None

    @pytest.mark.asyncio
    async def test_client_session_filter_is_per_showcase(self, A, B):
        await showcase_access.add_member("reklama", 50)
        with showcases.use_showcase(A):
            await h.cmd_operator(msg(50, "/operator"))
            assert await h.client_in_session(msg(50, "привет"))
        with showcases.use_showcase(B):
            assert not await h.client_in_session(msg(50, "привет"))

    @pytest.mark.asyncio
    async def test_non_member_cannot_escalate(self, B):
        bot = make_bot()
        m = msg(50, "/operator", bot=bot)
        with showcases.use_showcase(B):
            await h.cmd_operator(m)
        bot.create_forum_topic.assert_not_awaited()
        assert "Доступ запрещен" in said(m)

    @pytest.mark.asyncio
    async def test_showcase_without_group_has_no_operator(self, monkeypatch):
        import json

        (nogroup,) = showcases.parse_showcases(
            json.dumps([{"slug": "demo", "token": "333:CCC", "access": "open"}])
        )
        bot = make_bot()
        m = msg(50, "/operator", bot=bot)
        with showcases.use_showcase(nogroup):
            await h.cmd_operator(m)
        bot.create_forum_topic.assert_not_awaited()
        assert "недоступен" in said(m)


# ----------------------------- команды администратора -------------------- #
class TestAdminCommands:
    @pytest.mark.asyncio
    async def test_invite_creates_deeplink_for_this_showcase(self, B):
        bot = make_bot("ai_jurist_demo_bot")
        m = msg(ADMIN, "/invite 3 2", bot=bot)
        with showcases.use_showcase(B):
            await sc.cmd_invite(m, cmd("3 2"))
        out = said(m)
        assert "https://t.me/ai_jurist_demo_bot?start=inv_" in out
        code = out.split("start=inv_")[1].split()[0]
        invite = (await showcase_access.list_invites("jurist"))[0]
        assert invite.code == code and invite.max_uses == 3 and invite.expires_at is not None
        assert await showcase_access.list_invites("reklama") == []

    @pytest.mark.asyncio
    async def test_invite_defaults_and_unlimited(self, B):
        with showcases.use_showcase(B):
            await sc.cmd_invite(msg(ADMIN), cmd())
            await sc.cmd_invite(msg(ADMIN), cmd("0 0"))
        invites = await showcase_access.list_invites("jurist")
        assert sorted((i.max_uses, i.expires_at is None) for i in invites) == [(0, True), (1, False)]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("args", ["abc", "1 x", "-1", "1 -5"])
    async def test_invite_bad_args(self, B, args):
        m = msg(ADMIN)
        with showcases.use_showcase(B):
            await sc.cmd_invite(m, cmd(args))
        assert "Формат" in said(m)
        assert await showcase_access.list_invites("jurist") == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "handler, args",
        [("cmd_invite", "1 1"), ("cmd_invites", None), ("cmd_revoke_invite", "x"),
         ("cmd_allow", "5"), ("cmd_deny", "5"), ("cmd_members", None)],
    )
    async def test_non_admin_is_refused(self, B, handler, args):
        m = msg(50)
        fn = getattr(sc, handler)
        with showcases.use_showcase(B):
            if handler in ("cmd_invites", "cmd_members"):
                await fn(m)
            else:
                await fn(m, cmd(args))
        assert "Только для администраторов" in said(m)
        assert await showcase_access.list_invites("jurist") == []
        assert not await showcase_access.is_member("jurist", 5)

    @pytest.mark.asyncio
    async def test_allow_deny_members(self, B):
        with showcases.use_showcase(B):
            m1 = msg(ADMIN)
            await sc.cmd_allow(m1, cmd("5"))
            assert "Доступ выдан" in said(m1)
            m2 = msg(ADMIN)
            await sc.cmd_allow(m2, cmd("5"))
            assert "уже имеет доступ" in said(m2)
            m3 = msg(ADMIN)
            await sc.cmd_members(m3)
            assert "5" in said(m3)
            m4 = msg(ADMIN)
            await sc.cmd_deny(m4, cmd("5"))
            assert "отозван" in said(m4)
        assert not await showcase_access.is_member("jurist", 5)

    @pytest.mark.asyncio
    async def test_allow_bad_id(self, B):
        m = msg(ADMIN)
        with showcases.use_showcase(B):
            await sc.cmd_allow(m, cmd("не-число"))
            await sc.cmd_allow(m, cmd("0"))
            await sc.cmd_allow(m, cmd(None))
        assert said(m).count("Формат") == 3

    @pytest.mark.asyncio
    async def test_revoke_invite_only_in_own_showcase(self, A, B):
        code = await showcase_access.create_invite("jurist", ADMIN)
        with showcases.use_showcase(A):
            m = msg(ADMIN)
            await sc.cmd_revoke_invite(m, cmd(code))
            assert "нет в этой витрине" in said(m)
        with showcases.use_showcase(B):
            m = msg(ADMIN)
            await sc.cmd_revoke_invite(m, cmd(code))
            assert "отозвано" in said(m)

    @pytest.mark.asyncio
    async def test_invites_list(self, B):
        code = await showcase_access.create_invite("jurist", ADMIN, max_uses=4)
        m = msg(ADMIN)
        with showcases.use_showcase(B):
            await sc.cmd_invites(m)
        assert code in said(m) and "0/4" in said(m)


# ----------------------------- корпус ------------------------------------ #
class TestCorpusAdmin:
    @pytest.mark.asyncio
    async def test_stats_and_upload_target_the_showcase_collection(self, A, B, monkeypatch, tmp_path):
        commands = pytest.importorskip("app.handlers.commands")
        monkeypatch.setattr(commands, "is_admin", lambda uid: uid == ADMIN)

        stats = AsyncMock(return_value={"chunks": 3})
        add = AsyncMock()
        monkeypatch.setattr(sc.vector_memory, "get_corpus_stats", stats)
        monkeypatch.setattr(sc.vector_memory, "add_corpus_document", add)
        sources = AsyncMock(return_value=[{"name": "faq.md", "chunks": 4, "avg_chars": 310}])
        delete_by_name = AsyncMock(return_value=0)
        monkeypatch.setattr(sc.vector_memory, "get_corpus_sources", sources)
        monkeypatch.setattr(sc.vector_memory, "delete_corpus_by_name", delete_by_name)
        monkeypatch.setattr(sc.settings, "MAX_SKILL_FILE_SIZE", 10**6, raising=False)

        stats_msg = msg(ADMIN)
        with showcases.use_showcase(B):
            await sc.cmd_corpus_stats(stats_msg)
        assert stats.await_args.kwargs["corpus"] == "corpus_jurist"
        assert sources.await_args.kwargs["corpus"] == "corpus_jurist"
        assert "faq.md" in said(stats_msg) and "310" in said(stats_msg)

        async def download(path, dest):
            with open(dest, "w", encoding="utf-8") as fh:
                fh.write("Содержимое документа.")

        for showcase, expected in ((A, "corpus"), (B, "corpus_jurist")):
            bot = make_bot()
            bot.get_file = AsyncMock(return_value=SimpleNamespace(file_path="p"))
            bot.download_file = AsyncMock(side_effect=download)
            m = msg(ADMIN, bot=bot)
            m.document = SimpleNamespace(file_name="faq.md", file_size=10, file_id=f"f-{expected}")
            with showcases.use_showcase(showcase):
                await sc.handle_document(m)
            assert add.await_args.kwargs["corpus"] == expected
            # прежняя версия файла с тем же именем заменяется, а только что добавленная — нет
            assert delete_by_name.await_args.args[0] == "faq.md"
            assert delete_by_name.await_args.kwargs["corpus"] == expected
            assert delete_by_name.await_args.kwargs["keep_source_id"] == add.await_args.kwargs["source_id"]
