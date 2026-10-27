"""
/byok — пользовательские API-ключи (Bring Your Own Key).

  /byok                       — статус и справка
  /byok set <провайдер> <ключ> — сохранить ключ (сообщение с ключом удаляется)
  /byok del <провайдер|all>    — удалить

Безопасность: только личный чат; сообщение с ключом удаляется ДО проверки
и сохранения; ключ нигде не логируется и не показывается (только последние 4 символа).
"""
import html
import logging

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from app.core import byok
from app.core.access import user_allowed
from app.core.config import settings

logger = logging.getLogger(__name__)

router = Router()

_PROVIDER_LINKS = {
    "groq": "console.groq.com/keys",
    "openrouter": "openrouter.ai/keys",
    "anthropic": "console.anthropic.com/settings/keys",
    "openai": "platform.openai.com/api-keys",
}


async def _status_text(telegram_id: int) -> str:
    try:
        keys = await byok.list_user_keys(telegram_id)
    except Exception as e:
        logger.error("BYOK list failed for %s: %s", telegram_id, e)
        return "⚠️ Не удалось получить список ключей. Попробуйте позже."

    lines = ["🔑 <b>Свои API-ключи (BYOK)</b>\n"]
    if keys:
        for prov in byok.SUPPORTED_PROVIDERS:
            if prov in keys:
                lines.append(f"✅ <b>{prov}</b> — …{html.escape(keys[prov])}")
        lines.append(
            "\nСейчас все ваши запросы идут <b>только</b> на ваши ключи, "
            "суточные лимиты бота не применяются. Если ключ перестанет работать — "
            "будет ошибка, общие ключи бота использоваться не будут."
        )
    else:
        lines.append("Ключей нет — используются общие ключи бота и суточные лимиты.")
    lines.append(
        "\n<b>Команды</b>\n"
        "<code>/byok set &lt;провайдер&gt; &lt;ключ&gt;</code> — сохранить\n"
        "<code>/byok del &lt;провайдер|all&gt;</code> — удалить\n"
        f"Провайдеры: {', '.join(byok.SUPPORTED_PROVIDERS)}\n\n"
        "Сообщение с ключом бот удалит сразу после получения. "
        "Для голоса и анализа фото нужен ключ <b>groq</b>."
    )
    return "\n".join(lines)


@router.message(Command("byok"))
async def cmd_byok(message: Message, command: CommandObject):
    if not await user_allowed(message.from_user.id):
        await message.answer("⛔ Доступ запрещен")
        return

    args = (command.args or "").split()
    sub = args[0].lower() if args else ""

    # Ключ нельзя принимать вне личного чата: сообщение увидят другие участники
    if sub == "set" and message.chat.type != "private":
        try:
            await message.delete()
        except Exception:
            pass
        await message.answer("🔒 Ключи принимаются только в личном чате с ботом.")
        return

    # Сообщение с ключом удаляем немедленно, до любых проверок
    if sub == "set":
        try:
            await message.delete()
        except Exception as e:
            logger.warning("BYOK: cannot delete key message: %s", e)

    if getattr(settings, "BYOK_ENABLED", True) is not True:
        await message.answer("ℹ️ Режим BYOK отключён администратором.")
        return

    uid = message.from_user.id

    if sub == "set":
        await _handle_set(message, uid, args)
    elif sub in ("del", "delete", "rm"):
        await _handle_del(message, uid, args)
    else:
        await message.answer(await _status_text(uid))


async def _handle_set(message: Message, uid: int, args: list):
    if len(args) != 3:
        await message.answer(
            "Формат: <code>/byok set &lt;провайдер&gt; &lt;ключ&gt;</code>\n"
            f"Провайдеры: {', '.join(byok.SUPPORTED_PROVIDERS)}"
        )
        return
    provider, key = args[1].lower(), args[2]

    err = byok.validate_key_format(provider, key)
    if err:
        await message.answer(f"❌ {html.escape(err)}")
        return

    try:
        byok._crypto()  # fail closed: без шифрования ключ не сохраняем
    except byok.BYOKUnavailable:
        await message.answer(
            "❌ Хранение ключей не настроено у администратора бота (ENCRYPTION_KEY)."
        )
        return

    ok, reason = await byok.validate_key_live(provider, key)
    if ok is False:
        await message.answer(
            f"❌ Ключ {provider} не принят: {html.escape(reason)}. "
            f"Получить ключ: {_PROVIDER_LINKS[provider]}"
        )
        return
    if ok is None:
        await message.answer(
            f"⚠️ Не удалось проверить ключ ({html.escape(reason)}). "
            "Повторите через минуту — ключ не сохранён."
        )
        return

    try:
        hint = await byok.set_user_key(uid, provider, key)
    except Exception as e:
        logger.error("BYOK save failed for %s/%s: %s", uid, provider, type(e).__name__)
        await message.answer("⚠️ Не удалось сохранить ключ. Попробуйте позже.")
        return

    extra = "" if provider == "groq" else (
        "\nℹ️ Голос и анализ фото работают только с ключом groq."
    )
    await message.answer(
        f"✅ Ключ <b>{provider}</b> (…{html.escape(hint)}) сохранён.\n"
        "Теперь ваши запросы идут на него, суточные лимиты не применяются. "
        "Сообщение с ключом удалено." + extra
    )


async def _handle_del(message: Message, uid: int, args: list):
    if len(args) != 2:
        await message.answer("Формат: <code>/byok del &lt;провайдер|all&gt;</code>")
        return
    target = args[1].lower()
    if target != "all" and target not in byok.SUPPORTED_PROVIDERS:
        await message.answer(f"❌ Неизвестный провайдер. Доступны: {', '.join(byok.SUPPORTED_PROVIDERS)}, all")
        return
    try:
        n = await byok.delete_user_key(uid, None if target == "all" else target)
    except Exception as e:
        logger.error("BYOK delete failed for %s: %s", uid, e)
        await message.answer("⚠️ Не удалось удалить ключ. Попробуйте позже.")
        return
    await message.answer(f"🗑 Удалено ключей: {n}." if n else "Такого ключа нет.")
