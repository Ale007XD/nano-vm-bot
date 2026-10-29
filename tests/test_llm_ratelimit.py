"""429 у провайдера: ждём столько, сколько он просит; knowledge_base использует актуальный API клиента."""
import importlib.util
import os
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core import llm_client_v2 as v2

GROQ_429 = (
    'groq API error 429: {"error":{"message":"Rate limit reached for model `openai/gpt-oss-120b` '
    "on tokens per minute (TPM): Limit 8000, Used 7560, Requested 1352. "
    'Please try again in {hint}. Need more tokens?"}}'
)


def _err(hint):
    return GROQ_429.replace("{hint}", hint)


class TestParse:
    @pytest.mark.parametrize("hint, expected", [
        ("6.84s", 6.84), ("420ms", 0.42), ("1m30s", 90.0), ("7m12.5s", 432.5), ("2h", 7200.0),
    ])
    def test_hint_formats(self, hint, expected):
        assert v2._rate_limit_wait(Exception(_err(hint))) == pytest.approx(expected)

    def test_not_a_rate_limit(self):
        assert v2._rate_limit_wait(Exception("groq API error 500: boom")) is None

    def test_429_without_hint(self):
        assert v2._rate_limit_wait(Exception("openrouter API error 429: temporarily rate-limited")) is None


@pytest.fixture
def llm(monkeypatch):
    c = v2.MultiProviderLLMClient()
    c.providers = [
        v2.Provider(name="groq", base_url="https://g", api_key="k1", models=["m"], priority=1),
        v2.Provider(name="openrouter", base_url="https://o", api_key="k2", models=["m"], priority=2),
    ]
    monkeypatch.setattr(c, "_start_health_monitor", lambda: None)
    monkeypatch.setattr(c, "retry_delay", 2.0)
    c._user_keys = AsyncMock(return_value={})
    c._store_pending_task = MagicMock(return_value="pending-1")
    sleeps = []

    async def fake_sleep(sec):
        sleeps.append(sec)

    monkeypatch.setattr(v2.asyncio, "sleep", fake_sleep)
    c._sleeps = sleeps
    return c


def _script(llm, monkeypatch, outcomes):
    calls = []

    async def fake_request(provider, **kw):
        calls.append(provider.name)
        out = outcomes.pop(0)
        if isinstance(out, Exception):
            raise out
        return {"content": "ok", "model": "m", "usage": None, "finish_reason": "stop"}

    monkeypatch.setattr(llm, "_make_request", fake_request)
    return calls


@pytest.mark.asyncio
async def test_waits_as_long_as_provider_asks_and_stays_on_provider(llm, monkeypatch):
    calls = _script(llm, monkeypatch, [Exception(_err("6.84s")), "ok"])
    res = await llm.chat([v2.Message(role="user", content="hi")])
    assert res.content == "ok" and res.provider == "groq"
    assert calls == ["groq", "groq"]                  # без перехода на openrouter
    assert llm._sleeps == [pytest.approx(7.34)]       # не 2 секунды, как раньше


@pytest.mark.asyncio
async def test_long_limit_switches_provider_without_waiting(llm, monkeypatch):
    calls = _script(llm, monkeypatch, [Exception(_err("7m12s")), "ok"])
    res = await llm.chat([v2.Message(role="user", content="hi")])
    assert res.provider == "openrouter"
    assert calls == ["groq", "openrouter"]
    assert llm._sleeps == []


@pytest.mark.asyncio
async def test_error_without_hint_keeps_default_backoff(llm, monkeypatch):
    calls = _script(llm, monkeypatch, [Exception("groq API error 500: boom"), "ok"])
    res = await llm.chat([v2.Message(role="user", content="hi")])
    assert res.provider == "groq" and calls == ["groq", "groq"]
    assert llm._sleeps == [2.0]


@pytest.fixture
def kb():
    spec = importlib.util.spec_from_file_location("_t_kb_llm", os.path.join("skills", "custom", "knowledge_base.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Client:
    """Принимает только то, что принимает настоящий MultiProviderLLMClient.chat()."""

    def __init__(self, content):
        self.content, self.messages = content, None

    async def chat(self, messages, model=None, temperature=None, max_tokens=None, **kw):
        assert not kw, f"неподдерживаемые аргументы: {kw}"
        self.messages = messages
        return v2.LLMResponse(content=self.content, model="m", provider="groq")


class TestKnowledgeBaseExtract:
    DATA = '{"summary": "кратко", "tags": ["a"], "topics": ["b"], "key_facts": ["c"]}'

    @pytest.mark.asyncio
    async def test_plain_json(self, kb):
        cl = _Client(self.DATA)
        out = await kb._extract_entities("текст", "Заголовок", cl)
        assert out["tags"] == ["a"] and out["summary"] == "кратко"
        assert [m.role for m in cl.messages] == ["system", "user"]

    @pytest.mark.asyncio
    async def test_fenced_json(self, kb):
        out = await kb._extract_entities("т", "З", _Client("```json\n" + self.DATA + "\n```"))
        assert out["topics"] == ["b"]

    @pytest.mark.asyncio
    async def test_garbage_falls_back_to_title(self, kb):
        out = await kb._extract_entities("т", "Заголовок", _Client("не json"))
        assert out == {"summary": "Заголовок", "tags": [], "topics": [], "key_facts": []}
