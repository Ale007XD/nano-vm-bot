"""
Сбор расхода токенов LLM per-user через ContextVar.

Каждая обработка апдейта (сообщение/голос/фото/канал) открывает «сессию
учёта» start_usage_session(telegram_id). llm_client после каждого успешного
запроса вызывает record_usage(usage) — токены суммируются в контексте
текущей задачи (изоляция между конкурентными запросами разных
пользователей гарантируется ContextVar). В конце обработки хендлер
вызывает flush_usage() — накопленное атомарно добавляется в usage_daily.

Многовызовные сценарии (PlanBot: план + шаги + финал, Runtime: planner +
VM) суммируются автоматически, потому что все запросы идут в одной сессии.
"""
import logging
from contextvars import ContextVar
from typing import Optional

logger = logging.getLogger(__name__)

_session_user: ContextVar[Optional[int]] = ContextVar("usage_session_user", default=None)
_session_accum: ContextVar[Optional[dict]] = ContextVar("usage_session_accum", default=None)


def start_usage_session(telegram_id: int) -> None:
    """Открыть сессию учёта токенов для пользователя (в начале обработки апдейта)."""
    _session_user.set(telegram_id)
    _session_accum.set({})


def get_session_user() -> Optional[int]:
    """telegram_id пользователя текущей сессии учёта (None вне сессии)."""
    return _session_user.get()


def record_usage(usage: Optional[dict]) -> None:
    """Зафиксировать usage одного успешного LLM-запроса (вызывает llm_client)."""
    if _session_user.get() is None:
        return
    acc = _session_accum.get()
    if acc is None:
        acc = {}
        _session_accum.set(acc)

    acc["requests"] = acc.get("requests", 0) + 1
    if usage:
        acc["prompt_tokens"] = acc.get("prompt_tokens", 0) + int(usage.get("prompt_tokens") or 0)
        acc["completion_tokens"] = acc.get("completion_tokens", 0) + int(
            usage.get("completion_tokens") or 0
        )
        total = usage.get("total_tokens")
        acc["total_tokens"] = acc.get("total_tokens", 0) + int(
            total
            if total is not None
            else (int(usage.get("prompt_tokens") or 0) + int(usage.get("completion_tokens") or 0))
        )


def _end_session() -> Optional[dict]:
    telegram_id = _session_user.get()
    acc = _session_accum.get()
    _session_user.set(None)
    _session_accum.set(None)
    if telegram_id is None or not acc or not acc.get("requests"):
        return None
    return {"user_id": telegram_id, **acc}


async def flush_usage() -> None:
    """Сохранить накопленное за сессию в БД и закрыть сессию.

    Безопасно вызывать вне сессии. Ошибки БД не роняют обработку ответа.
    """
    data = _end_session()
    if not data:
        return
    try:
        from app.core.database import add_usage_daily

        await add_usage_daily(data["user_id"], data)
    except Exception as e:
        logger.warning(f"Failed to persist usage for {data.get('user_id')}: {e}")
