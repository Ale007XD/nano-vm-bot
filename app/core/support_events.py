"""
Мини-шина событий поддержки (в процессе).

Точка расширения под веб-интерфейс оператора: веб-слой подписывается через on()
и получает события жизненного цикла эскалации (SSE/WebSocket поверх этого).
Ошибки подписчиков не влияют на работу бота.
"""
from __future__ import annotations

import inspect
import logging
from typing import Any, Callable, List

logger = logging.getLogger(__name__)

OPENED = "escalation.opened"
SUMMARY = "escalation.summary"
CLOSED = "escalation.closed"

Listener = Callable[[str, dict], Any]
_listeners: List[Listener] = []


def on(listener: Listener) -> None:
    if listener not in _listeners:
        _listeners.append(listener)


def off(listener: Listener) -> None:
    if listener in _listeners:
        _listeners.remove(listener)


async def emit(event: str, **payload: Any) -> None:
    for fn in list(_listeners):
        try:
            res = fn(event, dict(payload))
            if inspect.isawaitable(res):
                await res  # type: ignore[misc]
        except Exception as e:  # подписчик не должен ломать бота
            logger.warning("support event listener failed for %s: %s", event, e)
