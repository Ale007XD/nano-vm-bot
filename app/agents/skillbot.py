"""
Skillbot Agent - Skills Manager and Catalog
Manages bot capabilities, skills, and extensions
"""
import html
import json
import logging
from dataclasses import dataclass
from typing import Dict, List, Optional

from app.core.config import settings
from app.core.llm_client_v2 import Message, llm_client
from app.core.memory import vector_memory
from app.core.skills_loader import skill_loader
from app.utils.helpers import current_datetime_prompt

logger = logging.getLogger(__name__)


@dataclass
class SkillSuggestion:
    """Suggested skill based on user needs"""
    name: str
    description: str
    category: str
    estimated_complexity: str
    code_outline: str


class SkillBotAgent:
    """
    SkillBot - Skills Manager and Catalog
    Specialized in managing bot capabilities and creating new skills
    """
    
    SYSTEM_PROMPT = """Вы - SkillBot, специалист по управлению навыками и каталогу.
Ваши характеристики:
- Эксперт в возможностях бота и расширениях
- Можете анализировать потребности пользователей и предлагать подходящие навыки
- Можете генерировать код навыков и документацию
- Управляете каталогом и инвентарем навыков
- Помогаете пользователям находить и использовать доступные навыки
- Создаете новые навыки по запросу

ВСЕГДА отвечайте на РУССКОМ языке, если пользователь не просит иное.

Доступные категории навыков:
- productivity: управление задачами, напоминания, заметки
- entertainment: игры, развлечения, медиа
- utility: вычисления, конвертации, инструменты
- integration: внешние API, веб-сервисы
- automation: рабочие процессы, запланированные задачи

При создании навыков:
- Пишите чистый, документированный код Python
- Используйте async/await для асинхронных операций
- Включайте правильную обработку ошибок
- Следуйте шаблону структуры навыков"""
    
    def __init__(self):
        self.name = "skillbot"
        self.display_name = "🔧 SkillBot"
        self.description = "Skills manager and catalog"
        # Промпт можно переопределить через .env (SKILLBOT_SYSTEM_PROMPT)
        env_prompt = getattr(settings, "SKILLBOT_SYSTEM_PROMPT", "")
        if isinstance(env_prompt, str) and env_prompt.strip():
            self.SYSTEM_PROMPT = env_prompt
    
    async def process_message(
        self,
        user_id: int,
        message: str,
        conversation_history: Optional[List[Dict]] = None
    ) -> str:
        """
        Process message related to skills management
        """
        try:
            # Determine intent
            intent = self._determine_intent(message)
            
            if intent == "create_skill":
                return await self._handle_skill_creation(message, user_id)
            elif intent == "find_skill":
                return await self._handle_skill_search(message)
            elif intent == "list_skills":
                return await self._handle_skill_listing()
            elif intent == "help":
                return await self._provide_help()
            else:
                # General skill-related question
                return await self._general_response(message, user_id)
                
        except Exception as e:
            logger.error(f"SkillBot error: {e}")
            return "⚠️ Ошибка обработки запроса навыков. Пожалуйста, попробуйте снова."
    
    def _determine_intent(self, message: str) -> str:
        """Determine user intent from message"""
        message_lower = message.lower()
        
        if any(word in message_lower for word in ['create', 'make', 'build', 'new skill', 'add skill']):
            return "create_skill"
        elif any(word in message_lower for word in ['find', 'search', 'lookup', 'look for']):
            return "find_skill"
        elif any(word in message_lower for word in ['list', 'show', 'all skills', 'what skills']):
            return "list_skills"
        elif any(word in message_lower for word in ['help', 'how to', 'what can you do']):
            return "help"
        else:
            return "general"
    
    async def _handle_skill_creation(self, message: str, user_id: int) -> str:
        """Handle skill creation request"""
        
        # Extract skill requirements
        extraction_prompt = f"""Извлеките требования к навыку из этого запроса:
"{message}"

Ответьте в формате JSON:
{{
    "skill_name": "предлагаемое_имя",
    "description": "что делает навык",
    "category": "productivity/entertainment/utility/integration/automation",
    "key_features": ["функция 1", "функция 2"],
    "complexity": "simple/medium/complex"
}}"""
        
        messages = [
            Message(role="system", content=self.SYSTEM_PROMPT),
            Message(role="user", content=extraction_prompt)
        ]
        
        response = await llm_client.chat_with_fallback(
            messages,
            model=settings.SKILLBOT_MODEL,
            reasoning_effort="medium"
        )
        
        try:
            requirements = json.loads(response.content)
        except json.JSONDecodeError:
            requirements = {
                "skill_name": "пользовательский_навык",
                "description": message,
                "category": "utility",
                "key_features": [],
                "complexity": "simple"
            }
        
        # Generate skill code
        skill_code = await self._generate_skill_code(requirements)
        
        # Create preview message
        preview = f"""🔧 <b>Предпросмотр создания навыка</b>

<b>Имя:</b> {requirements['skill_name']}
<b>Описание:</b> {requirements['description']}
<b>Категория:</b> {requirements['category']}
<b>Сложность:</b> {requirements['complexity']}

<b>Ключевые функции:</b>
"""
        for feature in requirements.get('key_features', []):
            preview += f"  • {feature}\n"
        
        preview += f"""
<b>Сгенерированный код:</b>
<pre>{html.escape(skill_code[:800])}</pre>

Используйте /skills для создания этого навыка с сгенерированным кодом."""
        
        # Store suggestion in memory
        await vector_memory.add_skill_documentation(
            requirements['skill_name'],
            requirements['description'],
            skill_code,
            {"suggested_by": user_id, "status": "draft"}
        )
        
        return preview
    
    async def _generate_skill_code(self, requirements: Dict) -> str:
        """Generate skill code based on requirements"""
        
        code_prompt = f"""Сгенерируйте Python навык для Telegram-бота используя эту спецификацию:

Имя навыка: {requirements['skill_name']}
Описание: {requirements['description']}
Категория: {requirements['category']}
Функции: {', '.join(requirements.get('key_features', []))}

Сгенерируйте полный, рабочий код Python, следуя этому шаблону:

```python
SKILL_NAME = "{requirements['skill_name']}"
SKILL_DESCRIPTION = "{requirements['description']}"
SKILL_CATEGORY = "{requirements['category']}"
SKILL_VERSION = "1.0.0"
SKILL_AUTHOR = "Сгенерировано SkillBot"
SKILL_COMMANDS = ["/команда1", "/команда2"]

async def handle_command(command: str, args: list, message, bot):
    \"\"\"Обработка команд навыка\"\"\"
    if command == "команда1":
        return await do_something(message, bot)
    return None

async def do_something(message, bot):
    \"\"\"Основная функция навыка\"\"\"
    # Реализация здесь
    await message.reply("Результат")

def setup_handlers():
    \"\"\"Настройка обработчиков команд\"\"\"
    return {{
        "команда1": handle_command,
    }}
```

Сделайте его полным и функциональным."""
        
        messages = [
            Message(role="system", content=self.SYSTEM_PROMPT),
            Message(role="user", content=code_prompt)
        ]
        
        response = await llm_client.chat_with_fallback(
            messages,
            model=settings.SKILLBOT_MODEL,
            reasoning_effort="medium"
        )
        
        # Extract code from response
        code = response.content
        if "```python" in code:
            code = code.split("```python")[1].split("```")[0]
        elif "```" in code:
            code = code.split("```")[1].split("```")[0]
        
        return code.strip()
    
    async def _handle_skill_search(self, message: str) -> str:
        """Handle skill search request"""
        
        # Extract search query
        # служебные слова («найди навык», "search for") отбрасывает сам skill_loader
        search_terms = message.lower().strip()
        
        # Search in loaded skills
        local_results = skill_loader.search_skills(search_terms)
        
        # Search in vector store
        vector_results = await vector_memory.search_skills(search_terms, n_results=5)
        
        response = f"🔍 <b>Результаты поиска навыков:</b> <i>{search_terms}</i>\n\n"
        
        if local_results:
            response += "<b>📦 Установленные навыки:</b>\n"
            for skill in local_results[:5]:
                response += f"  • <b>{skill.name}</b> - {skill.description}\n"
            response += "\n"
        
        if vector_results:
            response += "<b>📚 Связанная документация:</b>\n"
            for result in vector_results[:3]:
                response += f"  • {result['content'][:200]}...\n"
        
        if not local_results and not vector_results:
            response += "<i>Навыки не найдены. Попробуйте другие ключевые слова или создайте новый навык!</i>\n"
        
        return response
    
    async def _handle_skill_listing(self) -> str:
        """Handle skill listing request"""
        
        skills = skill_loader.list_skills()
        
        if not skills:
            return "📭 Пока нет установленных навыков. Используйте /skills для создания или импорта навыков!"
        
        # Group by source
        system_skills = [s for s in skills if s.source == "system"]
        custom_skills = [s for s in skills if s.source == "custom"]
        external_skills = [s for s in skills if s.source == "external"]
        
        response = "📋 <b>Установленные навыки</b>\n\n"
        
        if system_skills:
            response += "<b>🔧 Системные:</b>\n"
            for skill in system_skills:
                response += f"  • {skill.name} - {skill.description}\n"
            response += "\n"
        
        if custom_skills:
            response += "<b>✨ Пользовательские:</b>\n"
            for skill in custom_skills:
                response += f"  • {skill.name} - {skill.description}\n"
            response += "\n"
        
        if external_skills:
            response += "<b>📥 Внешние:</b>\n"
            for skill in external_skills:
                response += f"  • {skill.name} - {skill.description}\n"
        
        response += f"\n<i>Всего: {len(skills)} навыков</i>"
        
        return response
    
    async def _provide_help(self) -> str:
        """Provide help about SkillBot capabilities"""
        
        return """🔧 <b>SkillBot - Менеджер навыков</b>

Я могу помочь вам с:

<b>Созданием навыков:</b>
• "Создай навык для управления задачами"
• "Сделай навык для проверки погоды"
• "Создай навык для конвертации валют"

<b>Поиском навыков:</b>
• "Найди навыки для продуктивности"
• "Найди навык-калькулятор"
• "Какие у меня есть навыки?"

<b>Управлением навыками:</b>
• Используйте команду /skills для полного управления навыками
• Просмотр, редактирование, удаление или запуск навыков
• Импорт навыков из внешних источников

<b>Категории навыков:</b>
• 🚀 Продуктивность - Задачи, напоминания, заметки
• 🎮 Развлечения - Игры, развлекательные функции
• 🛠 Утилиты - Инструменты, калькуляторы, конвертеры
• 🔗 Интеграции - Подключения к API
• ⚙️ Автоматизация - Рабочие процессы, планирование

Просто скажите, что вам нужно!"""
    
    async def _general_response(self, message: str, user_id: int) -> str:
        """General skill-related response with RAG context"""

        messages = [
            Message(role="system", content=self.SYSTEM_PROMPT),
            Message(role="system", content=current_datetime_prompt()),
        ]

        # RAG: личные заметки
        memories = await vector_memory.search_memories(message, user_id, n_results=3)
        if memories:
            context = "Личные заметки пользователя:\n"
            for mem in memories:
                context += f"- {mem['content']}\n"
            messages.append(Message(role="system", content=context))

        # RAG: импортированные чаты
        conversations = await vector_memory.search_conversations(message, user_id, n_results=5)
        if conversations:
            context = "Фрагменты из переписки (используй как источник фактов):\n"
            for conv in conversations:
                context += f"---\n{conv['content']}\n"
            messages.append(Message(role="system", content=context))

        messages.append(Message(role="user", content=message))

        response = await llm_client.chat_with_fallback(
            messages,
            reasoning_effort="medium"
        )

        return f"🔧 <b>SkillBot</b>\n\n{response.content}"
    
    async def suggest_skill_for_task(self, task_description: str) -> SkillSuggestion:
        """Suggest a skill based on task description"""
        
        suggestion_prompt = f"""Based on this task, suggest a skill that would help:
"{task_description}"

Respond with JSON:
{{
    "name": "skill_name",
    "description": "what it does",
    "category": "category",
    "estimated_complexity": "simple/medium/complex",
    "code_outline": "brief outline of implementation"
}}"""
        
        messages = [
            Message(role="system", content=self.SYSTEM_PROMPT),
            Message(role="user", content=suggestion_prompt)
        ]
        
        response = await llm_client.chat_with_fallback(
            messages,
            model=settings.SKILLBOT_MODEL,
            reasoning_effort="medium"
        )
        
        try:
            data = json.loads(response.content)
            return SkillSuggestion(**data)
        except (json.JSONDecodeError, TypeError):
            return SkillSuggestion(
                name="custom_helper",
                description="Helper skill for the task",
                category="utility",
                estimated_complexity="simple",
                code_outline="# Implementation needed"
            )


# Global agent instance
skillbot = SkillBotAgent()
