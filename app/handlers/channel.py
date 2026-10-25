"""
Channel post handler — обрабатывает сообщения из Telegram-каналов.
Бот должен быть добавлен в канал как участник (достаточно read-only).
Список отслеживаемых каналов: settings.KB_CHANNEL_IDS
"""
import re
import logging

from aiogram import Router
from aiogram.types import Message

from app.core.config import settings

logger = logging.getLogger(__name__)

router = Router()

_URL_RE = re.compile(r'https?://[^\s\]\)>"\']+')


def _extract_url(text: str | None) -> str | None:
    if not text:
        return None
    m = _URL_RE.search(text)
    return m.group(0).rstrip(".,;)") if m else None


def _extract_url_from_entities(message: Message) -> str | None:
    """Ищет URL в entities сообщения (text_link, url)."""
    entities = message.entities or message.caption_entities or []
    for ent in entities:
        if ent.type == "text_link" and ent.url:
            return ent.url
        if ent.type == "url":
            text = message.text or message.caption or ""
            return text[ent.offset: ent.offset + ent.length]
    return None


@router.channel_post()
async def handle_channel_post(message: Message):
    """Обрабатывает новые посты в отслеживаемых каналах."""
    chat_id = message.chat.id

    # Проверяем что канал в белом списке
    if settings.KB_CHANNEL_IDS and chat_id not in settings.KB_CHANNEL_IDS:
        return

    # Извлекаем URL: сначала из entities (надёжнее), потом regex из текста
    text = message.text or message.caption or ""
    url = _extract_url_from_entities(message) or _extract_url(text)

    if not url:
        logger.debug(f"Channel post from {chat_id}: no URL, skipping")
        return

    # Комментарий = весь текст без URL
    comment = _URL_RE.sub("", text).strip()

    logger.info(f"Channel post from {chat_id}: URL={url}")

    # Добавляем в базу знаний от имени первого admin
    if not settings.ADMIN_IDS:
        logger.warning("KB: KB_CHANNEL_IDS set but ADMIN_IDS is empty — cannot assign user_id")
        return

    owner_id = settings.ADMIN_IDS[0]

    try:
        from app.core.skills_loader import skill_loader
        kb_skill = skill_loader.get_skill("knowledge_base")
        if not kb_skill:
            logger.warning("knowledge_base skill not loaded")
            return

        result = await kb_skill({
            "user_id": owner_id,
            "message_text": text,
            "args": {"url": url, "comment": comment},
        })
        logger.info(f"KB add result for {url}: {result[:80]}")

        # Уведомляем владельца в личку
        try:
            await message.bot.send_message(
                chat_id=owner_id,
                text=result,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except Exception as e:
            logger.warning(f"Could not notify owner {owner_id}: {e}")

    except Exception as e:
        logger.error(f"KB channel_post handler error: {e}", exc_info=True)
