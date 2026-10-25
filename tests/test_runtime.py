"""
tests/test_runtime.py — unit и e2e тесты app/runtime/.

Покрытие:
    MockLLMAdapter          — детерминированность, запись calls
    StateContext            — from_defaults, from_db, apply, to_db_context
    StepResultBuilder       — ok/error/message/memory_write/transition
    ExecutionVM.run()       — e2e прогон программ (2-шаговые + abort + continue)
    ExecutionVM._resolve()  — $-ref резолюция (str/dict/list/None)
    Planner._parse()        — валидный JSON, markdown-обёртка, невалидный JSON
    InstructionRegistry     — register/get/unknown
"""
from __future__ import annotations

import json
import pytest
from unittest.mock import AsyncMock, MagicMock


# ===========================================================================
# MockLLMAdapter
# ===========================================================================

class TestMockLLMAdapter:
    def test_fixed_response(self, mock_llm):
        assert mock_llm.fixed_response == "тестовый ответ LLM"
        assert mock_llm.calls == []

    @pytest.mark.asyncio
    async def test_generate_records_call(self, mock_llm):
        result = await mock_llm.generate("привет", role="default", system="sys")
        # generate() → Tuple[str, Optional[list]]: text + tool_calls
        text, tool_calls = result
        assert text == "тестовый ответ LLM"
        assert tool_calls is None
        assert len(mock_llm.calls) == 1
        assert mock_llm.calls[0]["prompt"] == "привет"
        assert mock_llm.calls[0]["role"] == "default"
        assert mock_llm.calls[0]["system_prompt"] == "sys"

    @pytest.mark.asyncio
    async def test_generate_multiple_calls_accumulated(self, mock_llm):
        await mock_llm.generate("one")
        await mock_llm.generate("two", role="planner")
        assert len(mock_llm.calls) == 2
        assert mock_llm.calls[1]["role"] == "planner"

    @pytest.mark.asyncio
    async def test_conforms_to_llm_protocol(self, mock_llm):
        from app.runtime.llm_adapter import LLMProtocol
        assert isinstance(mock_llm, LLMProtocol)


# ===========================================================================
# StateContext
# ===========================================================================

