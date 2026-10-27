"""
Showcase bot entry point — separate process with its own token.

Answers client questions strictly from the knowledge corpus loaded via
SKILL.md-style documents / document upload. Independent from the main bot
so each can be scaled/restarted independently.
"""
import asyncio

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand

from app.core.config import settings
from app.core.database import init_db
from app.core.logger import setup_logging
from app.core.memory import vector_memory
from app.handlers.operator import router as operator_router
from app.handlers.showcase import router as showcase_router

logger = setup_logging()


async def on_startup(bot: Bot):
    """Startup handler for showcase bot."""
    logger.info("Starting Alex-Nano-Bot Showcase...")

    await init_db()
    logger.info("Database initialized")

    await vector_memory.initialize()
    logger.info("Vector memory initialized")

    await bot.set_my_commands([
        BotCommand(command="start", description="Начать"),
        BotCommand(command="help", description="Справка"),
        BotCommand(command="operator", description="Позвать оператора"),
        BotCommand(command="endchat", description="Вернуться к боту"),
        BotCommand(command="corpus_stats", description="Статистика корпуса (admin)"),
    ])

    logger.info("Showcase bot started successfully!")


async def on_shutdown(bot: Bot):
    logger.info("Showcase bot shutting down...")


async def main():
    bot = Bot(
        token=settings.SHOWCASE_BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    storage = MemoryStorage()
    dp = Dispatcher(storage=storage)

    # Оператор — первым: сообщения клиента в активной сессии не должны уходить в LLM
    dp.include_router(operator_router)
    dp.include_router(showcase_router)

    dp.startup.register(on_startup)

    try:
        await dp.start_polling(bot, skip_updates=True)
    except Exception as e:
        logger.error(f"Showcase bot error: {e}")
        raise
    finally:
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Showcase bot stopped by user")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        raise
