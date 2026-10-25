"""
Catch-all для неопознанных callback-запросов.

Хендлер без фильтров перехватывает любой callback_query, поэтому этот роутер
должен быть подключён в register_handlers() ПОСЛЕДНИМ — иначе он сработает
раньше конкретных хендлеров skills/memory/providers.
"""
import logging

from aiogram import Router
from aiogram.types import CallbackQuery

logger = logging.getLogger(__name__)

router = Router()


@router.callback_query()
async def fallback_unhandled_callback(callback: CallbackQuery):
    """Catch-all for unmatched callbacks"""
    logger.warning(f"Unhandled callback query data: {callback.data}")
    await callback.answer("⚠️ Действие не распознано (unhandled)", show_alert=True)
