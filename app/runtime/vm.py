"""
ExecutionVM — детерминированный исполнитель DSL-программ.

Контракт: δ(S, Program) → S'

Program (dict) структура:
    {
        "plan": [
            {
                "id": "step1",
                "instruction": "call_llm",
                "on_error": "abort",          # "abort" | "continue" (default: "abort")
                "params": {
                    "prompt": "Привет",
                    "role": "default"
                }
            },
            {
                "id": "step2",
                "instruction": "respond",
                "params": {
                    "text": "$step1"           # $step_id → output шага step1
                }
            }
        ]
    }

$-refs резолвятся рекурсивно в dict и list.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

from app.runtime.builder import StepResultBuilder
from app.runtime.context import VMContext
from app.runtime.registry import InstructionRegistry
from app.runtime.state_context import StateContext
from app.runtime.step_result import StepResult

logger = logging.getLogger(__name__)


class VMRunResult:
    """Результат прогона программы."""

    def __init__(self, state: StateContext, results: List[StepResult], aborted: bool = False):
        self.state = state
        self.results = results
        self.aborted = aborted

    @property
    def outbox(self):
        return self.state.outbox

    @property
    def failed_steps(self) -> List[StepResult]:
        return [r for r in self.results if r.status == "error"]


class ExecutionVM:
    """
    Stateless исполнитель. Не хранит состояния между вызовами.
    ctx.variables мутируется только внутри одного run().
    """

    def __init__(self, registry: InstructionRegistry):
        self.registry = registry

    async def run(self, program: Dict, ctx: VMContext) -> VMRunResult:
        """
        Выполнить программу.

        Возвращает VMRunResult с итоговым state и всеми StepResult.
        Не бросает исключений: ошибки шагов оборачиваются в StepResult(status="error").
        """
        steps = program.get("plan", [])
        step_results: List[StepResult] = []

        for step in steps:
            step_id: str = step.get("id", f"step_{len(step_results)}")
            name: str = step.get("instruction")
            on_error: str = step.get("on_error", "abort")
            raw_params: Dict = step.get("params", {})

            params = self._resolve(raw_params, ctx)

            if not name:
                result = (
                    StepResultBuilder(step_id, "unknown")
                    .error("Step is missing required field 'instruction'")
                    .build()
                )
            else:
                result = await self._execute_step(step_id, name, params, ctx)
            step_results.append(result)

            # сохранить output для $-refs следующих шагов
            ctx.variables[step_id] = result.output

            # применить к state (иммутабельно)
            ctx.state = ctx.state.apply(result)

            if result.status == "error":
                logger.warning(
                    "Step %s (%s) failed: %s | on_error=%s",
                    step_id, name, result.error, on_error,
                )
                if on_error == "abort":
                    logger.info("Aborting program at step %s", step_id)
                    return VMRunResult(ctx.state, step_results, aborted=True)
                # on_error == "continue" → идём дальше

        return VMRunResult(ctx.state, step_results, aborted=False)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _execute_step(
        self,
        step_id: str,
        name: str,
        params: Dict,
        ctx: VMContext,
    ) -> StepResult:
        """Выполнить один шаг с перехватом исключений."""
        try:
            instr_cls = self.registry.get(name)
        except ValueError as e:
            return (
                StepResultBuilder(step_id, name)
                .error(f"UnknownInstruction: {e}")
                .build()
            )

        instr = instr_cls()
        try:
            return await instr.execute(step_id, params, ctx)
        except Exception as e:
            logger.exception("Instruction %s raised: %s", name, e)
            return (
                StepResultBuilder(step_id, name)
                .error(str(e))
                .build()
            )

    def _resolve(self, value: Any, ctx: VMContext) -> Any:
        """
        Рекурсивно резолвит $-refs.

        Поддерживает:
            "$step1"            → ctx.variables["step1"]
            {"key": "$step1"}   → {"key": <value>}
            ["$step1", "text"]  → [<value>, "text"]
        """
        if isinstance(value, str) and value.startswith("$"):
            key = value[1:]
            resolved = ctx.variables.get(key)
            if resolved is None:
                logger.warning("Unresolved ref: %s (variables: %s)", value, list(ctx.variables))
            return resolved

        if isinstance(value, dict):
            return {k: self._resolve(v, ctx) for k, v in value.items()}

        if isinstance(value, list):
            return [self._resolve(item, ctx) for item in value]

        return value
