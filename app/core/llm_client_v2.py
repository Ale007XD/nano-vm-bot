"""
Multi-Provider LLM Client with automatic fallback and task recovery
Supports: Groq, OpenRouter, Anthropic, OpenAI

Changelog:
- Fixed Anthropic provider: system prompt as top-level param, /v1/messages endpoint
- Added reload_provider() for hot key/priority swap without restart
- Added _check_single_provider() for immediate health verification
- Health monitor starts lazily on first chat() call
- v2 is now the SINGLE source of truth: llm_client_v2.llm_client is the one
  global MultiProviderLLMClient instance used by all agents and handlers.
  llm_client.py (v1 wrapper) is REMOVED — import from here directly.
- Added chat_with_fallback() as a semantic alias for chat() (backward compat).
- Added public check_health() — replaces _check_providers_health() call sites.
- transcribe_audio() stays on MultiProviderLLMClient (no separate wrapper needed).
"""
import json
import httpx
import asyncio
import time
from typing import List, Dict, Optional, Any, Callable, Union
from dataclasses import dataclass
from enum import Enum
from datetime import datetime

from app.core.config import settings
import logging

logger = logging.getLogger(__name__)


class ProviderStatus(Enum):
    """Provider health status"""
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    DOWN = "down"


@dataclass
class Provider:
    """LLM Provider configuration"""
    name: str
    base_url: str
    api_key: str
    models: List[str]
    priority: int = 0
    status: ProviderStatus = ProviderStatus.HEALTHY
    last_error: Optional[str] = None
    error_count: int = 0
    last_used: Optional[datetime] = None
    response_time_ms: float = 0.0


@dataclass
class Message:
    """LLM message structure"""
    role: str
    content: str


@dataclass
class LLMResponse:
    """LLM response structure"""
    content: str
    model: str
    provider: str
    usage: Optional[Dict[str, int]] = None
    finish_reason: Optional[str] = None
    response_time_ms: float = 0.0


@dataclass
class PendingTask:
    """Task waiting to be processed"""
    id: str
    messages: List[Message]
    model: Optional[str]
    temperature: Optional[float]
    max_tokens: Optional[int]
    created_at: datetime
    attempts: int = 0
    last_error: Optional[str] = None
    callback: Optional[Callable] = None


