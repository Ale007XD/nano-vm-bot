"""
Мультиюзерность: гейт доступа (открытый/закрытый режим, бан-лист)
и суточные лимиты сообщений — app/core/access.py.
"""
import sys
from unittest.mock import AsyncMock

import pytest


@pytest.fixture
def mock_settings():
    return sys.modules["app.core.config"].settings


@pytest.fixture
def mock_db():
    return sys.modules["app.core.database"]


@pytest.fixture(autouse=True)
def _no_byok(monkeypatch):
    """BYOK-проверка в check_daily_limit ходит в БД; здесь БД замокана — отключаем."""
    from app.core import byok

    monkeypatch.setattr(byok, "has_user_keys", AsyncMock(return_value=False))


@pytest.fixture(autouse=True)
def _no_credits(monkeypatch):
    """Списание кредитов ходит в БД; здесь БД замокана — кредитов нет."""
    from app.core.billing import service

    monkeypatch.setattr(service, "consume_credit", AsyncMock(return_value=False))


class TestUserAllowed:
    @pytest.mark.asyncio
    async def test_admin_always_allowed(self, monkeypatch, mock_settings):
        from app.core.access import user_allowed

        monkeypatch.setattr(mock_settings, "ADMIN_IDS", [1, 2])
        assert await user_allowed(1) is True

    @pytest.mark.asyncio
    async def test_closed_mode_blocks_stranger(self, monkeypatch, mock_settings):
        from app.core.access import user_allowed

        monkeypatch.setattr(mock_settings, "ADMIN_IDS", [1])
        monkeypatch.setattr(mock_settings, "BOT_OPEN_ACCESS", False)
        assert await user_allowed(999) is False

    @pytest.mark.asyncio
    async def test_open_mode_allows_stranger(self, monkeypatch, mock_settings, mock_db):
        from app.core.access import user_allowed

        monkeypatch.setattr(mock_settings, "ADMIN_IDS", [1])
        monkeypatch.setattr(mock_settings, "BOT_OPEN_ACCESS", True)
        monkeypatch.setattr(
            mock_db, "is_user_banned", AsyncMock(return_value=False)
        )
        assert await user_allowed(999) is True

    @pytest.mark.asyncio
    async def test_open_mode_blocks_banned(self, monkeypatch, mock_settings, mock_db):
        from app.core.access import user_allowed

        monkeypatch.setattr(mock_settings, "ADMIN_IDS", [1])
        monkeypatch.setattr(mock_settings, "BOT_OPEN_ACCESS", True)
        monkeypatch.setattr(
            mock_db, "is_user_banned", AsyncMock(return_value=True)
        )
        assert await user_allowed(999) is False

    @pytest.mark.asyncio
    async def test_ban_check_failure_does_not_lock_everyone(
        self, monkeypatch, mock_settings, mock_db
    ):
        from app.core.access import user_allowed

        monkeypatch.setattr(mock_settings, "ADMIN_IDS", [1])
        monkeypatch.setattr(mock_settings, "BOT_OPEN_ACCESS", True)

        async def boom(telegram_id):
            raise RuntimeError("db down")

        monkeypatch.setattr(mock_db, "is_user_banned", boom)
        assert await user_allowed(999) is True


