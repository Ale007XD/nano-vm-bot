"""
Keyboards and UI components
"""
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.core.skills_loader import SkillInfo


def get_main_menu() -> ReplyKeyboardMarkup:
    """Main menu keyboard"""
    keyboard = [
        [KeyboardButton(text="💬 Чат"), KeyboardButton(text="🤖 Режим")],
        [KeyboardButton(text="🛠 Навыки"), KeyboardButton(text="🧠 Память")],
        [KeyboardButton(text="❓ Помощь"), KeyboardButton(text="⚙️ Настройки")]
    ]
    return ReplyKeyboardMarkup(
        keyboard=keyboard,
        resize_keyboard=True,
        input_field_placeholder="Выберите опцию..."
    )


def get_mode_keyboard() -> InlineKeyboardMarkup:
    """Agent mode selection keyboard"""
    builder = InlineKeyboardBuilder()
    
    builder.button(text="⚡ FastBot", callback_data="mode:fastbot")
    builder.button(text="🧩 PlanBot", callback_data="mode:planbot")
    builder.button(text="🔧 SkillBot", callback_data="mode:skillbot")
    builder.button(text="⚙️ Runtime VM", callback_data="mode:runtime")

    builder.adjust(2)
    return builder.as_markup()


def get_skills_menu_keyboard() -> InlineKeyboardMarkup:
    """Skills menu keyboard"""
    builder = InlineKeyboardBuilder()
    
    builder.button(text="📋 Список навыков", callback_data="skills:list")
    builder.button(text="➕ Создать навык", callback_data="skills:create")
    builder.button(text="🔍 Поиск навыков", callback_data="skills:search")
    builder.button(text="📥 Импорт навыка", callback_data="skills:import")
    builder.button(text="🔙 Назад", callback_data="main:menu")
    
    builder.adjust(2, 2, 1)
    return builder.as_markup()


def get_skills_list_keyboard(
    skills: list[SkillInfo],
    page: int = 0,
    per_page: int = 5
) -> InlineKeyboardMarkup:
    """Skills list with pagination"""
    builder = InlineKeyboardBuilder()
    
    start = page * per_page
    end = start + per_page
    page_skills = skills[start:end]
    
    for skill in page_skills:
        status = "✅" if skill.is_active else "❌"
        builder.button(
            text=f"{status} {skill.name} ({skill.category})",
            callback_data=f"skill:view:{skill.name}"
        )
    
    # Pagination buttons
    nav_buttons = []
    if page > 0:
        nav_buttons.append(
            InlineKeyboardButton(
                text="⬅️ Назад",
                callback_data=f"skills:page:{page-1}"
            )
        )
    if end < len(skills):
        nav_buttons.append(
            InlineKeyboardButton(
                text="Вперед ➡️",
                callback_data=f"skills:page:{page+1}"
            )
        )
    
    if nav_buttons:
        builder.row(*nav_buttons)
    
    builder.button(text="🔙 Назад", callback_data="skills:menu")
    
    builder.adjust(1)
    return builder.as_markup()


def get_skill_detail_keyboard(skill_name: str, source: str) -> InlineKeyboardMarkup:
    """Skill detail actions"""
    builder = InlineKeyboardBuilder()
    
    builder.button(text="▶️ Запустить", callback_data=f"skill:run:{skill_name}")
    builder.button(text="📄 Код", callback_data=f"skill:code:{skill_name}")
    builder.button(text="✏️ Изменить", callback_data=f"skill:edit:{skill_name}")
    
    if source != "system":
        builder.button(
            text="🗑 Удалить",
            callback_data=f"skill:delete:{skill_name}"
        )
    
    builder.button(text="🔙 Назад", callback_data="skills:list")
    
    builder.adjust(2, 2, 1)
    return builder.as_markup()


