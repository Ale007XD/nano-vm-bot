"""
Planbot Agent - Smart Planner with multi-step reasoning
Advanced reasoning, planning, and verification capabilities
"""
import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from app.core.config import settings
from app.core.llm_client_v2 import Message, llm_client
from app.core.memory import vector_memory
from app.core.web_search import web_search
from app.utils.helpers import current_datetime_prompt

logger = logging.getLogger(__name__)


@dataclass
class PlanStep:
    """Single step in a plan"""
    number: int
    description: str
    status: str = "pending"  # pending, in_progress, completed, failed
    result: Optional[str] = None


@dataclass
class ExecutionPlan:
    """Multi-step execution plan"""
    goal: str
    steps: List[PlanStep]
    current_step: int = 0
    verified: bool = False


class PlanBotAgent:
    """
    PlanBot - Smart Planner with multi-step reasoning
    Excels at complex tasks requiring planning and verification
    """
    
    SYSTEM_PROMPT = """Вы - PlanBot, продвинутый ассистент по планированию и логическому мышлению.
Ваши характеристики:
- Разбивайте сложные задачи на логические шаги
- Мыслите методично
- Проверяйте каждый шаг перед продолжением
- Предоставляйте подробные объяснения
- Используйте структурированные рассуждения
- Учитывайте крайние случаи и альтернативы
- Отлично подходите для: планирования, анализа, решения сложных задач, проверки

ВСЕГДА отвечайте на РУССКОМ языке, если пользователь не просит иное.

При получении задачи:
1. Разбейте ее на четкие шаги
2. Выполните каждый шаг с проверкой
3. Предоставьте комплексный финальный ответ

У вас есть доступ к воспоминаниям пользователя и вы можете ссылаться на них в планировании.
- ИМЕЕТЕ ДОСТУП К ПОИСКУ В ИНТЕРНЕТЕ для актуальной информации"""
    
    def __init__(self):
        self.name = "planbot"
        self.display_name = "🧩 PlanBot"
        self.description = "Smart planner with multi-step reasoning"
        # Промпт можно переопределить через .env (PLANBOT_SYSTEM_PROMPT)
        env_prompt = getattr(settings, "PLANBOT_SYSTEM_PROMPT", "")
        if isinstance(env_prompt, str) and env_prompt.strip():
            self.SYSTEM_PROMPT = env_prompt
    
    # Keywords that trigger web search
    WEB_SEARCH_TRIGGERS = [
        'найди', 'поиск', 'ищи', 'search', 'find', 'google',
        'новости', 'news', 'актуально', 'сейчас', 'today',
        'погода', 'weather', 'курс', 'rate', 'цена', 'price',
        'события', 'events', 'ближайший', 'nearest',
        'когда', 'when', 'где', 'where', 'кто', 'who',
        '2024', '2025', '2026', 'января', 'февраля', 'марта',
        'сегодня', 'вчера', 'завтра', 'сейчас', 'свежие'
    ]
    
    def _should_search_web(self, message: str) -> bool:
        """Determine if web search should be triggered"""
        if not settings.ENABLE_WEB_SEARCH:
            return False

        message_lower = message.lower()
        # \b — чтобы «кто» не срабатывало внутри «никто», «найди» внутри «найдись» и т.п.
        return any(
            re.search(rf"\b{re.escape(trigger)}\b", message_lower)
            for trigger in self.WEB_SEARCH_TRIGGERS
        )
    
    async def process_message(
        self,
        user_id: int,
        message: str,
        conversation_history: Optional[List[Dict]] = None,
        on_status: Optional[Callable[[str], Any]] = None
    ) -> str:
        """
        Process message with planning and verification.

        on_status — опциональный async-callback со стадией выполнения
        («Составляю план…», «Выполняю шаг 2/5»…), для прогресс-сообщения.
        """
        try:
            # First, check if this needs multi-step processing
            if self._needs_planning(message):
                return await self._process_with_planning(
                    user_id, message, conversation_history, on_status
                )
            else:
                # Simple query, use direct response
                return await self._direct_response(user_id, message, conversation_history)

        except Exception as e:
            logger.error(f"PlanBot error: {e}")
            return "⚠️ Произошла ошибка во время обработки. Пожалуйста, попробуйте снова."

    async def _report(self, on_status, text: str) -> None:
        if on_status is None:
            return
        try:
            await on_status(text)
        except Exception as e:
            logger.debug(f"on_status callback failed: {e}")

    # Русские основы — совпадение с НАЧАЛА слова (\b), чтобы «план» не ловил «планета»/«планшет»
    _RU_PLANNING_RE = re.compile(
        r"\b(?:"
        r"план(?:а|у|ом|е|ы|ов|ир\w*)?\b|спланир\w*|"
        r"состав(?:ь|ьте)\b|созда(?:й|йте|ть)\b|постро(?:й|йте|ить)\b|"
        r"разработ(?:ай|айте|ать)\b|спроектиру\w*|"
        r"проанализиру\w*|анализ\w*|сравн(?:и|ите|ить|ение)\b|оцени\w*|"
        r"стратеги\w*|дорожн\w*\s+карт\w*|роадмап\w*|"
        r"пошагов\w*|шаги\b|этапы\b|процесс(?:а|у|ом|е|ы|ов)?\b|"
        r"как\s+(?:сделать|создать|построить|организовать|запустить|внедрить)\b|"
        r"помоги\s+(?:с\s+)?(?:план|состав|организ)\w*"
        r")",
        re.IGNORECASE,
    )

    def _needs_planning(self, message: str) -> bool:
        """Determine if message requires multi-step planning"""
        planning_keywords = [
            'plan', 'create', 'build', 'design', 'analyze', 'compare',
            'evaluate', 'strategy', 'roadmap', 'steps', 'process',
            'how to', 'what are the steps', 'help me plan'
        ]

        message_lower = message.lower()
        if any(keyword in message_lower for keyword in planning_keywords):
            return True
        return bool(self._RU_PLANNING_RE.search(message_lower))

    async def _process_with_planning(
        self,
        user_id: int,
        message: str,
        conversation_history: Optional[List[Dict]] = None,
        on_status: Optional[Callable[[str], Any]] = None
    ) -> str:
        """Process with full planning and verification"""

        # Step 0: Search web for current information if needed
        web_context = ""
        if self._should_search_web(message):
            logger.info(f"PlanBot triggering web search for planning: {message[:50]}...")
            try:
                web_context = await web_search.search_and_format(
                    message,
                    num_results=settings.WEB_SEARCH_RESULTS
                )
            except Exception as e:
                logger.warning(f"Web search failed: {e}")

        # Step 1: Create a plan (with web context if available)
        await self._report(on_status, "🧩 Составляю план…")
        plan = await self._create_plan(message, user_id, web_context)

        # Step 2: Execute and verify each step
        total = max(len(plan.steps), 1)
        results = []
        for idx, step in enumerate(plan.steps, start=1):
            step.status = "in_progress"
            await self._report(on_status, f"⚙️ Выполняю шаг {idx} из {total}…")

            # Execute step
            step_result = await self._execute_step(step, user_id, message)
            step.result = step_result
            step.status = "completed"
            results.append(f"{step.number}. {step.description}\n   → {step_result}")

        plan.verified = True

        # Step 3: Generate final comprehensive answer
        await self._report(on_status, "📝 Формирую итоговый ответ…")
        final_answer = await self._generate_final_answer(
            message, plan, results, user_id
        )

        return final_answer
    
    async def _create_plan(self, goal: str, user_id: int, web_context: str = "") -> ExecutionPlan:
        """Create execution plan for the goal"""
        
        # Get relevant memories
        memories = await vector_memory.search_memories(goal, user_id, n_results=3)
        context = current_datetime_prompt()
        if memories:
            context += "\nКонтекст пользователя:\n" + "\n".join([f"- {m['content']}" for m in memories])
        
        # Add web search context if available
        if web_context:
            context += f"\n\nАктуальная информация из интернета:\n{web_context}"
        
        planning_prompt = f"""Создайте пошаговый план для достижения этой цели:
Цель: {goal}

{context}

Предоставьте ответ в виде JSON-объекта со следующей структурой:
{{
    "steps": [
        {{"number": 1, "description": "Описание первого шага"}},
        {{"number": 2, "description": "Описание второго шага"}}
    ]
}}

Держите шаги четкими и выполнимыми. Максимум 7 шагов."""
        
        messages = [
            Message(role="system", content=self.SYSTEM_PROMPT),
            Message(role="user", content=planning_prompt)
        ]
        
        response = await llm_client.chat_with_fallback(
            messages,
            model=settings.PLANBOT_MODEL,
            reasoning_effort="medium"
        )
        
        try:
            plan_data = json.loads(response.content)
            steps = [PlanStep(**step) for step in plan_data.get('steps', [])]
        except (json.JSONDecodeError, TypeError):
            # Fallback: create simple single-step plan
            steps = [PlanStep(number=1, description="Analyze and respond to the request")]
        
        return ExecutionPlan(goal=goal, steps=steps)
    
    async def _execute_step(
        self,
        step: PlanStep,
        user_id: int,
        original_goal: str
    ) -> str:
        """Execute a single step"""
        
        execution_prompt = f"""Выполните этот шаг плана:

Исходная цель: {original_goal}
Текущий шаг {step.number}: {step.description}

Выполните этот шаг и предоставьте результат. Будьте краткими, но тщательными."""
        
        messages = [
            Message(role="system", content=self.SYSTEM_PROMPT),
            Message(role="user", content=execution_prompt)
        ]
        
        response = await llm_client.chat_with_fallback(
            messages,
            model=settings.PLANBOT_MODEL,
            reasoning_effort="medium"
        )
        
        return response.content
    
    async def _generate_final_answer(
        self,
        goal: str,
        plan: ExecutionPlan,
        results: List[str],
        user_id: int
    ) -> str:
        """Generate comprehensive final answer"""
        
        final_prompt = f"""На основе выполненного плана предоставьте комплексный финальный ответ.

Цель: {goal}

Выполненные шаги:
{chr(10).join(results)}

Предоставьте:
1. Резюме того, что было достигнуто
2. Ключевые идеи или рекомендации
3. Следующие шаги или действия для пользователя

Форматируйте для Telegram с HTML-разметкой."""
        
        messages = [
            Message(role="system", content=self.SYSTEM_PROMPT),
            Message(role="user", content=final_prompt)
        ]
        
        response = await llm_client.chat_with_fallback(
            messages,
            model=settings.PLANBOT_MODEL,
            reasoning_effort="medium"
        )
        
        # Add header showing it was processed by Claudbot
        header = "🧩 <b>Результат планирования PlanBot</b>\n\n"
        header += f"<i>План выполнен за {len(plan.steps)} шагов</i>\n\n"
        
        return header + response.content
    
    async def _direct_response(
        self,
        user_id: int,
        message: str,
        conversation_history: Optional[List[Dict]] = None
    ) -> str:
        """Direct response without full planning"""

        messages = [
            Message(role="system", content=self.SYSTEM_PROMPT),
            Message(role="system", content=current_datetime_prompt()),
        ]

        # Check if web search is needed
        search_results_text = ""
        if self._should_search_web(message):
            logger.info(f"PlanBot triggering web search for: {message[:50]}...")
            try:
                search_results_text = await web_search.search_and_format(
                    message,
                    num_results=settings.WEB_SEARCH_RESULTS
                )
            except Exception as e:
                logger.warning(f"Web search failed: {e}")
        
        # Add web search results if available
        if search_results_text:
            messages.append(Message(
                role="system",
                content=f"Актуальная информация из интернета:\n{search_results_text}"
            ))
        
        # Add relevant memories
        memories = await vector_memory.search_memories(message, user_id, n_results=3)
        if memories:
            context = "Relevant user memories:\n"
            for mem in memories:
                context += f"- {mem['content']}\n"
            messages.append(Message(role="system", content=context))
        
        # Add history
        if conversation_history:
            for msg in conversation_history[-5:]:
                messages.append(Message(
                    role=msg['role'],
                    content=msg['content']
                ))
        
        messages.append(Message(role="user", content=message))
        
        response = await llm_client.chat_with_fallback(
            messages,
            model=settings.PLANBOT_MODEL,
            reasoning_effort="medium"
        )
        
        return f"🧩 <b>PlanBot</b>\n\n{response.content}"
    
    async def analyze_and_verify(self, content: str) -> Dict[str, Any]:
        """Analyze content and provide verification"""
        
        verify_prompt = f"""Проанализируйте этот контент и предоставьте верификацию:

{content}

Ответьте в формате JSON:
{{
    "summary": "Краткое резюме",
    "key_points": ["пункт 1", "пункт 2"],
    "accuracy_score": 8,
    "suggestions": ["предложение 1"],
    "improved_version": "Улучшенный текст (опционально)"
}}"""
        
        messages = [
            Message(role="system", content=self.SYSTEM_PROMPT),
            Message(role="user", content=verify_prompt)
        ]
        
        response = await llm_client.chat_with_fallback(
            messages,
            model=settings.PLANBOT_MODEL,
            reasoning_effort="medium"
        )
        
        try:
            return json.loads(response.content)
        except json.JSONDecodeError:
            return {
                "summary": "Не удалось разобрать верификацию",
                "key_points": [],
                "accuracy_score": 5,
                "suggestions": ["Пожалуйста, попробуйте снова"],
                "improved_version": content
            }


# Global agent instance
planbot = PlanBotAgent()
