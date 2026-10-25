"""
Planner — генерирует DSL-программу из user_input.

Контракт: Planner(user_input, context) → Program (dict)

Модель: role="planner" → llama-3.3-70b-versatile (Groq p1)

Программа v0.1 поддерживает инструкции:
    call_llm    — запрос к LLM
    respond     — отправить текст пользователю
    store_memory — сохранить в ChromaDB

Формат Program:
{
    "plan": [
        {
            "id":          "step1",           # уникальный идентификатор шага
            "instruction": "call_llm",        # имя инструкции из registry
            "on_error":    "abort",           # "abort" | "continue"
            "params": { ... }                 # параметры инструкции
        }
    ]
}

$-refs: значение параметра вида "$step_id" резолвится в output шага step_id.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional

from app.runtime.llm_adapter import LLMProtocol

logger = logging.getLogger(__name__)

_RAG_MARKER = "[СИСТЕМНЫЙ КОНТЕКСТ: ИЗВЕСТНЫЕ ФАКТЫ / ВОСПОМИНАНИЯ]"

# ---------------------------------------------------------------------------
# System prompt для Planner LLM
# ---------------------------------------------------------------------------

_PLANNER_SYSTEM = """\
Ты — Planner, компонент детерминированного AI-рантайма.
Твоя единственная задача: преобразовать запрос пользователя в JSON-программу.

ДОСТУПНЫЕ ИНСТРУКЦИИ:
- call_llm    params: {prompt: str, role?: str, system?: str}
- respond     params: {text: str}   — текст или $ref на output предыдущего шага
- store_memory params: {content: str, memory_type?: str}

ПРАВИЛА:
1. Всегда возвращай ТОЛЬКО валидный JSON.
2. Каждый шаг имеет уникальный id (step1, step2, ...).
3. Последний шаг ВСЕГДА "respond" с text = $<id предыдущего шага>.
4. store_memory добавляй только если пользователь просит запомнить факт.
5. КРИТИЧЕСКИ ВАЖНО: Если в тексте "ЗАПРОС ПОЛЬЗОВАТЕЛЯ" присутствует блок, начинающийся с "[СИСТЕМНЫЙ КОНТЕКСТ: ИЗВЕСТНЫЕ ФАКТЫ / ВОСПОМИНАНИЯ]", ты ОБЯЗАН скопировать весь этот блок в параметр `system` внутри инструкции `call_llm`. Сам вопрос пользователя помести в `prompt`. Никогда не игнорируй системный контекст!

ПРИМЕР (вопрос с контекстом):
ЗАПРОС ПОЛЬЗОВАТЕЛЯ:
Что я просил запомнить?
[СИСТЕМНЫЙ КОНТЕКСТ: ИЗВЕСТНЫЕ ФАКТЫ / ВОСПОМИНАНИЯ]
- Сообщение 2

ОТВЕТ PLANNER:
{
  "plan": [
    {
      "id": "step1",
      "instruction": "call_llm",
      "on_error": "abort",
      "params": {
        "prompt": "Что я просил запомнить?",
        "system": "[СИСТЕМНЫЙ КОНТЕКСТ: ИЗВЕСТНЫЕ ФАКТЫ / ВОСПОМИНАНИЯ]\\n- Сообщение 2",
        "role": "default"
      }
    },
    {
      "id": "step2",
      "instruction": "respond",
      "on_error": "continue",
      "params": {"text": "$step1"}
    }
  ]
}

ПРИМЕР (простой вопрос):
{
  "plan": [
    {
      "id": "step1",
      "instruction": "call_llm",
      "on_error": "abort",
      "params": {"prompt": "Как работает asyncio?", "role": "default"}
    },
    {
      "id": "step2",
      "instruction": "respond",
      "on_error": "continue",
      "params": {"text": "$step1"}
    }
  ]
}

