"""
CallToolInstruction — выполняет навык через ctx.tools (ToolRegistry).

DSL params:
    tool  (str, required) — имя навыка из SkillLoader
    args  (dict, default={}) — аргументы, передаются в callable навыка

on_error: если инструмент не найден или упал, step получает status="error",
          VM принимает решение abort/continue согласно программе Planner'а.
"""
from typing import Dict

from ..builder import StepResultBuilder
from ..context import VMContext
from .base import BaseInstruction


class CallToolInstruction(BaseInstruction):
    name = "call_tool"

    async def execute(self, step_id: str, params: Dict, ctx: VMContext):
        tool_name = params.get("tool")
        args = params.get("args", {})

        if not tool_name:
            return (
                StepResultBuilder(step_id, self.name)
                .error("Missing required param 'tool'")
                .build()
            )

        if ctx.tools is None:
            return (
                StepResultBuilder(step_id, self.name)
                .error("ToolRegistry not wired: VMContext.tools is None")
                .build()
            )

        # user_id берём из StateContext, а не из args (args генерирует planner-LLM)
        result = await ctx.tools.execute(tool_name, args, user_id=ctx.user_id)

        # ToolRegistry возвращает строку с префиксом при ошибке поиска/выполнения
        if isinstance(result, str) and result.startswith("[ToolRegistry]"):
            return (
                StepResultBuilder(step_id, self.name)
                .error(result)
                .build()
            )

        return (
            StepResultBuilder(step_id, self.name)
            .output(result)
            .build()
        )