class MultiProviderLLMClient:
    """Multi-provider LLM client with automatic fallback"""

    def __init__(self):
        self.providers: List[Provider] = []
        self.pending_tasks: Dict[str, PendingTask] = {}
        self.max_retries = 3
        self.retry_delay = 2.0
        self.health_check_interval = 60  # seconds
        self._health_monitor_started = False
        self._setup_providers()

    def _setup_providers(self):
        """Initialize providers from settings"""
        # Provider 1: Groq (primary)
        if hasattr(settings, 'GROQ_API_KEY') and settings.GROQ_API_KEY:
            self.providers.append(Provider(
                name="groq",
                base_url="https://api.groq.com/openai/v1",
                api_key=settings.GROQ_API_KEY,
                models=[
                    "llama-3.1-8b-instant",
                    "llama-3.3-70b-versatile",
                    "gemma2-9b-it",
                    "whisper-large-v3"
                ],
                priority=1
            ))

        # Provider 2: OpenRouter (fallback)
        if settings.OPENROUTER_API_KEY:
            self.providers.append(Provider(
                name="openrouter",
                base_url="https://openrouter.ai/api/v1",
                api_key=settings.OPENROUTER_API_KEY,
                models=[
                    "meta-llama/llama-3.3-70b-instruct:free",
                    "meta-llama/llama-3.1-8b-instruct:free",
                ],
                priority=2
            ))

        # Provider 3: Anthropic (if key available)
        if hasattr(settings, 'ANTHROPIC_API_KEY') and settings.ANTHROPIC_API_KEY:
            self.providers.append(Provider(
                name="anthropic",
                base_url="https://api.anthropic.com",
                api_key=settings.ANTHROPIC_API_KEY,
                models=[
                    "claude-3-5-sonnet-20241022",
                    "claude-3-haiku-20240307"
                ],
                priority=3
            ))

        # Provider 4: OpenAI (if key available)
        if hasattr(settings, 'OPENAI_API_KEY') and settings.OPENAI_API_KEY:
            self.providers.append(Provider(
                name="openai",
                base_url="https://api.openai.com/v1",
                api_key=settings.OPENAI_API_KEY,
                models=[
                    "gpt-3.5-turbo",
                    "gpt-4o-mini"
                ],
                priority=4
            ))

        # Sort by priority
        self.providers.sort(key=lambda p: p.priority)
        logger.info(f"Initialized {len(self.providers)} providers: {[p.name for p in self.providers]}")

    # ------------------------------------------------------------------ #
    #  HOT RELOAD                                                          #
    # ------------------------------------------------------------------ #

    async def reload_provider(self, name: str, new_key: str) -> bool:
        """
        Hot-swap API key for a provider without restarting the bot.
        Resets error counters and runs immediate health check.
        Returns True if provider found and updated.
        """
        for p in self.providers:
            if p.name == name:
                p.api_key = new_key
                p.error_count = 0
                p.last_error = None
                p.status = ProviderStatus.HEALTHY
                logger.info(f"Provider {name}: key updated, running health check...")
                await self._check_single_provider(p)
                logger.info(f"Provider {name} post-reload status: {p.status.value}")
                return True
        return False

    async def set_provider_priority(self, name: str, priority: int) -> bool:
        """
        Change provider priority and re-sort the list.
        Lower number = higher priority (1 = primary).
        """
        for p in self.providers:
            if p.name == name:
                p.priority = priority
                self.providers.sort(key=lambda x: x.priority)
                logger.info(f"Provider {name} priority set to {priority}")
                return True
        return False

    async def set_provider_enabled(self, name: str, enabled: bool) -> bool:
        """Enable or disable a provider without removing it"""
        for p in self.providers:
            if p.name == name:
                if enabled:
                    p.status = ProviderStatus.HEALTHY
                    p.error_count = 0
                else:
                    p.status = ProviderStatus.DOWN
                logger.info(f"Provider {name} {'enabled' if enabled else 'disabled'}")
                return True
        return False

    def load_overrides_from_db(self, provider_name: str, role_models: Dict[str, str]):
        """Load model overrides from DB JSON block at startup."""
        if not hasattr(self, '_model_overrides'):
            self._model_overrides = {}
        if provider_name not in self._model_overrides:
            self._model_overrides[provider_name] = {}
        for role, model in role_models.items():
            self._model_overrides[provider_name][role] = model
            logger.info(f"Loaded DB override: {provider_name}.{role} = {model}")

    def get_assigned_model(self, provider_name: str, role: str) -> Optional[str]:
        """Return the exact model_id currently assigned to a role for a provider."""
        overrides = getattr(self, '_model_overrides', {})
        return overrides.get(provider_name, {}).get(role)

    def set_model(self, provider_name: str, role: str, model_idx: int) -> bool:
        """
        Назначить модель для роли (default/coder/planner) по индексу в provider.models.
        Индекс используется вместо строки в callback_data — избегает проблем с / и : в именах моделей.
        Возвращает True если провайдер найден и индекс валиден.
        """
        for p in self.providers:
            if p.name == provider_name:
                if model_idx < 0 or model_idx >= len(p.models):
                    logger.warning(f"set_model: invalid idx {model_idx} for {provider_name} (len={len(p.models)})")
                    return False
                model_id = p.models[model_idx]
                # _map_model_to_provider читает из self._model_overrides если есть,
                # иначе из захардкоженного словаря. Храним оверрайды в памяти процесса.
                if not hasattr(self, '_model_overrides'):
                    self._model_overrides: Dict[str, Dict[str, str]] = {}
                self._model_overrides.setdefault(provider_name, {})[role] = model_id
                logger.info(f"set_model: {provider_name}.{role} = {model_id}")
                return True
        return False

    def get_models_info(self) -> List[Dict]:
        """
        Возвращает список провайдеров с моделями и текущими назначениями по ролям.
        Используется для построения меню выбора модели.
        """
        overrides = getattr(self, '_model_overrides', {})
        result = []
        for p in self.providers:
            provider_overrides = overrides.get(p.name, {})
            # Определяем текущую модель для каждой роли
            roles = {}
            for role in ("default", "coder", "planner"):
                if role in provider_overrides:
                    roles[role] = provider_overrides[role]
                else:
                    # Читаем из статического маппинга через существующий метод
                    roles[role] = self._map_model_to_provider(role, p)
            result.append({
                "name": p.name,
                "status": p.status.value,
                "priority": p.priority,
                "models": p.models,
                "current_roles": roles,
            })
        return result

    # ------------------------------------------------------------------ #
    #  HEALTH MONITORING                                                   #
    # ------------------------------------------------------------------ #

    def _start_health_monitor(self):
        """Start background health monitoring (lazy initialization)"""
        if self._health_monitor_started:
            return

        async def health_check_loop():
            while True:
                await asyncio.sleep(self.health_check_interval)
                await self._check_providers_health()

        try:
            asyncio.get_running_loop()
            asyncio.create_task(health_check_loop())
            self._health_monitor_started = True
            logger.info("Health monitoring started")
        except RuntimeError:
            logger.debug("Health monitor: no event loop yet, deferring")

    async def _check_single_provider(self, provider: Provider):
        """Check health of a single provider (used after hot reload)"""
        try:
            start_time = time.time()
            headers = {
                "Authorization": f"Bearer {provider.api_key}",
                "Content-Type": "application/json"
            }
            # Anthropic uses a different models endpoint
            if provider.name == "anthropic":
                url = f"{provider.base_url}/v1/models"
                headers["x-api-key"] = provider.api_key
                headers["anthropic-version"] = "2023-06-01"
                del headers["Authorization"]
            else:
                url = f"{provider.base_url}/models"

            async with httpx.AsyncClient() as client:
                response = await client.get(url, headers=headers, timeout=10.0)
                response_time = (time.time() - start_time) * 1000
                provider.response_time_ms = response_time

                if response.status_code in (200, 400):
                    # 400 is acceptable — key is valid, endpoint exists
                    provider.status = ProviderStatus.HEALTHY
                    provider.error_count = 0
                    provider.last_error = None
                else:
                    provider.status = ProviderStatus.DEGRADED
                    provider.last_error = f"HTTP {response.status_code}"
        except Exception as e:
            provider.status = ProviderStatus.DEGRADED
            provider.last_error = str(e)
            logger.warning(f"Health check failed for {provider.name}: {e}")

    async def _check_providers_health(self):
        """Check health of all providers"""
        for provider in self.providers:
            await self._check_single_provider(provider)

    def _get_healthy_providers(self) -> List[Provider]:
        """Get list of healthy providers sorted by priority"""
        healthy = [p for p in self.providers if p.status != ProviderStatus.DOWN]
        return sorted(healthy, key=lambda p: p.priority)

    # ------------------------------------------------------------------ #
    #  MODEL MAPPING                                                       #
    # ------------------------------------------------------------------ #

    def _map_model_to_provider(self, model: str, provider: Provider) -> str:
        """Map generic model name to provider-specific model"""
        # Пользовательские оверрайды (set_model) имеют приоритет над статическим маппингом
        overrides = getattr(self, '_model_overrides', {})
        if provider.name in overrides and model in overrides[provider.name]:
            return overrides[provider.name][model]

        model_mappings = {
            "groq": {
                "default": "llama-3.1-8b-instant",
                "coder": "llama-3.1-8b-instant",
                "planner": "llama-3.3-70b-versatile"
            },
            "openrouter": {
                "default": "meta-llama/llama-3.3-70b-instruct:free",
                "coder": "meta-llama/llama-3.1-8b-instruct:free",
                "planner": "meta-llama/llama-3.3-70b-instruct:free"
            },
            "anthropic": {
                "default": "claude-3-5-sonnet-20241022",
                "coder": "claude-3-5-sonnet-20241022",
                "planner": "claude-3-5-sonnet-20241022"
            },
            "openai": {
                "default": "gpt-3.5-turbo",
                "coder": "gpt-3.5-turbo",
                "planner": "gpt-4o-mini"
            }
        }

        provider_mappings = model_mappings.get(provider.name, {})

        if model in provider_mappings:
            return provider_mappings[model]

        if model in provider.models:
            return model

        return provider_mappings.get("default", provider.models[0] if provider.models else "")

    # ------------------------------------------------------------------ #
    #  CHAT                                                                #
    # ------------------------------------------------------------------ #

    async def chat(
        self,
        messages: List[Message],
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        stream: bool = False,
        max_attempts: int = 3,
        tools: Optional[List[Dict[str, Any]]] = None
    ) -> Union[LLMResponse, Dict[str, Any]]:
        """Send chat request with automatic fallback across providers.
        If tools provided, returns Dict with 'text' and optional 'tool_calls'.
        Otherwise returns LLMResponse for backward compatibility."""

        self._start_health_monitor()

        temperature = temperature or settings.TEMPERATURE
        max_tokens = max_tokens or settings.MAX_TOKENS

        last_error = None
        healthy_providers = self._get_healthy_providers()

        if not healthy_providers:
            logger.warning("No healthy providers, attempting recovery...")
            for provider in self.providers:
                provider.status = ProviderStatus.HEALTHY
                provider.error_count = 0
            healthy_providers = self.providers

        for provider in healthy_providers:
            for attempt in range(max_attempts):
                try:
                    start_time = time.time()
                    provider_model = self._map_model_to_provider(model or "default", provider)

                    logger.info(f"Trying {provider.name} with model {provider_model} (attempt {attempt + 1})")

                    response = await self._make_request(
                        provider=provider,
                        messages=messages,
                        model=provider_model,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        stream=stream,
                        tools=tools
                    )

                    response_time = (time.time() - start_time) * 1000
                    provider.last_used = datetime.now()
                    provider.response_time_ms = response_time
                    provider.status = ProviderStatus.HEALTHY
                    provider.error_count = 0

                    logger.info(f"Success with {provider.name} in {response_time:.0f}ms")

                    content = response.get('content', '')
                    tool_calls = response.get('tool_calls')

                    if tools:
                        result: Dict[str, Any] = {"text": content or ""}
                        if tool_calls:
                            result["tool_calls"] = tool_calls
                        return result

                    return LLMResponse(
                        content=content,
                        model=response.get('model', provider_model),
                        provider=provider.name,
                        usage=response.get('usage'),
                        finish_reason=response.get('finish_reason'),
                        response_time_ms=response_time
                    )

                except Exception as e:
                    last_error = e
                    logger.warning(f"{provider.name} attempt {attempt + 1} failed: {e}")

                    if attempt < max_attempts - 1:
                        await asyncio.sleep(self.retry_delay * (attempt + 1))
                    else:
                        provider.error_count += 1
                        if provider.error_count >= 3:
                            provider.status = ProviderStatus.DEGRADED
                            logger.error(f"Provider {provider.name} degraded after {provider.error_count} errors")

        error_msg = f"All providers failed. Last error: {last_error}"
        logger.error(error_msg)

        task_id = self._store_pending_task(messages, model, temperature, max_tokens)
        raise Exception(f"{error_msg}. Task stored as pending: {task_id}")

    async def chat_with_fallback(
        self,
        messages: List[Message],
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> "LLMResponse":
        """
        Semantic alias for chat().
        Exists for backward compatibility with agent code previously using
        the v1 LLMClient wrapper. Prefer chat() in new code.
        """
        return await self.chat(
            messages=messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
        )

    async def check_health(self) -> None:
        """
        Public API: trigger health check for all providers.
        Use instead of calling the private _check_providers_health() directly.
        Suitable for: /providers refresh button, scheduled probes, startup checks.
        """
        await self._check_providers_health()

    async def stream_chat(
        self,
        messages: List[Message],
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ):
        """
        Streaming stub — yields single response chunk.
        Full streaming support is deferred; this keeps the interface consistent
        so agents can switch to real streaming without API changes later.
        """
        logger.warning("Streaming not yet implemented — falling back to regular chat()")
        response = await self.chat(
            messages=messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        yield response.content

    # ------------------------------------------------------------------ #
    #  REQUEST BACKENDS                                                    #
    # ------------------------------------------------------------------ #

    async def _make_request(
        self,
        provider: Provider,
        messages: List[Message],
        model: str,
        temperature: float,
        max_tokens: int,
        stream: bool = False,
        tools: Optional[List[Dict[str, Any]]] = None
    ) -> Dict:
        """Route request to provider-specific implementation"""
        if provider.name == "anthropic":
            return await self._make_anthropic_request(
                provider, messages, model, temperature, max_tokens, tools
            )
        else:
            return await self._make_openai_compatible_request(
                provider, messages, model, temperature, max_tokens, stream, tools
            )

    async def _make_openai_compatible_request(
        self,
        provider: Provider,
        messages: List[Message],
        model: str,
        temperature: float,
        max_tokens: int,
        stream: bool = False,
        tools: Optional[List[Dict[str, Any]]] = None
    ) -> Dict:
        """OpenAI-compatible endpoint (Groq, OpenRouter, OpenAI)"""
        headers = {
            "Authorization": f"Bearer {provider.api_key}",
            "Content-Type": "application/json"
        }

        formatted_messages = [{"role": m.role, "content": m.content} for m in messages]

        payload = {
            "model": model,
            "messages": formatted_messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": stream
        }

        if tools:
            payload["tools"] = [{"type": "function", "function": tool_schema} for tool_schema in tools]
            payload["tool_choice"] = "auto"

        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{provider.base_url}/chat/completions",
                headers=headers,
                json=payload,
                timeout=60.0
            )
            response.raise_for_status()
            data = response.json()

            if 'error' in data:
                raise Exception(f"API error: {data['error']}")

            choice = data['choices'][0]
            message = choice['message']

            result = {
                'content': message.get('content'),
                'model': data.get('model', model),
                'usage': data.get('usage'),
                'finish_reason': choice.get('finish_reason')
            }

            if 'tool_calls' in message and message['tool_calls']:
                parsed_tool_calls = []
                for tc in message['tool_calls']:
                    try:
                        args = json.loads(tc['function'].get('arguments', '{}'))
                    except json.JSONDecodeError:
                        args = {}
                    parsed_tool_calls.append({
                        "id": tc['id'],
                        "name": tc['function']['name'],
                        "arguments": args
                    })
                result['tool_calls'] = parsed_tool_calls

            return result

    async def _make_anthropic_request(
        self,
        provider: Provider,
        messages: List[Message],
        model: str,
        temperature: float,
        max_tokens: int,
        tools: Optional[List[Dict[str, Any]]] = None
    ) -> Dict:
        """
        Anthropic Messages API (/v1/messages).
        FIXED: system prompt is a top-level string, not a message role.
        Anthropic does not accept role='system' inside messages[].
        """
        headers = {
            "x-api-key": provider.api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json"
        }

        # Split system from conversation messages
        system_content: Optional[str] = None
        conversation: List[Dict] = []

        for m in messages:
            if m.role == "system":
                # Anthropic: system is a separate top-level parameter
                system_content = m.content
            else:
                conversation.append({"role": m.role, "content": m.content})

        payload: Dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": conversation
        }

        if system_content:
            payload["system"] = system_content

        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{provider.base_url}/v1/messages",
                headers=headers,
                json=payload,
                timeout=60.0
            )
            response.raise_for_status()
            data = response.json()

            if 'error' in data:
                raise Exception(f"Anthropic API error: {data['error']}")

            # Anthropic response: content is a list of blocks
            content_blocks = data.get('content', [])
            text = "".join(
                block.get('text', '')
                for block in content_blocks
                if block.get('type') == 'text'
            )

            return {
                'content': text,
                'model': data.get('model', model),
                'usage': data.get('usage'),
                'finish_reason': data.get('stop_reason')
            }

    # ------------------------------------------------------------------ #
    #  PENDING TASKS                                                       #
    # ------------------------------------------------------------------ #

    def _store_pending_task(
        self,
        messages: List[Message],
        model: Optional[str],
        temperature: Optional[float],
        max_tokens: Optional[int]
    ) -> str:
        """Store failed task for later retry"""
        import hashlib

        task_id = hashlib.md5(
            f"{datetime.now().isoformat()}{messages[0].content[:50]}".encode()
        ).hexdigest()[:12]

        self.pending_tasks[task_id] = PendingTask(
            id=task_id,
            messages=messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            created_at=datetime.now(),
            attempts=1
        )

        logger.info(f"Stored pending task: {task_id}")
        return task_id

    async def retry_pending_tasks(self) -> List[Dict]:
        """Retry all pending tasks"""
        results = []
        tasks_to_remove = []

        for task_id, task in list(self.pending_tasks.items()):
            if task.attempts >= self.max_retries:
                logger.warning(f"Task {task_id} exceeded max retries, removing")
                tasks_to_remove.append(task_id)
                continue

            try:
                logger.info(f"Retrying pending task {task_id} (attempt {task.attempts + 1})")

                response = await self.chat(
                    messages=task.messages,
                    model=task.model,
                    temperature=task.temperature,
                    max_tokens=task.max_tokens
                )

                results.append({'task_id': task_id, 'status': 'success', 'response': response})
                tasks_to_remove.append(task_id)

                if task.callback:
                    await task.callback(response)

            except Exception as e:
                task.attempts += 1
                task.last_error = str(e)
                results.append({
                    'task_id': task_id,
                    'status': 'failed',
                    'error': str(e),
                    'attempts': task.attempts
                })

        for task_id in tasks_to_remove:
            del self.pending_tasks[task_id]

        return results

    # ------------------------------------------------------------------ #
    #  STATS                                                               #
    # ------------------------------------------------------------------ #

    def get_provider_stats(self) -> List[Dict]:
        """Get statistics for all providers"""
        return [
            {
                'name': p.name,
                'status': p.status.value,
                'priority': p.priority,
                'models': len(p.models),
                'error_count': p.error_count,
                'last_error': p.last_error,
                'last_used': p.last_used.isoformat() if p.last_used else None,
                'response_time_ms': round(p.response_time_ms, 1)
            }
            for p in self.providers
        ]

    def get_pending_tasks_count(self) -> int:
        """Get number of pending tasks"""
        return len(self.pending_tasks)

    # ------------------------------------------------------------------ #
    #  AUDIO                                                               #
    # ------------------------------------------------------------------ #

    async def transcribe_audio(
        self,
        audio_file_path: str,
        model: Optional[str] = None,
        language: Optional[str] = None
    ) -> str:
        """Transcribe audio using Groq Whisper"""
        import aiofiles
        import os
        import mimetypes

        whisper_providers = [p for p in self.providers if 'whisper' in ' '.join(p.models)]

        if not whisper_providers:
            raise Exception("No provider with Whisper support available")

        provider = whisper_providers[0]
        model = model or "whisper-large-v3"

        async with aiofiles.open(audio_file_path, 'rb') as f:
            audio_data = await f.read()

        boundary = '----VoiceFormBoundary7MA4YWxkTrZu0gW'
        body_parts = []

        body_parts.append(f'--{boundary}\r\n'.encode())
        body_parts.append(b'Content-Disposition: form-data; name="model"\r\n\r\n')
        body_parts.append(f'{model}\r\n'.encode())

        if language:
            body_parts.append(f'--{boundary}\r\n'.encode())
            body_parts.append(b'Content-Disposition: form-data; name="language"\r\n\r\n')
            body_parts.append(f'{language}\r\n'.encode())

        filename = os.path.basename(audio_file_path)
        content_type, _ = mimetypes.guess_type(audio_file_path)
        if not content_type:
            content_type = 'audio/ogg'

        body_parts.append(f'--{boundary}\r\n'.encode())
        body_parts.append(
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode()
        )
        body_parts.append(f'Content-Type: {content_type}\r\n\r\n'.encode())
        body_parts.append(audio_data)
        body_parts.append(b'\r\n')
        body_parts.append(f'--{boundary}--\r\n'.encode())

        body = b''.join(body_parts)

        headers = {
            "Authorization": f"Bearer {provider.api_key}",
            "Content-Type": f"multipart/form-data; boundary={boundary}"
        }

        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{provider.base_url}/audio/transcriptions",
                content=body,
                headers=headers,
                timeout=60.0
            )
            response.raise_for_status()
            result = response.json()
            return result.get('text', '').strip()


# ---------------------------------------------------------------------------
# Singleton instance — единственный MultiProviderLLMClient в процессе.
# Все агенты, хендлеры и bot.py импортируют именно этот объект.
# НЕ создавайте новые экземпляры MultiProviderLLMClient в других модулях.
# ---------------------------------------------------------------------------
llm_client = MultiProviderLLMClient()
