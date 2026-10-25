"""
Reminder and scheduled task handlers
"""
from aiogram import Router
from aiogram.types import Message
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext

from app.core.scheduler import task_scheduler
from app.core.config import settings
from app.handlers.commands import get_allowed_users
from app.utils.states import ReminderStates
from app.utils.helpers import parse_time_input
import logging

logger = logging.getLogger(__name__)

router = Router()


@router.message(Command("remind"))
async def cmd_remind(message: Message, state: FSMContext):
    """Create a one-time reminder"""
    if message.from_user.id not in get_allowed_users():
        await message.answer("⛔ Доступ запрещен")
        return
    
    await message.answer(
        "⏰ <b>Создание напоминания</b>\n\n"
        "Введите текст напоминания:\n"
        "<i>Например: Позвонить маме, записаться к врачу, купить молоко</i>"
    )
    await state.set_state(ReminderStates.waiting_for_description)
    await state.update_data(reminder_type="one_time")


@router.message(ReminderStates.waiting_for_description)
async def process_reminder_description(message: Message, state: FSMContext):
    """Process reminder description"""
    description = message.text
    await state.update_data(description=description)
    
    await message.answer(
        "🕐 <b>Когда напомнить?</b>\n\n"
        "Введите время в формате:\n"
        "• <code>через 30 минут</code>\n"
        "• <code>через 2 часа</code>\n"
        "• <code>завтра в 9:00</code>\n"
        "• <code>2024-12-25 10:00</code>\n\n"
        "Или отмените: /cancel"
    )
    await state.set_state(ReminderStates.waiting_for_time)


@router.message(ReminderStates.waiting_for_time)
async def process_reminder_time(message: Message, state: FSMContext):
    """Process reminder time"""
    time_input = message.text.lower().strip()
    data = await state.get_data()
    description = data.get("description", "")
    reminder_type = data.get("reminder_type", "one_time")

    try:
        run_date = parse_time_input(time_input, settings.BOT_TIMEZONE)

        if not run_date:
            await message.answer(
                "❌ <b>Не удалось распознать время</b>\n\n"
                "Попробуйте еще раз:\n"
                "• через 30 минут\n"
                "• завтра в 9:00\n"
                "• 2024-12-25 15:30"
            )
            return

        if reminder_type == "daily":
            task = await task_scheduler.create_daily_task(
                user_id=message.from_user.id,
                telegram_id=message.from_user.id,
                description=description,
                hour=run_date.hour,
                minute=run_date.minute,
                message_text=description
            )
            time_str = f"ежедневно в {run_date.strftime('%H:%M')}"
        elif reminder_type == "weekly":
            task = await task_scheduler.create_weekly_task(
                user_id=message.from_user.id,
                telegram_id=message.from_user.id,
                description=description,
                day_of_week=run_date.weekday(),
                hour=run_date.hour,
                minute=run_date.minute,
                message_text=description
            )
            days_ru = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
            time_str = f"еженедельно {days_ru[run_date.weekday()]} в {run_date.strftime('%H:%M')}"
        else:
            task = await task_scheduler.create_reminder(
                user_id=message.from_user.id,
                telegram_id=message.from_user.id,
                description=description,
                run_date=run_date,
                message_text=description
            )
            time_str = run_date.strftime("%d.%m.%Y %H:%M")

        await message.answer(
            f"✅ <b>Напоминание создано!</b>\n\n"
            f"📝 {description}\n"
            f"🕐 {time_str}\n"
            f"📋 ID: <code>{task.id}</code>\n\n"
            f"Отменить: <code>/cancel_task {task.id}</code>"
        )

        await state.clear()

    except Exception as e:
        logger.error(f"Error creating reminder: {e}")
        await message.answer(
            "❌ <b>Ошибка создания напоминания</b>\n\n"
            "Попробуйте еще раз или отмените: /cancel"
        )


