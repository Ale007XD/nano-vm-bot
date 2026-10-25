from typing import Dict
from ..builder import StepResultBuilder
from ..context import VMContext
from .base import BaseInstruction


class RespondInstruction(BaseInstruction):
    name = "respond"

    async def execute(self, step_id: str, params: Dict, ctx: VMContext):
        # Неразрешённый $ref приходит как None — отвечаем пустой строкой,
        # а не роняем шаг на валидации OutboxMessage
        text = params.get("text")
        if text is None:
            text = ""

        return (
            StepResultBuilder(step_id, self.name)
            .message(str(text))
            .build()
        )
