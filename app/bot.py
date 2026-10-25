"""
Main bot initialization
"""
import asyncio

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from app.core.config import settings
from app.core.database import init_db
from app.core.logger import setup_logging
from app.core.memory import vector_memory
from app.core.scheduler import task_scheduler
from app.core.skills_loader import skill_loader
from app.handlers import register_handlers

logger = setup_logging()



async def _load_provider_configs_from_db():
    """
    On startup: read ProviderConfig rows from DB and hot-apply to llm_client.
    This ensures that keys/priorities changed via /providers survive restarts.
    Falls back silently to .env values if DB has no override.
    """
    try:
        from sqlalchemy import select

        from app.core.crypto import decrypt_key
        from app.core.database import ProviderConfig, async_session_maker
        from app.core.llm_client_v2 import llm_client

        async with async_session_maker() as session:
            result = await session.execute(select(ProviderConfig))
            configs = result.scalars().all()

        applied = 0
        for cfg in configs:
            # Apply priority
            if cfg.priority is not None:
                await llm_client.set_provider_priority(cfg.name, cfg.priority)

            # Apply enabled/disabled
            if not cfg.is_enabled:
                await llm_client.set_provider_enabled(cfg.name, False)

            # Apply role overrides
            if getattr(cfg, 'role_models', None):
                llm_client.load_overrides_from_db(cfg.name, cfg.role_models)

            # Apply key if stored
            if cfg.encrypted_key:
                try:
                    plain_key = decrypt_key(cfg.encrypted_key)
                    await llm_client.reload_provider(cfg.name, plain_key)
                    applied += 1
                except Exception as e:
                    logger.warning(f"Could not decrypt key for provider {cfg.name}: {e}")

        logger.info(f"Provider DB configs applied: {len(configs)} rows, {applied} keys loaded")
    except Exception as e:
        logger.warning(f"Could not load provider configs from DB (first run?): {e}")


async def on_startup(bot: Bot):
    """Startup handler"""
    logger.info("Starting Alex-Nano-Bot...")
    
    # Initialize database
    logger.info("Initializing database...")
    await init_db()
    
    # Load provider configs from DB (hot-swap persistence across restarts)
    logger.info("Loading provider configs from DB...")
    await _load_provider_configs_from_db()

    # Initialize vector memory
    logger.info("Initializing vector memory...")
    await vector_memory.initialize()
    
    # Load skills
    logger.info("Loading skills...")
    await skill_loader.load_all_skills()
    
    # Start scheduler
    logger.info("Starting task scheduler...")
    task_scheduler.set_bot(bot)
    task_scheduler.start()
    
    # Set bot commands
    commands = [
        ("start", "Start the bot"),
        ("help", "Show help message"),
        ("mode", "Switch agent mode"),
        ("skills", "Manage skills"),
        ("memory", "Memory operations"),
        ("clear", "Clear conversation history"),
        ("remind", "Create reminder"),
        ("tasks", "List scheduled tasks"),
        ("daily", "Create daily task"),
        ("weekly", "Create weekly task"),
        ("providers", "Manage LLM providers (admin only)"),
    ]
    
    from aiogram.types import BotCommand
    await bot.set_my_commands([
        BotCommand(command=cmd, description=desc)
        for cmd, desc in commands
    ])
    
    logger.info("Bot started successfully!")
    await _register_kb_refresh_cron(bot)


async def _register_kb_refresh_cron(bot):
    """Регистрирует ежедневный cron-job обновления устаревших статей БЗ."""
    from app.core.config import settings
    if not settings.ADMIN_IDS or not settings.KB_CHANNEL_IDS:
        return  # KB не настроена — пропускаем

    owner_id = settings.ADMIN_IDS[0]

    try:
        from sqlalchemy import select

        from app.core.database import ScheduledTask, async_session_maker
        from app.core.scheduler import task_scheduler

        async with async_session_maker() as session:
            existing = await session.execute(
                select(ScheduledTask).where(
                    ScheduledTask.name == "kb_stale_refresh"
                )
            )
            if existing.scalar_one_or_none():
                logger.info("KB stale refresh cron already registered")
                return

        await task_scheduler.create_recurring_task(
            user_id=owner_id,
            telegram_id=owner_id,
            description="Обновление устаревших статей базы знаний",
            cron_expression="0 3 * * *",  # ежедневно в 03:00 по BOT_TIMEZONE
            message_text="__kb_refresh_stale__",  # sentinel для _execute_task
            name="kb_stale_refresh",
        )
        logger.info("KB stale refresh cron registered (daily 03:00)")
    except Exception as e:
        logger.warning(f"Could not register KB cron: {e}")


async def on_shutdown(bot: Bot):
    """Shutdown handler"""
    logger.info("Shutting down...")

    # Shutdown scheduler
    logger.info("Shutting down scheduler...")
    task_scheduler.shutdown()

    logger.info("Bot stopped")


async def main():
    """Main entry point"""
    # Initialize bot and dispatcher
    bot = Bot(
        token=settings.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    storage = MemoryStorage()
    dp = Dispatcher(storage=storage)
    
    # Register handlers
    register_handlers(dp)
    
    # Register startup/shutdown
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)
    
    # Start polling
    try:
        await dp.start_polling(
            bot,
            skip_updates=True,
            allowed_updates=["message", "callback_query", "channel_post", "edited_channel_post"],
        )
    except Exception as e:
        logger.error(f"Bot error: {e}")
        raise
    finally:
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        raise