class TestStateContext:
    def test_from_defaults(self, state):
        assert state.user_id == 42
        assert state.fsm_state == "idle"
        assert state.agent_mode == "runtime"
        assert state.outbox == []
        assert state.memory.entries == []
        assert state.extra == {}

    def test_from_db_restores_fsm_state(self):
        from app.runtime.state_context import StateContext

        fake_user_state = MagicMock()
        fake_user_state.user_id = 7
        fake_user_state.current_agent = "fastbot"
        fake_user_state.context = {"fsm_state": "awaiting_input", "pending": "yes"}

        ctx = StateContext.from_db(fake_user_state)
        assert ctx.user_id == 7
        assert ctx.fsm_state == "awaiting_input"
        assert ctx.agent_mode == "fastbot"
        assert ctx.extra["pending"] == "yes"

    def test_from_db_none_raises(self):
        from app.runtime.state_context import StateContext
        with pytest.raises(ValueError, match="user_state is None"):
            StateContext.from_db(None)

    def test_from_db_empty_context(self):
        from app.runtime.state_context import StateContext

        fake = MagicMock()
        fake.user_id = 1
        fake.current_agent = "runtime"
        fake.context = {}

        ctx = StateContext.from_db(fake)
        assert ctx.fsm_state == "idle"

    def test_frozen_immutable(self, state):
        with pytest.raises(Exception):
            state.fsm_state = "other"  # type: ignore[misc]

    def test_apply_outbox(self):
        from app.runtime.state_context import StateContext
        from app.runtime.builder import StepResultBuilder

        ctx = StateContext.from_defaults(user_id=1)
        result = StepResultBuilder("s1", "respond").message("привет").build()
        ctx2 = ctx.apply(result)

        assert len(ctx2.outbox) == 1
        assert ctx2.outbox[0].text == "привет"
        # оригинал не изменился
        assert ctx.outbox == []

    def test_apply_fsm_transition(self):
        from app.runtime.state_context import StateContext
        from app.runtime.builder import StepResultBuilder

        ctx = StateContext.from_defaults(user_id=1)
        result = StepResultBuilder("s1", "call_llm").transition("awaiting_input").build()
        ctx2 = ctx.apply(result)

        assert ctx2.fsm_state == "awaiting_input"
        assert ctx.fsm_state == "idle"

    def test_apply_memory_write(self):
        from app.runtime.state_context import StateContext
        from app.runtime.builder import StepResultBuilder

        ctx = StateContext.from_defaults(user_id=1)
        result = (
            StepResultBuilder("s1", "store_memory")
            .memory_write("episodic", "важный факт", {"tag": "note"})
            .build()
        )
        ctx2 = ctx.apply(result)

        assert len(ctx2.memory.entries) == 1
        assert ctx2.memory.entries[0]["content"] == "важный факт"
        assert ctx2.memory.entries[0]["collection"] == "episodic"

    def test_to_db_context_excludes_ephemeral(self):
        from app.runtime.state_context import StateContext
        from app.runtime.builder import StepResultBuilder

        ctx = StateContext.from_defaults(user_id=1)
        # добавим outbox и memory — они НЕ должны попасть в to_db_context
        r = StepResultBuilder("s1", "respond").message("hi").build()
        ctx2 = ctx.apply(r)

        db_ctx = ctx2.to_db_context()
        assert "fsm_state" in db_ctx
        assert "outbox" not in db_ctx
        assert "memory" not in db_ctx

    def test_to_db_context_preserves_extra(self):
        from app.runtime.state_context import StateContext

        fake = MagicMock()
        fake.user_id = 1
        fake.current_agent = "runtime"
        fake.context = {"fsm_state": "idle", "skill_name": "calculator"}

        ctx = StateContext.from_db(fake)
        db_ctx = ctx.to_db_context()
        assert db_ctx["skill_name"] == "calculator"


# ===========================================================================
# StepResultBuilder
# ===========================================================================

class TestStepResultBuilder:
    def test_ok_result(self):
        from app.runtime.builder import StepResultBuilder

        r = StepResultBuilder("s1", "call_llm").output("resp").build()
        assert r.status == "ok"
        assert r.output == "resp"
        assert r.error is None
        assert r.step_id == "s1"
        assert r.action == "call_llm"

    def test_error_result(self):
        from app.runtime.builder import StepResultBuilder

        r = StepResultBuilder("s1", "call_llm").error("timeout").build()
        assert r.status == "error"
        assert r.error == "timeout"
        assert r.output is None

    def test_message_added_to_outbox(self):
        from app.runtime.builder import StepResultBuilder

        r = StepResultBuilder("s2", "respond").message("текст").build()
        assert len(r.outbox) == 1
        assert r.outbox[0].text == "текст"

    def test_memory_write(self):
        from app.runtime.builder import StepResultBuilder

        r = (
            StepResultBuilder("s1", "store_memory")
            .memory_write("episodic", "контент", {"k": "v"})
            .build()
        )
        assert len(r.memory_writes) == 1
        assert r.memory_writes[0].collection == "episodic"
        assert r.memory_writes[0].content == "контент"

    def test_transition(self):
        from app.runtime.builder import StepResultBuilder

        r = StepResultBuilder("s1", "x").transition("running", reason="test").build()
        assert r.fsm_transition is not None
        assert r.fsm_transition.new_state == "running"
        assert r.fsm_transition.reason == "test"

    def test_meta_auto_generated(self):
        from app.runtime.builder import StepResultBuilder

        r = StepResultBuilder("s1", "call_llm").build()
        assert r.meta is not None
        assert r.meta.step_id == "s1"

    def test_frozen(self):
        from app.runtime.builder import StepResultBuilder

        r = StepResultBuilder("s1", "call_llm").output("x").build()
        with pytest.raises(Exception):
            r.output = "y"  # type: ignore[misc]


# ===========================================================================
# InstructionRegistry
# ===========================================================================

