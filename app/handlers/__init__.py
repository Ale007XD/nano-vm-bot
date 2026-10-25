"""
Handler registration
"""
from aiogram import Dispatcher

from app.handlers import (
    channel,
    commands,
    fallback,
    memory,
    messages,
    providers,
    reminders,
    skills,
)


def register_handlers(dp: Dispatcher):
    """Register all handlers"""
    # Include routers — order matters for FSM priority
    dp.include_router(commands.router)
    dp.include_router(providers.router)   # Admin: hot-swap LLM providers
    dp.include_router(skills.router)
    dp.include_router(memory.router)
    dp.include_router(reminders.router)
    dp.include_router(channel.router)     # Channel posts → knowledge base
    dp.include_router(messages.router)    # Must be last to catch all text
    dp.include_router(fallback.router)    # Catch-all callbacks — строго последним
