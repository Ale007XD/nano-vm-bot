"""Промпт Planner: единый дефолт, проверка пользовательского, откат на дефолт, выгрузка в .env."""
import importlib.util
import logging
import os
import re
import sys
from types import SimpleNamespace

import pytest

_ROOT = os.path.join(os.path.dirname(__file__), "..")


@pytest.fixture(scope="module")
def pp():
    spec = importlib.util.spec_from_file_location("_planner_prompt_t", os.path.join(_ROOT, "app", "core", "planner_prompt.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# Промпт, который был на сервере: ссылается на «схему, переданную приложением», которой нет
INCOMPLETE = (
    "Ты — Planner для детерминированной Program-driven Runtime VM. Преобразуй запрос в минимальную "
    "корректную JSON-программу, соответствующую DSL и схеме инструкций, переданным приложением. "
    "ДОПУСТИМЫЕ ИНСТРУКЦИИ DSL v0.1: call_llm, respond, store_memory, call_tool. "
    "Верни ровно один JSON-объект в формате, определённом приложением."
)

MINIMAL_OK = (
    '{"plan": [{"id": "step1", "instruction": "call_llm", "params": {"prompt": "q"}}, '
    '{"id": "step2", "instruction": "respond", "params": {"text": "$step1"}}]}'
)


class TestValidation:
    def test_default_is_clean(self, pp):
        check = pp.validate_planner_prompt(pp.DEFAULT_PLANNER_PROMPT)
        assert check.errors == [] and check.warnings == []

    def test_incomplete_prompt_is_rejected_with_reasons(self, pp):
        errors = " | ".join(pp.validate_planner_prompt(INCOMPLETE).errors)
        for part in ("«plan»", "«params»", "«prompt»", "«text»", "$step1"):
            assert part in errors

    def test_minimal_valid_schema_passes_but_warns_about_optional_features(self, pp):
        check = pp.validate_planner_prompt(MINIMAL_OK)
        assert check.errors == []
        joined = " ".join(check.warnings)
        assert "store_memory" in joined and "call_tool" in joined

    @pytest.mark.parametrize("empty", ["", "   ", None])
    def test_empty_has_errors(self, pp, empty):
        assert pp.validate_planner_prompt(empty).errors

    def test_each_required_part_is_actually_required(self, pp):
        for word in ("plan", "instruction", "params", "call_llm", "respond", "prompt", "text", "id"):
            broken = re.sub(rf"\b{word}\b", "xxx", pp.DEFAULT_PLANNER_PROMPT)
            assert pp.validate_planner_prompt(broken).errors, f"без «{word}» проверка должна падать"
        no_ref = pp.DEFAULT_PLANNER_PROMPT.replace("$", "")
        assert pp.validate_planner_prompt(no_ref).errors


class TestContractWithCode:
    """Дефолт описывает ровно то, что умеет VM: расхождение кода и промпта ловится здесь."""

    def test_instructions_match_registry(self, pp):
        from app.runtime.registry import InstructionRegistry

        assert set(InstructionRegistry()._registry) == set(pp.INSTRUCTIONS)

    def test_default_mentions_every_instruction_and_param_the_vm_accepts(self, pp):
        from app.runtime.instructions.call_llm import _ALLOWED_PARAMS

        text = pp.DEFAULT_PLANNER_PROMPT
        for name in pp.INSTRUCTIONS:
            assert re.search(rf"\b{name}\b", text)
        for param in ("prompt", "system", "role"):  # то, что Planner должен уметь задавать у call_llm
            assert param in _ALLOWED_PARAMS and re.search(rf"\b{param}\b", text)
        for param in ("text", "content", "memory_type", "tool", "args"):
            assert re.search(rf"\b{param}\b", text)

    def test_rag_marker_is_same_as_planners_and_in_default(self, pp):
        from app.runtime import planner

        assert planner._RAG_MARKER == pp.RAG_MARKER
        assert pp.RAG_MARKER in pp.DEFAULT_PLANNER_PROMPT

    def test_single_source_of_default(self, pp):
        """Конфиг берёт дефолт из planner_prompt, а не хранит свою копию."""
        src = open(os.path.join(_ROOT, "app", "core", "config.py"), encoding="utf-8").read()
        assert re.search(r"from app\.core\.planner_prompt import \(?\s*DEFAULT_PLANNER_PROMPT as PLANNER_SYSTEM_PROMPT_DEFAULT", src)
        assert "Ты — Planner" not in src
        assert "_PLANNER_SYSTEM" not in open(os.path.join(_ROOT, "app", "runtime", "planner.py"), encoding="utf-8").read()

    def test_security_and_honesty_rules_are_in_default(self, pp):
        text = pp.DEFAULT_PLANNER_PROMPT
        assert "результатов инструментов" in text  # данные, а не инструкции
        assert "не заявляешь о результатах" in text
        assert "комментариев" in text


class TestPlannerUsesPrompt:
    @pytest.fixture
    def planner_cls(self, monkeypatch):
        from app.runtime import planner as planner_mod

        planner_mod._reported_prompts.clear()
        return planner_mod

    def _set(self, monkeypatch, value):
        st = sys.modules["app.core.config"].settings
        monkeypatch.setattr(st, "PLANNER_SYSTEM_PROMPT", value, raising=False)

    def _planner(self, planner_cls):
        return planner_cls.Planner(llm=SimpleNamespace())

    def test_empty_or_non_string_means_default(self, planner_cls, pp, monkeypatch):
        for v in ("", "   ", None, 123):
            self._set(monkeypatch, v)
            assert self._planner(planner_cls)._system_prompt() == pp.DEFAULT_PLANNER_PROMPT

    def test_valid_custom_prompt_is_used_and_editable(self, planner_cls, monkeypatch, caplog):
        custom = pp_default_with_extra_rule()
        self._set(monkeypatch, custom)
        with caplog.at_level(logging.WARNING):
            assert self._planner(planner_cls)._system_prompt() == custom
        assert not [r for r in caplog.records if r.levelno >= logging.ERROR]

    def test_prompt_without_schema_falls_back_loudly_once(self, planner_cls, pp, monkeypatch, caplog):
        self._set(monkeypatch, INCOMPLETE)
        with caplog.at_level(logging.WARNING):
            p = self._planner(planner_cls)
            # ошибка в логе уже при создании Planner (старт бота), до первого запроса
            assert len([r for r in caplog.records if r.levelno == logging.ERROR]) == 1
            assert p._system_prompt() == pp.DEFAULT_PLANNER_PROMPT
            assert p._system_prompt() == pp.DEFAULT_PLANNER_PROMPT
        errors = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert len(errors) == 1  # не на каждый запрос
        assert "PLANNER_SYSTEM_PROMPT" in errors[0].getMessage() and "--print-prompt" in errors[0].getMessage()

    def test_minimal_valid_prompt_warns_about_missing_optional_parts(self, planner_cls, monkeypatch, caplog):
        self._set(monkeypatch, MINIMAL_OK)
        with caplog.at_level(logging.WARNING):
            assert self._planner(planner_cls)._system_prompt() == MINIMAL_OK
        assert any("store_memory" in r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)

    @pytest.mark.asyncio
    async def test_effective_prompt_is_sent_to_llm(self, planner_cls, monkeypatch):
        custom = pp_default_with_extra_rule()
        self._set(monkeypatch, custom)
        seen = {}

        class LLM:
            async def generate(self, prompt, role="default", system=None, **kw):
                seen["system"] = system
                return MINIMAL_OK, None

        prog = await planner_cls.Planner(LLM()).generate("привет")
        assert seen["system"] == custom and prog["plan"]

    @pytest.mark.asyncio
    async def test_broken_prompt_still_gets_working_default(self, planner_cls, pp, monkeypatch):
        self._set(monkeypatch, INCOMPLETE)
        seen = {}

        class LLM:
            async def generate(self, prompt, role="default", system=None, **kw):
                seen["system"] = system
                return MINIMAL_OK, None

        await planner_cls.Planner(LLM()).generate("привет")
        assert seen["system"] == pp.DEFAULT_PLANNER_PROMPT


def pp_default_with_extra_rule():
    spec = importlib.util.spec_from_file_location("_pp_extra", os.path.join(_ROOT, "app", "core", "planner_prompt.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.DEFAULT_PLANNER_PROMPT + "\n8. Отвечай в деловом стиле.\n"


class TestEnvRoundtrip:
    def test_to_env_line_roundtrips_through_dotenv(self, pp, tmp_path):
        from dotenv import dotenv_values

        nasty = 'Строка с "кавычками", \\обратным слэшем\\ и\nпереносами\n\n$step1 {"a": 1}'
        env = tmp_path / ".env"
        env.write_text(pp.to_env_line("X", nasty) + "\n", encoding="utf-8")
        assert dotenv_values(env)["X"] == nasty

    def test_settings_read_default_from_env_file(self, pp, tmp_path, monkeypatch):
        """Выгруженный дефолт читается настоящим Settings и совпадает с встроенным."""
        spec = importlib.util.spec_from_file_location("_real_config_pp", os.path.join(_ROOT, "app", "core", "config.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        monkeypatch.delenv("PLANNER_SYSTEM_PROMPT", raising=False)
        env = tmp_path / ".env"
        env.write_text(pp.to_env_line("PLANNER_SYSTEM_PROMPT", pp.DEFAULT_PLANNER_PROMPT) + "\n", encoding="utf-8")
        assert mod.Settings(_env_file=str(env)).PLANNER_SYSTEM_PROMPT == pp.DEFAULT_PLANNER_PROMPT
        assert mod.Settings(_env_file=None).PLANNER_SYSTEM_PROMPT == pp.DEFAULT_PLANNER_PROMPT
