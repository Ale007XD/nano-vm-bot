"""
MCP-клиент: парсинг конфигурации, поведение без пакета mcp,
прокидывание внешних инструментов через ToolRegistry.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.core import mcp as mcp_mod


class TestParseMcpServers:
    def test_empty(self):
        assert mcp_mod.parse_mcp_servers("") == []
        assert mcp_mod.parse_mcp_servers("   ") == []

    def test_stdio_server(self):
        servers = mcp_mod.parse_mcp_servers(
            '[{"name":"fetch","command":"uvx","args":["mcp-server-fetch"]}]'
        )
        assert len(servers) == 1
        assert servers[0].name == "fetch"
        assert servers[0].command == "uvx"
        assert servers[0].args == ["mcp-server-fetch"]
        assert servers[0].url is None

    def test_http_server(self):
        servers = mcp_mod.parse_mcp_servers(
            '[{"name":"remote","url":"https://example.com/mcp"}]'
        )
        assert servers[0].url == "https://example.com/mcp"
        assert servers[0].command is None

    def test_invalid_json_raises(self):
        with pytest.raises(ValueError, match="некорректный JSON"):
            mcp_mod.parse_mcp_servers("[{")

    def test_not_a_list_raises(self):
        with pytest.raises(ValueError, match="JSON-список"):
            mcp_mod.parse_mcp_servers('{"name":"x"}')

    def test_missing_name_raises(self):
        with pytest.raises(ValueError, match="name"):
            mcp_mod.parse_mcp_servers('[{"command":"x"}]')

    def test_missing_transport_raises(self):
        with pytest.raises(ValueError, match="url.*command|command.*url"):
            mcp_mod.parse_mcp_servers('[{"name":"x"}]')


class TestMCPManagerWithoutPackage:
    """Пакет mcp не установлен в тестовом окружении — менеджер деградирует мягко."""

    @pytest.fixture
    def manager(self, monkeypatch):
        m = mcp_mod.MCPManager()
        monkeypatch.setattr(mcp_mod, "_client_available", lambda: False)
        return m

    @pytest.mark.asyncio
    async def test_list_tools_empty_when_no_client(self, manager, monkeypatch):
        servers = mcp_mod.parse_mcp_servers('[{"name":"x","url":"https://h/mcp"}]')
        monkeypatch.setattr(manager, "servers", lambda: servers)
        assert await manager.list_tools() == []

    @pytest.mark.asyncio
    async def test_call_tool_reports_missing_package(self, manager, monkeypatch):
        monkeypatch.setattr(manager, "servers", lambda: [])
        result = await manager.call_tool("fetch__fetch", {})
        assert result == "[MCP] mcp package is not installed"

    @pytest.mark.asyncio
    async def test_call_tool_unknown(self, manager, monkeypatch):
        monkeypatch.setattr(manager, "servers", lambda: [])
        # без пакета вызов невозможен в принципе — честное сообщение об этом
        result = await manager.call_tool("ghost__tool", {})
        assert result == "[MCP] mcp package is not installed"

    def test_content_to_text(self):
        result = SimpleNamespace(
            content=[SimpleNamespace(text="первый"), SimpleNamespace(text="второй")]
        )
        assert mcp_mod.MCPManager._content_to_text(result) == "первый\nвторой"

    def test_content_to_text_fallback(self):
        assert mcp_mod.MCPManager._content_to_text("plain") == "plain"


class TestToolRegistryMcpFallback:
    @pytest.mark.asyncio
    async def test_unknown_tool_mentions_mcp(self, monkeypatch):
        import sys

        monkeypatch.setattr(sys.modules["app.core.config"].settings, "ADMIN_IDS", [1])
        from app.runtime.tool_registry import ToolRegistry

        loader = MagicMock()
        loader.get_skill.return_value = None
        loader.list_skills.return_value = []
        registry = ToolRegistry(loader)

        result = await registry.execute("ghost__tool", {}, user_id=1)
        assert result.startswith("[ToolRegistry]")
        assert "ghost__tool" in result
