"""
Utility functions
"""
import asyncio
import re
from datetime import datetime, timedelta
from html import escape as _html_escape
from typing import Optional

# Теги, разрешённые Telegram HTML parse_mode
_ALLOWED_TAGS = {"b", "i", "u", "s", "code", "pre", "a", "tg-spoiler"}
_TAG_RE = re.compile(r"<(/?)([a-zA-Z][a-zA-Z0-9-]*)(?:\s[^>]*)?>")
_HREF_RE = re.compile(r"href\s*=\s*(?:\"([^\"]*)\"|'([^']*)')", re.IGNORECASE)

# LLM часто шлёт Markdown, хотя бот отправляет ответы с parse_mode=HTML
_MD_CODE_BLOCK_RE = re.compile(r"```[a-zA-Z0-9_-]*\n?(.*?)```", re.DOTALL)
_MD_BOLD_RE = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_MD_ITALIC_RE = re.compile(r"__(.+?)__", re.DOTALL)
_MD_CODE_RE = re.compile(r"`([^`\n]+)`")
_MD_HEADER_RE = re.compile(r"^#{1,3}\s+(.+)$", re.MULTILINE)


def _markdown_to_html(text: str) -> str:
    """Конвертирует базовый Markdown LLM-ответов в Telegram HTML:
    **bold**, __italic__, `code`, ```блоки```, ### заголовки."""
    if ("`" not in text and "**" not in text and "__" not in text
            and not _MD_HEADER_RE.search(text)):
        return text

    code_blocks: list = []

    def _stash_block(m):
        code_blocks.append(f"<pre>{_html_escape(m.group(1).strip())}</pre>")
        return f"\x00{len(code_blocks) - 1}\x00"

    text = _MD_CODE_BLOCK_RE.sub(_stash_block, text)
    text = _MD_BOLD_RE.sub(r"<b>\1</b>", text)
    text = _MD_ITALIC_RE.sub(r"<i>\1</i>", text)
    text = _MD_CODE_RE.sub(r"<code>\1</code>", text)
    text = _MD_HEADER_RE.sub(r"<b>\1</b>", text)
    for i, block in enumerate(code_blocks):
        text = text.replace(f"\x00{i}\x00", block)
    return text


def sanitize_html(text: str) -> str:
    """Escape HTML not supported by Telegram and balance allowed tags.

    Сначала базовый Markdown конвертируется в HTML-теги (LLM любит ** и ##),
    затем посторонние теги экранируются, разрешённые — балансируются.
    Telegram отклоняет сообщение целиком при незакрытом/висячем теге,
    постороннем теге или атрибутах (кроме href у <a>).
    """
    text = _markdown_to_html(text)
    parts: list = []
    open_tags: list = []
    pos = 0
    for m in _TAG_RE.finditer(text):
        parts.append(text[pos:m.start()].replace("<", "&lt;"))
        pos = m.end()
        is_closing = m.group(1) == "/"
        name = m.group(2).lower()
        if name not in _ALLOWED_TAGS:
            parts.append(m.group(0).replace("<", "&lt;", 1))
        elif is_closing:
            if name in open_tags:
                # закрываем вплоть до парного, сохраняя корректную вложенность
                while open_tags:
                    top = open_tags.pop()
                    parts.append(f"</{top}>")
                    if top == name:
                        break
            else:
                parts.append(m.group(0).replace("<", "&lt;", 1))
        else:
            if name == "a":
                href = _HREF_RE.search(m.group(0))
                if not href:
                    # <a> без валидного href Telegram не принимает
                    parts.append(m.group(0).replace("<", "&lt;", 1))
                    continue
                parts.append(f'<a href="{href.group(1) or href.group(2) or ""}">')
            else:
                parts.append(f"<{name}>")
            open_tags.append(name)
    parts.append(text[pos:].replace("<", "&lt;"))
    while open_tags:
        parts.append(f"</{open_tags.pop()}>")
    return "".join(parts)


