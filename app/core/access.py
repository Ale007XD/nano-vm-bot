"""
Гейт доступа и суточные лимиты сообщений.

Отдельный модуль без зависимостей от aiogram — используется хендлерами
и покрывается unit-тестами.

Режимы:
- BOT_OPEN_ACCESS=false (дефолт): бот приватный, доступ только ADMIN_IDS.
- BOT_OPEN_ACCESS=true: бот отвечает всем, кроме забаненных (/ban, /unban).

Лимиты:
- USER_DAILY_MESSAGE_LIMIT (0 = безлимит): суточное число сообщений
  на пользователя по UTC-суткам; админы не ограничены.
"""
import logging
from typing import NamedTuple, Optional

from app.core.config import settings

logger = logging.getLogger(__name__)


def is_admin(telegram_id: int) -> bool:
    ids = settings.ADMIN_IDS
    return isinstance(ids, (list, tuple, set)) and telegram_id in ids


async def user_allowed(telegram_id: int) -> bool:
    """Пропускать ли сообщение пользователя."""
    if is_admin(telegram_id):
        return True
    if not bool(getattr(settings, "BOT_OPEN_ACCESS", False)):
        return False
    try:
        from app.core.database import is_user_banned

        return not await is_user_banned(telegram_id)
    except Exception as e:
        logger.warning(f"Ban-list check failed for {telegram_id}: {e}")
        return True


DEFAULT_USER_TOOLS = frozenset({"validated_answer"})


def user_tool_allowlist() -> frozenset:
    """Инструменты call_tool, доступные не-админам (env RUNTIME_USER_TOOLS).

    Строка через запятую; пустая строка — ничего. Нечитаемое значение
    (не строка) — безопасный дефолт.
    """
    raw = getattr(settings, "RUNTIME_USER_TOOLS", None)
    if not isinstance(raw, str):
        return DEFAULT_USER_TOOLS
    return frozenset(t.strip() for t in raw.split(",") if t.strip())


def can_use_tool(telegram_id: int, tool_name: str) -> bool:
    """Может ли пользователь вызвать инструмент из Runtime VM.

    Админ — любой. Остальные — только из allowlist. Решение принимается по
    ID из StateContext (не из аргументов, сгенерированных planner-LLM).
    """
    if is_admin(telegram_id):
        return True
    return tool_name in user_tool_allowlist()


class LimitCheck(NamedTuple):
    allowed: bool
    message: Optional[str] = None


async def get_effective_limits() -> dict:
    """Действующие суточные лимиты: динамические из БД поверх env-дефолтов.

    Виды: messages (env USER_DAILY_MESSAGE_LIMIT), requests, tokens.
    0 — вид лимита отключён.
    """
    limits = {
        "messages": int(getattr(settings, "USER_DAILY_MESSAGE_LIMIT", 0) or 0),
        "requests": 0,
        "tokens": 0,
    }
    try:
        from app.core.database import get_bot_setting

        for key in list(limits):
            raw = await get_bot_setting(f"daily_limit_{key}")
            if raw is not None:
                limits[key] = int(raw)
    except Exception as e:
        logger.warning(f"Dynamic limits load failed, using env defaults: {e}")
    return limits


async def _deny_or_spend_credit(telegram_id: int, denial: str) -> LimitCheck:
    """Бесплатный лимит исчерпан: списать 1 кредит (если есть) либо отказать.

    Сбой БД биллинга не блокирует и не списывает — как и сбой самой проверки лимитов.
    """
    try:
        from app.core.billing.providers import billing_enabled
        from app.core.billing.service import consume_credit

        if await consume_credit(telegram_id):
            return LimitCheck(True)
        if billing_enabled():
            denial += "\n\n💳 Докупить запросы: /buy"
    except Exception as e:
        logger.warning(f"Credit check failed for {telegram_id}: {e}")
    return LimitCheck(False, denial)


async def check_daily_limit(telegram_id: int) -> LimitCheck:
    """Суточные лимиты (UTC): сообщения + LLM-запросы + токены.

    Админ — безлимит. При исчерпании возвращает готовый текст отказа.
    Сбой БД не блокирует пользователей.
    """
    if is_admin(telegram_id):
        return LimitCheck(True)

    # BYOK: пользователь платит за себя своими ключами — лимиты бота не применяются
    try:
        from app.core.byok import has_user_keys

        if await has_user_keys(telegram_id):
            return LimitCheck(True)
    except Exception as e:
        logger.warning(f"BYOK check failed for {telegram_id}: {e}")

    limits = await get_effective_limits()
    if not any(v for v in limits.values()):
        return LimitCheck(True)

    try:
        from app.core.database import count_today_messages, get_today_usage

        if limits["messages"] > 0:
            used = await count_today_messages(telegram_id)
            if used >= limits["messages"]:
                return await _deny_or_spend_credit(
                    telegram_id,
                    "⏳ <b>Дневной лимит исчерпан</b> "
                    f"(сообщения: {used}/{limits['messages']}).\n\n"
                    "Лимит обновится в 00:00 UTC.",
                )

        if limits["requests"] > 0 or limits["tokens"] > 0:
            usage = await get_today_usage(telegram_id)
            if usage is not None:
                if limits["requests"] > 0 and usage.requests >= limits["requests"]:
                    return await _deny_or_spend_credit(
                        telegram_id,
                        "⏳ <b>Дневной лимит исчерпан</b> "
                        f"(LLM-запросы: {usage.requests}/{limits['requests']}).\n\n"
                        "Лимит обновится в 00:00 UTC.",
                    )
                if limits["tokens"] > 0 and usage.total_tokens >= limits["tokens"]:
                    return await _deny_or_spend_credit(
                        telegram_id,
                        "⏳ <b>Дневной лимит исчерпан</b> "
                        f"(токены: {usage.total_tokens}/{limits['tokens']}).\n\n"
                        "Лимит обновится в 00:00 UTC.",
                    )
    except Exception as e:
        logger.warning(f"Daily limit check failed for {telegram_id}: {e}")

    return LimitCheck(True)
