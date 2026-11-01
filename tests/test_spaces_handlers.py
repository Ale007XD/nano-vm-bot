"""Хендлеры пространств: вступление по ссылке (через настоящий Dispatcher), создание, права, участники."""
import sys
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from aiogram import Bot, Dispatcher, Router
from aiogram.types import Chat, Message, Update, User
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.ext_db import ExtBase

sys.modules["app.core.config"].settings.BOT_TIMEZONE = "UTC"
from app.handlers import spaces as h  # noqa: E402
from app.spaces import repo, service  # noqa: E402

ADMIN, MOM, KID, STRANGER = 1, 102, 103, 999


@pytest_asyncio.fixture
async def sm(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(ExtBase.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    for mod in (service, repo):
        monkeypatch.setattr(mod, "resolve_session_maker", lambda _=None, m=maker: m)
    yield maker
    await engine.dispose()


@pytest.fixture(autouse=True)
def env(monkeypatch, sm):
    st = sys.modules["app.core.config"].settings
    for k, v in dict(ADMIN_IDS=[ADMIN], SPACES_ENABLED=True, SPACE_MAX_MEMBERS=5,
                     SPACE_INVITE_TTL_DAYS=7, BOT_TIMEZONE="UTC", BOT_OPEN_ACCESS=False).items():
        monkeypatch.setattr(st, k, v, raising=False)
    db = sys.modules["app.core.database"]
    monkeypatch.setattr(db, "is_user_banned", AsyncMock(return_value=False))
    return st


def msg(uid, text="", name="Имя"):
    m = MagicMock()
    m.from_user = SimpleNamespace(id=uid, first_name=name, username=None)
    m.chat = SimpleNamespace(type="private")
    m.answer = AsyncMock()
    m.bot.me = AsyncMock(return_value=SimpleNamespace(username="family_bot"))
    return m


def cmd(args=None):
    return SimpleNamespace(args=args)


def said(m) -> str:
    return "\n".join(c.args[0] for c in m.answer.await_args_list)


async def make_family(name="Ивановы"):
    return await service.create_space(name, ADMIN, kind="family", owner_name="Админ")


class TestCreate:
    @pytest.mark.asyncio
    async def test_only_bot_admin_creates(self):
        m = msg(STRANGER)
        await h.cmd_space_new(m, cmd("family Ивановы"))
        assert "Только для администраторов" in said(m)
        assert await service.spaces_of(STRANGER) == []

    @pytest.mark.asyncio
    async def test_admin_creates_and_becomes_owner(self):
        m = msg(ADMIN)
        await h.cmd_space_new(m, cmd("family Семья Ивановых"))
        [sp] = await service.spaces_of(ADMIN)
        assert (sp.name, sp.kind) == ("Семья Ивановых", "family")
        assert await service.member_role(sp.id, ADMIN) == "owner"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("args", [None, "family", "Bad-Kind name"])
    async def test_bad_args(self, args):
        m = msg(ADMIN)
        await h.cmd_space_new(m, cmd(args))
        assert await service.spaces_of(ADMIN) == []

    @pytest.mark.asyncio
    async def test_disabled_flag(self, env, monkeypatch):
        monkeypatch.setattr(env, "SPACES_ENABLED", False)
        m = msg(ADMIN)
        await h.cmd_space_new(m, cmd("family X"))
        assert "SPACES_ENABLED" in said(m) and await service.spaces_of(ADMIN) == []


class TestInviteAndJoin:
    @pytest.mark.asyncio
    async def test_invite_link_then_join(self):
        sp = await make_family()
        m = msg(ADMIN)
        await h.cmd_invite(m, cmd("member 2 3"))
        text = said(m)
        link = next(w for w in text.split() if w.startswith("https://t.me/family_bot?start=sp_"))
        code = link.split("sp_")[1]

        j = msg(MOM, name="Мама")
        await h.cmd_start_join(j, SimpleNamespace(args=f"sp_{code}"))
        assert "присоединились" in said(j) and "Семья" in said(j) and "Ивановы" in said(j)
        assert await service.member_role(sp.id, MOM) == "member"

        again = msg(MOM)
        await h.cmd_start_join(again, SimpleNamespace(args=f"sp_{code}"))
        assert "уже в пространстве" in said(again)

    @pytest.mark.asyncio
    async def test_member_cannot_invite(self):
        sp = await make_family()
        await service.add_member(sp.id, KID)
        m = msg(KID)
        await h.cmd_invite(m, cmd())
        assert "Нужны права" in said(m)

    @pytest.mark.asyncio
    async def test_stranger_cannot_invite(self):
        await make_family()
        m = msg(STRANGER)
        await h.cmd_invite(m, cmd())
        assert "не состоите" in said(m)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("args", ["x", "member -1", "member 1 0", "member 1 2 3 x"])
    async def test_invite_bad_args_create_nothing(self, args):
        await make_family()
        m = msg(ADMIN)
        await h.cmd_invite(m, cmd(args))
        assert "https://" not in said(m)

    @pytest.mark.asyncio
    async def test_exhausted_and_invalid_messages(self):
        sp = await make_family()
        code = await service.create_invite(sp.id, ADMIN, max_uses=1)
        await service.redeem_invite(code, MOM)
        j = msg(KID)
        await h.cmd_start_join(j, SimpleNamespace(args=f"sp_{code}"))
        assert "уже использовано" in said(j)
        j2 = msg(KID)
        await h.cmd_start_join(j2, SimpleNamespace(args="sp_nonexistent1"))
        assert "недействительно" in said(j2)
        assert not await service.is_member_anywhere(KID)

    @pytest.mark.asyncio
    async def test_banned_user_cannot_join(self):
        sp = await make_family()
        code = await service.create_invite(sp.id, ADMIN)
        sys.modules["app.core.database"].is_user_banned.return_value = True
        j = msg(MOM)
        await h.cmd_start_join(j, SimpleNamespace(args=f"sp_{code}"))
        assert not await service.is_member_anywhere(MOM)

    @pytest.mark.asyncio
    async def test_join_when_disabled(self, env, monkeypatch):
        sp = await make_family()
        code = await service.create_invite(sp.id, ADMIN)
        monkeypatch.setattr(env, "SPACES_ENABLED", False)
        j = msg(MOM)
        await h.cmd_start_join(j, SimpleNamespace(args=f"sp_{code}"))
        assert not await service.is_member_anywhere(MOM)

    @pytest.mark.asyncio
    async def test_revoke_only_own_space(self):
        a = await make_family()
        other = await service.create_space("Чужая", 555)
        foreign = await service.create_invite(other.id, 555)
        mine = await service.create_invite(a.id, ADMIN)
        m = msg(ADMIN)
        await h.cmd_revoke_invite(m, cmd(foreign))
        assert "не найдено" in said(m)
        assert (await service.redeem_invite(foreign, MOM))[0] == service.RESULT_OK  # чужой код не тронут
        m2 = msg(ADMIN)
        await h.cmd_revoke_invite(m2, cmd(mine))
        assert "отозвано" in said(m2)
        assert (await service.redeem_invite(mine, KID))[0] == service.RESULT_REVOKED


class TestMembers:
    @pytest.mark.asyncio
    async def test_list_shows_ids_only_to_admins(self):
        sp = await make_family()
        await service.add_member(sp.id, KID, display_name="Сын")
        a, k = msg(ADMIN), msg(KID)
        await h.cmd_members(a)
        await h.cmd_members(k)
        assert f"<code>{KID}</code>" in said(a)
        assert "Сын" in said(k) and f"<code>{KID}</code>" not in said(k)

    @pytest.mark.asyncio
    async def test_member_list_escapes_html(self):
        sp = await make_family()
        await service.add_member(sp.id, KID, display_name="<b>x</b>")
        m = msg(ADMIN)
        await h.cmd_members(m)
        assert "<b>x</b>" not in said(m) and "&lt;b&gt;x" in said(m)

    @pytest.mark.asyncio
    async def test_kick_rules(self):
        sp = await make_family()
        await service.add_member(sp.id, MOM, role="admin")
        await service.add_member(sp.id, KID)
        # админ не может убрать владельца, участник — никого
        m = msg(MOM)
        await h.cmd_kick(m, cmd(str(ADMIN)))
        assert "старшей ролью" in said(m)
        k = msg(KID)
        await h.cmd_kick(k, cmd(str(MOM)))
        assert "Нужны права" in said(k)
        # админ убирает участника
        m2 = msg(MOM)
        await h.cmd_kick(m2, cmd(str(KID)))
        assert await service.member_role(sp.id, KID) is None
        bad = msg(MOM)
        await h.cmd_kick(bad, cmd("не число"))
        assert "Формат" in said(bad)

    @pytest.mark.asyncio
    async def test_kicked_user_loses_access_gate(self):
        from app.core.access import user_allowed

        sp = await make_family()
        await service.add_member(sp.id, KID)
        assert await user_allowed(KID)
        await h.cmd_kick(msg(ADMIN), cmd(str(KID)))
        assert not await user_allowed(KID)

    @pytest.mark.asyncio
    async def test_last_owner_cannot_leave(self):
        sp = await make_family()
        m = msg(ADMIN)
        await h.cmd_leave(m)
        assert "последнего владельца" in said(m)
        assert await service.member_role(sp.id, ADMIN) == "owner"

    @pytest.mark.asyncio
    async def test_leave(self):
        sp = await make_family()
        await service.add_member(sp.id, KID)
        m = msg(KID)
        await h.cmd_leave(m)
        assert await service.member_role(sp.id, KID) is None


class TestRouting:
    """Через настоящий Dispatcher: ссылка пускает чужого, обычный /start — нет."""

    def _update(self, uid, text):
        user = User(id=uid, is_bot=False, first_name="N")
        message = Message(message_id=1, date=datetime.now(), chat=Chat(id=uid, type="private"),
                          from_user=user, text=text,
                          entities=[{"type": "bot_command", "offset": 0, "length": text.split()[0].__len__()}])
        return Update(update_id=1, message=message)

    @pytest_asyncio.fixture
    async def dp(self, monkeypatch):
        monkeypatch.setattr(Message, "answer", AsyncMock())
        dp = Dispatcher()
        dp.include_router(h.router)
        passed = Router()
        seen = []

        @passed.message()
        async def _fallback(message: Message):
            seen.append(message.text)

        dp.include_router(passed)
        dp.seen = seen
        yield dp
        h.router._parent_router = None  # роутер — синглтон модуля: освободить для следующего теста

    @pytest.mark.asyncio
    async def test_deep_link_reaches_join_handler(self, dp):
        sp = await make_family()
        code = await service.create_invite(sp.id, ADMIN)
        bot = Bot("1:AAA")
        await dp.feed_update(bot, self._update(STRANGER, f"/start sp_{code}"))
        assert await service.member_role(sp.id, STRANGER) == "member"
        assert dp.seen == []

    @pytest.mark.asyncio
    async def test_plain_start_and_other_links_fall_through(self, dp):
        bot = Bot("1:AAA")
        await dp.feed_update(bot, self._update(STRANGER, "/start"))
        await dp.feed_update(bot, self._update(STRANGER, "/start inv_abcdef12"))
        assert dp.seen == ["/start", "/start inv_abcdef12"]

    def test_router_is_registered_before_commands(self):
        import inspect

        from app.handlers import register_handlers

        src = inspect.getsource(register_handlers)
        assert src.index("spaces.router") < src.index("commands.router")
