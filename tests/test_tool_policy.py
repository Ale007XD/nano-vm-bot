"""
Политика call_tool в Runtime VM: allowlist для не-админов и принудительный
user_id из StateContext.

Модель угроз: planner-LLM недоверен (prompt injection через сообщения, память,
пересланные посты). Он генерирует tool и args, поэтому не должен мочь:
  * вызвать инструмент вне allowlist (import_chat читает файл с сервера,
    youtube_transcript запускает yt-dlp, MCP-инструменты ходят в сеть);
  * подменить user_id и прочитать/записать чужую память.
"""
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

ADMIN = 1
USER = 500
VICTIM = 777


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    st = sys.modules["app.core.config"].settings
    monkeypatch.setattr(st, "ADMIN_IDS", [ADMIN])
    monkeypatch.setattr(st, "RUNTIME_USER_TOOLS", "validated_answer")
    return st


def _registry(skills: dict):
    from app.runtime.tool_registry import ToolRegistry

    loader = MagicMock()
    loader.list_skills.return_value = []
    loader.get_skill.side_effect = lambda n: skills.get(n)
    return ToolRegistry(loader)


class TestAllowlistParsing:
    def test_default(self, monkeypatch, _settings):
        from app.core.access import user_tool_allowlist

        monkeypatch.setattr(_settings, "RUNTIME_USER_TOOLS", "validated_answer")
        assert user_tool_allowlist() == {"validated_answer"}

    def test_custom_with_spaces_and_empties(self, monkeypatch, _settings):
        from app.core.access import user_tool_allowlist

        monkeypatch.setattr(_settings, "RUNTIME_USER_TOOLS", " a , b,, fetch__fetch ,")
        assert user_tool_allowlist() == {"a", "b", "fetch__fetch"}

    def test_empty_string_means_nothing(self, monkeypatch, _settings):
        from app.core.access import user_tool_allowlist

        monkeypatch.setattr(_settings, "RUNTIME_USER_TOOLS", "")
        assert user_tool_allowlist() == frozenset()

    def test_non_string_falls_back_to_safe_default(self, monkeypatch, _settings):
        from app.core.access import DEFAULT_USER_TOOLS, user_tool_allowlist

        monkeypatch.setattr(_settings, "RUNTIME_USER_TOOLS", MagicMock())
        assert user_tool_allowlist() == DEFAULT_USER_TOOLS


class TestCanUseTool:
    def test_admin_any_tool(self):
        from app.core.access import can_use_tool

        assert can_use_tool(ADMIN, "import_chat")
        assert can_use_tool(ADMIN, "fetch__fetch")

    def test_user_only_allowlisted(self):
        from app.core.access import can_use_tool

        assert can_use_tool(USER, "validated_answer")
        assert not can_use_tool(USER, "import_chat")
        assert not can_use_tool(USER, "youtube_transcript")
        assert not can_use_tool(USER, "fetch__fetch")

    def test_admin_can_extend_allowlist_for_users(self, monkeypatch, _settings):
        from app.core.access import can_use_tool

        monkeypatch.setattr(_settings, "RUNTIME_USER_TOOLS", "validated_answer,fetch__fetch")
        assert can_use_tool(USER, "fetch__fetch")


