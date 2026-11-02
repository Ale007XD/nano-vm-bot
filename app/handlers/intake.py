"""Участники пространств пишут (или наговаривают) свободным текстом — бот записывает.

Роутер стоит перед messages.router: для участников общего пространства (при SPACES_ENABLED)
текст и голос идут сюда, остальные пользователи общаются с ассистентом как раньше.
Также /list (список покупок с кнопками), /expenses (расходы за месяц), /report, /undo.
"""
import html
import logging
import os
from datetime import datetime
from typing import Optional

import pytz
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from app.core.access import check_daily_limit, user_allowed
from app.core.config import settings
from app.core.usage import flush_usage, start_usage_session
from app.spaces import checklists, intake, ledger, metrics, repo, reports, service

logger = logging.getLogger(__name__)

router = Router()

HINT = (
    "Не нашёл, что записать. Примеры:\n"
    "• <i>напомни завтра в 18:00 купить хлеб</i>\n"
    "• <i>купи молоко и яйца</i>\n"
    "• <i>потратил 1200 на продукты</i>"
)


async def _applicable(message: Message):
    """Фильтр: личный чат + участник общего пространства с включённым разбором."""
    if getattr(settings, "SPACES_ENABLED", False) is not True:
        return False
    if getattr(settings, "SPACE_INTAKE_ENABLED", True) is not True or message.chat.type != "private":
        return False
    scope = await repo.scope_for_user(message.from_user.id)
    if scope is None:
        return False
    space = await service.get_space(scope.space_id)
    if space is None or (isinstance(space.settings, dict) and space.settings.get("intake") is False):
        return False
    return {"scope": scope, "space": space}


async def _llm(system: str, user: str) -> str:
    from app.core.llm_client_v2 import Message as LLMMessage
    from app.core.llm_client_v2 import llm_client

    resp = await llm_client.chat(
        [LLMMessage(role="system", content=system), LLMMessage(role="user", content=user)],
        model=getattr(settings, "SPACE_INTAKE_MODEL", "") or None,
        temperature=0.1, max_tokens=900, response_format={"type": "json_object"},
    )
    return resp.content


def _keyboard(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Записать", callback_data=f"ix:ok:{token}"),
        InlineKeyboardButton(text="✖️ Отмена", callback_data=f"ix:no:{token}"),
    ]])


async def process_text(message: Message, text: str, scope, space, source: str = "text", llm=None) -> None:
    uid = message.from_user.id
    if not await user_allowed(uid):
        await message.answer("⛔ Доступ запрещен")
        return
    limit = await check_daily_limit(uid)
    if not limit.allowed:
        await message.answer(limit.message)
        return
    start_usage_session(uid)
    try:
        await message.bot.send_chat_action(message.chat.id, "typing")
        members = await intake.member_names(space.id)
        parsed = await intake.analyze(text, space, members, llm or _llm)
    except intake.IntakeError as e:
        logger.warning(f"intake: bad model answer: {e}")
        await message.answer("Не смог разобрать сообщение. Попробуйте сформулировать иначе.")
        return
    except Exception as e:  # noqa: BLE001 — BYOK, лимиты провайдера, сеть: человеку — короткая причина
        logger.error(f"intake failed: {e}")
        from app.core.byok import BYOKError

        await message.answer(html.escape(str(e)) if isinstance(e, BYOKError)
                             else "Не получилось обработать сообщение, попробуйте позже.")
        return
    finally:
        await flush_usage()

    if not parsed.proposals:
        extra = ("\n\n" + "\n".join("⚠️ " + html.escape(n) for n in parsed.notes)) if parsed.notes else ""
        await message.answer(HINT + extra)
        return
    if intake.needs_confirmation(parsed.proposals, space):
        token = await intake.put_pending(space.id, uid, parsed, source, text)
        titles = {k: m.title for k, m in metrics.metrics_for(space).items()}
        await message.answer("Записать?\n\n" + intake.render(parsed, titles=titles), reply_markup=_keyboard(token))
        return
    lines = await intake.commit(scope, space, parsed.proposals, source=source, raw_text=text)
    lines += [f"⚠️ {html.escape(n)}" for n in parsed.notes]
    await message.answer("\n".join(lines))


