"""
Main command handlers
"""
import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.core.config import settings
from app.core.database import async_session_maker, get_or_create_user
from app.utils.keyboards import (
    get_main_menu,
    get_memory_menu_keyboard,
    get_mode_keyboard,
    get_provider_detail_keyboard,
    get_provider_models_keyboard,
    get_providers_keyboard,
    get_settings_keyboard,
    get_skills_menu_keyboard,
)

logger = logging.getLogger(__name__)

router = Router()


def get_allowed_users() -> set:
    """
    Возвращает актуальный set разрешённых пользователей из settings.ADMIN_IDS.
    Вызывается при каждой проверке — подхватывает горячие изменения без рестарта.
    Удалён захардкоженный ALLOWED_USERS = {195351142}.
    """
    return set(settings.ADMIN_IDS)


async def check_access(message: Message) -> bool:
    """Check if user is allowed (ADMIN_IDS from settings/DB)"""
    if message.from_user.id not in get_allowed_users():
        await message.answer(
            "⛔ <b>Доступ запрещен</b>\n\n"
            "Этот бот является приватным. У вас нет прав на использование."
        )
        return False
    return True


async def check_access_callback(callback: CallbackQuery) -> bool:
    """Check access for callback queries"""
    if callback.from_user.id not in get_allowed_users():
        await callback.answer("⛔ Доступ запрещен", show_alert=True)
        return False
    return True


@router.message(CommandStart())
async def cmd_start(message: Message):
    """Handle /start command"""
    if not await check_access(message):
        return

    user = message.from_user

    async with async_session_maker() as session:
        # Вызов нужен ради side effect — пользователь создаётся при первом /start
        await get_or_create_user(
            session,
            telegram_id=user.id,
            username=user.username,
            first_name=user.first_name,
            last_name=user.last_name,
            language_code=user.language_code
        )

        welcome_text = f"""👋 <b>Добро пожаловать в {settings.APP_NAME}!</b>

Привет, {user.first_name or 'друг'}! Я ваш AI-ассистент с несколькими режимами:

⚡ <b>FastBot</b> - Быстрый помощник для повседневных задач
🧩 <b>PlanBot</b> - Умный планировщик с логическим мышлением
🔧 <b>SkillBot</b> - Менеджер навыков и каталог

Я могу:
• Хранить и вспоминать ваши воспоминания и заметки
• Создавать и управлять пользовательскими навыками
• Помогать с планированием и анализом
• Запоминать наши разговоры

Используйте меню ниже или введите /help для получения справки!"""

        await message.answer(welcome_text, reply_markup=get_main_menu())


@router.message(Command("help"))
async def cmd_help(message: Message):
    """Handle /help command"""
    if not await check_access(message):
        return

    help_text = f"""📖 <b>Справка по {settings.APP_NAME}</b>

<b>🤖 Режимы агентов:</b>
/mode - Переключение между агентами
• <b>FastBot</b>: Быстрые, повседневные задачи
• <b>PlanBot</b>: Планирование, анализ, логика
• <b>SkillBot</b>: Управление навыками

<b>🛠 Навыки:</b>
/skills - Открыть менеджер навыков

<b>🧠 Память:</b>
/memory - Операции с памятью

<b>⚙️ Управление:</b>
/clear - Очистить историю разговора
/settings - Настройки бота
/providers - Управление ключами и статусом провайдеров (только admin)
/models - Выбор моделей для ролей (только admin)

<b>💬 Чат:</b>
Просто отправьте мне сообщение!"""

    await message.answer(help_text)


@router.message(Command("mode"))
async def cmd_mode(message: Message, state: FSMContext):
    """Handle /mode command"""
    if not await check_access(message):
        return

    await message.answer(
        "🤖 <b>Выберите режим агента:</b>",
        reply_markup=get_mode_keyboard()
    )