class TestInstructionRegistry:
    def test_register_and_get(self, registry):
        from app.runtime.instructions.call_llm import CallLLMInstruction

        cls = registry.get("call_llm")
        assert cls is CallLLMInstruction

    def test_unknown_instruction_raises(self, registry):
        with pytest.raises(ValueError, match="Instruction not found"):
            registry.get("nonexistent_instruction")

    def test_all_base_instructions_registered(self, registry):
        for name in ("call_llm", "respond", "store_memory", "call_tool"):
            assert registry.get(name) is not None


# ===========================================================================
# ExecutionVM — e2e
# ===========================================================================

class TestExecutionVM:
    # --- базовый 2-шаговый прогон ---

    @pytest.mark.asyncio
    async def test_simple_call_llm_respond(self, vm, vm_ctx):
        program = {
            "plan": [
                {
                    "id": "step1",
                    "instruction": "call_llm",
                    "on_error": "abort",
                    "params": {"prompt": "привет", "role": "default"},
                },
                {
                    "id": "step2",
                    "instruction": "respond",
                    "on_error": "continue",
                    "params": {"text": "$step1"},
                },
            ]
        }
        result = await vm.run(program, vm_ctx)

        assert not result.aborted
        assert len(result.results) == 2
        assert result.results[0].status == "ok"
        assert result.results[1].status == "ok"
        # outbox содержит ответ LLM
        assert len(result.outbox) == 1
        assert result.outbox[0].text == "тестовый ответ LLM"

    # --- on_error=abort останавливает прогон ---

    @pytest.mark.asyncio
    async def test_abort_on_error_stops_execution(self, vm, vm_ctx):
        program = {
            "plan": [
                {
                    "id": "step1",
                    "instruction": "unknown_instruction",  # вызовет ошибку
                    "on_error": "abort",
                    "params": {},
                },
                {
                    "id": "step2",
                    "instruction": "respond",
                    "on_error": "continue",
                    "params": {"text": "не должно выполниться"},
                },
            ]
        }
        result = await vm.run(program, vm_ctx)

        assert result.aborted is True
        assert len(result.results) == 1          # step2 не выполнился
        assert result.results[0].status == "error"

    # --- on_error=continue продолжает после ошибки ---

    @pytest.mark.asyncio
    async def test_continue_on_error_completes_program(self, vm, vm_ctx):
        program = {
            "plan": [
                {
                    "id": "step1",
                    "instruction": "unknown_instruction",
                    "on_error": "continue",   # продолжаем несмотря на ошибку
                    "params": {},
                },
                {
                    "id": "step2",
                    "instruction": "respond",
                    "on_error": "continue",
                    "params": {"text": "всё равно отвечаем"},
                },
            ]
        }
        result = await vm.run(program, vm_ctx)

        assert result.aborted is False
        assert len(result.results) == 2
        assert result.results[0].status == "error"
        assert result.results[1].status == "ok"
        assert result.outbox[0].text == "всё равно отвечаем"

    # --- $-ref резолюция ---

    @pytest.mark.asyncio
    async def test_ref_resolution_string(self, vm, vm_ctx):
        program = {
            "plan": [
                {
                    "id": "llm",
                    "instruction": "call_llm",
                    "on_error": "abort",
                    "params": {"prompt": "тест"},
                },
                {
                    "id": "out",
                    "instruction": "respond",
                    "on_error": "continue",
                    "params": {"text": "$llm"},
                },
            ]
        }
        result = await vm.run(program, vm_ctx)
        # respond получил output call_llm через $-ref
        assert result.outbox[0].text == "тестовый ответ LLM"

    @pytest.mark.asyncio
    async def test_empty_plan_returns_no_results(self, vm, vm_ctx):
        program = {"plan": []}
        result = await vm.run(program, vm_ctx)
        assert not result.aborted
        assert result.results == []

    @pytest.mark.asyncio
    async def test_state_accumulates_across_steps(self, vm, vm_ctx):
        """StateContext иммутабельно обновляется на каждом шаге."""
        program = {
            "plan": [
                {
                    "id": "s1",
                    "instruction": "call_llm",
                    "on_error": "abort",
                    "params": {"prompt": "раз"},
                },
                {
                    "id": "s2",
                    "instruction": "respond",
                    "on_error": "continue",
                    "params": {"text": "$s1"},
                },
            ]
        }
        initial_state = vm_ctx.state
        result = await vm.run(program, vm_ctx)

        # финальный state в result ≠ начальный (не тот же объект)
        assert result.state is not initial_state
        # outbox финального state содержит ответ
        assert len(result.state.outbox) == 1

    # --- store_memory ---

    @pytest.mark.asyncio
    async def test_store_memory_instruction(self, vm, vm_ctx, mock_memory):
        program = {
            "plan": [
                {
                    "id": "mem",
                    "instruction": "store_memory",
                    "on_error": "continue",
                    "params": {"content": "факт для памяти", "memory_type": "note"},
                },
                {
                    "id": "resp",
                    "instruction": "respond",
                    "on_error": "continue",
                    "params": {"text": "запомнил"},
                },
            ]
        }
        result = await vm.run(program, vm_ctx)

        mock_memory.add_memory.assert_called_once()
        call_kwargs = mock_memory.add_memory.call_args.kwargs
        assert call_kwargs["content"] == "факт для памяти"
        assert call_kwargs["memory_type"] == "note"

        # memory_write отражён в state
        assert len(result.state.memory.entries) == 1
        assert result.state.memory.entries[0]["content"] == "факт для памяти"