def get_memory_menu_keyboard() -> InlineKeyboardMarkup:
    """Memory operations menu"""
    builder = InlineKeyboardBuilder()
    
    builder.button(text="📝 Добавить заметку", callback_data="memory:add:note")
    builder.button(text="✈️ Добавить поездку", callback_data="memory:add:trip")
    builder.button(text="💰 Добавить бюджет", callback_data="memory:add:budget")
    builder.button(text="📅 Добавить план", callback_data="memory:add:plan")
    builder.button(text="🔍 Поиск", callback_data="memory:search")
    builder.button(text="📊 Статистика", callback_data="memory:stats")
    builder.button(text="🔙 Назад", callback_data="main:menu")
    
    builder.adjust(2, 2, 2, 1)
    return builder.as_markup()


def get_confirmation_keyboard(confirm_data: str, cancel_data: str) -> InlineKeyboardMarkup:
    """Yes/No confirmation"""
    builder = InlineKeyboardBuilder()
    
    builder.button(text="✅ Да", callback_data=confirm_data)
    builder.button(text="❌ Нет", callback_data=cancel_data)
    
    return builder.as_markup()


def get_cancel_keyboard() -> InlineKeyboardMarkup:
    """Cancel operation keyboard"""
    builder = InlineKeyboardBuilder()
    builder.button(text="❌ Отмена", callback_data="action:cancel")
    return builder.as_markup()


def get_settings_keyboard() -> InlineKeyboardMarkup:
    """Settings menu"""
    builder = InlineKeyboardBuilder()
    
    builder.button(text="🤖 Сменить агента", callback_data="settings:agent")
    builder.button(text="🎨 Интерфейс", callback_data="settings:interface")
    builder.button(text="🔔 Уведомления", callback_data="settings:notifications")
    builder.button(text="🔙 Назад", callback_data="main:menu")
    
    builder.adjust(1)
    return builder.as_markup()


def get_providers_keyboard(providers_info: list) -> InlineKeyboardMarkup:
    """
    Главное меню /providers.
    providers_info — список dict из llm_client.get_models_info().
    """
    builder = InlineKeyboardBuilder()

    status_icon = {
        "healthy": "🟢",
        "degraded": "🟡",
        "down": "🔴",
    }

    for p in providers_info:
        icon = status_icon.get(p["status"], "⚪")
        builder.button(
            text=f"{icon} {p['name']} (p{p['priority']})",
            callback_data=f"providers:show:{p['name']}"
        )

    builder.button(text="🔄 Проверить здоровье", callback_data="providers:refresh")
    builder.adjust(1)
    return builder.as_markup()


def get_provider_detail_keyboard(provider_name: str, models: list, current_roles: dict) -> InlineKeyboardMarkup:
    """
    Детальное меню провайдера: кнопки выбора роли для назначения модели.
    current_roles = {"default": "...", "coder": "...", "planner": "..."}
    """
    builder = InlineKeyboardBuilder()

    role_labels = {
        "default": "⚡ default",
        "coder":   "💻 coder",
        "planner": "🧩 planner",
    }

    for role, label in role_labels.items():
        current = current_roles.get(role, "—")
        # Показываем только имя модели без vendor-префикса для экономии места
        short = current.split("/")[-1] if "/" in current else current
        builder.button(
            text=f"{label}: {short}",
            callback_data=f"providers:models:{provider_name}:{role}"
        )

    builder.button(text="🔙 Назад", callback_data=f"providers:show:{provider_name}")
    builder.adjust(1)
    return builder.as_markup()


def get_provider_models_keyboard(provider_name: str, role: str, models: list, current_model: str) -> InlineKeyboardMarkup:
    """
    Список моделей провайдера для выбора роли.
    Использует порядковый индекс в callback_data — избегает / и : в именах моделей.
    """
    builder = InlineKeyboardBuilder()

    for idx, model_id in enumerate(models):
        is_current = (model_id == current_model)
        short = model_id.split("/")[-1] if "/" in model_id else model_id
        prefix = "✅ " if is_current else ""
        builder.button(
            text=f"{prefix}{short}",
            callback_data=f"providers:set:{provider_name}:{role}:{idx}"
        )

    builder.button(
        text="🔙 Назад",
        callback_data=f"providers:show:{provider_name}"
    )
    builder.adjust(1)
    return builder.as_markup()
