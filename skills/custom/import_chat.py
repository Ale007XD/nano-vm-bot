"""
Skill: import_chat
Description: Импорт истории Telegram-чата (JSON) в векторную память RAG.
Поддерживает формат экспорта Telegram Desktop.
Usage: Отправьте файл result.json боту или укажите путь к файлу.
"""
import json
import logging
import os
from typing import Any, Dict, List, Optional

from app.core.memory import vector_memory

logger = logging.getLogger(__name__)

SKILL_NAME = "import_chat"
SKILL_DESCRIPTION = "Импорт истории Telegram-чата в память бота для поиска по переписке"
SKILL_VERSION = "1.0.0"

# Максимальная длина одного фрагмента (символов)
CHUNK_SIZE = 500
# Перекрытие между фрагментами
CHUNK_OVERLAP = 50
# Минимальная длина сообщения для сохранения
MIN_MESSAGE_LEN = 10


def _extract_text(message: Dict) -> Optional[str]:
    """Извлекает текст из сообщения (поддерживает text и text_entities)"""
    text = message.get("text", "")

    if isinstance(text, list):
        parts = []
        for part in text:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                parts.append(part.get("text", ""))
        text = "".join(parts)

    return text.strip() if text else None


def _chunk_text(text: str, chunk_size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> List[str]:
    """Разбивает длинный текст на перекрывающиеся фрагменты"""
    if len(text) <= chunk_size:
        return [text]

    chunks = []
    start = 0
    while start < len(text):
        end = start + chunk_size
        chunks.append(text[start:end])
        start += chunk_size - overlap

    return chunks


def _build_fragments(messages: List[Dict], window: int = 3) -> List[Dict]:
    """
    Строит фрагменты из окна нескольких сообщений подряд.
    chunk_index гарантирует уникальность ID даже для одинакового контента.
    """
    fragments = []
    text_messages = []
    chunk_index = 0  # FIX: глобальный счётчик для уникальных ID

    for msg in messages:
        if msg.get("type") != "message":
            continue
        text = _extract_text(msg)
        if not text or len(text) < MIN_MESSAGE_LEN:
            continue

        sender = msg.get("from", "Unknown")
        date = msg.get("date", "")
        text_messages.append({
            "sender": sender,
            "date": date,
            "text": text
        })

    for i in range(len(text_messages)):
        window_msgs = text_messages[i:i + window]
        combined = "\n".join(
            f"[{m['date'][:10]}] {m['sender']}: {m['text']}"
            for m in window_msgs
        )

        for chunk in _chunk_text(combined):
            fragments.append({
                "content": chunk,
                "metadata": {
                    "source": "telegram_export",
                    "sender": window_msgs[0]["sender"],
                    "date": window_msgs[0]["date"],
                    "window_size": len(window_msgs),
                    "chunk_index": chunk_index,  # FIX: уникальный индекс чанка
                }
            })
            chunk_index += 1  # FIX: инкремент

    return fragments


async def import_from_file(
    file_path: str,
    user_id: int,
    importance: float = 0.6,
    window: int = 3
) -> Dict[str, Any]:
    """
    Импортирует Telegram JSON в векторную память.

    Args:
        file_path: путь к result.json
        user_id: ID пользователя для привязки памяти
        importance: важность фрагментов (0.0-1.0)
        window: размер окна сообщений для одного фрагмента

    Returns:
        Статистика импорта
    """
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"Файл не найден: {file_path}")

    logger.info(f"Начинаем импорт из {file_path} для user {user_id}")

    with open(file_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    chat_name = data.get("name", "Unknown chat")
    messages = data.get("messages", [])
    total_messages = len(messages)

    logger.info(f"Чат: {chat_name}, сообщений: {total_messages}")

    fragments = _build_fragments(messages, window=window)
    logger.info(f"Подготовлено фрагментов: {len(fragments)}")

    saved = 0
    skipped = 0

    for fragment in fragments:
        try:
            await vector_memory.add_conversation_fragment(
                content=fragment["content"],
                user_id=user_id,
                importance=importance,
                metadata={
                    **fragment["metadata"],
                    "chat_name": chat_name,
                    "imported": True
                }
            )
            saved += 1
        except Exception as e:
            logger.warning(f"Пропущен фрагмент: {e}")
            skipped += 1

    result = {
        "chat_name": chat_name,
        "total_messages": total_messages,
        "fragments_created": len(fragments),
        "saved": saved,
        "skipped": skipped
    }

    logger.info(f"Импорт завершён: {result}")
    return result


async def run(context: Dict[str, Any]) -> str:
    """Точка входа скилла"""
    user_id = context.get("user_id")
    file_path = context.get("file_path") or context.get("args", {}).get("file_path")

    if not file_path:
        return (
            "📂 Укажите путь к файлу экспорта Telegram.\n\n"
            "Пример: /app/data/result.json\n\n"
            "Как экспортировать чат:\n"
            "Telegram Desktop → чат → ⋮ → Экспорт истории чата → JSON"
        )

    try:
        stats = await import_from_file(
            file_path=file_path,
            user_id=user_id,
            importance=0.6,
            window=3
        )

        return (
            f"✅ Импорт завершён!\n\n"
            f"📌 Чат: {stats['chat_name']}\n"
            f"💬 Сообщений в файле: {stats['total_messages']}\n"
            f"🧩 Фрагментов создано: {stats['fragments_created']}\n"
            f"💾 Сохранено: {stats['saved']}\n"
            f"⚠️ Пропущено: {stats['skipped']}\n\n"
            f"Теперь бот будет использовать эту переписку при ответах."
        )

    except FileNotFoundError as e:
        return f"❌ {e}"
    except Exception as e:
        logger.error(f"Ошибка импорта: {e}")
        return f"❌ Ошибка импорта: {e}"