class TestDailyLimit:
    @pytest.fixture(autouse=True)
    def _no_dynamic_overrides(self, monkeypatch, mock_db):
        # по умолчанию динамические лимиты из БД не заданы — env-дефолты
        monkeypatch.setattr(
            mock_db,
            "get_bot_setting",
            AsyncMock(return_value=None),
        )

    @pytest.mark.asyncio
    async def test_admin_bypasses_limit(self, monkeypatch, mock_settings):
        from app.core.access import check_daily_limit

        monkeypatch.setattr(mock_settings, "ADMIN_IDS", [1])
        monkeypatch.setattr(mock_settings, "USER_DAILY_MESSAGE_LIMIT", 3)
        check = await check_daily_limit(1)
        assert check.allowed is True
        assert check.message is None

    @pytest.mark.asyncio
    async def test_all_limits_zero_is_unlimited(self, monkeypatch, mock_settings):
        from app.core.access import check_daily_limit

        monkeypatch.setattr(mock_settings, "ADMIN_IDS", [])
        monkeypatch.setattr(mock_settings, "USER_DAILY_MESSAGE_LIMIT", 0)
        check = await check_daily_limit(999)
        assert check.allowed is True

    @pytest.mark.asyncio
    async def test_messages_limit_blocks_when_reached(
        self, monkeypatch, mock_settings, mock_db
    ):
        from app.core.access import check_daily_limit

        monkeypatch.setattr(mock_settings, "ADMIN_IDS", [])
        monkeypatch.setattr(mock_settings, "USER_DAILY_MESSAGE_LIMIT", 5)
        monkeypatch.setattr(
            mock_db, "count_today_messages", AsyncMock(return_value=5)
        )
        check = await check_daily_limit(999)
        assert check.allowed is False
        assert "сообщения: 5/5" in check.message

    @pytest.mark.asyncio
    async def test_messages_limit_allows_below_threshold(
        self, monkeypatch, mock_settings, mock_db
    ):
        from app.core.access import check_daily_limit

        monkeypatch.setattr(mock_settings, "ADMIN_IDS", [])
        monkeypatch.setattr(mock_settings, "USER_DAILY_MESSAGE_LIMIT", 5)
        monkeypatch.setattr(
            mock_db, "count_today_messages", AsyncMock(return_value=4)
        )
        check = await check_daily_limit(999)
        assert check.allowed is True

    @pytest.mark.asyncio
    async def test_dynamic_limit_overrides_env(
        self, monkeypatch, mock_settings, mock_db
    ):
        from app.core.access import check_daily_limit

        monkeypatch.setattr(mock_settings, "ADMIN_IDS", [])
        monkeypatch.setattr(mock_settings, "USER_DAILY_MESSAGE_LIMIT", 5)
        # по env: 5 сообщений — блок; динамически поднято до 7 — должно пустить
        monkeypatch.setattr(
            mock_db,
            "get_bot_setting",
            AsyncMock(side_effect=lambda key: "7" if key == "daily_limit_messages" else None),
        )
        monkeypatch.setattr(
            mock_db, "count_today_messages", AsyncMock(return_value=5)
        )
        check = await check_daily_limit(999)
        assert check.allowed is True

    @pytest.mark.asyncio
    async def test_requests_limit_blocks(self, monkeypatch, mock_settings, mock_db):
        from types import SimpleNamespace

        from app.core.access import check_daily_limit

        monkeypatch.setattr(mock_settings, "ADMIN_IDS", [])
        monkeypatch.setattr(mock_settings, "USER_DAILY_MESSAGE_LIMIT", 0)
        monkeypatch.setattr(
            mock_db,
            "get_bot_setting",
            AsyncMock(
                side_effect=lambda key: "10" if key == "daily_limit_requests" else None
            ),
        )
        monkeypatch.setattr(
            mock_db,
            "get_today_usage",
            AsyncMock(
                return_value=SimpleNamespace(
                    requests=10, prompt_tokens=100, completion_tokens=50, total_tokens=150
                )
            ),
        )
        check = await check_daily_limit(999)
        assert check.allowed is False
        assert "LLM-запросы: 10/10" in check.message

    @pytest.mark.asyncio
    async def test_tokens_limit_blocks(self, monkeypatch, mock_settings, mock_db):
        from types import SimpleNamespace

        from app.core.access import check_daily_limit

        monkeypatch.setattr(mock_settings, "ADMIN_IDS", [])
        monkeypatch.setattr(mock_settings, "USER_DAILY_MESSAGE_LIMIT", 0)
        monkeypatch.setattr(
            mock_db,
            "get_bot_setting",
            AsyncMock(
                side_effect=lambda key: "1000" if key == "daily_limit_tokens" else None
            ),
        )
        monkeypatch.setattr(
            mock_db,
            "get_today_usage",
            AsyncMock(
                return_value=SimpleNamespace(
                    requests=3, prompt_tokens=1200, completion_tokens=300, total_tokens=1500
                )
            ),
        )
        check = await check_daily_limit(999)
        assert check.allowed is False
        assert "токены: 1500/1000" in check.message

    @pytest.mark.asyncio
    async def test_limit_check_survives_db_error(
        self, monkeypatch, mock_settings, mock_db
    ):
        from app.core.access import check_daily_limit

        monkeypatch.setattr(mock_settings, "ADMIN_IDS", [])
        monkeypatch.setattr(mock_settings, "USER_DAILY_MESSAGE_LIMIT", 5)

        async def boom(tid):
            raise RuntimeError("db down")

        monkeypatch.setattr(mock_db, "count_today_messages", boom)
        check = await check_daily_limit(999)
        assert check.allowed is True


class TestShowcaseAccess:
    def test_inherits_bot_flag_when_unset(self, monkeypatch, mock_settings):
        from app.core.access import showcase_open

        monkeypatch.setattr(mock_settings, "SHOWCASE_OPEN_ACCESS", None)
        monkeypatch.setattr(mock_settings, "BOT_OPEN_ACCESS", True)
        assert showcase_open() is True
        monkeypatch.setattr(mock_settings, "BOT_OPEN_ACCESS", False)
        assert showcase_open() is False

    def test_own_flag_wins(self, monkeypatch, mock_settings):
        from app.core.access import showcase_open

        monkeypatch.setattr(mock_settings, "BOT_OPEN_ACCESS", False)
        monkeypatch.setattr(mock_settings, "SHOWCASE_OPEN_ACCESS", True)
        assert showcase_open() is True
        monkeypatch.setattr(mock_settings, "BOT_OPEN_ACCESS", True)
        monkeypatch.setattr(mock_settings, "SHOWCASE_OPEN_ACCESS", False)
        assert showcase_open() is False

    @pytest.mark.asyncio
    async def test_private_bot_open_showcase(self, monkeypatch, mock_settings, mock_db):
        from app.core.access import showcase_open, user_allowed

        monkeypatch.setattr(mock_settings, "ADMIN_IDS", [1])
        monkeypatch.setattr(mock_settings, "BOT_OPEN_ACCESS", False)
        monkeypatch.setattr(mock_settings, "SHOWCASE_OPEN_ACCESS", True)
        monkeypatch.setattr(mock_db, "is_user_banned", AsyncMock(return_value=False))
        assert await user_allowed(999) is False  # основной бот закрыт
        assert await user_allowed(999, showcase_open()) is True  # витрина открыта

    @pytest.mark.asyncio
    async def test_banned_blocked_in_open_showcase(self, monkeypatch, mock_settings, mock_db):
        from app.core.access import user_allowed

        monkeypatch.setattr(mock_settings, "ADMIN_IDS", [1])
        monkeypatch.setattr(mock_db, "is_user_banned", AsyncMock(return_value=True))
        assert await user_allowed(999, True) is False

    @pytest.mark.asyncio
    async def test_closed_showcase_still_admin_only(self, monkeypatch, mock_settings):
        from app.core.access import user_allowed

        monkeypatch.setattr(mock_settings, "ADMIN_IDS", [1])
        assert await user_allowed(1, False) is True
        assert await user_allowed(999, False) is False