def parse_time_input(text: str, bot_timezone: str) -> Optional[datetime]:
    """Parse various Russian time input formats into an aware datetime."""
    from pytz import timezone

    tz = timezone(bot_timezone)
    now = datetime.now(tz)
    text = text.lower().strip()

    # через X минут
    m = re.match(r'через\s+(\d+)\s*мин', text)
    if m:
        return now + timedelta(minutes=int(m.group(1)))

    # через X часов
    m = re.match(r'через\s+(\d+)\s*час', text)
    if m:
        return now + timedelta(hours=int(m.group(1)))

    # через X дней
    m = re.match(r'через\s+(\d+)\s*дн', text)
    if m:
        return now + timedelta(days=int(m.group(1)))

    # завтра в HH:MM
    m = re.match(r'завтра\s+в\s*(\d{1,2}):(\d{2})', text)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        return (now + timedelta(days=1)).replace(hour=h, minute=mi, second=0, microsecond=0)

    # сегодня в HH:MM
    m = re.match(r'сегодня\s+в\s*(\d{1,2}):(\d{2})', text)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        return now.replace(hour=h, minute=mi, second=0, microsecond=0)

    # YYYY-MM-DD HH:MM
    m = re.match(r'(\d{4})-(\d{2})-(\d{2})\s+(\d{1,2}):(\d{2})', text)
    if m:
        year, month, day, h, mi = map(int, m.groups())
        return tz.localize(datetime(year, month, day, h, mi, 0))

    # DD.MM.YYYY HH:MM
    m = re.match(r'(\d{1,2})\.(\d{1,2})\.(\d{4})\s+(\d{1,2}):(\d{2})', text)
    if m:
        day, month, year, h, mi = map(int, m.groups())
        return tz.localize(datetime(year, month, day, h, mi, 0))

    # HH:MM — сегодня или завтра если уже прошло
    m = re.match(r'(\d{1,2}):(\d{2})', text)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        target = now.replace(hour=h, minute=mi, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return target

    return None


def escape_markdown(text: str) -> str:
    """Escape Markdown special characters"""
    chars = ['_', '*', '[', ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']
    for char in chars:
        text = text.replace(char, f'\\{char}')
    return text


def truncate_text(text: str, max_length: int = 4000) -> str:
    """Truncate text to max length with ellipsis"""
    if len(text) <= max_length:
        return text
    return text[:max_length-3] + "..."


def format_datetime(dt: datetime) -> str:
    """Format datetime for display"""
    return dt.strftime("%Y-%m-%d %H:%M")


def parse_code_from_message(text: str) -> Optional[str]:
    """Extract code from markdown code blocks"""
    # Match ```python ... ``` or ``` ... ```
    pattern = r'```(?:python)?\n(.*?)\n```'
    match = re.search(pattern, text, re.DOTALL)
    if match:
        return match.group(1).strip()
    return None


def is_valid_skill_name(name: str) -> bool:
    """Check if skill name is valid"""
    return bool(re.fullmatch(r'[a-zA-Z_][a-zA-Z0-9_]*', name))


def format_skill_info(skill_info) -> str:
    """Format skill info for display"""
    text = f"📦 <b>{skill_info.name}</b>\n"
    text += f"📝 {skill_info.description}\n"
    text += f"🏷 Категория: {skill_info.category}\n"
    text += f"📂 Источник: {skill_info.source}\n"
    text += f"🔢 Версия: {skill_info.version}\n"
    text += f"👤 Автор: {skill_info.author}\n"

    if getattr(skill_info, "kind", "py") == "skillmd":
        text += "🧾 Тип: инструкционный (SKILL.md, исполняет LLM)\n"

    if skill_info.commands:
        text += f"⌨️ Команды: {', '.join(skill_info.commands)}\n"

    status = "✅ Активен" if skill_info.is_active else "❌ Неактивен"
    text += f"📊 Статус: {status}\n"

    return text


def format_memory(memory) -> str:
    """Format memory for display (HTML parse_mode)."""
    emoji_map = {
        'note': '📝',
        'trip': '✈️',
        'budget': '💰',
        'plan': '📅',
        'dialog': '💬'
    }

    emoji = emoji_map.get(memory.memory_type, '📄')
    text = f"{emoji} <b>{memory.memory_type.upper()}</b>\n"
    text += f"🕐 {format_datetime(memory.created_at)}\n\n"
    content = sanitize_html(memory.content[:500])
    text += content

    if len(memory.content) > 500:
        text += "..."

    return text


# ---------------------------------------------------------------------------
# Индикатор «печатает…» и контекст текущего времени
# ---------------------------------------------------------------------------

async def deliver_via_message(processing_msg, text: str, reply_markup=None) -> None:
    """Заменить статусное сообщение итоговым текстом (учитывая лимит 4096).

    reply_markup прикрепляется к последнему сообщению.
    """
    text = sanitize_html(text)
    if len(text) > 4000:
        await processing_msg.delete()
        chunks = [text[i:i+4000] for i in range(0, len(text), 4000)]
        for i, chunk in enumerate(chunks):
            body = chunk if i == 0 else f"<i>(продолжение {i+1}/{len(chunks)})</i>\n\n{chunk}"
            if i == len(chunks) - 1 and reply_markup is not None:
                await processing_msg.answer(body, reply_markup=reply_markup)
            else:
                await processing_msg.answer(body)
    elif reply_markup is not None:
        await processing_msg.edit_text(text, reply_markup=reply_markup)
    else:
        await processing_msg.edit_text(text)


async def _typing_loop(bot, chat_id: int, interval: float = 4.0, max_seconds: float = 180.0):
    # Telegram гасит chat action через ~5 секунд — для долгих ответов
    # (фоллбэк LLM, веб-поиск, планирование) индикатор нужно продлевать.
    # max_seconds — страховка от утечки задачи, если cancel по какой-то
    # причине не вызвали.
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max_seconds
    while loop.time() < deadline:
        try:
            await bot.send_chat_action(chat_id, "typing")
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        await asyncio.sleep(interval)


def start_typing(bot, chat_id: int, interval: float = 4.0):
    """Запустить фоновое продление «печатает…». Вернуть задачу для stop_typing."""
    return asyncio.create_task(_typing_loop(bot, chat_id, interval))


def stop_typing(task) -> None:
    """Остановить продление индикатора (безопасно для уже завершённой задачи)."""
    if task is not None:
        task.cancel()


_WEEKDAYS_RU = (
    "понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье",
)


def _resolve_zone(tz_name: str):
    """ZoneInfo (stdlib) с fallback на pytz — на случай контейнера без системной tzdata."""
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(tz_name)
    except Exception:
        from pytz import timezone

        return timezone(tz_name)


def current_datetime_prompt(tz_name: Optional[str] = None) -> str:
    """System-строка с текущими датой/временем — у LLM нет собственных часов."""
    from app.core.config import settings

    tz_name = tz_name or settings.BOT_TIMEZONE
    now = datetime.now(_resolve_zone(tz_name))
    return (
        f"Текущие дата и время: {_WEEKDAYS_RU[now.weekday()]}, "
        f"{now.strftime('%d.%m.%Y')}, {now.strftime('%H:%M')} ({tz_name})."
    )
