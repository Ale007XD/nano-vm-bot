"""«Печатает…» не должно висеть после ответа: ранние return в обработчиках сообщений.

Регрессия: ссылка пересланным сообщением уходила в базу знаний и делала `return` до
stop_typing — индикатор продолжал обновляться до 3 минут, а затем пропадал без ответа.
"""
import sys
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.modules["app.core.config"].settings.BOT_TIMEZONE = "UTC"  # нужен при импорте обработчиков

from app.handlers import messages  # noqa: E402


@pytest.fixture
def env(monkeypatch, tmp_path):
    ev = SimpleNamespace(started=[], stopped=[], flushed=0)

    def start(bot, chat_id, *a, **k):
        task = object()
        ev.started.append(task)
        return task

    async def flush():
        ev.flushed += 1

    @asynccontextmanager
    async def session_maker():
        yield MagicMock()

    monkeypatch.setattr(messages, "start_typing", start)
    monkeypatch.setattr(messages, "stop_typing", lambda t: ev.stopped.append(t))
    monkeypatch.setattr(messages, "flush_usage", flush)
    monkeypatch.setattr(messages, "start_usage_session", lambda uid: None)
    monkeypatch.setattr(messages, "user_allowed", AsyncMock(return_value=True))
    monkeypatch.setattr(messages, "check_daily_limit", AsyncMock(return_value=SimpleNamespace(allowed=True)))
    monkeypatch.setattr(messages, "async_session_maker", session_maker)
    monkeypatch.setattr(messages, "get_or_create_user", AsyncMock(return_value=SimpleNamespace(id=1)))
    monkeypatch.setattr(messages, "get_user_agent_mode", AsyncMock(return_value="fastbot"))
    monkeypatch.setattr(sys.modules["app.core.database"], "get_user_messages", AsyncMock(return_value=[]), raising=False)
    st = sys.modules["app.core.config"].settings
    monkeypatch.setattr(st, "MAX_VOICE_DURATION", 300, raising=False)
    monkeypatch.setattr(st, "ENABLE_VOICE_IN_GROUPS", True, raising=False)
    monkeypatch.setattr(st, "TEMP_DIR", str(tmp_path), raising=False)
    return ev


def _msg(**kw):
    base = dict(
        from_user=SimpleNamespace(id=7, username="u", first_name="U", last_name=None, language_code="ru"),
        chat=SimpleNamespace(id=7, type="private"),
        bot=SimpleNamespace(send_chat_action=AsyncMock(), get_file=AsyncMock(), download_file=AsyncMock()),
        answer=AsyncMock(return_value=SimpleNamespace(delete=AsyncMock(), edit_text=AsyncMock())),
        forward_origin=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.mark.asyncio
async def test_forwarded_url_to_knowledge_base_stops_typing(env, monkeypatch):
    kb = AsyncMock(return_value="✅ Статья добавлена")
    monkeypatch.setattr(
        sys.modules["app.core.skills_loader"], "skill_loader", SimpleNamespace(get_skill=lambda name: kb)
    )
    msg = _msg(text="https://example.com/post", forward_origin=object())

    await messages.handle_message(msg, state=None)

    kb.assert_awaited_once()
    assert len(env.started) == 1
    assert env.stopped == env.started          # индикатор остановлен
    assert env.flushed >= 1                     # расход токенов KB-разбора записан


@pytest.mark.asyncio
async def test_error_path_still_stops_typing(env, monkeypatch):
    kb = AsyncMock(side_effect=RuntimeError("boom"))
    monkeypatch.setattr(
        sys.modules["app.core.skills_loader"], "skill_loader", SimpleNamespace(get_skill=lambda name: kb)
    )
    msg = _msg(text="https://example.com/post", forward_origin=object())

    await messages.handle_message(msg, state=None)

    assert len(env.started) == 1 and set(env.stopped) == set(env.started)   # остановка идемпотентна
    assert "ошибка" in msg.answer.await_args.args[0]


@pytest.mark.asyncio
async def test_voice_with_empty_transcript_stops_typing(env, monkeypatch):
    from app.core import llm_client_v2

    monkeypatch.setattr(llm_client_v2.llm_client, "transcribe_audio", AsyncMock(return_value=""))
    msg = _msg(voice=SimpleNamespace(duration=3, file_id="f1"))
    msg.bot.get_file = AsyncMock(return_value=SimpleNamespace(file_path="x"))

    await messages.handle_voice(msg, state=None)

    assert len(env.started) == 1 and env.stopped == env.started
    assert "Не удалось распознать" in msg.answer.await_args_list[-1].args[0]
