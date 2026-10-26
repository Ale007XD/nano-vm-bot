"""
ToolRegistry — мост между VM и SkillLoader.

CallToolInstruction делает: ctx.tools.execute(tool_name, args)
ToolRegistry проксирует это в SkillLoader.get_skill(name) → callable.

Архитектурная граница:
    VM / instructions  →  ToolRegistry  →  SkillLoader  →  файловая система
    VM ничего не знает о SkillLoader. SkillLoader ничего не знает о VM.
    ToolRegistry — единственная точка сшивки.
"""
from __future__ import annotations

import inspect
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.core.skills_loader import SkillLoader

logger = logging.getLogger(__name__)


class ToolRegistry:
    """
    Адаптер SkillLoader → интерфейс ctx.tools, ожидаемый CallToolInstruction.

    Интерфейс: await ctx.tools.execute(tool_name: str, args: dict) -> Any
    """

    def __init__(self, skill_loader: "SkillLoader") -> None:
        self._loader = skill_loader

    async def execute(self, tool_name: str, args: dict) -> Any:
        """
        Находит и вызывает навык по имени; если навыка нет — внешний
        MCP-инструмент '<server>__<tool>'.

        Возвращает результат или строку с описанием ошибки
        (не бросает исключений — VM сам решит что делать через on_error).
        """
        skill_callable = self._loader.get_skill(tool_name)

        if skill_callable is None:
            # навыка нет — пробуем внешний MCP-инструмент
            from app.core.mcp import mcp_manager

            mcp_result = await mcp_manager.call_tool(tool_name, args)
            if not mcp_result.startswith("[MCP]"):
                return mcp_result
            msg = f"Tool '{tool_name}' not found in SkillLoader/MCP ({mcp_result})"
            logger.warning(msg)
            return f"[ToolRegistry] {msg}"

        try:
            # Навыки могут принимать context-dict или именованные аргументы
            if inspect.iscoroutinefunction(skill_callable):
                result = await skill_callable(args)
            else:
                result = skill_callable(args)
            return result
        except Exception as e:
            logger.error("Tool '%s' raised: %s", tool_name, e, exc_info=True)
            return f"[ToolRegistry] Tool '{tool_name}' error: {e}"

    def list_tools(self) -> list[str]:
        """Список доступных инструментов — для отладки и логирования."""
        return [info.name for info in self._loader.list_skills() if info.is_active]
