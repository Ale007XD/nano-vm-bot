"""Напоминания из свободного текста и голоса для любого допущенного пользователя (вне пространств).

Раньше «оповести через 5 минут» уходило обычному агенту, у которого нет инструмента напоминаний: модель
выдумывала подтверждение, ничего не ставилось, а обещание оставалось в истории диалога и повторялось.
Теперь сообщение с явной просьбой напомнить разбирается тем же кодом, что и в пространствах (даты, границы,
Decimal-проверки — на стороне кода), и становится личным напоминанием, которое доставит общий исполнитель.
Не напоминание — возвращаем None, сообщение идёт агенту как раньше.
"""
from __future__ import annotations

import html
import logging
import re
from datetime import datetime
from typing import Awaitable, Callable, Optional

import pytz

from app.core.config import settings
from app.spaces import intake, personal, reminders

logger = logging.getLogger(__name__)

# Только однозначные слова: «через 5 минут буду» в обычной переписке напоминанием не считается
TRIGGER = re.compile(r"напоми?н|оповест|оповещени|уведоми|уведомл|будильник|таймер", re.I)
URL = re.compile(r"https?://|t\.me/|www\.", re.I)
MAX_LEN = 300  # длинный текст — статья или пост для пересказа, а не просьба
HELP = "Пример: «напомни завтра в 18:00 позвонить маме» или команда /remind."


def looks_like_reminder(text: Optional[str]) -> bool:
    t = (text or "").strip()
    return bool(t) and len(t) <= MAX_LEN and not URL.search(t) and bool(TRIGGER.search(t))


async def handle(
    text: str, user_id: int, llm: Callable[[str, str], Awaitable[str]], *, in_group: bool = False,
    now: Optional[datetime] = None, session_maker=None,
) -> Optional[str]:
    """HTML-ответ пользователю или None, если это не просьба о напоминании (пусть отвечает агент)."""
    if not looks_like_reminder(text):
        return None
    tz = settings.BOT_TIMEZONE
    now_local = (now or datetime.now(pytz.utc)).astimezone(pytz.timezone(tz))
    ctx = intake.Context(now_local=now_local, tz=tz, currency="RUB", kind=personal.PERSONAL_KIND, members={},
                         categories=[], only=("reminder",))
    try:
        parsed = intake.parse_response(await llm(intake.build_system_prompt(ctx), text[:intake.MAX_TEXT_IN]), ctx)
    except intake.IntakeError as e:
        logger.warning(f"quick reminder: bad model answer: {e}")
        return "Не смог разобрать напоминание. " + HELP
    except Exception as e:  # noqa: BLE001 — лимиты, сеть: честно говорим, что не поставлено
        logger.error(f"quick reminder failed: {e}")
        return "⚠️ Не получилось поставить напоминание (модель недоступна). Попробуйте позже или команда /remind."
    if not parsed.proposals:
        if parsed.notes:
            return "\n".join("⚠️ " + html.escape(n) for n in parsed.notes) + "\n\n" + HELP
        return None
    lines = []
    for p in parsed.proposals:
        try:
            r = await personal.add_reminder(
                user_id, p.text, tz=tz, cron=p.cron,
                run_at=datetime.fromisoformat(p.run_at) if p.run_at else None, session_maker=session_maker)
        except reminders.ReminderError as e:
            lines.append(f"❌ «{html.escape(p.text)}»: {html.escape(str(e))}")
            continue
        lines.append(f"⏰ Напомню: <b>{html.escape(p.text)}</b> — {html.escape(p.when)}\n"
                     f"   Отменить: <code>/cancel_task r{r.id}</code>")
    if in_group:
        lines.append("Напоминание придёт вам в личные сообщения.")
    return "\n".join(lines)
