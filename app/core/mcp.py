"""
MCP-клиент: внешние инструменты через Model Context Protocol.

Конфигурация — settings.MCP_SERVERS, JSON одной строкой в .env:
  MCP_SERVERS=[{"name":"fetch","command":"uvx","args":["mcp-server-fetch"]}]
  MCP_SERVERS=[{"name":"remote","url":"https://example.com/mcp"}]

Каждый сервер: name + (command + args — stdio-подпроцесс) или (url —
Streamable HTTP). Имя инструмента для call_tool: "<server>__<tool>".

Пакет `mcp` требует Python 3.11+ и подключается мягко: если не установлен,
менеджер сообщает об этом, остальной бот работает как раньше.
"""
import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

TOOL_SEP = "__"
LIST_CACHE_TTL = 300.0  # секунд
CALL_TIMEOUT = 60.0


def _client_available() -> bool:
    try:
        import mcp  # noqa: F401

        return True
    except ImportError:
        return False


@dataclass
class MCPServerConfig:
    name: str
    command: Optional[str] = None
    args: List[str] = field(default_factory=list)
    url: Optional[str] = None
    env: Optional[Dict[str, str]] = None


def parse_mcp_servers(raw: str) -> List[MCPServerConfig]:
    """JSON-строка из MCP_SERVERS → список конфигов серверов."""
    if not isinstance(raw, str) or not raw.strip():
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"MCP_SERVERS: некорректный JSON ({e})")
    if not isinstance(data, list):
        raise ValueError("MCP_SERVERS: ожидается JSON-список серверов")

    servers: List[MCPServerConfig] = []
    for item in data:
        if not isinstance(item, dict) or not item.get("name"):
            raise ValueError("MCP_SERVERS: каждый сервер должен иметь 'name'")
        if not (item.get("url") or item.get("command")):
            raise ValueError(f"MCP_SERVERS[{item['name']}]: нужен 'url' или 'command'")
        servers.append(
            MCPServerConfig(
                name=str(item["name"]),
                command=item.get("command"),
                args=list(item.get("args") or []),
                url=item.get("url"),
                env=item.get("env"),
            )
        )
    return servers


class MCPManager:
    """Подключение к внешним MCP-серверам и вызов их инструментов."""

    def __init__(self) -> None:
        self._tools_cache: Optional[List[Dict[str, Any]]] = None
        self._cache_at: float = 0.0

    def servers(self) -> List[MCPServerConfig]:
        from app.core.config import settings

        try:
            return parse_mcp_servers(getattr(settings, "MCP_SERVERS", "") or "")
        except ValueError as e:
            logger.warning(str(e))
            return []

    async def list_tools(self, force_refresh: bool = False) -> List[Dict[str, Any]]:
        """Инструменты всех серверов: [{name, description, input_schema, server, tool}].

        Список кэшируется на LIST_CACHE_TTL; недоступный сервер просто
        пропускается с warning — бот продолжает работать.
        """
        if (
            not force_refresh
            and self._tools_cache is not None
            and time.monotonic() - self._cache_at < LIST_CACHE_TTL
        ):
            return self._tools_cache

        tools: List[Dict[str, Any]] = []
        if _client_available():
            for srv in self.servers():
                try:
                    raw = await asyncio.wait_for(
                        self._session_call(srv, lambda s: s.list_tools()),
                        timeout=CALL_TIMEOUT,
                    )
                    for t in raw.tools:
                        tools.append(
                            {
                                "name": f"{srv.name}{TOOL_SEP}{t.name}",
                                "description": (t.description or "")[:300],
                                "input_schema": getattr(t, "inputSchema", None)
                                or {"type": "object", "properties": {}},
                                "server": srv.name,
                                "tool": t.name,
                            }
                        )
                except Exception as e:
                    logger.warning("MCP list_tools failed for %s: %s", srv.name, e)
        elif self.servers():
            logger.warning("MCP servers configured, but 'mcp' package is not installed")

        self._tools_cache = tools
        self._cache_at = time.monotonic()
        return tools

    async def call_tool(self, name: str, args: Dict[str, Any]) -> str:
        """Вызвать внешний инструмент '<server>__<tool>'. Возвращает текст.

        Ошибки возвращаются строкой с префиксом '[MCP]' (не бросаются).
        """
        if not _client_available():
            return "[MCP] mcp package is not installed"

        for tool in await self.list_tools():
            if tool["name"] == name:
                for srv in self.servers():
                    if srv.name != tool["server"]:
                        continue
                    try:
                        result = await asyncio.wait_for(
                            self._session_call(
                                srv, lambda s: s.call_tool(tool["tool"], args)
                            ),
                            timeout=CALL_TIMEOUT,
                        )
                        return self._content_to_text(result)
                    except Exception as e:
                        logger.warning("MCP call %s failed: %s", name, e)
                        return f"[MCP] tool '{name}' error: {e}"
        return f"[MCP] tool '{name}' not found"

    async def _session_call(self, srv: MCPServerConfig, fn: Callable):
        """Открыть сессию к серверу и выполнить fn(session)."""
        from mcp import ClientSession

        if srv.url:
            from mcp.client.streamable_http import streamablehttp_client

            async with streamablehttp_client(srv.url) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    return await fn(session)

        from mcp import StdioServerParameters
        from mcp.client.stdio import stdio_client

        params = StdioServerParameters(
            command=srv.command,
            args=list(srv.args or []),
            env=srv.env or None,
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await fn(session)

    @staticmethod
    def _content_to_text(result: Any) -> str:
        parts = []
        for block in getattr(result, "content", None) or []:
            text = getattr(block, "text", None)
            if text:
                parts.append(text)
        return ("\n".join(parts) or str(result))[:8000]


mcp_manager = MCPManager()