ПРИМЕР (запомнить факт):
{
  "plan": [
    {
      "id": "step1",
      "instruction": "call_llm",
      "on_error": "abort",
      "params": {"prompt": "Подтверди что запомнил: люблю Python", "role": "default"}
    },
    {
      "id": "step2",
      "instruction": "store_memory",
      "on_error": "continue",
      "params": {"content": "Пользователь любит Python", "memory_type": "note"}
    },
    {
      "id": "step3",
      "instruction": "respond",
      "on_error": "continue",
      "params": {"text": "$step1"}
    }
  ]
}
"""


# Fallback-программа если Planner вернул невалидный JSON
def _fallback_program(user_input: str) -> Dict[str, Any]:
    return {
        "plan": [
            {
                "id": "step1",
                "instruction": "call_llm",
                "on_error": "abort",
                "params": {"prompt": user_input, "role": "default"},
            },
            {
                "id": "step2",
                "instruction": "respond",
                "on_error": "continue",
                "params": {"text": "$step1"},
            },
        ]
    }


# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------

class Planner:
    """
    Генерирует Program из user_input через LLM (role="planner").

    Использование:
        planner = Planner(llm_adapter)
        program = await planner.generate(user_input, history)
        run_result = await vm.run(program, ctx)
    """

    def __init__(self, llm: LLMProtocol):
        self._llm = llm

    async def generate(
        self,
        user_input: str,
        history: Optional[List[Dict[str, str]]] = None,
    ) -> Dict[str, Any]:
        """
        Сгенерировать Program.

        Args:
            user_input: текст сообщения пользователя (может содержать RAG-блок)
            history:    последние N сообщений [{"role": "user"|"assistant", "content": ...}]

        Returns:
            Program dict. При любой ошибке возвращает fallback_program (2 шага).
        """
        prompt = self._build_prompt(user_input, history)

        try:
            raw_result = await self._llm.generate(
                prompt=prompt,
                role="planner",
                system=_PLANNER_SYSTEM,
            )
            raw = raw_result[0] if isinstance(raw_result, tuple) else raw_result
            program = self._parse(raw, user_input)
            # Гарантируем что RAG-контекст попадёт в call_llm.system
            # даже если LLM-Planner не выполнил правило 5
            program = self._ensure_rag_in_system(program, user_input)
            logger.info(
                "Planner generated program: %d steps for input=%r",
                len(program.get("plan", [])),
                user_input[:60],
            )
            return program

        except Exception as e:
            logger.warning("Planner failed (%s), using fallback program", e)
            return _fallback_program(user_input)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _build_prompt(
        self,
        user_input: str,
        history: Optional[List[Dict[str, str]]],
    ) -> str:
        parts = []

        if history:
            recent = history[-12:]
            lines = []
            for msg in recent:
                role = "Пользователь" if msg["role"] == "user" else "Ассистент"
                lines.append(f"{role}: {msg['content']}")
            if lines:
                parts.append("ИСТОРИЯ ДИАЛОГА:\n" + "\n".join(lines))

        parts.append(f"ЗАПРОС ПОЛЬЗОВАТЕЛЯ:\n{user_input}")
        parts.append("Верни JSON-программу.")

        return "\n\n".join(parts)

    def _parse(self, raw: str, user_input: str) -> Dict[str, Any]:
        """
        Извлечь JSON из ответа LLM.
        LLM иногда оборачивает в ```json ... ``` — зачищаем.
        """
        cleaned = re.sub(r"```(?:json)?\s*", "", raw).replace("```", "").strip()

        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start == -1 or end == -1:
            raise ValueError(f"No JSON object found in planner output: {raw[:200]}")

        json_str = cleaned[start:end + 1]

        # Экранировать буквальные \n и \r внутри строковых значений JSON.
        # Простой re.sub ломает структуру — нужен посимвольный проход.
        json_str = self._fix_newlines_in_strings(json_str)

        try:
            program = json.loads(json_str)
        except json.JSONDecodeError:
            sanitized = ''.join(c for c in json_str if ord(c) >= 32 or c in '\n\r\t')
            program = json.loads(sanitized)

        if "plan" not in program or not isinstance(program["plan"], list):
            raise ValueError("Invalid program structure: missing 'plan' list")
        if len(program["plan"]) == 0:
            raise ValueError("Empty plan")

        for step in program["plan"]:
            step.setdefault("on_error", "abort")

        return program

    @staticmethod
    def _ensure_rag_in_system(program: Dict[str, Any], user_input: str) -> Dict[str, Any]:
        """
        Гарантирует что RAG-блок из user_input попадает в params.system
        первого call_llm шага программы.

        Если Planner уже положил system — не трогаем.
        Если RAG-блока в user_input нет — не трогаем.
        Это не бизнес-логика, а гарантия контракта между messages.py и VM.
        """
        rag_start = user_input.find(_RAG_MARKER)
        if rag_start == -1:
            return program

        rag_block = user_input[rag_start:].strip()

        for step in program.get("plan", []):
            if step.get("instruction") == "call_llm":
                params = step.setdefault("params", {})
                if not params.get("system"):
                    params["system"] = rag_block
                    logger.debug(
                        "RAG fallback: injected system context into %s", step["id"]
                    )
                break

        return program

    @staticmethod
    def _fix_newlines_in_strings(s: str) -> str:
        """
        Экранирует буквальные переносы строк внутри JSON-строк.
        Трогает только содержимое строковых значений, не структуру JSON.
        """
        result = []
        in_string = False
        i = 0
        while i < len(s):
            c = s[i]
            if c == '\\' and in_string:
                result.append(c)
                i += 1
                if i < len(s):
                    result.append(s[i])
                i += 1
                continue
            if c == '"':
                in_string = not in_string
                result.append(c)
                i += 1
                continue
            if in_string and c == '\n':
                result.append('\\n')
                i += 1
                continue
            if in_string and c == '\r':
                result.append('\\r')
                i += 1
                continue
            result.append(c)
            i += 1
        return ''.join(result)
