"""Админские команды/колбэки доступны только ADMIN_IDS (user_allowed пускает всех при BOT_OPEN_ACCESS)."""
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.modules["app.core.config"].settings.BOT_TIMEZONE = "UTC"  # нужен при импорте обработчиков

from app.handlers import commands, providers, reminders  # noqa: E402

ADMIN, USER = 1, 2


@pytest.fixture(autouse=True)
def _admins(monkeypatch):
    monkeypatch.setattr(commands, "is_admin", lambda uid: uid == ADMIN)
    monkeypatch.setattr(reminders, "is_admin", lambda uid: uid == ADMIN)
    monkeypatch.setattr(commands, "user_allowed", AsyncMock(return_value=True))  # открытый режим


def _msg(uid):
    m = MagicMock()
    m.from_user = SimpleNamespace(id=uid)
    m.answer = AsyncMock()
    return m


def _cb(uid, data):
    c = MagicMock()
    c.from_user = SimpleNamespace(id=uid)
    c.data = data
    c.answer = AsyncMock()
    c.message.edit_text = AsyncMock()
    return c


@pytest.mark.asyncio
async def test_models_command_denies_regular_user():
    m = _msg(USER)
    await commands.cmd_models(m)
    assert "администратор" in m.answer.await_args.args[0]


@pytest.mark.asyncio
async def test_providers_set_denied_and_does_not_touch_models(monkeypatch):
    fake_llm = MagicMock()
    monkeypatch.setitem(sys.modules, "app.core.llm_client_v2", SimpleNamespace(llm_client=fake_llm))
    c = _cb(USER, "providers:set:groq:default:0")
    await commands.providers_set(c)
    fake_llm.set_model.assert_not_called()
    c.answer.assert_awaited_once()
    assert c.answer.await_args.kwargs.get("show_alert") is True


@pytest.mark.asyncio
@pytest.mark.parametrize("handler", ["providers_menu", "providers_refresh", "providers_show", "providers_models"])
async def test_provider_menu_callbacks_denied(handler):
    c = _cb(USER, "providers:x:groq:default")
    await getattr(commands, handler)(c)
    c.message.edit_text.assert_not_awaited()
    assert c.answer.await_args.kwargs.get("show_alert") is True


@pytest.mark.asyncio
async def test_scheduler_stats_denied_for_regular_user():
    m = _msg(USER)
    await reminders.cmd_scheduler_stats(m)
    assert "администратор" in m.answer.await_args.args[0]


def test_prov_callbacks_not_shadowed_by_commands_router():
    """prov:select/close/refresh принадлежат providers.router; compat-дубли в commands их затеняли."""
    for name in ("prov_select_compat", "prov_close_compat", "prov_refresh_compat"):
        assert not hasattr(commands, name)
    assert hasattr(providers, "cb_prov_select")
