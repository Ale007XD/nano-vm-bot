"""PlanBot: бюджет LLM-вызовов (план + шаги + итог) задаётся PLANBOT_MAX_CALLS."""
import json
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agents import planbot as pb


def _steps(n):
    return json.dumps({"steps": [{"number": i, "description": f"шаг {i}"} for i in range(1, n + 1)]})


@pytest.fixture
def agent(monkeypatch):
    st = sys.modules["app.core.config"].settings
    monkeypatch.setattr(st, "PLANBOT_MAX_CALLS", 4, raising=False)
    monkeypatch.setattr(st, "PLANBOT_MODEL", "m", raising=False)
    monkeypatch.setattr(st, "BOT_TIMEZONE", "UTC", raising=False)
    return pb.PlanBotAgent(), st


@pytest.mark.parametrize("calls, steps", [(4, 2), (7, 5), (3, 1)])
def test_steps_budget(agent, calls, steps, monkeypatch):
    a, st = agent
    monkeypatch.setattr(st, "PLANBOT_MAX_CALLS", calls, raising=False)
    assert a._max_steps() == steps


def test_parse_truncates_and_renumbers():
    out = pb.PlanBotAgent._parse_steps(_steps(6), 2)
    assert [s.number for s in out] == [1, 2] and out[1].description == "шаг 2"


def test_parse_tolerates_fences_and_extra_keys():
    raw = '```json\n{"steps":[{"number":1,"description":"a","foo":1}]}\n```'
    out = pb.PlanBotAgent._parse_steps(raw, 5)
    assert len(out) == 1 and out[0].description == "a"


@pytest.mark.parametrize("raw", ["не json", "", "{}", '{"steps": [{"no_description": 1}]}'])
def test_parse_garbage_gives_empty(raw):
    assert pb.PlanBotAgent._parse_steps(raw, 3) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("calls", [4, 7])
async def test_total_llm_calls_never_exceed_budget(agent, calls, monkeypatch):
    a, st = agent
    monkeypatch.setattr(st, "PLANBOT_MAX_CALLS", calls, raising=False)
    answers = [_steps(7)] + ["результат шага"] * 20            # модель «расписала» 7 шагов
    chat = AsyncMock(side_effect=lambda *x, **k: MagicMock(content=answers.pop(0)))
    monkeypatch.setattr(pb.llm_client, "chat_with_fallback", chat)
    monkeypatch.setattr(pb.vector_memory, "search_memories", AsyncMock(return_value=[]), raising=False)
    monkeypatch.setattr(a, "_should_search_web", lambda m: False)
    await a._process_with_planning(1, "составь план поездки")
    assert chat.await_count == calls            # 1 план + (calls-2) шагов + 1 итог
