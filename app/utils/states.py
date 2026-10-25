"""
FSM States for conversation management
"""
from enum import Enum

from aiogram.fsm.state import State, StatesGroup


class BotMode(str, Enum):
    FASTBOT = "fastbot"
    PLANBOT = "planbot"
    SKILLBOT = "skillbot"
    RUNTIME = "runtime"


class FastBotState(StatesGroup):
    waiting_for_query = State()


class AgentMode(StatesGroup):
    """Agent mode selection"""
    selecting = State()


class SkillCreation(StatesGroup):
    """Skill creation states"""
    waiting_name = State()
    waiting_description = State()
    waiting_code = State()
    waiting_confirmation = State()


class SkillEdit(StatesGroup):
    """Skill editing states"""
    editing_code = State()
    waiting_confirmation = State()


class MemoryAdd(StatesGroup):
    """Memory addition states"""
    waiting_content = State()
    waiting_type = State()


class MemorySearch(StatesGroup):
    """Memory search states"""
    waiting_query = State()


class ChatState(StatesGroup):
    """Chat conversation states"""
    chatting = State()
    waiting_skill_input = State()


class ProviderKeyUpdate(StatesGroup):
    """FSM for hot-swapping provider API keys from the bot"""
    selecting_provider = State()   # Inline keyboard: pick provider
    waiting_key = State()          # User sends new key (message deleted immediately)
    confirming = State()           # Show masked key, confirm Y/N


class ReminderStates(StatesGroup):
    """
    Reminder and recurring task creation states.
    Defined here (not in reminders.py) — single source of truth for all FSM states.
    """
    waiting_for_description = State()
    waiting_for_time = State()
    waiting_for_cron = State()
