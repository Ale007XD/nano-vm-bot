"""
/providers — Admin handler for hot-swapping LLM provider keys and priorities.

Flow:
  /providers
    → inline list of providers with status indicators
    → tap provider → action menu (Update Key / Toggle / Set Primary)
    → Update Key: bot asks for new key
      → user sends key (bot IMMEDIATELY deletes the message)
      → bot shows masked key + confirm keyboard
      → on confirm: encrypt + save to DB, call llm_client.reload_provider()
    → Toggle: enable/disable without changing key
    → Set Primary: set priority=1, demote others

Security:
  - Only ADMIN_IDS can access these handlers
  - New key message is deleted immediately after reading
  - Keys stored Fernet-encrypted in DB
  - Only last 4 chars shown in any bot message
"""
import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.core.config import settings
from app.core.crypto import decrypt_key, encrypt_key, mask_key
from app.core.database import async_session_maker, upsert_provider_config
from app.core.llm_client_v2 import llm_client
from app.utils.states import ProviderKeyUpdate

logger = logging.getLogger(__name__)
router = Router()

# ------------------------------------------------------------------ #
#  ACCESS GUARD                                                        #
# ------------------------------------------------------------------ #

def _is_admin(user_id: int) -> bool:
    return user_id in set(settings.ADMIN_IDS)


async def _deny(message: Message):
    await message.answer("⛔ Только для администраторов.")


async def _deny_cb(callback: CallbackQuery):
    await callback.answer("⛔ Только для администраторов.", show_alert=True)


# ------------------------------------------------------------------ #
#  KEYBOARDS                                                           #
# ------------------------------------------------------------------ #

def _provider_list_keyboard() -> InlineKeyboardMarkup:
    """Keyboard with all known providers + their live status"""
    builder = InlineKeyboardBuilder()
    stats = {s['name']: s for s in llm_client.get_provider_stats()}

    provider_names = ["groq", "openrouter", "anthropic", "openai"]
    status_emoji = {"healthy": "🟢", "degraded": "🟡", "down": "🔴"}

    for name in provider_names:
        stat = stats.get(name)
        if stat:
            emoji = status_emoji.get(stat['status'], "⚪")
            label = f"{emoji} {name.upper()} | p{stat['priority']} | {stat['response_time_ms']:.0f}ms"
        else:
            label = f"⚫ {name.upper()} (не настроен)"
        builder.button(text=label, callback_data=f"prov:select:{name}")

    builder.button(text="🔄 Обновить", callback_data="prov:refresh")
    builder.button(text="❌ Закрыть", callback_data="prov:close")
    builder.adjust(1)
    return builder.as_markup()


def _provider_action_keyboard(name: str) -> InlineKeyboardMarkup:
    """Action menu for a specific provider"""
    builder = InlineKeyboardBuilder()

    stats = {s['name']: s for s in llm_client.get_provider_stats()}
    stat = stats.get(name, {})
    is_down = stat.get('status') == 'down'

    builder.button(text="🤖 Выбрать модели", callback_data=f"providers:show:{name}")
    builder.button(text="🔑 Обновить ключ", callback_data=f"prov:update_key:{name}")
    builder.button(
        text="✅ Включить" if is_down else "⛔ Отключить",
        callback_data=f"prov:toggle:{name}"
    )
    builder.button(text="⭐ Сделать основным", callback_data=f"prov:primary:{name}")
    builder.button(text="🔙 Назад", callback_data="prov:list")
    builder.adjust(1)
    return builder.as_markup()