@router.callback_query(F.data.startswith("mode:"))
async def process_mode_selection(callback: CallbackQuery, state: FSMContext):
    """Handle agent mode selection"""
    logger.info(f"Mode selection triggered: {callback.data} by user {callback.from_user.id}")
    if not await check_access_callback(callback):
        return

    mode = callback.data.split(":")[1]

    async with async_session_maker() as session:
        from sqlalchemy import select

        from app.core.database import UserState

        # UserState.user_id — FK на users.id, поэтому резолвим internal id
        db_user = await get_or_create_user(session, telegram_id=callback.from_user.id)

        result = await session.execute(
            select(UserState).where(UserState.user_id == db_user.id)
        )
        user_state = result.scalar_one_or_none()

        if not user_state:
            user_state = UserState(
                user_id=db_user.id,
                current_agent=mode
            )
            session.add(user_state)
        else:
            user_state.current_agent = mode

        await session.commit()

    mode_names = {
        "fastbot": "⚡ FastBot",
        "planbot": "🧩 PlanBot",
        "skillbot": "🔧 SkillBot",
        "runtime": "⚙️ Runtime VM",
    }

    await callback.message.edit_text(
        f"✅ <b>Режим изменен на:</b> {mode_names.get(mode, mode)}\n\n"
        f"Отправьте мне сообщение, чтобы начать общение!"
    )
    await callback.answer()


@router.message(Command("skills"))
async def cmd_skills(message: Message):
    """Handle /skills command"""
    if not await check_access(message):
        return

    await message.answer(
        "🛠 <b>Менеджер навыков</b>\n\n"
        "Управляйте возможностями бота и создавайте новые навыки:",
        reply_markup=get_skills_menu_keyboard()
    )


@router.message(Command("memory"))
async def cmd_memory(message: Message):
    """Handle /memory command"""
    if not await check_access(message):
        return

    await message.answer(
        "🧠 <b>Менеджер памяти</b>\n\n"
        "Храните и управляйте вашими воспоминаниями, заметками и информацией:",
        reply_markup=get_memory_menu_keyboard()
    )


@router.message(Command("clear"))
async def cmd_clear(message: Message):
    """Handle /clear command"""
    if not await check_access(message):
        return

    await message.answer(
        "🧹 <b>История разговора очищена!</b>\n\n"
        "Я забыл наш недавний разговор. Давайте начнем сначала!"
    )


@router.message(Command("settings"))
async def cmd_settings(message: Message):
    """Handle /settings command"""
    if not await check_access(message):
        return

    await message.answer(
        "⚙️ <b>Настройки</b>\n\n"
        "Настройте параметры бота:",
        reply_markup=get_settings_keyboard()
    )


@router.message(F.text.in_(["💬 Чат", "🤖 Режим", "🛠 Навыки", "🧠 Память", "❓ Помощь", "⚙️ Настройки"]))
async def handle_menu_buttons(message: Message, state: FSMContext):
    """Handle menu button presses"""
    logger.info(f"Menu button pressed: {message.text} by user {message.from_user.id}")
    if not await check_access(message):
        logger.warning(f"Access denied for user {message.from_user.id}")
        return
    logger.info(f"Access granted, processing: {message.text}")

    text = message.text

    if text == "💬 Чат":
        await message.answer(
            "💬 <b>Режим чата</b>\n\n"
            "Просто отправьте мне любое сообщение, и я отвечу!\n"
            "Я могу отвечать на вопросы, помогать с задачами или просто общаться.",
            reply_markup=get_main_menu()
        )
    elif text == "🤖 Режим":
        await cmd_mode(message, state)
    elif text == "🛠 Навыки":
        await cmd_skills(message)
    elif text == "🧠 Память":
        await cmd_memory(message)
    elif text == "❓ Помощь":
        await cmd_help(message)
    elif text == "⚙️ Настройки":
        await cmd_settings(message)


@router.callback_query(F.data.startswith("settings:"))
async def handle_settings_callback(callback: CallbackQuery):
    """Handle settings inline button presses"""
    if not await check_access_callback(callback):
        return

    action = callback.data.split(":", 1)[1]  # agent / interface / notifications

    if action == "agent":
        await callback.message.edit_text(
            "🤖 <b>Смена агента</b>\n\nВыберите режим:",
            reply_markup=get_mode_keyboard()
        )
    elif action == "interface":
        await callback.message.edit_text(
            "🎨 <b>Интерфейс</b>\n\nНастройки интерфейса пока в разработке.",
            reply_markup=get_settings_keyboard()
        )
    elif action == "notifications":
        await callback.message.edit_text(
            "🔔 <b>Уведомления</b>\n\nНастройки уведомлений пока в разработке.",
            reply_markup=get_settings_keyboard()
        )
    else:
        await callback.answer(f"Неизвестное действие: {action}", show_alert=True)
        return

    await callback.answer()


@router.callback_query(F.data == "main:menu")
async def back_to_main(callback: CallbackQuery):
    """Handle back to main menu"""
    if not await check_access_callback(callback):
        return

    await callback.message.edit_text(
        "🏠 <b>Главное меню</b>\n\nЧто бы вы хотели сделать?"
    )
    await callback.answer()


