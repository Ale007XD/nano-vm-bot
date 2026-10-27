"""
BYOK: хранение ключей, маршрутизация LLM без фолбэка, снятие лимитов, /byok.
БД — in-memory SQLite (aiosqlite); сеть не используется.
"""
import sys
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core import byok
from app.core.ext_db import ExtBase

U1, U2 = 111, 222
KEY_GROQ = "gsk_" + "a" * 40
KEY_OAI = "sk-" + "b" * 40


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    settings = sys.modules["app.core.config"].settings
    monkeypatch.setattr(settings, "ADMIN_IDS", [999])
    monkeypatch.setattr(settings, "BYOK_ENABLED", True)
    monkeypatch.setattr(settings, "BOT_TIMEZONE", "UTC")
    byok.invalidate()
    yield
    byok.invalidate()


@pytest_asyncio.fixture
async def sm():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(ExtBase.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


class TestStorage:
    @pytest.mark.asyncio
    async def test_roundtrip_and_hint(self, sm):
        hint = await byok.set_user_key(U1, "groq", KEY_GROQ, sm)
        assert hint == KEY_GROQ[-4:]
        assert await byok.get_user_keys(U1, sm) == {"groq": KEY_GROQ}

    @pytest.mark.asyncio
    async def test_key_is_encrypted_at_rest(self, sm):
        await byok.set_user_key(U1, "groq", KEY_GROQ, sm)
        async with sm() as s:
            raw = (await s.execute(text("SELECT encrypted_key, key_hint FROM user_api_keys"))).one()
        assert KEY_GROQ not in raw[0]
        assert raw[0] != KEY_GROQ
        assert raw[1] == KEY_GROQ[-4:]

    @pytest.mark.asyncio
    async def test_replace_same_provider_keeps_single_row(self, sm):
        await byok.set_user_key(U1, "groq", KEY_GROQ, sm)
        new = "gsk_" + "z" * 40
        await byok.set_user_key(U1, "groq", new, sm)
        assert await byok.get_user_keys(U1, sm) == {"groq": new}
        async with sm() as s:
            assert (await s.execute(text("SELECT COUNT(*) FROM user_api_keys"))).scalar() == 1

    @pytest.mark.asyncio
    async def test_users_isolated(self, sm):
        await byok.set_user_key(U1, "groq", KEY_GROQ, sm)
        await byok.set_user_key(U2, "openai", KEY_OAI, sm)
        assert await byok.get_user_keys(U1, sm) == {"groq": KEY_GROQ}
        assert await byok.get_user_keys(U2, sm) == {"openai": KEY_OAI}
        assert await byok.has_user_keys(U1, sm) and not await byok.has_user_keys(333, sm)

    @pytest.mark.asyncio
    async def test_delete_one_and_all(self, sm):
        await byok.set_user_key(U1, "groq", KEY_GROQ, sm)
        await byok.set_user_key(U1, "openai", KEY_OAI, sm)
        assert await byok.delete_user_key(U1, "groq", sm) == 1
        assert set(await byok.get_user_keys(U1, sm)) == {"openai"}
        assert await byok.delete_user_key(U1, None, sm) == 1
        assert await byok.get_user_keys(U1, sm) == {}

    @pytest.mark.asyncio
    async def test_delete_does_not_touch_other_users(self, sm):
        await byok.set_user_key(U1, "groq", KEY_GROQ, sm)
        await byok.set_user_key(U2, "groq", KEY_GROQ, sm)
        await byok.delete_user_key(U1, None, sm)
        assert await byok.has_user_keys(U2, sm)

    @pytest.mark.asyncio
    async def test_list_returns_hints_not_keys(self, sm):
        await byok.set_user_key(U1, "groq", KEY_GROQ, sm)
        listing = await byok.list_user_keys(U1, sm)
        assert listing == {"groq": KEY_GROQ[-4:]}

    @pytest.mark.asyncio
    async def test_cache_invalidated_on_change(self, sm):
        await byok.set_user_key(U1, "groq", KEY_GROQ, sm)
        assert await byok.get_user_keys(U1, sm)  # прогрев кэша
        await byok.delete_user_key(U1, None, sm)
        assert await byok.get_user_keys(U1, sm) == {}  # не из кэша

    @pytest.mark.asyncio
    async def test_cache_ttl_expires(self, sm, monkeypatch):
        await byok.set_user_key(U1, "groq", KEY_GROQ, sm)
        await byok.get_user_keys(U1, sm)
        # удаление «снаружи» (другой процесс) — кэш держит ключ до TTL
        async with sm() as s:
            await s.execute(text("DELETE FROM user_api_keys"))
            await s.commit()
        assert await byok.get_user_keys(U1, sm)
        exp, keys = byok._cache[U1]
        byok._cache[U1] = (0.0, keys)
        assert await byok.get_user_keys(U1, sm) == {}

    @pytest.mark.asyncio
    async def test_without_encryption_key_fails_closed(self, sm, monkeypatch):
        monkeypatch.delenv("ENCRYPTION_KEY")
        monkeypatch.setattr(sys.modules["app.core.config"].settings, "ENCRYPTION_KEY", "")
        with pytest.raises(byok.BYOKUnavailable):
            await byok.set_user_key(U1, "groq", KEY_GROQ, sm)
        async with sm() as s:
            assert (await s.execute(text("SELECT COUNT(*) FROM user_api_keys"))).scalar() == 0

    @pytest.mark.asyncio
    async def test_rejects_bad_format_and_provider(self, sm):
        for prov, key in [("groq", "short"), ("groq", "has space " + "x" * 30),
                          ("nope", KEY_GROQ), ("groq", "")]:
            with pytest.raises(ValueError):
                await byok.set_user_key(U1, prov, key, sm)
        assert not await byok.has_user_keys(U1, sm)


class TestLiveValidation:
    @staticmethod
    def _transport(status=200, seen=None, exc=None):
        def handler(req):
            if exc:
                raise exc
            if seen is not None:
                seen.append(req)
            return httpx.Response(status)
        return httpx.MockTransport(handler)

    @pytest.mark.asyncio
    async def test_ok(self):
        assert (await byok.validate_key_live("groq", KEY_GROQ, self._transport(200)))[0] is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize("code", [401, 403])
    async def test_rejected(self, code):
        ok, _ = await byok.validate_key_live("openai", KEY_OAI, self._transport(code))
        assert ok is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize("code", [429, 500, 503])
    async def test_inconclusive_not_treated_as_valid(self, code):
        ok, _ = await byok.validate_key_live("groq", KEY_GROQ, self._transport(code))
        assert ok is None

    @pytest.mark.asyncio
    async def test_network_error_inconclusive(self):
        ok, reason = await byok.validate_key_live(
            "groq", KEY_GROQ, self._transport(exc=httpx.ConnectError("boom"))
        )
        assert ok is None and KEY_GROQ not in reason

    @pytest.mark.asyncio
    async def test_auth_headers_per_provider(self):
        seen = []
        await byok.validate_key_live("anthropic", "sk-ant-" + "c" * 30, self._transport(200, seen))
        await byok.validate_key_live("groq", KEY_GROQ, self._transport(200, seen))
        assert seen[0].headers["x-api-key"].startswith("sk-ant-")
        assert "authorization" not in seen[0].headers
        assert seen[1].headers["authorization"] == f"Bearer {KEY_GROQ}"
        assert seen[0].url.host == "api.anthropic.com" and seen[1].url.host == "api.groq.com"


class TestSessionResolution:
    @pytest.mark.asyncio
    async def test_current_user_keys_follow_usage_session(self, sm, monkeypatch):
        from app.core import usage

        await byok.set_user_key(U1, "groq", KEY_GROQ, sm)
        monkeypatch.setattr(byok, "resolve_session_maker", lambda _=None: sm)
        assert await byok.current_user_keys() == {}  # вне сессии
        usage.start_usage_session(U1)
        try:
            assert await byok.current_user_keys() == {"groq": KEY_GROQ}
            usage.start_usage_session(U2)
            assert await byok.current_user_keys() == {}
        finally:
            usage._session_user.set(None)

    @pytest.mark.asyncio
    async def test_resolve_provider_key_shared_vs_byok(self, monkeypatch):
        monkeypatch.setattr(byok, "current_user_keys", AsyncMock(return_value={}))
        assert await byok.resolve_provider_key("groq", "SHARED") == ("SHARED", False)
        assert await byok.resolve_provider_key("groq", "") == (None, False)
        monkeypatch.setattr(byok, "current_user_keys", AsyncMock(return_value={"openai": KEY_OAI}))
        # BYOK активен, ключа groq нет → общий НЕ подставляется
        assert await byok.resolve_provider_key("groq", "SHARED") == (None, True)
        monkeypatch.setattr(byok, "current_user_keys", AsyncMock(return_value={"groq": KEY_GROQ}))
        assert await byok.resolve_provider_key("groq", "SHARED") == (KEY_GROQ, True)


class TestLlmRouting:
    @pytest.fixture
    def llm(self, monkeypatch):
        from app.core.llm_client_v2 import MultiProviderLLMClient, Provider

        c = MultiProviderLLMClient()
        c.providers = [
            Provider(name="groq", base_url="https://g", api_key="SHARED-GROQ", models=["m"], priority=1),
            Provider(name="openrouter", base_url="https://o", api_key="SHARED-OR", models=["m"], priority=2),
        ]
        monkeypatch.setattr(c, "_start_health_monitor", lambda: None)
        monkeypatch.setattr(c, "retry_delay", 0)
        c._store_pending_task = MagicMock(return_value="pending-1")
        return c

    @staticmethod
    def _ok(seen):
        async def make_request(provider, **kw):
            seen.append(provider)
            return {"content": "hi", "model": "m", "usage": None}
        return make_request

    @pytest.mark.asyncio
    async def test_no_byok_uses_shared_providers(self, llm, monkeypatch):
        seen = []
        llm._user_keys = AsyncMock(return_value={})
        llm._make_request = self._ok(seen)
        r = await llm.chat([MagicMock()], temperature=0.1, max_tokens=10)
        assert r.content == "hi" and seen[0].api_key == "SHARED-GROQ"

    @pytest.mark.asyncio
    async def test_byok_uses_only_user_key(self, llm):
        seen = []
        llm._user_keys = AsyncMock(return_value={"openai": KEY_OAI})
        llm._make_request = self._ok(seen)
        r = await llm.chat([MagicMock()], temperature=0.1, max_tokens=10)
        assert r.provider == "openai"
        assert [p.api_key for p in seen] == [KEY_OAI]

    @pytest.mark.asyncio
    async def test_byok_works_even_if_bot_has_no_such_provider(self, llm):
        """Общего ключа openai у бота нет, но BYOK-ключ пользователя работает."""
        assert all(p.name != "openai" for p in llm.providers)
        seen = []
        llm._user_keys = AsyncMock(return_value={"openai": KEY_OAI})
        llm._make_request = self._ok(seen)
        await llm.chat([MagicMock()], temperature=0.1, max_tokens=10)
        assert seen and seen[0].name == "openai"

    @pytest.mark.asyncio
    async def test_byok_failure_never_falls_back_to_shared(self, llm):
        seen = []

        async def failing(provider, **kw):
            seen.append(provider.api_key)
            raise RuntimeError("HTTP 500 upstream")

        llm._user_keys = AsyncMock(return_value={"groq": KEY_GROQ})
        llm._make_request = failing
        with pytest.raises(byok.BYOKError):
            await llm.chat([MagicMock()], temperature=0.1, max_tokens=10, max_attempts=2)
        assert set(seen) == {KEY_GROQ}              # общие ключи не пробовались
        llm._store_pending_task.assert_not_called()  # pending ушёл бы на общие ключи

    @pytest.mark.asyncio
    async def test_byok_auth_error_not_retried(self, llm):
        n = []

        async def unauthorized(provider, **kw):
            n.append(1)
            raise RuntimeError("Client error '401 Unauthorized'")

        llm._user_keys = AsyncMock(return_value={"groq": KEY_GROQ})
        llm._make_request = unauthorized
        with pytest.raises(byok.BYOKError):
            await llm.chat([MagicMock()], temperature=0.1, max_tokens=10, max_attempts=3)
        assert len(n) == 1

    @pytest.mark.asyncio
    async def test_byok_does_not_mutate_shared_provider_state(self, llm):
        async def failing(provider, **kw):
            raise RuntimeError("boom")

        llm._user_keys = AsyncMock(return_value={"groq": KEY_GROQ})
        llm._make_request = failing
        with pytest.raises(byok.BYOKError):
            await llm.chat([MagicMock()], temperature=0.1, max_tokens=10, max_attempts=3)
        assert all(p.error_count == 0 and p.api_key.startswith("SHARED") for p in llm.providers)

    @pytest.mark.asyncio
    async def test_non_byok_failure_still_stores_pending(self, llm):
        async def failing(provider, **kw):
            raise RuntimeError("boom")

        llm._user_keys = AsyncMock(return_value={})
        llm._make_request = failing
        with pytest.raises(Exception, match="pending"):
            await llm.chat([MagicMock()], temperature=0.1, max_tokens=10, max_attempts=1)
        llm._store_pending_task.assert_called_once()

    @pytest.mark.asyncio
    async def test_transcribe_requires_user_groq_key_under_byok(self, llm):
        llm._user_keys = AsyncMock(return_value={"openai": KEY_OAI})
        with pytest.raises(byok.BYOKError, match="Groq"):
            await llm.transcribe_audio("/nonexistent.ogg")

    @pytest.mark.asyncio
    async def test_transcribe_uses_user_groq_key(self, llm, tmp_path, monkeypatch):
        audio = tmp_path / "a.ogg"
        audio.write_bytes(b"data")
        captured = {}

        class FakeClient:
            def __init__(self, *a, **k): ...
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def post(self, url, content=None, headers=None, timeout=None):
                captured["url"], captured["auth"] = url, headers["Authorization"]
                r = MagicMock()
                r.raise_for_status = lambda: None
                r.json = lambda: {"text": " привет "}
                return r

        monkeypatch.setattr("app.core.llm_client_v2.httpx.AsyncClient", FakeClient)
        llm._user_keys = AsyncMock(return_value={"groq": KEY_GROQ})
        assert await llm.transcribe_audio(str(audio)) == "привет"
        assert captured["auth"] == f"Bearer {KEY_GROQ}"
        assert "SHARED" not in captured["auth"]


class TestLimitExemption:
    @pytest.fixture
    def over_limit(self, monkeypatch):
        st = sys.modules["app.core.config"].settings
        monkeypatch.setattr(st, "USER_DAILY_MESSAGE_LIMIT", 5)
        db = sys.modules["app.core.database"]
        monkeypatch.setattr(db, "get_bot_setting", AsyncMock(return_value=None), raising=False)
        monkeypatch.setattr(db, "count_today_messages", AsyncMock(return_value=50), raising=False)
        monkeypatch.setattr(db, "get_today_usage", AsyncMock(return_value=None), raising=False)
        from app.core.billing import service

        monkeypatch.setattr(service, "consume_credit", AsyncMock(return_value=False))

    @pytest.mark.asyncio
    async def test_limit_applies_without_byok(self, over_limit, monkeypatch):
        from app.core.access import check_daily_limit

        monkeypatch.setattr(byok, "has_user_keys", AsyncMock(return_value=False))
        assert (await check_daily_limit(U1)).allowed is False

    @pytest.mark.asyncio
    async def test_byok_user_not_limited(self, over_limit, monkeypatch):
        from app.core.access import check_daily_limit

        monkeypatch.setattr(byok, "has_user_keys", AsyncMock(return_value=True))
        assert (await check_daily_limit(U1)).allowed is True

    @pytest.mark.asyncio
    async def test_byok_lookup_error_falls_back_to_normal_limits(self, over_limit, monkeypatch):
        from app.core.access import check_daily_limit

        monkeypatch.setattr(byok, "has_user_keys", AsyncMock(side_effect=RuntimeError("db down")))
        assert (await check_daily_limit(U1)).allowed is False


# --------------------------------------------------------------------------- #
# Handler /byok
# --------------------------------------------------------------------------- #

def _msg(text_args, chat_type="private", uid=U1):
    m = MagicMock()
    m.from_user.id = uid
    m.chat.type = chat_type
    m.delete = AsyncMock()
    m.answer = AsyncMock()
    from aiogram.filters import CommandObject

    cmd = CommandObject(prefix="/", command="byok", args=text_args)
    return m, cmd


def _answers(m):
    return "\n".join(str(c.args[0]) for c in m.answer.await_args_list)


class TestHandler:
    @pytest.fixture
    def h(self, monkeypatch):
        from app.handlers import byok as h

        monkeypatch.setattr(h, "user_allowed", AsyncMock(return_value=True))
        monkeypatch.setattr(h.byok, "validate_key_live", AsyncMock(return_value=(True, "ok")))
        monkeypatch.setattr(h.byok, "set_user_key", AsyncMock(return_value=KEY_GROQ[-4:]))
        monkeypatch.setattr(h.byok, "delete_user_key", AsyncMock(return_value=1))
        monkeypatch.setattr(h.byok, "list_user_keys", AsyncMock(return_value={"groq": "abcd"}))
        return h

    @pytest.mark.asyncio
    async def test_set_deletes_message_before_validation_and_hides_key(self, h):
        order = []
        m, cmd = _msg(f"set groq {KEY_GROQ}")
        m.delete.side_effect = lambda: order.append("delete")
        h.byok.validate_key_live.side_effect = lambda *a, **k: (order.append("validate"), (True, "ok"))[1]
        await h.cmd_byok(m, cmd)
        assert order == ["delete", "validate"]
        h.byok.set_user_key.assert_awaited_once_with(U1, "groq", KEY_GROQ)
        assert KEY_GROQ not in _answers(m)
        assert KEY_GROQ[-4:] in _answers(m)

    @pytest.mark.asyncio
    async def test_set_refused_in_group_and_message_deleted(self, h):
        m, cmd = _msg(f"set groq {KEY_GROQ}", chat_type="supergroup")
        await h.cmd_byok(m, cmd)
        m.delete.assert_awaited_once()
        h.byok.set_user_key.assert_not_called()
        h.byok.validate_key_live.assert_not_called()
        assert KEY_GROQ not in _answers(m)

    @pytest.mark.asyncio
    async def test_rejected_key_not_saved(self, h):
        h.byok.validate_key_live.return_value = (False, "провайдер отклонил ключ")
        m, cmd = _msg(f"set groq {KEY_GROQ}")
        await h.cmd_byok(m, cmd)
        h.byok.set_user_key.assert_not_called()
        assert "не принят" in _answers(m) and KEY_GROQ not in _answers(m)

    @pytest.mark.asyncio
    async def test_inconclusive_check_not_saved(self, h):
        h.byok.validate_key_live.return_value = (None, "HTTP 503")
        m, cmd = _msg(f"set groq {KEY_GROQ}")
        await h.cmd_byok(m, cmd)
        h.byok.set_user_key.assert_not_called()
        assert "не сохранён" in _answers(m)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("args", ["set", "set groq", "set groq a b", "set nope " + KEY_GROQ,
                                      "set groq short"])
    async def test_bad_input_not_saved(self, h, args):
        m, cmd = _msg(args)
        await h.cmd_byok(m, cmd)
        h.byok.set_user_key.assert_not_called()
        m.delete.assert_awaited()  # сообщение с возможным ключом удалено всё равно

    @pytest.mark.asyncio
    async def test_no_encryption_key_message(self, h, monkeypatch):
        monkeypatch.delenv("ENCRYPTION_KEY")
        monkeypatch.setattr(sys.modules["app.core.config"].settings, "ENCRYPTION_KEY", "")
        m, cmd = _msg(f"set groq {KEY_GROQ}")
        await h.cmd_byok(m, cmd)
        h.byok.set_user_key.assert_not_called()
        h.byok.validate_key_live.assert_not_called()
        assert "ENCRYPTION_KEY" in _answers(m)

    @pytest.mark.asyncio
    async def test_disabled_by_admin(self, h, monkeypatch):
        monkeypatch.setattr(sys.modules["app.core.config"].settings, "BYOK_ENABLED", False)
        m, cmd = _msg(f"set groq {KEY_GROQ}")
        await h.cmd_byok(m, cmd)
        h.byok.set_user_key.assert_not_called()
        m.delete.assert_awaited()
        assert "отключ" in _answers(m)

    @pytest.mark.asyncio
    async def test_del_one_all_unknown(self, h):
        m, cmd = _msg("del groq")
        await h.cmd_byok(m, cmd)
        h.byok.delete_user_key.assert_awaited_with(U1, "groq")
        m, cmd = _msg("del all")
        await h.cmd_byok(m, cmd)
        h.byok.delete_user_key.assert_awaited_with(U1, None)
        h.byok.delete_user_key.reset_mock()
        m, cmd = _msg("del nope")
        await h.cmd_byok(m, cmd)
        h.byok.delete_user_key.assert_not_called()

    @pytest.mark.asyncio
    async def test_status_shows_hint_only(self, h):
        m, cmd = _msg(None)
        await h.cmd_byok(m, cmd)
        out = _answers(m)
        assert "groq" in out and "abcd" in out and "только" in out

    @pytest.mark.asyncio
    async def test_status_without_keys(self, h):
        h.byok.list_user_keys.return_value = {}
        m, cmd = _msg("")
        await h.cmd_byok(m, cmd)
        assert "общие ключи" in _answers(m)

    @pytest.mark.asyncio
    async def test_banned_user_denied(self, h):
        h.user_allowed.return_value = False
        m, cmd = _msg(f"set groq {KEY_GROQ}")
        await h.cmd_byok(m, cmd)
        h.byok.set_user_key.assert_not_called()
