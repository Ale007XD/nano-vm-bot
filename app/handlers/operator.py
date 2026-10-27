"""
Эскалация на оператора для бота-витрины.

Клиент:  /operator или кнопка «Позвать оператора» (под ответом «не нашёл»);
         /endchat — вернуться к боту.
Оператор: пишет в теме клиента в группе поддержки (OPERATOR_CHAT_ID);
         /close в теме — завершить обращение.

Роутер подключается в bot_showcase.py ПЕРЕД основным роутером витрины,
чтобы сообщения клиента в активной сессии не уходили в LLM.
"""
import html
import logging
from typing import Optional

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.core import operator as store
from app.core.access import user_allowed
from app.core.config import settings

logger = logging.getLogger(__name__)

router = Router()

CALL_CALLBACK = "op:call"
_limiter = store.RateLimiter(max_events=30, window=60.0)
_warned_flood: set = set()

# Служебные сообщения (создание темы и т.п.) копировать нельзя
_COPYABLE = {
    "text", "photo", "document", "voice", "video", "audio", "video_note",
    "animation", "sticker", "location", "contact",
}
_CLIENT_OWN_COMMANDS = {"start", "help", "operator", "endchat"}


def operator_chat_id() -> int:
    val = getattr(settings, "OPERATOR_CHAT_ID", 0)
    return val if isinstance(val, int) and not isinstance(val, bool) else 0


def operator_enabled() -> bool:
    return operator_chat_id() != 0


def call_operator_keyboard() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="👤 Позвать оператора", callback_data=CALL_CALLBACK)
    return b.as_markup()


def _command_name(text: Optional[str]) -> str:
    if not text or not text.startswith("/"):
        return ""
    return text[1:].split()[0].split("@")[0].lower()


# ----------------------------- фильтры ----------------------------------- #
async def in_operator_topic(message: Message) -> bool:
    return (
        operator_enabled()
        and message.chat.id == operator_chat_id()
        and message.message_thread_id is not None
        and message.from_user is not None
        and not message.from_user.is_bot
    )


async def client_in_session(message: Message) -> bool:
    if message.chat.type != "private" or message.from_user is None:
        return False
    if _command_name(message.text) in _CLIENT_OWN_COMMANDS:
        return False
    if not operator_enabled():
        return False
    return await store.get_open_by_client(message.from_user.id) is not None


# ----------------------------- вспомогательное --------------------------- #
def _display_name(user) -> str:
    name = " ".join(p for p in (user.first_name, user.last_name) if p) or "Клиент"
    return name[:60]


async def _create_topic_and_card(bot, user, question: str) -> int:
    """Создать тему в группе поддержки, опубликовать карточку. Возвращает thread_id."""
    name = f"{_display_name(user)} · {user.id}"[:120]
    topic = await bot.create_forum_topic(chat_id=operator_chat_id(), name=name)
    thread_id = topic.message_thread_id

    uname = f"@{html.escape(user.username)}" if user.username else "—"
    card = (
        "🆘 <b>Новое обращение</b>\n"
        f"Клиент: <a href=\"tg://user?id={user.id}\">{html.escape(_display_name(user))}</a> ({uname})\n"
        f"ID: <code>{user.id}</code>\n"
    )
    if question:
        card += f"Вопрос боту: <i>{html.escape(question[:300])}</i>\n"
    card += "\nПишите ответ прямо в этой теме — он уйдёт клиенту. <code>/close</code> — завершить."
    await bot.send_message(operator_chat_id(), card, message_thread_id=thread_id)
    return thread_id


async def _forward_to_topic(bot, session_row, message: Message) -> bool:
    """Скопировать сообщение клиента в его тему. Пересоздаёт тему, если её удалили."""
    try:
        await bot.copy_message(
            chat_id=operator_chat_id(),
            from_chat_id=message.chat.id,
            message_id=message.message_id,
            message_thread_id=session_row.thread_id,
        )
        return True
    except TelegramBadRequest as e:
        if "thread" not in str(e).lower():
            logger.warning("operator relay failed (client %s): %s", message.from_user.id, e)
            return False
    # тему удалили вручную — создаём новую и повторяем
    try:
        thread_id = await _create_topic_and_card(bot, message.from_user, "")
        await store.set_thread(message.from_user.id, thread_id)
        await bot.copy_message(
            chat_id=operator_chat_id(),
            from_chat_id=message.chat.id,
            message_id=message.message_id,
            message_thread_id=thread_id,
        )
        return True
    except Exception as e:
        logger.error("operator topic recreate failed (client %s): %s", message.from_user.id, e)
        return False