class TestToolRegistryPolicy:
    @pytest.mark.asyncio
    async def test_non_admin_denied_outside_allowlist_skill_not_called(self):
        skill = AsyncMock(return_value="leaked")
        reg = _registry({"import_chat": skill})
        r = await reg.execute(
            "import_chat", {"file_path": "/app/data/secret.json"}, user_id=USER
        )
        assert r.startswith("[ToolRegistry]")
        assert "not available" in r
        skill.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_admin_denied_mcp_tool_no_network_call(self, monkeypatch):
        from app.core import mcp as mcp_mod

        called = AsyncMock(return_value="data")
        monkeypatch.setattr(mcp_mod.mcp_manager, "call_tool", called)
        reg = _registry({})
        r = await reg.execute("fetch__fetch", {"url": "http://169.254.169.254"}, user_id=USER)
        assert r.startswith("[ToolRegistry]")
        called.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_admin_allowed_tool_runs(self):
        skill = AsyncMock(return_value="answer")
        reg = _registry({"validated_answer": skill})
        assert await reg.execute("validated_answer", {"query": "q"}, user_id=USER) == "answer"

    @pytest.mark.asyncio
    async def test_user_id_from_args_is_overridden(self):
        """planner подсовывает чужой user_id — навык получает настоящий."""
        skill = AsyncMock(return_value="ok")
        reg = _registry({"validated_answer": skill})
        await reg.execute("validated_answer", {"query": "q", "user_id": VICTIM}, user_id=USER)
        ctx = skill.await_args.args[0]
        assert ctx["user_id"] == USER
        assert ctx["query"] == "q"

    @pytest.mark.asyncio
    async def test_admin_user_id_is_also_forced(self):
        skill = AsyncMock(return_value="ok")
        reg = _registry({"import_chat": skill})
        await reg.execute("import_chat", {"user_id": VICTIM, "file_path": "/x"}, user_id=ADMIN)
        assert skill.await_args.args[0]["user_id"] == ADMIN

    @pytest.mark.asyncio
    async def test_missing_user_context_fails_closed(self):
        skill = AsyncMock(return_value="ok")
        reg = _registry({"validated_answer": skill})
        r = await reg.execute("validated_answer", {})
        assert r.startswith("[ToolRegistry]") and "no user context" in r
        skill.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad", ["str", None, ["a"], 5])
    async def test_non_dict_args_rejected(self, bad):
        skill = AsyncMock(return_value="ok")
        reg = _registry({"validated_answer": skill})
        r = await reg.execute("validated_answer", bad, user_id=USER)
        assert r.startswith("[ToolRegistry]")
        skill.assert_not_called()

    @pytest.mark.asyncio
    async def test_denial_checked_before_skill_lookup(self):
        """Не-админ не должен узнавать, существует ли навык (одинаковый отказ)."""
        reg = _registry({"import_chat": AsyncMock()})
        a = await reg.execute("import_chat", {}, user_id=USER)
        b = await reg.execute("does_not_exist", {}, user_id=USER)
        assert "not available" in a and "not available" in b


class TestEndToEndThroughVM:
    """planner-программа с атакой → VM → CallToolInstruction → ToolRegistry."""

    def _ctx(self, registry, user_id):
        from app.runtime.context import VMContext
        from app.runtime.state_context import StateContext

        return VMContext(
            state=StateContext.from_defaults(user_id=user_id, agent_mode="runtime"),
            llm=MagicMock(),
            memory=AsyncMock(),
            tools=registry,
        )

    @pytest.fixture
    def vm(self):
        from app.runtime.registry import InstructionRegistry
        from app.runtime.vm import ExecutionVM

        return ExecutionVM(registry=InstructionRegistry())

    @pytest.mark.asyncio
    async def test_injected_import_chat_blocked_for_user(self, vm):
        skill = AsyncMock(return_value="imported")
        ctx = self._ctx(_registry({"import_chat": skill}), USER)
        program = {"plan": [{
            "id": "s1", "instruction": "call_tool", "on_error": "abort",
            "params": {"tool": "import_chat",
                       "args": {"file_path": "/app/data/x.json", "user_id": VICTIM}},
        }]}
        res = await vm.run(program, ctx)
        assert res.aborted
        assert res.results[0].status == "error"
        skill.assert_not_called()

    @pytest.mark.asyncio
    async def test_allowed_tool_gets_state_user_id_not_args(self, vm):
        skill = AsyncMock(return_value="ok")
        ctx = self._ctx(_registry({"validated_answer": skill}), USER)
        program = {"plan": [{
            "id": "s1", "instruction": "call_tool",
            "params": {"tool": "validated_answer",
                       "args": {"query": "q", "user_id": VICTIM}},
        }]}
        res = await vm.run(program, ctx)
        assert res.results[0].status == "ok"
        assert skill.await_args.args[0]["user_id"] == USER

    @pytest.mark.asyncio
    async def test_dollar_ref_cannot_smuggle_user_id(self, vm):
        """user_id, пришедший через $-ref из вывода другого шага, тоже перезаписывается."""
        skill = AsyncMock(return_value="ok")
        ctx = self._ctx(_registry({"validated_answer": skill}), USER)
        ctx.variables["prev"] = VICTIM
        program = {"plan": [{
            "id": "s1", "instruction": "call_tool",
            "params": {"tool": "validated_answer", "args": {"query": "q", "user_id": "$prev"}},
        }]}
        await vm.run(program, ctx)
        assert skill.await_args.args[0]["user_id"] == USER
