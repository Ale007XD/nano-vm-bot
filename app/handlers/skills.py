"""
Skills management handlers
"""
import html

from aiogram import Router, F
from aiogram.types import CallbackQuery, Message
from aiogram.fsm.context import FSMContext

from app.core.skills_loader import skill_loader
from app.core.memory import vector_memory
from app.utils.keyboards import (
    get_skills_menu_keyboard, get_skills_list_keyboard,
    get_skill_detail_keyboard, get_confirmation_keyboard, get_cancel_keyboard
)
from app.utils.states import SkillCreation, SkillEdit
from app.utils.helpers import format_skill_info, is_valid_skill_name
from app.handlers.commands import get_allowed_users, check_access_callback as check_callback_access
import logging

logger = logging.getLogger(__name__)

router = Router()


@router.callback_query(F.data == "skills:menu")
async def skills_menu(callback: CallbackQuery):
    """Show skills menu"""
    if not await check_callback_access(callback):
        return
    await callback.message.edit_text(
        "🛠 <b>Менеджер навыков</b>\n\n"
        "Управляйте возможностями бота:",
        reply_markup=get_skills_menu_keyboard()
    )
    await callback.answer()


@router.callback_query(F.data == "skills:list")
async def skills_list(callback: CallbackQuery):
    """List all skills"""
    skills = skill_loader.list_skills()
    
    if not skills:
        await callback.message.edit_text(
            "📭 <b>Нет установленных навыков</b>\n\n"
            "Создайте новый навык или импортируйте один!",
            reply_markup=get_skills_menu_keyboard()
        )
    else:
        await callback.message.edit_text(
            f"📋 <b>Установленные навыки ({len(skills)})</b>\n\n"
            f"Выберите навык для просмотра деталей:",
            reply_markup=get_skills_list_keyboard(skills, page=0)
        )
    await callback.answer()


@router.callback_query(F.data.startswith("skills:page:"))
async def skills_page(callback: CallbackQuery):
    """Handle pagination"""
    page = int(callback.data.split(":")[2])
    skills = skill_loader.list_skills()
    
    await callback.message.edit_text(
        f"📋 <b>Установленные навыки ({len(skills)})</b>\n\n"
        f"Выберите навык для просмотра деталей:",
        reply_markup=get_skills_list_keyboard(skills, page=page)
    )
    await callback.answer()


@router.callback_query(F.data.startswith("skill:view:"))
async def skill_detail(callback: CallbackQuery):
    """Show skill details"""
    skill_name = callback.data.split(":")[2]
    skill_info = skill_loader.get_skill_info(skill_name)
    
    if not skill_info:
        await callback.answer("Навык не найден!", show_alert=True)
        return
    
    text = format_skill_info(skill_info)
    
    await callback.message.edit_text(
        text,
        reply_markup=get_skill_detail_keyboard(skill_name, skill_info.source)
    )
    await callback.answer()


@router.callback_query(F.data.startswith("skill:code:"))
async def skill_code(callback: CallbackQuery):
    """Show skill code"""
    skill_name = callback.data.split(":")[2]
    
    code = await skill_loader.get_skill_code(skill_name)
    if not code:
        await callback.answer("Не удалось получить код!", show_alert=True)
        return
    
    # Truncate if too long
    if len(code) > 3500:
        code = code[:3500] + "\n\n... (код обрезан)"

    try:
        await callback.message.edit_text(
            f"📄 <b>Код навыка: {skill_name}</b>\n\n"
            f"<pre>{html.escape(code)}</pre>",
            reply_markup=get_skill_detail_keyboard(skill_name, "custom")
        )
    finally:
        await callback.answer()


@router.callback_query(F.data.startswith("skill:delete:"))
async def skill_delete_confirm(callback: CallbackQuery):
    """Confirm skill deletion"""
    skill_name = callback.data.split(":")[2]
    
    await callback.message.edit_text(
        f"🗑 <b>Удалить навык?</b>\n\n"
        f"Вы уверены, что хотите удалить <b>{skill_name}</b>?\n\n"
        f"Это действие нельзя отменить!",
        reply_markup=get_confirmation_keyboard(
            f"skill:confirm_delete:{skill_name}",
            f"skill:view:{skill_name}"
        )
    )
    await callback.answer()


