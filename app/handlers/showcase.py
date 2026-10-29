"""
Showcase handlers — demo bot for clients, answers strictly from corpus.
"""
import html
import logging
import os
import re

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Document, Message

from app.core import showcase_access
from app.core.access import check_showcase_limit, is_admin, showcase_allowed
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
from app.core.showcases import ACCESS_INVITE
from app.core.showcases import current as current_showcase
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


_INVITE_RE = re.compile(r"^inv_([A-Za-z0-9_-]{6,32})$")
_INVITE_ERRORS = {
    showcase_access.RESULT_INVALID: "Приглашение недействительно.",
    showcase_access.RESULT_REVOKED: "Приглашение отозвано.",
    showcase_access.RESULT_EXPIRED: "Срок приглашения истёк.",
    showcase_access.RESULT_EXHAUSTED: "Приглашение уже использовано.",
}


def _invite_code(args):
    m = _INVITE_RE.match((args or "").strip())
    return m.group(1) if m else None


def _denied_text(sc) -> str:
    if sc.access == ACCESS_INVITE:
        return (
            "🔒 <b>Доступ по приглашению.</b>\n\n"
            "Откройте ссылку-приглашение, которую вам прислал администратор."
        )
    return "⛔ Доступ запрещен."


async def _is_banned(user_id: int) -> bool:
    try:
        from app.core.database import is_user_banned

        return bool(await is_user_banned(user_id))
    except Exception as e:
        logger.warning(f"Ban check failed for {user_id}: {e}")
        return False


@router.message(Command("start"))
async def cmd_start(message: Message, command: CommandObject = None):
    sc = current_showcase()
    uid = message.from_user.id
    code = _invite_code(command.args if command else None)

    allowed = await showcase_allowed(sc, uid)
    joined = False
    if not allowed and code and not await _is_banned(uid):
        result = await showcase_access.redeem_invite(sc.slug, code, uid)
        if result not in (showcase_access.RESULT_OK, showcase_access.RESULT_ALREADY):
            await message.answer(
                _INVITE_ERRORS.get(result, "Приглашение недействительно.")
                + "\n\nЗапросите новую ссылку у администратора."
            )
            return
        joined = result == showcase_access.RESULT_OK
        allowed = await showcase_allowed(sc, uid)

    if not allowed:
        await message.answer(_denied_text(sc))
        return

    if sc.welcome:
        text = sc.welcome
    else:
        title = f"<b>{html.escape(sc.name)}</b>\n\n" if sc.name else ""
        text = (
            "👋 <b>Добро пожаловать!</b>\n\n" + title +
            "Задавайте вопрос по теме нашей базы знаний, и я отвечу по документам, "
            "которые загружены в систему.\n\n"
            "Напишите свой вопрос одним сообщением.\n\n"
            "Справка: /help"
        )
    if joined:
        text = "✅ Доступ открыт.\n\n" + text
    await message.answer(text)


@router.message(Command("help"))
async def cmd_help(message: Message):
    sc = current_showcase()
    if not await showcase_allowed(sc, message.from_user.id):
        await message.answer(_denied_text(sc))
        return

    text = (
        "📘 <b>Как пользоваться витриной:</b>\n\n"
        "1. Пришлите вопрос своими словами.\n"
        "2. Я найду релевантные фрагменты в базе знаний и отвечу по ним.\n"
        "3. В конце ответа я приведу источники.\n\n"
        "Если ответа нет в базе — я скажу об этом честно, не придумывая ничего. "
        "Нужен живой человек — /operator.\n\n"
        "Примеры вопросов:\n"
        "• «Как восстановить доступ к личному кабинету?»\n"
        "• «Какие документы нужны для подачи заявки?»\n"
        "• «Сколько стоит услуга X?»"
    )
    if is_admin(message.from_user.id):
        text += (
            "\n\nАдминистратору: /corpus_stats, /corpus_upload, "
            "/invite, /invites, /revoke_invite, /allow, /deny, /members"
        )
    await message.answer(text)


