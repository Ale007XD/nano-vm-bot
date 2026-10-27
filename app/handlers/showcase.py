"""
Showcase handlers — demo bot for clients, answers strictly from corpus.
"""
import logging
import os

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import Document, Message

from app.core.access import user_allowed
from app.core.config import settings
from app.core.dialog import (
    KIND_ANSWER,
    KIND_ERROR,
    KIND_NOT_FOUND,
    KIND_QUESTION,
    ROLE_BOT,
    ROLE_CLIENT,
)
from app.core.dialog import history as dialog_history
from app.core.llm_client_v2 import Message as LLMMessage
from app.core.llm_client_v2 import llm_client
from app.core.memory import vector_memory
from app.core.usage import flush_usage, start_usage_session
from app.handlers.operator import call_operator_keyboard, operator_enabled
from app.utils.helpers import (
    current_datetime_prompt,
    deliver_via_message,
    sanitize_html,
    start_typing,
    stop_typing,
)

logger = logging.getLogger(__name__)

router = Router()


def _source_line(meta: dict) -> str:
    name = meta.get("source_name", "")
    url = meta.get("source_url", "")
    if name:
        return f"• {name}" + (f" ({url})" if url else "")
    if url:
        return f"• {url}"
    return "• неизвестный источник"


@router.message(Command("start"))
async def cmd_start(message: Message):
    if not await user_allowed(message.from_user.id):
        await message.answer(
            "⛔ Доступ запрещен.\n\n"
            "Этот бот — витрина службы поддержки. Обратитесь к администратору, "
            "если вы считаете, что у вас должен быть доступ."
        )
        return

    await message.answer(
        "👋 <b>Добро пожаловать в витрину службы поддержки!</b>\n\n"
        "Задавайте вопрос по теме нашей базы знаний, и я отвечу по документам, "
        "которые загружены в систему.\n\n"
        "Напишите свой вопрос одним сообщением.\n\n"
        "Справка: /help"
    )


@router.message(Command("help"))
async def cmd_help(message: Message):
    if not await user_allowed(message.from_user.id):
        await message.answer("⛔ Доступ запрещен.")
        return

    await message.answer(
        "📘 <b>Как пользоваться витриной:</b>\n\n"
        "1. Пришлите вопрос своими словами.\n"
        "2. Я найду релевантные фрагменты в базе знаний и отвечу по ним.\n"
        "3. В конце ответа я приведу источники.\n\n"
        "Если ответа нет в базе — я скажу об этом честно, не придумывая ничего. "
        "Нужен живой человек — /operator.\n\n"
        "Примеры вопросов:\n"
        "• «Как восстановить доступ к личному кабинету?»\n"
        "• «Какие документы нужны для подачи заявки?»\n"
        "• «Сколько стоит услуга X?»\n\n"
        "Администратору: /corpus_stats, /corpus_upload"
    )


@router.message(Command("corpus_stats"))
async def cmd_corpus_stats(message: Message):
    from app.handlers.commands import is_admin

    if not is_admin(message.from_user.id):
        await message.answer("⛔ Только для администраторов.")
        return

    stats = await vector_memory.get_corpus_stats()
    await message.answer(
        f"📊 <b>Корпус базы знаний:</b>\n"
        f"• чанков в индексе: <b>{stats.get('chunks', 0)}</b>\n"
    )


@router.message(Command("corpus_upload"))
async def cmd_corpus_upload(message: Message):
    from app.handlers.commands import is_admin

    if not is_admin(message.from_user.id):
        await message.answer("⛔ Только для администраторов.")
        return

    await message.answer(
        "📎 Пришлите документ (.md или .txt) — я добавлю его в базу знаний "
        "витрины. Документ будет разбит на чанки и проиндексирован для поиска."
    )


