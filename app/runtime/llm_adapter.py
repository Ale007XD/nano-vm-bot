"""
LLMAdapter — изолирует runtime от конкретной реализации MultiProviderLLMClient.

VM и instructions зависят от LLMProtocol, не от llm_client_v2.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Protocol, Tuple, runtime_checkable


@runtime_checkable
class LLMProtocol(Protocol):
    """Минимальный контракт LLM-адаптера для runtime."""

    async def generate(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        role: Optional[str] = None,
        system: Optional[str] = None,
    ) -> Tuple[str, Optional[List[Dict[str, Any]]]]:
        ...


class MultiProviderLLMAdapter:
    """Адаптирует MultiProviderLLMClient под LLMProtocol."""

    def __init__(self, client):
        self._client = client

    async def generate(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        role: Optional[str] = None,
        system: Optional[str] = None,
    ) -> Tuple[str, Optional[List[Dict[str, Any]]]]:
        from app.core.llm_client_v2 import Message

        # system= алиас system_prompt= (Planner передаёт system=)
        effective_system = system_prompt or system
        # role= определяет модель через _map_model_to_provider (default/planner/coder)
        effective_model = role or "default"

        messages = []
        if effective_system:
            messages.append(Message(role="system", content=effective_system))
        messages.append(Message(role="user", content=prompt))

        # Planner должен вернуть JSON-программу: на Groq включаем json_object
        # и medium-усилие рассуждения (groq-only, остальные провайдеры игнорируют)
        response_format = {"type": "json_object"} if role == "planner" else None
        reasoning_effort = "medium" if role == "planner" else None

        response = await self._client.chat(
            messages=messages,
            model=effective_model,
            tools=tools,
            response_format=response_format,
            reasoning_effort=reasoning_effort,
        )

        if isinstance(response, dict):
            return response.get("text", ""), response.get("tool_calls")
        return str(response.content if hasattr(response, 'content') else response), None


class MockLLMAdapter:
    """Детерминированный адаптер для unit-тестов VM."""

    def __init__(self, fixed_response: str = "mock_response"):
        self.fixed_response = fixed_response
        self.calls: list[dict] = []

    async def generate(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        # Принимаем role и system как алиасы для совместимости с тестами
        # и с call_llm.py который передаёт role= и system=
        role: Optional[str] = None,
        system: Optional[str] = None,
    ) -> Tuple[str, Optional[List[Dict[str, Any]]]]:
        # Нормализуем: system= и system_prompt= — одно и то же
        effective_system = system_prompt or system

        self.calls.append({
            "prompt": prompt,
            "system_prompt": effective_system,
            "role": role,
            "tools": tools,
        })

        if tools:
            mock_tool_call = [{"name": tools[0]["name"], "arguments": {}}]
            return f"Mocked tool response to: {prompt}", mock_tool_call

        return self.fixed_response, None