# ===========================================================================
# ExecutionVM._resolve
# ===========================================================================

class TestVMResolve:
    def setup_method(self):
        from app.runtime.registry import InstructionRegistry
        from app.runtime.vm import ExecutionVM

        self.vm = ExecutionVM(InstructionRegistry())

    def _ctx_with_vars(self, variables: dict):
        from app.runtime.state_context import StateContext
        from app.runtime.context import VMContext
        from app.runtime.llm_adapter import MockLLMAdapter

        ctx = VMContext(
            state=StateContext.from_defaults(user_id=1),
            llm=MockLLMAdapter(),
            memory=MagicMock(),
            tools=MagicMock(),
        )
        ctx.variables = variables
        return ctx

    def test_resolve_string_ref(self):
        ctx = self._ctx_with_vars({"step1": "привет"})
        assert self.vm._resolve("$step1", ctx) == "привет"

    def test_resolve_plain_string(self):
        ctx = self._ctx_with_vars({})
        assert self.vm._resolve("просто текст", ctx) == "просто текст"

    def test_resolve_dict(self):
        ctx = self._ctx_with_vars({"s1": "val"})
        resolved = self.vm._resolve({"key": "$s1", "other": "literal"}, ctx)
        assert resolved == {"key": "val", "other": "literal"}

    def test_resolve_list(self):
        ctx = self._ctx_with_vars({"x": "result"})
        resolved = self.vm._resolve(["$x", "plain"], ctx)
        assert resolved == ["result", "plain"]

    def test_resolve_unresolved_ref_returns_none(self):
        ctx = self._ctx_with_vars({})
        result = self.vm._resolve("$missing", ctx)
        assert result is None

    def test_resolve_nested(self):
        ctx = self._ctx_with_vars({"a": "A"})
        resolved = self.vm._resolve({"outer": {"inner": "$a"}}, ctx)
        assert resolved == {"outer": {"inner": "A"}}

    def test_resolve_non_string_passthrough(self):
        ctx = self._ctx_with_vars({})
        assert self.vm._resolve(42, ctx) == 42
        assert self.vm._resolve(None, ctx) is None
        assert self.vm._resolve(True, ctx) is True


# ===========================================================================
# Planner._parse — unit тест
# ===========================================================================