@router.message(Command("tasks"))
async def cmd_tasks(message: Message):
    """Show user's scheduled tasks"""
    if message.from_user.id not in get_allowed_users():
        await message.answer("⛔ Доступ запрещен")
        return
    
    try:
        tasks = await task_scheduler.get_user_tasks(message.from_user.id)
        
        if not tasks:
            await message.answer(
                "📋 <b>У вас нет активных задач</b>\n\n"
                "Создать:\n"
                "• /remind - напоминание\n"
                "• /daily - ежедневная задача\n"
                "• /weekly - еженедельная задача"
            )
            return
        
        response = "📋 <b>Ваши задачи:</b>\n\n"
        
        for task in tasks:
            # Format time
            if task.next_run_at:
                time_str = task.next_run_at.strftime("%d.%m %H:%M")
            elif task.run_date:
                time_str = task.run_date.strftime("%d.%m %H:%M")
            else:
                time_str = "По расписанию"
            
            # Type emoji
            type_emoji = {
                "one_time": "⏰",
                "recurring": "🔄",
                "interval": "⏱️"
            }.get(task.task_type, "📌")
            
            response += (
                f"{type_emoji} <b>{task.name}</b>\n"
                f"   {task.description[:50]}{'...' if len(task.description) > 50 else ''}\n"
                f"   🕐 {time_str}\n"
                f"   Отменить: <code>/cancel_task {task.id}</code>\n\n"
            )
        
        await message.answer(response)
        
    except Exception as e:
        logger.error(f"Error getting tasks: {e}")
        await message.answer("❌ Ошибка получения списка задач")


@router.message(Command("cancel_task"))
async def cmd_cancel_task(message: Message):
    """Cancel a specific task"""
    if message.from_user.id not in get_allowed_users():
        await message.answer("⛔ Доступ запрещен")
        return
    
    # Parse task ID from command
    args = message.text.split()
    if len(args) < 2:
        await message.answer(
            "❌ <b>Укажите ID задачи</b>\n\n"
            "Пример: <code>/cancel_task 123</code>\n\n"
            "Посмотреть ID: /tasks"
        )
        return
    
    try:
        task_id = int(args[1])
        success = await task_scheduler.cancel_task(task_id, message.from_user.id)
        
        if success:
            await message.answer(f"✅ Задача <code>{task_id}</code> отменена")
        else:
            await message.answer(
                f"❌ Задача <code>{task_id}</code> не найдена\n\n"
                "Проверьте ID: /tasks"
            )
            
    except ValueError:
        await message.answer("❌ Неверный ID задачи. Укажите число.")
    except Exception as e:
        logger.error(f"Error cancelling task: {e}")
        await message.answer("❌ Ошибка отмены задачи")


@router.message(Command("daily"))
async def cmd_daily(message: Message, state: FSMContext):
    """Create daily recurring task"""
    if message.from_user.id not in get_allowed_users():
        await message.answer("⛔ Доступ запрещен")
        return
    
    await message.answer(
        "🔄 <b>Ежедневная задача</b>\n\n"
        "Введите описание задачи:\n"
        "<i>Например: Проверить почту, сделать зарядку</i>"
    )
    await state.set_state(ReminderStates.waiting_for_description)
    await state.update_data(reminder_type="daily")


@router.message(Command("weekly"))
async def cmd_weekly(message: Message, state: FSMContext):
    """Create weekly recurring task"""
    if message.from_user.id not in get_allowed_users():
        await message.answer("⛔ Доступ запрещен")
        return
    
    await message.answer(
        "📅 <b>Еженедельная задача</b>\n\n"
        "Введите описание задачи:\n"
        "<i>Например: Написать отчет, проверить бюджет</i>"
    )
    await state.set_state(ReminderStates.waiting_for_description)
    await state.update_data(reminder_type="weekly")


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    """Cancel current operation"""
    await state.clear()
    await message.answer("❌ Действие отменено")


@router.message(Command("scheduler_stats"))
async def cmd_scheduler_stats(message: Message):
    """Show scheduler statistics (admin only)"""
    if message.from_user.id not in get_allowed_users():
        await message.answer("⛔ Доступ запрещен")
        return
    
    try:
        stats = await task_scheduler.get_task_stats()
        
        response = (
            "📊 <b>Статистика планировщика</b>\n\n"
            f"Активных задач: <b>{stats['total_active']}</b>\n"
            f"Выполнено: <b>{stats['total_completed']}</b>\n"
            f"Запланировано в APScheduler: <b>{stats['scheduled_jobs']}</b>\n\n"
        )
        
        if stats['by_type']:
            response += "<b>По типам:</b>\n"
            for task_type, count in stats['by_type'].items():
                type_name = {
                    'one_time': '⏰ Разовые',
                    'recurring': '🔄 Повторяющиеся',
                    'interval': '⏱️ Интервальные'
                }.get(task_type, task_type)
                response += f"  {type_name}: {count}\n"
        
        await message.answer(response)
        
    except Exception as e:
        logger.error(f"Error getting scheduler stats: {e}")
        await message.answer("❌ Ошибка получения статистики")


