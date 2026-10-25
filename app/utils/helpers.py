"""
Utility functions
"""
import re
from datetime import datetime, timedelta
from typing import Optional

# Теги, разрешённые Telegram HTML parse_mode
_ALLOWED_TAGS = {"b", "i", "u", "s", "code", "pre", "a", "tg-spoiler"}
_TAG_RE = re.compile(r"<(/?)([a-zA-Z][a-zA-Z0-9-]*)(?:\s[^>]*)?>")
_HREF_RE = re.compile(r"href\s*=\s*(?:\"([^\"]*)\"|'([^']*)')", re.IGNORECASE)


def sanitize_html(text: str) -> str:
    """Escape HTML not supported by Telegram and balance allowed tags.

    Telegram отклоняет сообщение целиком при незакрытом/висячем теге,
    постороннем теге или атрибутах (кроме href у <a>).
    """
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
    return bool(re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', name))


def format_skill_info(skill_info) -> str:
    """Format skill info for display"""
    text = f"📦 <b>{skill_info.name}</b>\n"
    text += f"📝 {skill_info.description}\n"
    text += f"🏷 Категория: {skill_info.category}\n"
    text += f"📂 Источник: {skill_info.source}\n"
    text += f"🔢 Версия: {skill_info.version}\n"
    text += f"👤 Автор: {skill_info.author}\n"
    
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
