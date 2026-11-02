"""Запуск исполнителя напоминаний пространств в процессе бота."""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from app.core.config import settings
from app.spaces import reminders, reports

logger = logging.getLogger(__name__)

_task: Optional[asyncio.Task] = None
_stop: Optional[asyncio.Event] = None


def poll_seconds() -> float:
    v = getattr(settings, "SPACE_REMINDER_POLL_SECONDS", 15)
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 1 else 15.0


def start(bot) -> Optional[asyncio.Task]:
    """Запустить цикл (всегда: он же доставляет личные напоминания /remind, /daily, /weekly);
    повторный вызов второй цикл не создаёт."""
    global _task, _stop
    if _task is not None and not _task.done():
        return _task

    async def send(chat_id: int, text: str) -> None:
        await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")

    hooks = []
    if reports.enabled():
        reports.load_plugins(getattr(settings, "SPACE_REPORT_PLUGINS", ""))
        hooks.append((lambda: reports.run_due(send), 60.0))
    _stop = asyncio.Event()
    _task = asyncio.create_task(reminders.run_loop(send, poll_seconds(), _stop, hooks=hooks), name="space-reminders")
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