@router.callback_query(F.data.startswith("skill:confirm_delete:"))
async def skill_delete(callback: CallbackQuery):
    """Delete skill"""
    skill_name = callback.data.split(":")[3]
    
    try:
        await skill_loader.delete_skill(skill_name)
        await callback.message.edit_text(
            f"✅ <b>Навык '{skill_name}' успешно удален!</b>",
            reply_markup=get_skills_menu_keyboard()
        )
    except Exception as e:
        logger.error(f"Error deleting skill: {e}")
        await callback.message.edit_text(
            f"❌ <b>Ошибка удаления навыка:</b> {str(e)}",
            reply_markup=get_skills_menu_keyboard()
        )
    await callback.answer()


@router.callback_query(F.data == "skills:create")
async def skill_create_start(callback: CallbackQuery, state: FSMContext):
    """Start skill creation"""
    await state.set_state(SkillCreation.waiting_name)
    await callback.message.edit_text(
        "➕ <b>Создать новый навык</b>\n\n"
        "Шаг 1/3: Введите имя для вашего навыка\n\n"
        "<i>Требования:</i>\n"
        "• Только буквы, цифры и подчеркивания\n"
        "• Должно начинаться с буквы\n"
        "• Пример: weather_checker",
        reply_markup=get_cancel_keyboard()
    )
    await callback.answer()


@router.message(SkillCreation.waiting_name)
async def skill_create_name(message: Message, state: FSMContext):
    """Process skill name"""
    name = message.text.strip()
    
    if not is_valid_skill_name(name):
        await message.answer(
            "❌ <b>Недопустимое имя навыка!</b>\n\n"
            "Используйте только буквы, цифры и подчеркивания.\n"
            "Должно начинаться с буквы.\n\n"
            "Попробуйте снова:",
            reply_markup=get_cancel_keyboard()
        )
        return
    
    await state.update_data(skill_name=name)
    await state.set_state(SkillCreation.waiting_description)
    
    await message.answer(
        "✅ <b>Имя сохранено!</b>\n\n"
        "Шаг 2/3: Введите описание для вашего навыка\n\n"
        "<i>Что делает этот навык?</i>\n"
        "Пример: Проверяет погоду для указанного города",
        reply_markup=get_cancel_keyboard()
    )


@router.message(SkillCreation.waiting_description)
async def skill_create_description(message: Message, state: FSMContext):
    """Process skill description"""
    description = message.text.strip()
    
    await state.update_data(skill_description=description)
    await state.set_state(SkillCreation.waiting_code)
    
    await message.answer(
        "✅ <b>Описание сохранено!</b>\n\n"
        "Шаг 3/3: Введите Python код для вашего навыка\n\n"
        "<i>Советы:</i>\n"
        "• Используйте async/await для асинхронных функций\n"
        "• Включите переменные SKILL_NAME и SKILL_DESCRIPTION\n"
        "• Определите основную функцию для обработки команд\n\n"
        "Отправьте код сейчас:",
        reply_markup=get_cancel_keyboard()
    )


@router.message(SkillCreation.waiting_code)
async def skill_create_code(message: Message, state: FSMContext):
    """Process skill code and create skill"""
    code = message.text
    
    data = await state.get_data()
    name = data['skill_name']
    description = data['skill_description']
    
    try:
        # Create skill
        skill_info = await skill_loader.create_skill(
            name=name,
            description=description,
            code=code,
            category="custom",
            author=message.from_user.username or str(message.from_user.id)
        )
        
        await message.answer(
            f"✅ <b>Навык '{name}' успешно создан!</b>\n\n"
            f"{format_skill_info(skill_info)}\n\n"
            f"Теперь вы можете использовать этот навык!",
            reply_markup=get_skills_menu_keyboard()
        )
        
    except Exception as e:
        logger.error(f"Error creating skill: {e}")
        await message.answer(
            f"❌ <b>Ошибка создания навыка:</b>\n{str(e)}\n\n"
            f"Пожалуйста, проверьте код и попробуйте снова.",
            reply_markup=get_skills_menu_keyboard()
        )
    
    await state.clear()


@router.callback_query(F.data.startswith("skill:run:"))
async def skill_run(callback: CallbackQuery):
    """Run a skill"""
    skill_name = callback.data.split(":")[2]
    skill_info = skill_loader.get_skill_info(skill_name)

    if not skill_info:
        await callback.answer("Навык не найден!", show_alert=True)
        return

    await callback.answer()
    await callback.message.answer(f"⚙️ Запускаю навык <b>{skill_name}</b>...")

    try:
        skill = skill_loader.get_skill(skill_name)
        if not skill:
            await callback.message.answer(f"❌ Навык <b>{skill_name}</b> не загружен.")
            return

        context = {
            "user_id": callback.from_user.id,
            "args": {},
            "message": None,
        }

        if hasattr(skill, "run"):
            result = await skill.run(context)
        elif callable(skill):
            result = await skill(context)
        else:
            result = "❓ Навык не имеет функции запуска."

        await callback.message.answer(
            f"✅ <b>{skill_name}</b>:\n\n{result}"
        )

    except Exception as e:
        logger.error(f"Error running skill {skill_name}: {e}")
        await callback.message.answer(
            f"❌ Ошибка при запуске навыка <b>{skill_name}</b>:\n<code>{e}</code>"
        )