class TestPlannerParse:
    def setup_method(self):
        from app.runtime.llm_adapter import MockLLMAdapter
        from app.runtime.planner import Planner

        self.planner = Planner(MockLLMAdapter())

    def _valid_json(self, steps: list) -> str:
        return json.dumps({"plan": steps})

    def _step(self, id_="step1", instruction="call_llm"):
        return {
            "id": id_,
            "instruction": instruction,
            "on_error": "abort",
            "params": {"prompt": "тест"},
        }

    # --- корректный JSON ---

    def test_parse_valid_json(self):
        raw = self._valid_json([self._step()])
        program = self.planner._parse(raw, "тест")
        assert "plan" in program
        assert len(program["plan"]) == 1

    def test_parse_json_with_markdown_fence(self):
        raw = "```json\n" + self._valid_json([self._step()]) + "\n```"
        program = self.planner._parse(raw, "тест")
        assert len(program["plan"]) == 1

    def test_parse_json_with_plain_fence(self):
        raw = "```\n" + self._valid_json([self._step()]) + "\n```"
        program = self.planner._parse(raw, "тест")
        assert len(program["plan"]) == 1

    def test_parse_sets_default_on_error(self):
        """Шаг без on_error должен получить on_error='abort' по умолчанию."""
        step = {"id": "s1", "instruction": "call_llm", "params": {"prompt": "x"}}
        raw = json.dumps({"plan": [step]})
        program = self.planner._parse(raw, "x")
        assert program["plan"][0]["on_error"] == "abort"

    def test_parse_preserves_explicit_on_error_continue(self):
        step = {
            "id": "s1",
            "instruction": "respond",
            "on_error": "continue",
            "params": {"text": "hi"},
        }
        raw = json.dumps({"plan": [step]})
        program = self.planner._parse(raw, "hi")
        assert program["plan"][0]["on_error"] == "continue"

    # --- невалидный JSON ---

    def test_parse_invalid_json_raises_value_error(self):
        with pytest.raises(ValueError):
            self.planner._parse("это не JSON", "тест")

    def test_parse_no_json_object_raises(self):
        with pytest.raises(ValueError, match="No JSON object found"):
            self.planner._parse("просто текст без фигурных скобок", "тест")

    def test_parse_missing_plan_key_raises(self):
        raw = json.dumps({"steps": []})  # нет ключа "plan"
        with pytest.raises(ValueError, match="Invalid program structure"):
            self.planner._parse(raw, "тест")

    def test_parse_plan_not_list_raises(self):
        raw = json.dumps({"plan": "не список"})
        with pytest.raises(ValueError, match="Invalid program structure"):
            self.planner._parse(raw, "тест")

    def test_parse_empty_plan_raises(self):
        raw = json.dumps({"plan": []})
        with pytest.raises(ValueError, match="Empty plan"):
            self.planner._parse(raw, "тест")

    def test_parse_garbage_with_embedded_json(self):
        """LLM добавил текст вокруг JSON — должен распарситься."""
        inner = self._valid_json([self._step()])
        raw = f"Конечно, вот программа:\n{inner}\nНадеюсь помог!"
        program = self.planner._parse(raw, "тест")
        assert len(program["plan"]) == 1

    # --- fallback через generate() ---

    @pytest.mark.asyncio
    async def test_generate_returns_fallback_on_parse_error(self):
        from app.runtime.llm_adapter import MockLLMAdapter
        from app.runtime.planner import Planner

        bad_llm = MockLLMAdapter(fixed_response="не JSON вообще")
        planner = Planner(bad_llm)
        program = await planner.generate("тестовый запрос")

        # fallback: 2 шага, call_llm + respond
        assert "plan" in program
        assert len(program["plan"]) == 2
        assert program["plan"][0]["instruction"] == "call_llm"
        assert program["plan"][1]["instruction"] == "respond"
        assert program["plan"][0]["params"]["prompt"] == "тестовый запрос"

    @pytest.mark.asyncio
    async def test_generate_valid_program_from_mock(self):
        from app.runtime.llm_adapter import MockLLMAdapter
        from app.runtime.planner import Planner

        valid_program = {
            "plan": [
                {
                    "id": "step1",
                    "instruction": "call_llm",
                    "on_error": "abort",
                    "params": {"prompt": "тест", "role": "default"},
                },
                {
                    "id": "step2",
                    "instruction": "respond",
                    "on_error": "continue",
                    "params": {"text": "$step1"},
                },
            ]
        }
        good_llm = MockLLMAdapter(fixed_response=json.dumps(valid_program))
        planner = Planner(good_llm)
        program = await planner.generate("тест")

        assert len(program["plan"]) == 2
        assert good_llm.calls[0]["role"] == "planner"


