"""PlanBot._needs_planning: русские формулировки запускают планирование."""
import pytest

from app.agents.planbot import PlanBotAgent


@pytest.fixture
def agent():
    return PlanBotAgent()


@pytest.mark.parametrize("msg", [
    "составь план поездки в Таиланд",
    "Помоги спланировать запуск магазина",
    "разработай стратегию продвижения",
    "Проанализируй конкурентов и сравни цены",
    "нужна дорожная карта проекта",
    "дай пошаговую инструкцию",
    "как создать интернет-магазин?",
    "какие этапы у ремонта",
    "build a roadmap",  # английский по-прежнему работает
])
def test_planning_requests(agent, msg):
    assert agent._needs_planning(msg) is True


@pytest.mark.parametrize("msg", [
    "привет",
    "сколько сейчас времени?",
    "какая сегодня погода",
    "планшет сломался, что делать",   # «план» внутри слова
    "Планета Марс далеко?",
    "расскажи анекдот",
    "какой процессор лучше купить",
])
def test_simple_messages_stay_direct(agent, msg):
    assert agent._needs_planning(msg) is False
