"""/monitor доходит до навыка site_monitor, число-интервал тоже, фон стартует."""
import sys
import types
from unittest.mock import AsyncMock

import pytest

sys.modules["app.core.config"].settings.BOT_TIMEZONE = "UTC"  # нужен при импорте обработчиков

from app.handlers import messages as m  # noqa: E402


class FakeMsg:
    def __init__(self, text, uid=7):
        self.text = text
        self.from_user = types.SimpleNamespace(id=uid, username="u", first_name="n", last_name=None, language_code="ru")
        self.out = []
        self.bot = types.SimpleNamespace(send_message=self._send)
        self.sent = []

    async def _send(self, uid, text):
        self.sent.append((uid, text))

    async def answer(self, t, **kw):
        self.out.append(t)


class FakeLoader:
    def __init__(self, skill):
        self.skill = skill

    def get_skill(self, name):
        return self.skill if name == "site_monitor" else None


@pytest.mark.asyncio
async def test_slash_monitor_runs_skill_with_notifier(monkeypatch):
    seen = {}

    async def run(ctx):
        seen.update(ctx)
        return "ok"

    monkeypatch.setattr(m, "skill_loader", FakeLoader(run))
    msg = FakeMsg("/monitor@MyBot add https://example.com/")
    await m._run_skill_message(msg, "site_monitor", "monitor add https://example.com/")
    assert msg.out == ["ok"]
    assert seen["message_text"] == "monitor add https://example.com/"
    assert callable(seen["send_message"])


@pytest.mark.asyncio
async def test_skill_error_still_answers(monkeypatch):
    async def run(ctx):
        raise RuntimeError("boom")

    monkeypatch.setattr(m, "skill_loader", FakeLoader(run))
    msg = FakeMsg("x")
    await m._run_skill_message(msg, "site_monitor", "monitor list")
    assert len(msg.out) == 1 and "Ошибка" in msg.out[0]


def test_pending_interval_detection(monkeypatch):
    pending = {}

    async def run(ctx):
        return ""

    run.__globals__["_pending_adds"] = pending
    monkeypatch.setattr(m, "skill_loader", FakeLoader(run))
    assert not m._monitor_awaits_interval(7, "15")
    pending[7] = "https://example.com/"
    assert m._monitor_awaits_interval(7, "15")
    assert not m._monitor_awaits_interval(7, "привет")
    assert not m._monitor_awaits_interval(8, "15")
    del run.__globals__["_pending_adds"]


def test_menu_buttons_exact_match_only():
    assert "💬 Чат" in m.MENU_BUTTONS
    assert "/monitor add x" not in m.MENU_BUTTONS


@pytest.mark.asyncio
async def test_handle_message_routes_slash_monitor_end_to_end(monkeypatch):
    """Регрессия: «/monitor add URL» раньше молча отбрасывался как «кнопка меню»."""
    calls = []

    async def run(ctx):
        calls.append(ctx["message_text"])
        return "Интервал?"

    monkeypatch.setattr(m, "skill_loader", FakeLoader(run))
    monkeypatch.setattr(m, "user_allowed", AsyncMock(return_value=True))
    msg = FakeMsg("/monitor add https://siesh-ka.online/")
    await m.handle_message(msg, state=None)
    assert calls == ["monitor add https://siesh-ka.online/"]
    assert msg.out == ["Интервал?"]


@pytest.mark.asyncio
async def test_handle_message_unknown_command_replies(monkeypatch):
    monkeypatch.setattr(m, "user_allowed", AsyncMock(return_value=True))
    msg = FakeMsg("/nosuchcmd")
    await m.handle_message(msg, state=None)
    assert len(msg.out) == 1 and "Неизвестная" in msg.out[0]


@pytest.mark.asyncio
async def test_handle_message_menu_button_stays_silent(monkeypatch):
    monkeypatch.setattr(m, "user_allowed", AsyncMock(return_value=True))
    msg = FakeMsg("🛠 Навыки")
    await m.handle_message(msg, state=None)
    assert msg.out == []