@router.message(Command("corpus_stats"))
async def cmd_corpus_stats(message: Message):
    from app.handlers.commands import is_admin

    if not is_admin(message.from_user.id):
        await message.answer("⛔ Только для администраторов.")
        return

    sc = current_showcase()
    stats = await vector_memory.get_corpus_stats(corpus=sc.collection)
    await message.answer(
        f"📊 <b>Корпус базы знаний{' «' + html.escape(sc.slug) + '»' if sc.slug else ''}:</b>\n"
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
            corpus=current_showcase().collection,
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


# ----------------------------- доступ: команды администратора ------------ #
def _admin_guard(message: Message) -> bool:
    return is_admin(message.from_user.id)


def _int_args(command, defaults):
    """Целые аргументы команды; None — если что-то не число."""
    parts = (command.args or "").split() if command else []
    values = list(defaults)
    for i, part in enumerate(parts[: len(defaults)]):
        try:
            values[i] = int(part)
        except ValueError:
            return None
    return values


@router.message(Command("invite"), F.chat.type == "private")
async def cmd_invite(message: Message, command: CommandObject = None):
    """/invite [использований=1 (0 — без лимита)] [дней=7 (0 — без срока)]"""
    if not _admin_guard(message):
        await message.answer("⛔ Только для администраторов.")
        return
    args = _int_args(command, (1, 7))
    if args is None or args[0] < 0 or args[1] < 0:
        await message.answer(
            "Формат: <code>/invite [использований] [дней]</code>\n"
            "Например, <code>/invite 5 3</code> — до 5 человек, 3 дня. "
            "0 — без ограничения."
        )
        return
    uses, days = args
    sc = current_showcase()
    code = await showcase_access.create_invite(
        sc.slug, message.from_user.id, max_uses=uses, days=days or None
    )
    me = await message.bot.me()
    link = f"https://t.me/{me.username}?start=inv_{code}"
    await message.answer(
        "🔗 <b>Приглашение</b>\n"
        f"{link}\n\n"
        f"Использований: {uses or 'без ограничения'}, "
        f"срок: {str(days) + ' дн.' if days else 'без срока'}.\n"
        f"Отозвать: <code>/revoke_invite {code}</code>"
    )


@router.message(Command("invites"), F.chat.type == "private")
async def cmd_invites(message: Message):
    if not _admin_guard(message):
        await message.answer("⛔ Только для администраторов.")
        return
    rows = await showcase_access.list_invites(current_showcase().slug)
    if not rows:
        await message.answer("Действующих приглашений нет. Создать: /invite")
        return
    lines = []
    for r in rows:
        limit = r.max_uses or "∞"
        until = r.expires_at.strftime("%d.%m %H:%M UTC") if r.expires_at else "без срока"
        lines.append(f"• <code>{r.code}</code> — {r.uses}/{limit}, до {until}")
    await message.answer("🔗 <b>Действующие приглашения:</b>\n" + "\n".join(lines))


@router.message(Command("revoke_invite"), F.chat.type == "private")
async def cmd_revoke_invite(message: Message, command: CommandObject = None):
    if not _admin_guard(message):
        await message.answer("⛔ Только для администраторов.")
        return
    code = (command.args or "").strip() if command else ""
    if not code:
        await message.answer("Формат: <code>/revoke_invite КОД</code>")
        return
    ok = await showcase_access.revoke_invite(current_showcase().slug, code)
    await message.answer("✅ Приглашение отозвано." if ok else "Такого приглашения нет в этой витрине.")


@router.message(Command("allow"), F.chat.type == "private")
async def cmd_allow(message: Message, command: CommandObject = None):
    """/allow <id> — выдать доступ по ID (пользователь должен был написать боту)."""
    if not _admin_guard(message):
        await message.answer("⛔ Только для администраторов.")
        return
    args = _int_args(command, (0,))
    if not args or args[0] <= 0:
        await message.answer("Формат: <code>/allow ID</code>")
        return
    created = await showcase_access.add_member(
        current_showcase().slug, args[0], via="admin", added_by=message.from_user.id
    )
    await message.answer(
        f"✅ Доступ выдан: <code>{args[0]}</code>" if created
        else f"<code>{args[0]}</code> уже имеет доступ."
    )


@router.message(Command("deny"), F.chat.type == "private")
async def cmd_deny(message: Message, command: CommandObject = None):
    if not _admin_guard(message):
        await message.answer("⛔ Только для администраторов.")
        return
    args = _int_args(command, (0,))
    if not args or args[0] <= 0:
        await message.answer("Формат: <code>/deny ID</code>")
        return
    removed = await showcase_access.remove_member(current_showcase().slug, args[0])
    await message.answer(
        f"✅ Доступ отозван: <code>{args[0]}</code>" if removed
        else f"<code>{args[0]}</code> не был участником."
    )


@router.message(Command("members"), F.chat.type == "private")
async def cmd_members(message: Message):
    if not _admin_guard(message):
        await message.answer("⛔ Только для администраторов.")
        return
    sc = current_showcase()
    rows = await showcase_access.list_members(sc.slug, limit=50)
    mode = "по приглашению" if sc.access == ACCESS_INVITE else "открытая"
    if not rows:
        await message.answer(f"Витрина: {mode}. Участников пока нет.")
        return
    lines = [f"• <code>{r.user_id}</code> ({r.via})" for r in rows]
    await message.answer(f"👥 <b>Участники</b> (витрина {mode}, последние {len(rows)}):\n" + "\n".join(lines))


async def _ensure_user(telegram_id: int) -> None:
    """Строка users нужна для учёта расхода (usage_daily); хранится только id."""
    try:
        from app.core.database import async_session_maker, get_or_create_user

        async with async_session_maker() as session:
            await get_or_create_user(session, telegram_id=telegram_id)
    except Exception as e:
        logger.warning(f"Showcase: cannot register user {telegram_id}: {e}")


@router.message()
async def handle_message(message: Message):
    """Main entry: answer strictly from corpus."""
    sc = current_showcase()
    if not await showcase_allowed(sc, message.from_user.id):
        await message.answer(_denied_text(sc))
        return

    text = (message.text or "").strip()
    if not text:
        await message.answer("Пришлите вопрос текстом.")
        return

    await _ensure_user(message.from_user.id)
    limit_check = await check_showcase_limit(message.from_user.id)
    if not limit_check.allowed:
        await message.answer(
            limit_check.message,
            reply_markup=call_operator_keyboard() if operator_enabled() else None,
        )
        return

    hkey = sc.client_key(message.from_user.id)
    dialog_history.add(hkey, ROLE_CLIENT, text, KIND_QUESTION)
    escalate_kb = call_operator_keyboard() if operator_enabled() else None
    processing_msg = await message.answer("🔍 Ищу в базе знаний…")
    typing_task = start_typing(message.bot, message.chat.id)
    start_usage_session(message.from_user.id)
    try:
        docs = await vector_memory.search_corpus(text, corpus=sc.collection)

        if not docs:
            dialog_history.add(hkey, ROLE_BOT, "Ответа в базе знаний не нашлось.", KIND_NOT_FOUND)
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
            (sc.system_prompt or settings.SHOWCASE_SYSTEM_PROMPT)
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
            model=sc.model or settings.SHOWCASE_MODEL,
            reasoning_effort="medium",
        )

        answer = response.content
        if sources:
            answer += "\n\n<b>Источники:</b>\n" + "\n".join(sources)

        dialog_history.add(hkey, ROLE_BOT, response.content, KIND_ANSWER, sources=sources)
        await deliver_via_message(processing_msg, answer, reply_markup=escalate_kb)

    except Exception as e:
        logger.error(f"Error in showcase handler: {e}")
        dialog_history.add(hkey, ROLE_BOT, "Бот не смог ответить (ошибка).", KIND_ERROR)
        await processing_msg.edit_text(
            "⚠️ Ошибка при обработке вопроса. Попробуйте позже.",
            reply_markup=escalate_kb,
        )
    finally:
        stop_typing(typing_task)
        await flush_usage()