# ===========================================================================
# ToolRegistry + CallToolInstruction (P-6)
# ===========================================================================

class TestToolRegistry:
    """ToolRegistry проксирует вызовы в SkillLoader."""

    def _make_registry(self, skills: dict):
        """Создаёт ToolRegistry с мок-лоадером."""
        from unittest.mock import MagicMock
        from app.runtime.tool_registry import ToolRegistry

        loader = MagicMock()
        loader.list_skills.return_value = []

        def get_skill(name):
            return skills.get(name)

        loader.get_skill.side_effect = get_skill
        return ToolRegistry(loader)

    @pytest.mark.asyncio
    async def test_execute_known_sync_tool(self):
        def my_tool(args): return f"ok:{args}"
        registry = self._make_registry({"my_tool": my_tool})
        result = await registry.execute("my_tool", {"x": 1})
        assert result == "ok:{'x': 1}"

    @pytest.mark.asyncio
    async def test_execute_known_async_tool(self):
        async def async_tool(args): return "async_ok"
        registry = self._make_registry({"async_tool": async_tool})
        result = await registry.execute("async_tool", {})
        assert result == "async_ok"

    @pytest.mark.asyncio
    async def test_execute_unknown_tool_returns_error_string(self):
        registry = self._make_registry({})
        result = await registry.execute("ghost", {})
        assert isinstance(result, str)
        assert "[ToolRegistry]" in result
        assert "ghost" in result

    @pytest.mark.asyncio
    async def test_execute_tool_exception_returns_error_string(self):
        def broken(args): raise RuntimeError("boom")
        registry = self._make_registry({"broken": broken})
        result = await registry.execute("broken", {})
        assert "[ToolRegistry]" in result
        assert "boom" in result

    def test_list_tools(self):
        from unittest.mock import MagicMock
        from app.runtime.tool_registry import ToolRegistry
        from app.core.skills_loader import SkillInfo

        loader = MagicMock()
        loader.list_skills.return_value = [
            SkillInfo(name="echo", description="d", is_active=True),
            SkillInfo(name="hidden", description="d", is_active=False),
        ]
        registry = ToolRegistry(loader)
        tools = registry.list_tools()
        assert "echo" in tools
        assert "hidden" not in tools


class TestCallToolInstruction:
    """CallToolInstruction интегрируется с ToolRegistry."""

    def _make_ctx(self, tool_registry=None):
        from unittest.mock import MagicMock
        from app.runtime.context import VMContext
        from app.runtime.state_context import StateContext

        state = StateContext.from_defaults(user_id=1, agent_mode="runtime")
        ctx = VMContext(
            state=state,
            llm=MagicMock(),
            memory=AsyncMock(),
            tools=tool_registry,
        )
        return ctx

    @pytest.mark.asyncio
    async def test_call_tool_success(self):
        from app.runtime.instructions.call_tool import CallToolInstruction
        from app.runtime.tool_registry import ToolRegistry
        from unittest.mock import MagicMock

        loader = MagicMock()
        loader.get_skill.return_value = lambda args: "result"
        loader.list_skills.return_value = []
        registry = ToolRegistry(loader)

        ctx = self._make_ctx(registry)
        instr = CallToolInstruction()
        result = await instr.execute("s1", {"tool": "mytool", "args": {}}, ctx)
        assert result.status == "ok"
        assert result.output == "result"

    @pytest.mark.asyncio
    async def test_call_tool_missing_param(self):
        from app.runtime.instructions.call_tool import CallToolInstruction
        ctx = self._make_ctx()
        instr = CallToolInstruction()
        result = await instr.execute("s1", {}, ctx)
        assert result.status == "error"
        assert "tool" in result.error

    @pytest.mark.asyncio
    async def test_call_tool_none_registry(self):
        from app.runtime.instructions.call_tool import CallToolInstruction
        ctx = self._make_ctx(tool_registry=None)
        instr = CallToolInstruction()
        result = await instr.execute("s1", {"tool": "any"}, ctx)
        assert result.status == "error"
        assert "None" in result.error

    @pytest.mark.asyncio
    async def test_call_tool_unknown_tool_returns_error_step(self):
        from app.runtime.instructions.call_tool import CallToolInstruction
        from app.runtime.tool_registry import ToolRegistry
        from unittest.mock import MagicMock

        loader = MagicMock()
        loader.get_skill.return_value = None
        loader.list_skills.return_value = []
        registry = ToolRegistry(loader)

        ctx = self._make_ctx(registry)
        instr = CallToolInstruction()
        result = await instr.execute("s1", {"tool": "ghost"}, ctx)
        assert result.status == "error"
        assert "ghost" in result.error


