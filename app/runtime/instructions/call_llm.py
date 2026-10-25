from typing import Any, Dict

from app.runtime.builder import StepResultBuilder
from app.runtime.registry import BaseInstruction

_ALLOWED_PARAMS = {"prompt", "system", "system_prompt", "role", "tools"}


class CallLLMInstruction(BaseInstruction):
    """Вызов LLM-провайдера и сохранение текстового результата."""

    async def execute(self, step_id: str, params: Dict[str, Any], ctx: Any) -> Any:
        # Planner-LLM может добавить ключи вне сигнатуры адаптера —
        # передаём только поддерживаемые
        filtered = {k: v for k, v in params.items() if k in _ALLOWED_PARAMS}
        if "prompt" not in filtered:
            return (
                StepResultBuilder(step_id, action="call_llm")
                .error("Missing required param 'prompt'")
                .build()
            )

        result = await ctx.llm.generate(**filtered)
        
        # Адаптация к новой сигнатуре Tuple[str, Optional[List]]
        # Если адаптер возвращает кортеж с tool_calls, извлекаем только текст
        text_response = result[0] if isinstance(result, tuple) else result
        
        # Строгая сборка результата согласно контракту StepResultBuilder:
        # Сохраняем в output для доступности через $step_id в следующих шагах
        return StepResultBuilder(step_id, action="call_llm").output(text_response).build()
