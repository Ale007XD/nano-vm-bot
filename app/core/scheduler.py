"""
Task Scheduler using APScheduler
Handles reminders, recurring tasks, and scheduled messages
"""
import asyncio
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import and_, select, update

from app.agents.router import agent_router
from app.core.config import settings
from app.core.database import ScheduledTask, async_session_maker, get_or_create_user

logger = logging.getLogger(__name__)


class TaskScheduler:
    """APScheduler wrapper for managing scheduled tasks"""
    
    def __init__(self):
        self.scheduler = AsyncIOScheduler(timezone=pytz.timezone(settings.BOT_TIMEZONE))
        self._bot = None  # Will be set on startup
        self._is_running = False
    
    def set_bot(self, bot):
        """Set bot instance for sending messages"""
        self._bot = bot
    
    def start(self):
        """Start the scheduler"""
        if not self._is_running:
            self.scheduler.start()
            self._is_running = True
            logger.info("Task scheduler started")
            
            # Load and schedule existing tasks from database
            asyncio.create_task(self._load_existing_tasks())
    
    def shutdown(self):
        """Shutdown the scheduler"""
        if self._is_running:
            self.scheduler.shutdown()
            self._is_running = False
            logger.info("Task scheduler shutdown")
    
    async def _load_existing_tasks(self):
        """Load active tasks from database on startup"""
        try:
            async with async_session_maker() as session:
                result = await session.execute(
                    select(ScheduledTask).where(
                        and_(
                            ScheduledTask.is_active.is_(True),
                            ScheduledTask.is_completed.is_(False)
                        )
                    )
                )
                tasks = result.scalars().all()
                
                for task in tasks:
                    await self._schedule_task(task)
                
                logger.info(f"Loaded and scheduled {len(tasks)} existing tasks")
        except Exception as e:
            logger.error(f"Error loading existing tasks: {e}")
    
    async def _schedule_task(self, task: ScheduledTask):
        """Schedule a task in APScheduler"""
        try:
            job_id = f"task_{task.id}"
            
            # Remove existing job if any
            if self.scheduler.get_job(job_id):
                self.scheduler.remove_job(job_id)
            
            if task.task_type == "one_time":
                # One-time task at specific date
                if task.run_date:
                    run_dt = task.run_date
                    if run_dt.tzinfo is None:
                        # SQLite возвращает naive datetime в wall-time зоны задачи
                        run_dt = pytz.timezone(task.timezone or settings.BOT_TIMEZONE).localize(run_dt)
                    if run_dt > datetime.now(pytz.UTC):
                        trigger = DateTrigger(run_date=run_dt)
                        self.scheduler.add_job(
                            func=self._execute_task,
                            trigger=trigger,
                            id=job_id,
                            args=[task.id],
                            replace_existing=True
                        )
                        logger.info(f"Scheduled one-time task {task.id} for {task.run_date}")
                    else:
                        logger.warning(f"Task {task.id} has past date, marking completed")
                        await self._mark_task_completed(task.id)
                    
            elif task.task_type == "recurring":
                # Recurring task with cron expression
                if task.cron_expression:
                    trigger = CronTrigger.from_crontab(
                        task.cron_expression,
                        timezone=pytz.timezone(task.timezone or "UTC")
                    )
                    self.scheduler.add_job(
                        func=self._execute_task,
                        trigger=trigger,
                        id=job_id,
                        args=[task.id],
                        replace_existing=True
                    )
                    logger.info(f"Scheduled recurring task {task.id} with cron: {task.cron_expression}")
                    
            elif task.task_type == "interval":
                # Interval-based task
                if task.extra_data and "interval_minutes" in task.extra_data:
                    minutes = task.extra_data["interval_minutes"]
                    trigger = IntervalTrigger(minutes=minutes)
                    self.scheduler.add_job(
                        func=self._execute_task,
                        trigger=trigger,
                        id=job_id,
                        args=[task.id],
                        replace_existing=True
                    )
                    logger.info(f"Scheduled interval task {task.id} every {minutes} minutes")
                    
        except Exception as e:
            logger.error(f"Error scheduling task {task.id}: {e}")
    
    async def _execute_task(self, task_id: int):
        """Execute a scheduled task"""
        try:
            async with async_session_maker() as session:
                # Get task
                result = await session.execute(
                    select(ScheduledTask).where(ScheduledTask.id == task_id)
                )
                task = result.scalar_one_or_none()
                
                if not task or not task.is_active:
                    logger.warning(f"Task {task_id} not found or inactive")
                    return
                
                # Get user by internal DB id
                from app.core.database import User
                user_result = await session.execute(
                    select(User).where(User.id == task.user_id)
                )
                user = user_result.scalar_one_or_none()
                
                if not user:
                    logger.error(f"User {task.user_id} not found for task {task_id}")
                    return
                
                logger.info(f"Executing task {task_id} for user {user.telegram_id}")
                
                # Update task stats
                task.last_run_at = datetime.now(pytz.UTC)
                task.run_count += 1
                task.next_run_at = self._get_next_run_time(task_id)
                
                # Check if max runs reached
                if task.max_runs and task.run_count >= task.max_runs:
                    task.is_active = False
                    task.is_completed = True
                    logger.info(f"Task {task_id} reached max runs, deactivating")
                
                await session.commit()
                
                # Execute the task
                if task.task_type == "reminder":
                    # Simple reminder - send message
                    await self._send_reminder(user.telegram_id, task.message_text or task.description)

                elif task.task_type in ["one_time", "recurring", "interval"]:
                    # AI-powered task
                    if task.message_text == "__kb_refresh_stale__":
                        # Специальная задача: обновить устаревшие статьи базы знаний
                        await self._run_kb_stale_refresh(user.telegram_id)
                    elif task.message_text:
                        await self._process_ai_task(
                            user_id=user.telegram_id,
                            message=task.message_text,
                            agent_mode=task.agent_mode
                        )
                    else:
                        await self._send_reminder(user.telegram_id, task.description)
                
                logger.info(f"Task {task_id} executed successfully")
                
        except Exception as e:
            logger.error(f"Error executing task {task_id}: {e}")
            await self._increment_task_error(task_id, str(e))
    
    async def _run_kb_stale_refresh(self, telegram_id: int):
        """Обновляет устаревшие статьи базы знаний и уведомляет владельца."""
        try:
            from app.core.skills_loader import skill_loader
            kb_skill = skill_loader.get_skill("knowledge_base")
            if not kb_skill:
                logger.warning("KB stale refresh: knowledge_base skill not loaded")
                return

            result = await kb_skill({
                "user_id": telegram_id,
                "message_text": "kb refresh_stale",
                "args": {"cmd": "refresh_stale"},
            })
            if self._bot:
                await self._bot.send_message(
                    chat_id=telegram_id,
                    text=result,
                    parse_mode="HTML",
                )
            logger.info(f"KB stale refresh completed for {telegram_id}")
        except Exception as e:
            logger.error(f"KB stale refresh error: {e}")

    async def _send_reminder(self, telegram_id: int, message: str):
        """Send a simple reminder message"""
        if self._bot:
            try:
                await self._bot.send_message(
                    chat_id=telegram_id,
                    text=f"⏰ <b>Напоминание</b>\n\n{message}",
                    parse_mode="HTML"
                )
                logger.info(f"Sent reminder to {telegram_id}")
            except Exception as e:
                logger.error(f"Failed to send reminder to {telegram_id}: {e}")
    
    async def _process_ai_task(self, user_id: int, message: str, agent_mode: str = "fastbot"):
        """Process task with AI agent"""
        try:
            # Route to appropriate agent
            response = await agent_router.route_message(
                user_id=user_id,
                message=message,
                agent_mode=agent_mode
            )
            
            if self._bot:
                await self._bot.send_message(
                    chat_id=user_id,
                    text=f"🤖 <b>Автоматическое задание</b>\n\n{response}"
                )
        except Exception as e:
            logger.error(f"Error processing AI task: {e}")
            if self._bot:
                await self._bot.send_message(
                    chat_id=user_id,
                    text=f"⚠️ <b>Ошибка выполнения задания</b>\n\n{message}"
                )
    
    def _get_next_run_time(self, job_id: str) -> Optional[datetime]:
        """Get next run time for a scheduled job"""
        try:
            job = self.scheduler.get_job(f"task_{job_id}")
            if job and job.next_run_time:
                return job.next_run_time
        except Exception:
            pass
        return None
    
    async def _mark_task_completed(self, task_id: int):
        """Mark task as completed"""
        try:
            async with async_session_maker() as session:
                await session.execute(
                    update(ScheduledTask)
                    .where(ScheduledTask.id == task_id)
                    .values(is_completed=True, is_active=False)
                )
                await session.commit()
        except Exception as e:
            logger.error(f"Error marking task {task_id} completed: {e}")
    
    async def _increment_task_error(self, task_id: int, error: str):
        """Increment task error count"""
        try:
            async with async_session_maker() as session:
                result = await session.execute(
                    select(ScheduledTask).where(ScheduledTask.id == task_id)
                )
                task = result.scalar_one_or_none()
                if task:
                    task.error_count += 1
                    task.last_error = error
                    
                    # Deactivate if too many errors
                    if task.error_count >= 3:
                        task.is_active = False
                        logger.warning(f"Task {task_id} deactivated due to errors")
                    
                    await session.commit()
        except Exception as e:
            logger.error(f"Error incrementing task error: {e}")
    
    # Public API methods
    
    async def create_reminder(
        self,
        user_id: int,
        telegram_id: int,
        description: str,
        run_date: datetime,
        message_text: Optional[str] = None,
        name: Optional[str] = None
    ) -> ScheduledTask:
        """Create a one-time reminder"""
        async with async_session_maker() as session:
            # Get or create user
            user = await get_or_create_user(session, telegram_id=telegram_id)
            
            task = ScheduledTask(
                user_id=user.id,
                name=name or f"Напоминание {datetime.now().strftime('%Y-%m-%d %H:%M')}",
                description=description,
                task_type="one_time",
                run_date=run_date,
                message_text=message_text or description,
                timezone=settings.BOT_TIMEZONE
            )
            session.add(task)
            await session.commit()
            await session.refresh(task)
            
            # Schedule the task
            await self._schedule_task(task)
            
            logger.info(f"Created reminder {task.id} for user {telegram_id}")
            return task
    
    async def create_recurring_task(
        self,
        user_id: int,
        telegram_id: int,
        description: str,
        cron_expression: str,
        message_text: Optional[str] = None,
        name: Optional[str] = None,
        max_runs: Optional[int] = None
    ) -> ScheduledTask:
        """Create a recurring task with cron expression"""
        async with async_session_maker() as session:
            user = await get_or_create_user(session, telegram_id=telegram_id)
            
            task = ScheduledTask(
                user_id=user.id,
                name=name or f"Задача {datetime.now().strftime('%Y-%m-%d %H:%M')}",
                description=description,
                task_type="recurring",
                cron_expression=cron_expression,
                message_text=message_text,
                max_runs=max_runs,
                timezone=settings.BOT_TIMEZONE
            )
            session.add(task)
            await session.commit()
            await session.refresh(task)
            
            await self._schedule_task(task)
            
            logger.info(f"Created recurring task {task.id} with cron: {cron_expression}")
            return task
    
    async def create_interval_task(
        self,
        user_id: int,
        telegram_id: int,
        description: str,
        interval_minutes: int,
        message_text: Optional[str] = None,
        name: Optional[str] = None,
        max_runs: Optional[int] = None
    ) -> ScheduledTask:
        """Create an interval-based task"""
        async with async_session_maker() as session:
            user = await get_or_create_user(session, telegram_id=telegram_id)
            
            task = ScheduledTask(
                user_id=user.id,
                name=name or f"Интервал {datetime.now().strftime('%Y-%m-%d %H:%M')}",
                description=description,
                task_type="interval",
                message_text=message_text,
                max_runs=max_runs,
                timezone=settings.BOT_TIMEZONE,
                extra_data={"interval_minutes": interval_minutes}
            )
            session.add(task)
            await session.commit()
            await session.refresh(task)
            
            await self._schedule_task(task)
            
            logger.info(f"Created interval task {task.id} every {interval_minutes} minutes")
            return task

    async def create_daily_task(
        self,
        user_id: int,
        telegram_id: int,
        description: str,
        hour: int,
        minute: int,
        message_text: Optional[str] = None,
        name: Optional[str] = None,
        max_runs: Optional[int] = None
    ) -> ScheduledTask:
        """Create a daily recurring task (cron: every day at HH:MM)"""
        return await self.create_recurring_task(
            user_id=user_id,
            telegram_id=telegram_id,
            description=description,
            cron_expression=f"{minute} {hour} * * *",
            message_text=message_text,
            name=name,
            max_runs=max_runs
        )

    async def create_weekly_task(
        self,
        user_id: int,
        telegram_id: int,
        description: str,
        day_of_week: int,
        hour: int,
        minute: int,
        message_text: Optional[str] = None,
        name: Optional[str] = None,
        max_runs: Optional[int] = None
    ) -> ScheduledTask:
        """Create a weekly recurring task (cron: every week on day_of_week at HH:MM).

        day_of_week: 0=понедельник .. 6=воскресенье (datetime.weekday()).
        APScheduler CronTrigger использует ту же нумерацию.
        """
        return await self.create_recurring_task(
            user_id=user_id,
            telegram_id=telegram_id,
            description=description,
            cron_expression=f"{minute} {hour} * * {day_of_week}",
            message_text=message_text,
            name=name,
            max_runs=max_runs
        )
    
    async def get_user_tasks(self, telegram_id: int) -> List[ScheduledTask]:
        """Get all active tasks for a user"""
        async with async_session_maker() as session:
            # Get user
            from app.core.database import User
            result = await session.execute(
                select(User).where(User.telegram_id == telegram_id)
            )
            user = result.scalar_one_or_none()
            
            if not user:
                return []
            
            # Get tasks
            result = await session.execute(
                select(ScheduledTask).where(
                    and_(
                        ScheduledTask.user_id == user.id,
                        ScheduledTask.is_active.is_(True)
                    )
                ).order_by(ScheduledTask.next_run_at)
            )
            return list(result.scalars().all())
    
    async def cancel_task(self, task_id: int, telegram_id: int) -> bool:
        """Cancel a task"""
        try:
            async with async_session_maker() as session:
                # Verify ownership
                from app.core.database import User
                user_result = await session.execute(
                    select(User).where(User.telegram_id == telegram_id)
                )
                user = user_result.scalar_one_or_none()
                
                if not user:
                    return False
                
                result = await session.execute(
                    select(ScheduledTask).where(
                        and_(
                            ScheduledTask.id == task_id,
                            ScheduledTask.user_id == user.id
                        )
                    )
                )
                task = result.scalar_one_or_none()
                
                if not task:
                    return False
                
                # Cancel in scheduler
                job_id = f"task_{task_id}"
                if self.scheduler.get_job(job_id):
                    self.scheduler.remove_job(job_id)
                
                # Mark as inactive
                task.is_active = False
                await session.commit()
                
                logger.info(f"Cancelled task {task_id}")
                return True
                
        except Exception as e:
            logger.error(f"Error cancelling task {task_id}: {e}")
            return False
    
    async def get_task_stats(self) -> Dict[str, Any]:
        """Get scheduler statistics"""
        async with async_session_maker() as session:
            from sqlalchemy import func
            
            # Count by type
            result = await session.execute(
                select(ScheduledTask.task_type, func.count(ScheduledTask.id))
                .where(ScheduledTask.is_active.is_(True))
                .group_by(ScheduledTask.task_type)
            )
            by_type = dict(result.all())
            
            # Count total active
            result = await session.execute(
                select(func.count(ScheduledTask.id))
                .where(ScheduledTask.is_active.is_(True))
            )
            total_active = result.scalar()
            
            # Count completed
            result = await session.execute(
                select(func.count(ScheduledTask.id))
                .where(ScheduledTask.is_completed.is_(True))
            )
            total_completed = result.scalar()
            
            return {
                "total_active": total_active,
                "total_completed": total_completed,
                "by_type": by_type,
                "scheduled_jobs": len(self.scheduler.get_jobs())
            }


# Global scheduler instance
task_scheduler = TaskScheduler()