def _confirm_key_keyboard(name: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Подтвердить", callback_data=f"prov:confirm_key:{name}")
    builder.button(text="❌ Отмена", callback_data=f"prov:select:{name}")
    builder.adjust(2)
    return builder.as_markup()


# ------------------------------------------------------------------ #
#  ENTRY POINT                                                         #
# ------------------------------------------------------------------ #

@router.message(Command("providers"))
async def cmd_providers(message: Message):
    if not _is_admin(message.from_user.id):
        await _deny(message)
        return

    await message.answer(
        "🔌 <b>Управление LLM-провайдерами</b>\n\n"
        "Нажмите на провайдер для управления:\n"
        "<i>🟢 healthy | 🟡 degraded | 🔴 down | ⚫ не настроен</i>",
        reply_markup=_provider_list_keyboard()
    )


# ------------------------------------------------------------------ #
#  CALLBACKS: LIST & REFRESH                                           #
# ------------------------------------------------------------------ #

@router.callback_query(F.data == "prov:list")
async def cb_prov_list(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await _deny_cb(callback)
        return
    await state.clear()
    await callback.message.edit_text(
        "🔌 <b>Управление LLM-провайдерами</b>\n\n"
        "Нажмите на провайдер для управления:\n"
        "<i>🟢 healthy | 🟡 degraded | 🔴 down | ⚫ не настроен</i>",
        reply_markup=_provider_list_keyboard()
    )
    await callback.answer()


@router.callback_query(F.data == "prov:refresh")
async def cb_prov_refresh(callback: CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await _deny_cb(callback)
        return
    await llm_client.check_health()
    await callback.message.edit_reply_markup(reply_markup=_provider_list_keyboard())
    await callback.answer("🔄 Обновлено")


@router.callback_query(F.data == "prov:close")
async def cb_prov_close(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await _deny_cb(callback)
        return
    await state.clear()
    await callback.message.delete()
    await callback.answer()


# ------------------------------------------------------------------ #
#  PROVIDER SELECT                                                     #
# ------------------------------------------------------------------ #

@router.callback_query(F.data.startswith("prov:select:"))
async def cb_prov_select(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await _deny_cb(callback)
        return

    name = callback.data.split(":", 2)[2]
    stats = {s['name']: s for s in llm_client.get_provider_stats()}
    stat = stats.get(name)

    if stat:
        info = (
            f"🔌 <b>{name.upper()}</b>\n\n"
            f"Статус: {stat['status']}\n"
            f"Приоритет: {stat['priority']}\n"
            f"Ошибок: {stat['error_count']}\n"
            f"Последняя ошибка: {stat['last_error'] or '—'}\n"
            f"Время отклика: {stat['response_time_ms']:.0f}ms\n"
            f"Последнее использование: {stat['last_used'] or '—'}"
        )
    else:
        info = f"🔌 <b>{name.upper()}</b>\n\nПровайдер не настроен (нет ключа в .env)"

    await callback.message.edit_text(info, reply_markup=_provider_action_keyboard(name))
    await callback.answer()


# ------------------------------------------------------------------ #
#  TOGGLE ENABLE/DISABLE                                               #
# ------------------------------------------------------------------ #

@router.callback_query(F.data.startswith("prov:toggle:"))
async def cb_prov_toggle(callback: CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await _deny_cb(callback)
        return

    name = callback.data.split(":", 2)[2]
    stats = {s['name']: s for s in llm_client.get_provider_stats()}
    stat = stats.get(name)

    if not stat:
        await callback.answer(f"Провайдер {name} не найден", show_alert=True)
        return

    is_down = stat['status'] == 'down'
    await llm_client.set_provider_enabled(name, enabled=is_down)

    async with async_session_maker() as session:
        await upsert_provider_config(
            session, name, is_enabled=is_down, updated_by=callback.from_user.id
        )

    action = "включён" if is_down else "отключён"
    await callback.answer(f"✅ {name.upper()} {action}", show_alert=True)

    # Refresh action view
    stats2 = {s['name']: s for s in llm_client.get_provider_stats()}
    stat2 = stats2.get(name, {})
    info = (
        f"🔌 <b>{name.upper()}</b>\n\n"
        f"Статус: {stat2.get('status', '?')}\n"
        f"Приоритет: {stat2.get('priority', '?')}"
    )
    await callback.message.edit_text(info, reply_markup=_provider_action_keyboard(name))


# ------------------------------------------------------------------ #
#  SET PRIMARY                                                         #
# ------------------------------------------------------------------ #

@router.callback_query(F.data.startswith("prov:primary:"))
async def cb_prov_primary(callback: CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await _deny_cb(callback)
        return

    name = callback.data.split(":", 2)[2]

    # Promote target to priority=1, push others up by 1
    for p in llm_client.providers:
        if p.name == name:
            p.priority = 1
        elif p.priority >= 1:
            p.priority += 1

    llm_client.providers.sort(key=lambda x: x.priority)

    async with async_session_maker() as session:
        for p in llm_client.providers:
            await upsert_provider_config(
                session, p.name, priority=p.priority, updated_by=callback.from_user.id
            )

    await callback.answer(f"⭐ {name.upper()} теперь основной провайдер", show_alert=True)

    stats = {s['name']: s for s in llm_client.get_provider_stats()}
    stat = stats.get(name, {})
    info = (
        f"🔌 <b>{name.upper()}</b>\n\n"
        f"Статус: {stat.get('status', '?')}\n"
        f"Приоритет: {stat.get('priority', '?')} ⭐"
    )
    await callback.message.edit_text(info, reply_markup=_provider_action_keyboard(name))


# ------------------------------------------------------------------ #
#  UPDATE KEY — STEP 1: ask for new key                               #
# ------------------------------------------------------------------ #

@router.callback_query(F.data.startswith("prov:update_key:"))
async def cb_prov_update_key_start(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await _deny_cb(callback)
        return

    name = callback.data.split(":", 2)[2]
    await state.set_state(ProviderKeyUpdate.waiting_key)
    await state.update_data(provider_name=name, origin_message_id=callback.message.message_id)

    builder = InlineKeyboardBuilder()
    builder.button(text="❌ Отмена", callback_data=f"prov:select:{name}")

    await callback.message.edit_text(
        f"🔑 <b>Обновление ключа {name.upper()}</b>\n\n"
        f"Отправьте новый API ключ следующим сообщением.\n"
        f"⚠️ Сообщение с ключом будет <b>немедленно удалено</b>.",
        reply_markup=builder.as_markup()
    )
    await callback.answer()


# ------------------------------------------------------------------ #
#  UPDATE KEY — STEP 2: receive key, delete message, show masked      #
# ------------------------------------------------------------------ #

@router.message(ProviderKeyUpdate.waiting_key)
async def handle_new_key_input(message: Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        await message.delete()
        await _deny(message)
        return

    new_key = message.text.strip() if message.text else ""

    # Delete user's message IMMEDIATELY — key must not stay in chat history
    try:
        await message.delete()
    except Exception as e:
        logger.warning(f"Could not delete key message: {e}")

    if not new_key or len(new_key) < 10:
        await message.answer(
            "⚠️ Ключ слишком короткий или пустой. Попробуйте снова.",
            reply_markup=InlineKeyboardBuilder().button(
                text="❌ Отмена", callback_data="prov:list"
            ).as_markup()
        )
        await state.clear()
        return

    data = await state.get_data()
    name = data.get("provider_name", "unknown")

    # Encrypt before storing in state
    try:
        encrypted = encrypt_key(new_key)
    except RuntimeError as e:
        await message.answer(f"⛔ Ошибка шифрования: {e}")
        await state.clear()
        return

    await state.update_data(encrypted_key=encrypted)
    await state.set_state(ProviderKeyUpdate.confirming)

    masked = mask_key(new_key)
    await message.answer(
        f"🔑 <b>Подтверждение ключа {name.upper()}</b>\n\n"
        f"Ключ: <code>{masked}</code>\n\n"
        f"Применить изменение?",
        reply_markup=_confirm_key_keyboard(name)
    )


# ------------------------------------------------------------------ #
#  UPDATE KEY — STEP 3: confirm and apply                             #
# ------------------------------------------------------------------ #

@router.callback_query(F.data.startswith("prov:confirm_key:"))
async def cb_prov_confirm_key(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await _deny_cb(callback)
        return

    name = callback.data.split(":", 2)[2]
    data = await state.get_data()
    encrypted = data.get("encrypted_key")

    if not encrypted:
        await callback.answer("⚠️ Нет данных для сохранения", show_alert=True)
        await state.clear()
        return

    # Decrypt to pass to live client
    try:
        plain_key = decrypt_key(encrypted)
    except ValueError as e:
        await callback.answer(f"Ошибка расшифровки: {e}", show_alert=True)
        await state.clear()
        return

    # 1. Save encrypted key to DB
    async with async_session_maker() as session:
        await upsert_provider_config(
            session,
            name,
            encrypted_key=encrypted,
            updated_by=callback.from_user.id
        )

    # 2. Hot-reload live client
    found = await llm_client.reload_provider(name, plain_key)

    await state.clear()

    if found:
        stats = {s['name']: s for s in llm_client.get_provider_stats()}
        stat = stats.get(name, {})
        status = stat.get('status', 'unknown')
        status_emoji = {"healthy": "🟢", "degraded": "🟡", "down": "🔴"}.get(status, "⚪")

        await callback.message.edit_text(
            f"✅ <b>Ключ {name.upper()} обновлён</b>\n\n"
            f"Статус после проверки: {status_emoji} {status}\n"
            f"Ошибок: {stat.get('error_count', 0)}\n"
            f"Последняя ошибка: {stat.get('last_error') or '—'}",
            reply_markup=_provider_action_keyboard(name)
        )
        logger.info(f"Provider {name} key hot-reloaded by admin {callback.from_user.id}")
    else:
        await callback.message.edit_text(
            f"⚠️ Провайдер {name} не найден в живом клиенте.\n"
            f"Ключ сохранён в БД — вступит в силу после рестарта.",
            reply_markup=_provider_list_keyboard()
        )

    await callback.answer()