@router.message(F.text, ~F.text.startswith("/"), _applicable)
async def on_text(message: Message, scope, space):
    await process_text(message, message.text, scope, space)


@router.message(F.voice, _applicable)
async def on_voice(message: Message, scope, space):
    uid = message.from_user.id
    voice = message.voice
    if voice.duration > settings.MAX_VOICE_DURATION:
        await message.answer(f"⚠️ Голосовое длиннее {settings.MAX_VOICE_DURATION} с — отправьте покороче.")
        return
    if not await user_allowed(uid):
        await message.answer("⛔ Доступ запрещен")
        return
    limit = await check_daily_limit(uid)
    if not limit.allowed:
        await message.answer(limit.message)
        return
    start_usage_session(uid)
    os.makedirs(settings.TEMP_DIR, exist_ok=True)
    path = os.path.join(settings.TEMP_DIR, f"voice_{uid}_{voice.file_id}.ogg")
    try:
        f = await message.bot.get_file(voice.file_id)
        await message.bot.download_file(f.file_path, path)
        from app.core.llm_client_v2 import llm_client

        text = await llm_client.transcribe_audio(audio_file_path=path, language=None)
    except Exception as e:  # noqa: BLE001
        from app.core.byok import BYOKError

        logger.error(f"intake voice failed: {e}")
        await message.answer(html.escape(str(e)) if isinstance(e, BYOKError)
                             else "Не удалось распознать голосовое, попробуйте ещё раз или напишите текстом.")
        return
    finally:
        await flush_usage()
        if os.path.exists(path):
            os.remove(path)
    if not (text or "").strip():
        await message.answer("🎤 Не удалось распознать речь. Говорите чётче или напишите текстом.")
        return
    await message.answer(f"🎤 <i>{html.escape(text.strip())}</i>")
    await process_text(message, text.strip(), scope, space, source="voice")


@router.callback_query(F.data.startswith("ix:"))
async def on_confirm(callback: CallbackQuery):
    uid = callback.from_user.id
    if not await user_allowed(uid):
        await callback.answer("⛔ Доступ запрещен", show_alert=True)
        return
    try:
        _, action, token = callback.data.split(":", 2)
    except ValueError:
        await callback.answer()
        return
    got = await intake.take_pending(token, uid)
    if got is None:
        await callback.answer("Устарело или уже обработано — отправьте сообщение заново", show_alert=True)
        return
    space_id, proposals, source, raw_text = got
    if action != "ok":
        await callback.message.edit_text("✖️ Отменено")
        await callback.answer()
        return
    try:
        scope = await repo.open_scope(space_id, uid)
    except repo.SpaceAccessError:
        await callback.message.edit_text("⛔ Доступ к пространству утрачен")
        await callback.answer()
        return
    space = await service.get_space(space_id)
    lines = await intake.commit(scope, space, proposals, source=source, raw_text=raw_text)
    await callback.message.edit_text("\n".join(lines))
    await callback.answer("Готово")


# ----------------------------- команды ------------------------------------- #
async def _member_scope(message: Message):
    scope = await repo.scope_for_user(message.from_user.id)
    if scope is None:
        await message.answer("Вы не состоите ни в одном пространстве.")
        return None, None
    return scope, await service.get_space(scope.space_id)


def _list_markup(items) -> Optional[InlineKeyboardMarkup]:
    rows = [[InlineKeyboardButton(text=("✅ " if i.done else "▫️ ") + i.text[:40], callback_data=f"cl:t:{i.id}")]
            for i in items[:40]]
    if any(i.done for i in items):
        rows.append([InlineKeyboardButton(text="🧹 Убрать вычеркнутое", callback_data="cl:c")])
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


async def _render_list(scope):
    lst = await checklists.default_list(scope, "shopping", "Покупки")
    items = await checklists.list_items(scope, lst.id)
    text = f"🛒 <b>{html.escape(lst.title)}</b>" + (f" — {sum(not i.done for i in items)} осталось" if items else "")
    text += "\nНажмите на позицию, чтобы вычеркнуть." if items else "\nСписок пуст. Напишите «купи молоко» — добавлю."
    return text, _list_markup(items), lst.id


