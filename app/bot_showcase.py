"""
Showcase entry point — один процесс, одна или несколько витрин.

Каждая витрина — свой Telegram-бот со своим токеном, корпусом знаний, режимом
доступа и группой оператора (см. app/core/showcases.py, env SHOWCASES). Бот
отвечает клиентам строго по загруженному корпусу. Процесс независим от основного
бота, поэтому их можно запускать и останавливать отдельно.

Эмбеддинг-модель, БД и LLM-клиент общие — витрины не умножают потребление памяти.
"""
import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, BotCommandScopeChat

from app.core import operator as operator_store
from app.core.config import settings
from app.core.database import init_db
from app.core.logger import setup_logging
from app.core.memory import vector_memory
from app.core.showcases import (
    Showcase,
    ShowcaseConfigError,
    load_registry,
    set_registry,
    showcase_middleware,
)
from app.handlers.operator import router as operator_router
from app.handlers.showcase import router as showcase_router

logger = setup_logging()

_CLIENT_COMMANDS = [
    BotCommand(command="start", description="Начать"),
    BotCommand(command="help", description="Справка"),
    BotCommand(command="operator", description="Позвать оператора"),
    BotCommand(command="endchat", description="Вернуться к боту"),
]
_ADMIN_COMMANDS = _CLIENT_COMMANDS + [
    BotCommand(command="corpus_stats", description="Статистика корпуса"),
    BotCommand(command="corpus_upload", description="Загрузить документ"),
    BotCommand(command="corpus_delete", description="Удалить документ"),
    BotCommand(command="invite", description="Создать приглашение"),
    BotCommand(command="invites", description="Действующие приглашения"),
    BotCommand(command="allow", description="Выдать доступ по ID"),
    BotCommand(command="deny", description="Отозвать доступ"),
    BotCommand(command="members", description="Участники"),
]


async def _start_bot(sc: Showcase):
    """Проверить токен и настроить меню. None — витрина пропускается (остальные работают)."""
    bot = Bot(token=sc.token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    try:
        me = await bot.get_me()
        await vector_memory.ensure_corpus(sc.collection)
        await bot.set_my_commands(_CLIENT_COMMANDS)
        for admin_id in settings.ADMIN_IDS:
            try:  # админ мог ещё не открывать чат с ботом
                await bot.set_my_commands(_ADMIN_COMMANDS, scope=BotCommandScopeChat(chat_id=admin_id))
            except Exception:
                pass
    except Exception as e:
        logger.error(f"Витрина «{sc.slug or 'default'}» пропущена: {type(e).__name__}: {e}")
        await bot.session.close()
        return None
    logger.info(
        f"Витрина «{sc.slug or 'default'}» → @{me.username} "
        f"(доступ: {sc.access}, корпус: {sc.collection}, "
        f"оператор: {'вкл' if sc.operator_enabled else 'выкл'})"
    )
    return bot


async def main():
    logger.info("Starting Alex-Nano-Bot Showcase...")
    try:
        registry = load_registry(settings)
    except ShowcaseConfigError as e:
        logger.error(f"Ошибка конфигурации витрин: {e}")
        raise SystemExit(2)
    set_registry(registry)

    await init_db()
    logger.info("Database initialized")
    await vector_memory.initialize()
    logger.info("Vector memory initialized")

    # сессии оператора, созданные до разделения витрин, достаются первой витрине
    adopted = await operator_store.adopt_legacy_sessions(registry.first.slug)
    if adopted:
        logger.info(f"Старых сессий оператора передано витрине «{registry.first.slug}»: {adopted}")

    bots = []
    for sc in registry:
        bot = await _start_bot(sc)
        if bot is not None:
            bots.append(bot)
    if not bots:
        logger.error("Ни одна витрина не запустилась — проверьте токены")
        raise SystemExit(1)

    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(showcase_middleware(registry))
    # Оператор — первым: сообщения клиента в активной сессии не должны уходить в LLM
    dp.include_router(operator_router)
    dp.include_router(showcase_router)

    logger.info(f"Showcase bots started: {len(bots)} of {len(registry)}")
    try:
        await dp.start_polling(*bots, skip_updates=True)
    except Exception as e:
        logger.error(f"Showcase bot error: {e}")
        raise
    finally:
        for bot in bots:
            await bot.session.close()
        logger.info("Showcase bots shut down")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.getLogger(__name__).info("Showcase bot stopped by user")
    except SystemExit:
        raise
    except Exception as e:
        logging.getLogger(__name__).error(f"Fatal error: {e}")
        raise
