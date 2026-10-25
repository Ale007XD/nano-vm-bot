"""
Per-user учёт токенов LLM: сессии ContextVar, суммирование, flush в БД.
"""
import asyncio
import sys
from unittest.mock import AsyncMock

import pytest


@pytest.fixture
def mock_db():
    return sys.modules["app.core.database"]


@pytest.fixture(autouse=True)
def clean_session():
    from app.core import usage

    usage._session_user.set(None)
    usage._session_accum.set(None)
    yield
    usage._session_user.set(None)
    usage._session_accum.set(None)


class TestUsageSession:
    def test_record_outside_session_is_noop(self):
        from app.core import usage

        usage.record_usage({"prompt_tokens": 10, "completion_tokens": 5})
        # flush вне сессии ничего не пишет
        assert usage._end_session() is None

    @pytest.mark.asyncio
    async def test_accumulates_and_flushes(self, monkeypatch, mock_db):
        from app.core import usage

        add_mock = AsyncMock()
        monkeypatch.setattr(mock_db, "add_usage_daily", add_mock)

        usage.start_usage_session(42)
        usage.record_usage(None)  # запрос без usage — только счётчик запросов
        usage.record_usage({"prompt_tokens": 10, "completion_tokens": 5})
        usage.record_usage({"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10})
        await usage.flush_usage()

        add_mock.assert_awaited_once()
        kwargs = add_mock.await_args.args
        assert kwargs[0] == 42
        data = kwargs[1]
        assert data["requests"] == 3
        assert data["prompt_tokens"] == 17
        assert data["completion_tokens"] == 8
        # total: у первого запроса не было total_tokens → prompt+completion (15),
        # у второго — явный total 10
        assert data["total_tokens"] == 25

    @pytest.mark.asyncio
    async def test_double_flush_writes_once(self, monkeypatch, mock_db):
        from app.core import usage

        add_mock = AsyncMock()
        monkeypatch.setattr(mock_db, "add_usage_daily", add_mock)

        usage.start_usage_session(1)
        usage.record_usage({"prompt_tokens": 1})
        await usage.flush_usage()
        await usage.flush_usage()

        add_mock.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_flush_survives_db_error(self, monkeypatch, mock_db):
        from app.core import usage

        async def boom(uid, data):
            raise RuntimeError("db down")

        monkeypatch.setattr(mock_db, "add_usage_daily", boom)

        usage.start_usage_session(1)
        usage.record_usage({"prompt_tokens": 1})
        await usage.flush_usage()  # не должно бросить
        assert usage._end_session() is None  # сессия закрыта

    @pytest.mark.asyncio
    async def test_concurrent_sessions_are_isolated(self, monkeypatch, mock_db):
        from app.core import usage

        add_mock = AsyncMock()
        monkeypatch.setattr(mock_db, "add_usage_daily", add_mock)

        async def worker(uid: int):
            usage.start_usage_session(uid)
            usage.record_usage({"prompt_tokens": uid, "completion_tokens": 1})
            await asyncio.sleep(0.02)  # дать interleaving
            usage.record_usage({"prompt_tokens": uid, "completion_tokens": 1})
            await usage.flush_usage()

        await asyncio.gather(worker(7), worker(9))

        saved = {call.args[0]: call.args[1] for call in add_mock.await_args_list}
        assert set(saved) == {7, 9}
        assert saved[7]["prompt_tokens"] == 14
        assert saved[9]["prompt_tokens"] == 18