@router.message(F.document)
async def handle_document(message: Message):
    """Admins upload .md/.txt documents into the showcase corpus."""
    from app.handlers.commands import is_admin

    if not is_admin(message.from_user.id):
        await message.answer("⛔ Только для администраторов.")
        return

    doc: Document = message.document
    file_name = (doc.file_name or "").lower()

    if not file_name.endswith((".md", ".txt")):
        await message.answer(
            "❌ Поддерживаются только файлы .md и .txt."
        )
        return

    if doc.file_size and doc.file_size > settings.MAX_SKILL_FILE_SIZE:
        await message.answer("❌ Файл слишком большой.")
        return

    processing_msg = await message.answer("📥 Загружаю документ в базу знаний…")
    temp_path = f"/tmp/showcase_corpus_{message.from_user.id}_{doc.file_id}"

    try:
        file = await message.bot.get_file(doc.file_id)
        await message.bot.download_file(file.file_path, temp_path)

        with open(temp_path, encoding="utf-8") as fh:
            content = fh.read()

        source_id = f"upload_{message.from_user.id}_{doc.file_id}"
        source_name = os.path.basename(doc.file_name or "документ")

        await vector_memory.add_corpus_document(
            source_id=source_id,
            source_name=source_name,
            content=content,
            source_url=None,
        )

        await processing_msg.edit_text(
            f"✅ Документ <b>{sanitize_html(source_name)}</b> добавлен в базу знаний "
            f"(источник: <code>{source_id}</code>)."
        )

    except Exception as e:
        logger.error(f"Error uploading corpus document: {e}")
        await processing_msg.edit_text(
            "❌ Ошибка загрузки документа в базу знаний."
        )
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


@router.message()
async def handle_message(message: Message):
    """Main entry: answer strictly from corpus."""
    if not await user_allowed(message.from_user.id):
        await message.answer("⛔ Доступ запрещен.")
        return

    text = (message.text or "").strip()
    if not text:
        await message.answer("Пришлите вопрос текстом.")
        return

    dialog_history.add(message.from_user.id, ROLE_CLIENT, text, KIND_QUESTION)
    escalate_kb = call_operator_keyboard() if operator_enabled() else None
    processing_msg = await message.answer("🔍 Ищу в базе знаний…")
    typing_task = start_typing(message.bot, message.chat.id)
    start_usage_session(message.from_user.id)
    try:
        docs = await vector_memory.search_corpus(text)

        if not docs:
            dialog_history.add(
                message.from_user.id, ROLE_BOT, "Ответа в базе знаний не нашлось.", KIND_NOT_FOUND
            )
            await processing_msg.edit_text(
                "Я не нашёл ответа на ваш вопрос в базе знаний. "
                "Попробуйте переформулировать вопрос или обратитесь в поддержку.",
                reply_markup=escalate_kb,
            )
            return

        # Build context from top docs, dedupe by source
        seen_sources = set()
        context_lines = []
        sources = []

        for doc in docs[: settings.CORPUS_TOP_K]:
            meta = doc.get("metadata", {}) or {}
            sid = meta.get("source_id", "")
            if sid and sid not in seen_sources:
                seen_sources.add(sid)
                sources.append(_source_line(meta))
            context_lines.append(
                f"[Документ {meta.get('source_name', 'без названия')}, чанк "
                f"{meta.get('chunk_index', 0)}]\n{doc['content']}"
            )

        context = "\n\n".join(context_lines)

        system_prompt = (
            settings.SHOWCASE_SYSTEM_PROMPT
            + "\n\n"
            + "<b>БАЗА ЗНАНИЙ:</b>\n" + context
        )

        messages = [
            LLMMessage(role="system", content=system_prompt),
            LLMMessage(role="system", content=current_datetime_prompt()),
            LLMMessage(role="user", content=text),
        ]

        response = await llm_client.chat_with_fallback(
            messages,
            model=settings.SHOWCASE_MODEL,
            reasoning_effort="medium",
        )

        answer = response.content
        if sources:
            answer += "\n\n<b>Источники:</b>\n" + "\n".join(sources)

        dialog_history.add(
            message.from_user.id, ROLE_BOT, response.content, KIND_ANSWER, sources=sources
        )
        await deliver_via_message(processing_msg, answer)

    except Exception as e:
        logger.error(f"Error in showcase handler: {e}")
        dialog_history.add(message.from_user.id, ROLE_BOT, "Бот не смог ответить (ошибка).", KIND_ERROR)
        await processing_msg.edit_text(
            "⚠️ Ошибка при обработке вопроса. Попробуйте позже.",
            reply_markup=escalate_kb,
        )
    finally:
        stop_typing(typing_task)
        await flush_usage()