async def _end_session(bot, client_id: int, thread_id: Optional[int], notify_client: str) -> None:
    await store.close_session(client_id)
    if thread_id is not None and operator_enabled():
        try:
            await bot.close_forum_topic(chat_id=operator_chat_id(), message_thread_id=thread_id)
        except Exception as e:  # тема могла быть удалена — не критично
            logger.info("close_forum_topic failed: %s", e)
    if notify_client:
        try:
            await bot.send_message(client_id, notify_client)
        except (TelegramForbiddenError, TelegramBadRequest):
            pass


async def start_escalation(bot, user, chat_id: int, reply) -> None:
    """Общая логика /operator и кнопки. reply(text) — как ответить клиенту."""
    if not await user_allowed(user.id):
        await reply("⛔ Доступ запрещен.")
        return
    if not operator_enabled():
        await reply("Оператор сейчас недоступен. Попробуйте задать вопрос боту или обратитесь позже.")
        return

    existing = await store.get_open_by_client(user.id)
    if existing:
        await reply("Вы уже на связи с оператором — просто пишите сюда. /endchat — вернуться к боту.")
        return

    try:
        thread_id = await _create_topic_and_card(bot, user, store.last_question(user.id))
    except Exception as e:
        logger.error("operator topic creation failed: %s", e)
        await reply("⚠️ Не удалось связаться с оператором. Попробуйте позже.")
        return

    await store.open_session(user.id, thread_id, _display_name(user))
    await reply(
        "👤 <b>Передаю вопрос оператору.</b>\n\n"
        "Пишите сюда всё, что нужно (текст, фото, файлы) — я перешлю в службу поддержки, "
        "ответ придёт в этот чат. Сообщения видят сотрудники поддержки.\n\n"
        "/endchat — вернуться к боту."
    )


# ----------------------------- клиент ------------------------------------ #
@router.message(Command("operator"), F.chat.type == "private")
async def cmd_operator(message: Message):
    await start_escalation(message.bot, message.from_user, message.chat.id, message.answer)


@router.callback_query(F.data == CALL_CALLBACK)
async def cb_call_operator(callback: CallbackQuery):
    await callback.answer()
    if callback.message is None:
        return
    await start_escalation(
        callback.bot, callback.from_user, callback.message.chat.id, callback.message.answer
    )


@router.message(Command("endchat"), F.chat.type == "private")
async def cmd_endchat(message: Message):
    row = await store.get_open_by_client(message.from_user.id)
    if not row:
        await message.answer("Активного обращения к оператору нет.")
        return
    await _end_session(
        message.bot, message.from_user.id, row.thread_id, "✅ Вернулись к боту. Задавайте вопрос."
    )
    if operator_enabled():
        try:
            await message.bot.send_message(
                operator_chat_id(), "ℹ️ Клиент завершил обращение (/endchat).",
                message_thread_id=row.thread_id,
            )
        except Exception:
            pass


# ----------------------------- оператор ---------------------------------- #
@router.message(Command("close"), in_operator_topic)
async def cmd_close(message: Message):
    row = await store.get_open_by_thread(message.message_thread_id)
    if not row:
        await message.reply("Эта тема не связана с открытым обращением.")
        return
    await _end_session(
        message.bot, row.client_id, row.thread_id,
        "✅ Обращение закрыто оператором. Если остались вопросы — спросите бота или нажмите /operator.",
    )
    await message.reply("✅ Обращение закрыто.")


@router.message(in_operator_topic)
async def operator_reply(message: Message):
    """Сообщение оператора в теме → клиенту."""
    if message.content_type not in _COPYABLE:
        return
    row = await store.get_open_by_thread(message.message_thread_id)
    if not row:
        return
    try:
        await message.bot.copy_message(
            chat_id=row.client_id,
            from_chat_id=message.chat.id,
            message_id=message.message_id,
        )
    except TelegramForbiddenError:
        await store.close_session(row.client_id)
        await message.reply("⚠️ Клиент заблокировал бота — обращение закрыто.")
    except TelegramBadRequest as e:
        logger.warning("operator reply not delivered to %s: %s", row.client_id, e)
        await message.reply("⚠️ Не удалось доставить сообщение клиенту.")


# ----------------------------- клиент → оператор ------------------------- #
@router.message(client_in_session)
async def client_message(message: Message):
    if message.content_type not in _COPYABLE:
        return
    uid = message.from_user.id
    if not _limiter.allow(uid):
        if uid not in _warned_flood:
            _warned_flood.add(uid)
            await message.answer("⏳ Слишком много сообщений подряд — подождите минуту.")
        return
    _warned_flood.discard(uid)

    row = await store.get_open_by_client(uid)
    if not row:
        return
    if not await _forward_to_topic(message.bot, row, message):
        await message.answer("⚠️ Не удалось передать сообщение оператору. Попробуйте ещё раз.")
