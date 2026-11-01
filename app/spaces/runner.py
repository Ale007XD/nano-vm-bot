"""Запуск исполнителя напоминаний пространств в процессе бота."""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from app.core.config import settings
from app.spaces import reminders

logger = logging.getLogger(__name__)

_task: Optional[asyncio.Task] = None
_stop: Optional[asyncio.Event] = None


def poll_seconds() -> float:
    v = getattr(settings, "SPACE_REMINDER_POLL_SECONDS", 15)
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 1 else 15.0


def start(bot) -> Optional[asyncio.Task]:
    """Запустить цикл, если SPACES_ENABLED=true; повторный вызов не создаёт второй цикл."""
    global _task, _stop
    if getattr(settings, "SPACES_ENABLED", False) is not True:
        return None
    if _task is not None and not _task.done():
        return _task

    async def send(chat_id: int, text: str) -> None:
        await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")

    _stop = asyncio.Event()
    _task = asyncio.create_task(reminders.run_loop(send, poll_seconds(), _stop), name="space-reminders")
    return _task


async def stop() -> None:
    global _task, _stop
    if _task is None:
        return
    if _stop is not None:
        _stop.set()
    try:
        await asyncio.wait_for(_task, timeout=5)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        _task.cancel()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"space reminders loop stopped with error: {e}")
    _task = _stop = None
