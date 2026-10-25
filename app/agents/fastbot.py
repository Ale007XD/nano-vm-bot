"""
Fastbot Agent - Quick universal assistant
Fast, efficient, and versatile for everyday tasks
"""
import logging
import re
from dataclasses import dataclass
from typing import AsyncGenerator, Dict, List, Optional

from app.core.config import settings
from app.core.llm_client_v2 import Message, llm_client
from app.core.memory import vector_memory
from app.core.web_search import web_search

logger = logging.getLogger(__name__)


@dataclass
class ConversationContext:
    """Conversation context holder"""
    user_id: int
    messages: List[Message]
    relevant_memories: List[Dict]
    agent_mode: str = "fastbot"


class FastBotAgent:
    """
    FastBot - Fast universal assistant
    Optimized for quick responses and everyday tasks
    """

    SYSTEM_PROMPT = """Вы - FastBot, быстрый и эффективный AI-ассистент, интегрированный в Telegram-бот.
Ваши характеристики:
- Быстрые и краткие ответы
- Дружелюбный и полезный тон
- Хорошо разбираетесь в общих знаниях, быстрых вычислениях и повседневных задачах
- Можете использовать контекст из воспоминаний пользователя, когда это уместно
- Держите ответы краткими, но информативными
- Используйте форматирование Telegram HTML, когда это уместно
- ИМЕЕТЕ ДОСТУП К ПОИСКУ В ИНТЕРНЕТЕ для актуальной информации

ВСЕГДА отвечайте на РУССКОМ языке, если пользователь не просит иное.

У вас есть доступ к:
- Сохраненным воспоминаниям пользователя
- Истории разговоров
- Фрагментам переписки из импортированных чатов (conversations)
- Поиску в интернете для актуальной информации

При получении результатов поиска из интернета, используйте их для формирования актуального ответа.

Если сообщение содержит блок [ТРАНСКРИПТ ВИДЕО] — это текст видео, полученный автоматически. Используй его как основной источник и выполни задачу пользователя (саммари, перевод, анализ и т.д.) строго по транскрипту.
При наличии фрагментов из чата — используй их как основной источник ответа, указывая конкретные детали."""

    def __init__(self):
        self.name = "fastbot"
        self.display_name = "⚡ FastBot"
        self.description = "Quick universal assistant for everyday tasks"

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
        conversation_history: Optional[List[Dict]] = None
    ) -> str:
        try:
            messages = [Message(role="system", content=self.SYSTEM_PROMPT)]

            # Web search
            search_results_text = ""
            if self._should_search_web(message):
                logger.info(f"Triggering web search for: {message[:50]}...")
                try:
                    search_results_text = await web_search.search_and_format(
                        message,
                        num_results=settings.WEB_SEARCH_RESULTS
                    )
                except Exception as e:
                    logger.warning(f"Web search failed: {e}")

            if search_results_text:
                messages.append(Message(
                    role="system",
                    content=f"Актуальная информация из интернета:\n{search_results_text}"
                ))

            # Memories (личные заметки пользователя)
            memories = await vector_memory.search_memories(message, user_id, n_results=3)
            if memories:
                context = "Личные заметки пользователя:\n"
                for mem in memories:
                    context += f"- {mem['content']}\n"
                messages.append(Message(role="system", content=context))

            # Conversations (импортированные чаты — RAG)
            conversations = await vector_memory.search_conversations(message, user_id, n_results=5)
            if conversations:
                context = "Фрагменты из переписки (используй как источник фактов):\n"
                for conv in conversations:
                    context += f"---\n{conv['content']}\n"
                messages.append(Message(role="system", content=context))

            # История диалога
            if conversation_history:
                for msg in conversation_history[-10:]:
                    messages.append(Message(
                        role=msg['role'],
                        content=msg['content']
                    ))

            messages.append(Message(role="user", content=message))

            response = await llm_client.chat_with_fallback(
                messages,
                model=settings.FASTBOT_MODEL
            )

            return response.content

        except Exception as e:
            logger.error(f"FastBot error: {e}")
            return "⚠️ Извините, произошла ошибка. Пожалуйста, попробуйте снова."

    async def stream_message(
        self,
        user_id: int,
        message: str,
        conversation_history: Optional[List[Dict]] = None
    ) -> AsyncGenerator[str, None]:
        try:
            messages = [Message(role="system", content=self.SYSTEM_PROMPT)]

            memories = await vector_memory.search_memories(message, user_id, n_results=3)
            if memories:
                context = "Личные заметки пользователя:\n"
                for mem in memories:
                    context += f"- {mem['content']}\n"
                messages.append(Message(role="system", content=context))

            conversations = await vector_memory.search_conversations(message, user_id, n_results=5)
            if conversations:
                context = "Фрагменты из переписки:\n"
                for conv in conversations:
                    context += f"---\n{conv['content']}\n"
                messages.append(Message(role="system", content=context))

            if conversation_history:
                for msg in conversation_history[-10:]:
                    messages.append(Message(
                        role=msg['role'],
                        content=msg['content']
                    ))

            messages.append(Message(role="user", content=message))

            async for chunk in llm_client.stream_chat(messages):
                yield chunk

        except Exception as e:
            logger.error(f"Nanobot streaming error: {e}")
            yield "⚠️ Извините, произошла ошибка."

    async def quick_answer(self, question: str) -> str:
        try:
            messages = [
                Message(role="system", content=self.SYSTEM_PROMPT),
                Message(role="user", content=question)
            ]
            response = await llm_client.chat_with_fallback(messages)
            return response.content
        except Exception as e:
            logger.error(f"Nanobot quick answer error: {e}")
            return "⚠️ Не удалось обработать ваш запрос."


# Global agent instance
fastbot = FastBotAgent()