@router.message(Command("list"), F.chat.type == "private")
async def cmd_list(message: Message):
    if getattr(settings, "SPACES_ENABLED", False) is not True:
        return
    scope, _ = await _member_scope(message)
    if scope is None:
        return
    text, markup, _ = await _render_list(scope)
    await message.answer(text, reply_markup=markup)


@router.callback_query(F.data.startswith("cl:"))
async def on_list_button(callback: CallbackQuery):
    scope = await repo.scope_for_user(callback.from_user.id)
    if scope is None:
        await callback.answer("Нет доступа", show_alert=True)
        return
    parts = callback.data.split(":")
    if parts[1] == "t" and len(parts) == 3 and parts[2].isdigit():
        await checklists.toggle(scope, int(parts[2]))
    elif parts[1] == "c":
        lst = await checklists.default_list(scope, "shopping", "Покупки")
        await checklists.clear_done(scope, lst.id)
    text, markup, _ = await _render_list(scope)
    try:
        await callback.message.edit_text(text, reply_markup=markup)
    except Exception as e:  # noqa: BLE001 — «message is not modified» и т. п.
        logger.debug(f"list edit skipped: {e}")
    await callback.answer()


@router.message(Command("undo"), F.chat.type == "private")
async def cmd_undo(message: Message):
    if getattr(settings, "SPACES_ENABLED", False) is not True:
        return
    scope, space = await _member_scope(message)
    if scope is None:
        return
    entry = await ledger.undo_last(scope)
    if entry is None:
        await message.answer("У вас нет записей, которые можно отменить.")
        return
    m = metrics.metrics_for(space).get(entry.metric)
    shown = (metrics.formatter(m)(entry.amount_minor) if m else metrics.format_count(entry.amount_minor, entry.currency))
    title = f"{html.escape(m.title)}: " if m and m.key != metrics.EXPENSE else ""
    await message.answer(f"↩️ Отменено: {title}{shown}" + (f" · {html.escape(entry.note)}" if entry.note else ""))


@router.message(Command("expenses"), F.chat.type == "private")
async def cmd_expenses(message: Message, command=None):
    if getattr(settings, "SPACES_ENABLED", False) is not True:
        return
    scope, space = await _member_scope(message)
    if scope is None:
        return
    now = datetime.now(pytz.utc).astimezone(pytz.timezone(space.timezone))
    year, month = now.year, now.month
    arg = ((getattr(command, "args", None) or "").strip().lower())
    if arg in ("prev", "прошлый"):
        year, month = ledger.previous_month(year, month)
    elif arg.isdigit() and 1 <= int(arg) <= 12:
        month = int(arg)
        year = year - 1 if month > now.month else year
    summary = await ledger.monthly_summary(scope, year, month, tz=space.timezone, currency=space.currency)
    names = await intake.member_names(space.id)
    await message.answer(ledger.format_report(summary, names, "Расходы"))


@router.message(Command("report"), F.chat.type == "private")
async def cmd_report(message: Message, command=None):
    """/report — список отчётов пространства; /report <id> — построить сейчас за последний полный период."""
    if getattr(settings, "SPACES_ENABLED", False) is not True:
        return
    scope, space = await _member_scope(message)
    if scope is None:
        return
    specs = [s for s in reports.specs_for(space) if s.audience == "all" or scope.can("admin")]
    arg = ((getattr(command, "args", None) or "").strip().lower())
    if not arg:
        if not specs:
            await message.answer("Отчётов нет.")
            return
        await message.answer("📊 Отчёты:\n" + "\n".join(
            f"• <code>{s.id}</code> — {html.escape(s.title)}" for s in specs) + "\n\n/report &lt;id&gt; — построить сейчас")
        return
    spec = next((s for s in specs if s.id == arg), None)
    if spec is None:
        await message.answer("Такого отчёта нет. Список: /report")
        return
    now = datetime.now(pytz.utc).astimezone(pytz.timezone(space.timezone))
    win = reports.window_for(spec, reports.slot_for(spec, now), space.timezone)
    try:
        text = await reports.build_text(scope, space, spec, win)
    except reports.ReportError as e:
        await message.answer(f"❌ {html.escape(str(e))}")
        return
    await message.answer(text or f"За период {html.escape(win.label)} записей нет.")