# ------------------------------------------------------------------ #
#  /providers — управление LLM-провайдерами (только admin)            #
# ------------------------------------------------------------------ #

def _providers_menu_text(providers_info: list) -> str:
    """Формирует текст главного меню провайдеров."""
    status_icon = {"healthy": "🟢", "degraded": "🟡", "down": "🔴"}
    lines = ["⚙️ <b>LLM-провайдеры</b>\n"]
    for p in providers_info:
        icon = status_icon.get(p["status"], "⚪")
        lines.append(f"{icon} <b>{p['name']}</b> (приоритет {p['priority']})")
        for role, model in p["current_roles"].items():
            short = model.split("/")[-1] if "/" in model else model
            lines.append(f"   • {role}: <code>{short}</code>")
    return "\n".join(lines)


@router.message(Command("models"))
async def cmd_models(message: Message):
    """Главное меню управления моделями — только для admin"""
    if not await check_access(message):
        return

    from app.core.llm_client_v2 import llm_client
    info = llm_client.get_models_info()
    await message.answer(
        _providers_menu_text(info),
        reply_markup=get_providers_keyboard(info)
    )


@router.callback_query(F.data == "providers:menu")
async def providers_menu(callback: CallbackQuery):
    """Возврат в главное меню провайдеров"""
    if not await check_access_callback(callback):
        return

    from app.core.llm_client_v2 import llm_client
    info = llm_client.get_models_info()
    await callback.message.edit_text(
        _providers_menu_text(info),
        reply_markup=get_providers_keyboard(info)
    )
    await callback.answer()


@router.callback_query(F.data == "providers:refresh")
async def providers_refresh(callback: CallbackQuery):
    """Запустить health check всех провайдеров"""
    if not await check_access_callback(callback):
        return

    await callback.answer("🔄 Проверяю...", show_alert=False)

    from app.core.llm_client_v2 import llm_client
    await llm_client.check_health()
    info = llm_client.get_models_info()
    await callback.message.edit_text(
        _providers_menu_text(info),
        reply_markup=get_providers_keyboard(info)
    )


@router.callback_query(F.data.startswith("providers:show:"))
async def providers_show(callback: CallbackQuery):
    """Детальная карточка провайдера с текущими ролями"""
    if not await check_access_callback(callback):
        return

    provider_name = callback.data.split(":", 2)[2]

    from app.core.llm_client_v2 import llm_client
    info = llm_client.get_models_info()
    provider = next((p for p in info if p["name"] == provider_name), None)

    if not provider:
        await callback.answer("Провайдер не найден", show_alert=True)
        return

    status_icon = {"healthy": "🟢", "degraded": "🟡", "down": "🔴"}
    icon = status_icon.get(provider["status"], "⚪")

    lines = [f"{icon} <b>{provider_name}</b> — выберите роль для смены модели\n"]
    for role, model in provider["current_roles"].items():
        lines.append(f"• {role}: <code>{model}</code>")

    await callback.message.edit_text(
        "\n".join(lines),
        reply_markup=get_provider_detail_keyboard(
            provider_name, provider["models"], provider["current_roles"]
        )
    )
    await callback.answer()


@router.callback_query(F.data.startswith("providers:models:"))
async def providers_models(callback: CallbackQuery):
    """Список моделей провайдера для выбранной роли"""
    if not await check_access_callback(callback):
        return

    # providers:models:<provider>:<role>
    parts = callback.data.split(":")
    provider_name = parts[2]
    role = parts[3]

    from app.core.llm_client_v2 import llm_client
    info = llm_client.get_models_info()
    provider = next((p for p in info if p["name"] == provider_name), None)

    if not provider:
        await callback.answer("Провайдер не найден", show_alert=True)
        return

    current_model = provider["current_roles"].get(role, "")
    role_labels = {"default": "⚡ default", "coder": "💻 coder", "planner": "🧩 planner"}

    await callback.message.edit_text(
        f"🔧 <b>{provider_name}</b> → {role_labels.get(role, role)}\n\n"
        f"Текущая: <code>{current_model}</code>\n\n"
        f"Выберите модель:",
        reply_markup=get_provider_models_keyboard(
            provider_name, role, provider["models"], current_model
        )
    )
    await callback.answer()


