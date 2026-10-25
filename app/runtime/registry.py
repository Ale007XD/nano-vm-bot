from typing import Dict, Any
import importlib
import logging

logger = logging.getLogger(__name__)

class BaseInstruction:
    async def execute(self, *args, **kwargs):
        raise NotImplementedError

class InstructionRegistry:
    def __init__(self):
        self._registry: Dict[str, Any] = {}
        self._load_defaults()

    def _load_defaults(self):
        """Автоматическая регистрация базовых инструкций при инициализации."""
        default_instructions = ["call_llm", "respond", "store_memory", "call_tool"]
        for name in default_instructions:
            try:
                module = importlib.import_module(f"app.runtime.instructions.{name}")
                for attr in dir(module):
                    if attr.endswith("Instruction") and attr != "BaseInstruction":
                        self.register(name, getattr(module, attr))
            except ImportError as e:
                logger.debug(f"Could not auto-load instruction {name}: {e}")

    def register(self, name: str, instruction_cls: Any):
        self._registry[name] = instruction_cls

    def get(self, name: str) -> Any:
        if name not in self._registry:
            raise ValueError(f"Instruction not found: {name}")
        return self._registry[name]