# ===========================================================================
# Регрессионные тесты багфиксов (Фаза 2)
# ===========================================================================

class TestRegressionFixes:
    """Точечные тесты исправленных дефектов VM/instructions/StateContext."""

    @pytest.mark.asyncio
    async def test_step_without_instruction_returns_error_step(self, vm, vm_ctx):
        """Шаг без 'instruction' не должен бросать KeyError из run()."""
        program = {"plan": [{"id": "s1", "params": {"x": 1}}]}
        result = await vm.run(program, vm_ctx)

        assert result.aborted is True
        assert len(result.results) == 1
        assert result.results[0].status == "error"
        assert "instruction" in result.results[0].error

    @pytest.mark.asyncio
    async def test_respond_none_text_produces_empty_message(self, vm, vm_ctx):
        """Неразрешённый $ref (None) в respond → пустая строка, а не error-шаг."""
        program = {
            "plan": [
                {
                    "id": "out",
                    "instruction": "respond",
                    "on_error": "continue",
                    "params": {"text": "$missing_step"},
                }
            ]
        }
        result = await vm.run(program, vm_ctx)

        assert result.aborted is False
        assert result.results[0].status == "ok"
        assert len(result.outbox) == 1
        assert result.outbox[0].text == ""

    def test_to_db_context_actual_fsm_state_wins(self):
        """Актуальный fsm_state не должен перетираться значением из extra."""
        from app.runtime.state_context import StateContext
        from app.runtime.builder import StepResultBuilder

        fake = MagicMock()
        fake.user_id = 1
        fake.current_agent = "runtime"
        fake.context = {"fsm_state": "idle", "skill_name": "calc"}

        ctx = StateContext.from_db(fake)
        transition = StepResultBuilder("s1", "x").transition("awaiting_input").build()
        ctx2 = ctx.apply(transition)

        db_ctx = ctx2.to_db_context()
        assert db_ctx["fsm_state"] == "awaiting_input"
        assert db_ctx["skill_name"] == "calc"

    @pytest.mark.asyncio
    async def test_call_llm_ignores_unknown_params(self, vm_ctx):
        """Лишние ключи params (напр. temperature) не должны ронять шаг."""
        from app.runtime.instructions.call_llm import CallLLMInstruction
        from app.runtime.llm_adapter import MockLLMAdapter

        vm_ctx.llm = MockLLMAdapter()
        instr = CallLLMInstruction()
        result = await instr.execute(
            "s1",
            {"prompt": "тест", "role": "default", "temperature": 0.2, "max_tokens": 100},
            vm_ctx,
        )
        assert result.status == "ok"
        assert result.output == vm_ctx.llm.fixed_response
        assert "temperature" not in vm_ctx.llm.calls[0]

    @pytest.mark.asyncio
    async def test_call_llm_missing_prompt_returns_error(self):
        """Отсутствие обязательного prompt → error StepResult, не TypeError."""
        from app.runtime.instructions.call_llm import CallLLMInstruction

        instr = CallLLMInstruction()
        result = await instr.execute("s1", {"role": "default"}, MagicMock())
        assert result.status == "error"
        assert "prompt" in result.error
