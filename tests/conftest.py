import importlib.util
import os
import sys
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock

import pytest


class SafeMock(MagicMock):
    """MagicMock, переживающий откат monkeypatch.

    pytest>=9.2 запоминает старое значение атрибута только из __dict__ объекта;
    атрибуты MagicMock живут в _mock_children, поэтому при откате pytest делает
    delattr, а мок помечает атрибут удалённым -> AttributeError в следующих
    тестах. Здесь delattr только снимает установленное значение, не помечая атрибут удалённым.
    """

    def __delattr__(self, name):
        # убрать установленное значение, но не помечать атрибут «удалённым»
        self.__dict__.pop(name, None)


# 1. Заглушки тяжелых внешних зависимостей (ChromaDB, FastEmbed)
mock_chroma = MagicMock()
mock_chroma_config = MagicMock()
mock_chroma_config.Settings = MagicMock()
mock_fastembed = MagicMock()
mock_fastembed.TextEmbedding = MagicMock()

sys.modules["chromadb"] = mock_chroma
sys.modules["chromadb.config"] = mock_chroma_config
sys.modules["fastembed"] = mock_fastembed

# 2. Окружение (обязательно до инициализации любых компонентов app)
os.environ["BOT_TOKEN"] = "123:mock"
os.environ["OPENROUTER_API_KEY"] = "mock_key"

# 3. Точечные заглушки внутренних модулей, вызывающих побочные эффекты при загрузке
mock_settings = SafeMock()
mock_settings.APP_NAME = "Alex-Nano-Bot"
mock_settings.DEFAULT_MODEL = "llama-3.1-8b-instant"
mock_settings.CODER_MODEL = "llama-3.1-8b-instant"
mock_settings.PLANNER_MODEL = "llama-3.3-70b-versatile"

mock_config = SafeMock()
mock_config.Settings.return_value = mock_settings
mock_config.settings = mock_settings
sys.modules["app.core.config"] = mock_config

mock_db = SafeMock()
mock_db.engine = AsyncMock()
mock_db.init_db = AsyncMock()
mock_db.declarative_base.return_value = MagicMock()
sys.modules["app.core.database"] = mock_db

mock_client = SafeMock()

@dataclass
class Message:
    role: str
    content: str

@dataclass
class LLMResponse:
    content: str
    model: str = ""
    tool_calls: list = None

mock_client.Message = Message
mock_client.LLMResponse = LLMResponse
sys.modules["app.core.llm_client"] = mock_client

# 4. Изолированная загрузка критичных модулей с сохранением оригинального поведения
def _load_real_module(module_name, file_path):
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module

_load_real_module("app.core.memory", "app/core/memory.py")
_load_real_module("app.core.skills_loader", "app/core/skills_loader.py")

# Мы НЕ глушим пакеты "app" и "app.core" целиком.
# Стандартный механизм импорта теперь свободно найдет app.agents и app.runtime на диске,
# а все опасные импорты (config, database) будут перехвачены словарем sys.modules выше.


# ===========================================================================
# Фикстуры для test_runtime.py
# ===========================================================================

@pytest.fixture
def mock_llm():
    from app.runtime.llm_adapter import MockLLMAdapter
    return MockLLMAdapter(fixed_response="тестовый ответ LLM")


@pytest.fixture
def state():
    from app.runtime.state_context import StateContext
    return StateContext.from_defaults(user_id=42, agent_mode="runtime")


@pytest.fixture
def registry():
    from app.runtime.registry import InstructionRegistry
    return InstructionRegistry()


@pytest.fixture
def mock_memory():
    m = AsyncMock()
    m.store = AsyncMock()
    m.add_memory = AsyncMock()
    return m


@pytest.fixture
def vm():
    from app.runtime.registry import InstructionRegistry
    from app.runtime.vm import ExecutionVM
    return ExecutionVM(registry=InstructionRegistry())


@pytest.fixture
def vm_ctx(state, mock_llm, mock_memory):
    from app.runtime.context import VMContext
    return VMContext(
        state=state,
        llm=mock_llm,
        memory=mock_memory,
        tools=MagicMock(),
    )