@router.callback_query(F.data.startswith("skill:edit:"))
async def skill_edit_start(callback: CallbackQuery, state: FSMContext):
    """Start skill editing"""
    skill_name = callback.data.split(":")[2]
    skill_info = skill_loader.get_skill_info(skill_name)

    if not skill_info:
        await callback.answer("Навык не найден!", show_alert=True)
        return

    if skill_info.source == "system":
        await callback.answer("⛔ Системные навыки нельзя редактировать!", show_alert=True)
        return

    await state.set_state(SkillEdit.editing_code)
    await state.update_data(skill_name=skill_name)

    code = await skill_loader.get_skill_code(skill_name) or ""
    if len(code) > 3500:
        code = code[:3500]

    try:
        await callback.message.edit_text(
            f"✏️ <b>Редактирование навыка: {skill_name}</b>\n\n"
            f"Отправьте новый код навыка:\n\n"
            f"<pre>{html.escape(code)}</pre>",
            reply_markup=get_cancel_keyboard()
        )
    finally:
        await callback.answer()


@router.message(SkillEdit.editing_code)
async def skill_edit_save(message: Message, state: FSMContext):
    """Save edited skill code"""
    data = await state.get_data()
    skill_name = data["skill_name"]
    new_code = message.text

    try:
        skill_info = skill_loader.get_skill_info(skill_name)
        await skill_loader.create_skill(
            name=skill_name,
            description=skill_info.description if skill_info else "",
            code=new_code,
            category="custom",
            author=message.from_user.username or str(message.from_user.id)
        )
        await message.answer(
            f"✅ <b>Навык '{skill_name}' обновлён!</b>",
            reply_markup=get_skills_menu_keyboard()
        )
    except Exception as e:
        logger.error(f"Error editing skill {skill_name}: {e}")
        await message.answer(
            f"❌ Ошибка сохранения:\n<code>{e}</code>",
            reply_markup=get_skills_menu_keyboard()
        )

    await state.clear()


@router.callback_query(F.data == "skills:search")
async def skill_search_start(callback: CallbackQuery):
    """Start skill search"""
    await callback.message.edit_text(
        "🔍 <b>Поиск навыков</b>\n\n"
        "Эта функция интегрирована в чат!\n\n"
        "Переключитесь в режим 🔧 <b>SkillBot</b> и попросите меня найти навыки,\n"
        "или используйте меню Навыки для просмотра.",
        reply_markup=get_skills_menu_keyboard()
    )
    await callback.answer()


@router.callback_query(F.data == "skills:import")
async def skill_import_info(callback: CallbackQuery):
    """Show import instructions"""
    await callback.message.edit_text(
        "📥 <b>Импорт навыков</b>\n\n"
        "Для импорта внешних навыков:\n\n"
        "1. Поместите файлы навыков в директорию <code>skills/external/</code>\n"
        "2. Перезапустите бота, или\n"
        "3. Используйте функцию создания навыка и вставьте код\n\n"
        "<i>Примечание:</i> Импортируйте навыки только из доверенных источников!",
        reply_markup=get_skills_menu_keyboard()
    )
    await callback.answer()


@router.callback_query(F.data == "action:cancel")
async def cancel_action(callback: CallbackQuery, state: FSMContext):
    """Cancel current operation"""
    from app.utils.states import MemoryAdd, MemorySearch
    current_state = await state.get_state()

    if current_state:
        # Определяем в какое меню вернуться по типу FSM
        if current_state in (MemoryAdd.waiting_content, MemorySearch.waiting_query):
            from app.utils.keyboards import get_memory_menu_keyboard
            await state.clear()
            await callback.message.edit_text(
                "❌ <b>Операция отменена</b>",
                reply_markup=get_memory_menu_keyboard()
            )
        else:
            await state.clear()
            await callback.message.edit_text(
                "❌ <b>Операция отменена</b>",
                reply_markup=get_skills_menu_keyboard()
            )
    else:
        await callback.message.edit_text(
            "🛠 <b>Менеджер навыков</b>",
            reply_markup=get_skills_menu_keyboard()
        )
    await callback.answer()