@router.callback_query(F.data.startswith("providers:set:"))
async def providers_set(callback: CallbackQuery):
    """Применить выбор модели для роли"""
    if not await check_access_callback(callback):
        return

    # providers:set:<provider>:<role>:<idx>
    parts = callback.data.split(":")
    provider_name = parts[2]
    role = parts[3]
    try:
        idx = int(parts[4])
    except (IndexError, ValueError):
        await callback.answer("Некорректный индекс", show_alert=True)
        return

    from app.core.llm_client_v2 import llm_client
    ok = llm_client.set_model(provider_name, role, idx)

    if not ok:
        await callback.answer("❌ Ошибка: индекс вне диапазона", show_alert=True)
        return

    # Персистируем выбор в БД (один атомарный upsert)
    applied_model_id = llm_client.get_assigned_model(provider_name, role)
    if applied_model_id:
        async with async_session_maker() as session:
            from app.core.database import get_provider_config, upsert_provider_config
            cfg = await get_provider_config(session, provider_name)
            existing = dict(cfg.role_models) if cfg and cfg.role_models else {}
            existing[role] = applied_model_id
            await upsert_provider_config(
                session=session,
                name=provider_name,
                role_models=existing,
                updated_by=callback.from_user.id,
            )
            logger.info(f"DB Persist: {provider_name}.{role} = {applied_model_id}")

    # Читаем применённую модель для подтверждения
    info = llm_client.get_models_info()
    provider = next((p for p in info if p["name"] == provider_name), None)
    applied = provider["current_roles"].get(role, "?") if provider else "?"

    await callback.answer(f"✅ {role} → {applied.split('/')[-1]}", show_alert=False)

    # Возвращаемся на карточку провайдера
    if provider:
        status_icon = {"healthy": "🟢", "degraded": "🟡", "down": "🔴"}
        icon = status_icon.get(provider["status"], "⚪")
        lines = [f"{icon} <b>{provider_name}</b> — выберите роль для смены модели\n"]
        for r, m in provider["current_roles"].items():
            lines.append(f"• {r}: <code>{m}</code>")
        await callback.message.edit_text(
            "\n".join(lines),
            reply_markup=get_provider_detail_keyboard(
                provider_name, provider["models"], provider["current_roles"]
            )
        )


async def get_user_agent_mode(user_id: int) -> str:
    """Get user's current agent mode (user_id — Telegram ID)"""
    async with async_session_maker() as session:
        from sqlalchemy import select

        from app.core.database import UserState

        # UserState.user_id — FK на users.id, поэтому резолвим internal id
        db_user = await get_or_create_user(session, telegram_id=user_id)
        result = await session.execute(
            select(UserState).where(UserState.user_id == db_user.id)
        )
        user_state = result.scalar_one_or_none()

        return user_state.current_agent if user_state else "fastbot"


@router.callback_query(F.data.startswith("prov:select:"))
async def prov_select_compat(callback: CallbackQuery):
    """Compat: старые сообщения prov:select:<name> → карточка провайдера"""
    if not await check_access_callback(callback):
        return
    provider_name = callback.data.split(":", 2)[2]
    from app.core.llm_client_v2 import llm_client
    info = llm_client.get_models_info()
    provider = next((p for p in info if p["name"] == provider_name), None)
    if not provider:
        await callback.answer("Провайдер не найден", show_alert=True)
        return
    status_icon = {"healthy": "🟢", "degraded": "🟡", "down": "🔴"}
    icon = status_icon.get(provider["status"], "⚪")
    lines = [f"{icon} <b>{provider_name}</b> — выберите роль для смены модели\n"]
    for role, model in provider["current_roles"].items():
        lines.append(f"• {role}: <code>{model}</code>")
    await callback.message.edit_text(
        "\n".join(lines),
        reply_markup=get_provider_detail_keyboard(
            provider_name, provider["models"], provider["current_roles"]
        )
    )
    await callback.answer()


@router.callback_query(F.data == "prov:close")
async def prov_close_compat(callback: CallbackQuery):
    """Compat: старые сообщения prov:close → удалить сообщение"""
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.answer()


@router.callback_query(F.data == "prov:refresh")
async def prov_refresh_compat(callback: CallbackQuery):
    """Compat: старые сообщения prov:refresh → health check"""
    if not await check_access_callback(callback):
        return
    await callback.answer("🔄 Проверяю...", show_alert=False)
    from app.core.llm_client_v2 import llm_client
    await llm_client.check_health()
    info = llm_client.get_models_info()
    await callback.message.edit_text(
        _providers_menu_text(info),
        reply_markup=get_providers_keyboard(info)
    )
