"""
Skill: validated_answer
Description: Валидация RAG-выдачи из импортированных чатов.
  Шаг 1 — консенсус-кластеризация: группируем похожие ответы по embedding-близости,
           берём кластер с большинством голосов.
  Шаг 2 — web-верификация (только если консенсус слабый): проверяем лидирующий
           кластер через поиск, LLM сравнивает с реальностью.
  Итог — ответ с тегом уверенности: ✅ HIGH / ⚠️ MEDIUM / ❓ LOW.

Usage:
  context = {
      "user_id": 123,
      "query": "Как настроить nginx для FastAPI?",
      # опционально — если уже есть результаты из RAG:
      "fragments": [{"content": "...", "distance": 0.3}, ...]
  }
  result = await run(context)

Changelog:
  1.0.0 — initial: consensus clustering + optional web verify
"""
import asyncio
import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

SKILL_NAME = "validated_answer"
SKILL_DESCRIPTION = (
    "Валидирует RAG-ответы из базы чатов: консенсус-кластеризация + "
    "web-верификация для слабых консенсусов"
)
SKILL_VERSION = "1.0.0"
SKILL_CATEGORY = "utility"
SKILL_AUTHOR = "Alex"

# ------------------------------------------------------------------
# Настройки
# ------------------------------------------------------------------

# Порог cosine-similarity для объединения в один кластер (0..1)
CLUSTER_SIMILARITY_THRESHOLD = 0.75

# Если доля лидирующего кластера ≥ порога — web-проверка не нужна
CONSENSUS_CONFIDENCE_THRESHOLD = 0.60

# Сколько фрагментов запросить из RAG (если не переданы снаружи)
RAG_N_RESULTS = 8

# Сколько результатов поиска использовать для web-верификации
WEB_SEARCH_N = 4

# Минимальная длина фрагмента для участия в голосовании
MIN_FRAGMENT_LEN = 20


# ------------------------------------------------------------------
# Шаг 0 — получить фрагменты из RAG (если не переданы снаружи)
# ------------------------------------------------------------------

async def _fetch_fragments(query: str, user_id: int) -> List[Dict]:
    """Получаем фрагменты из ChromaDB conversations-коллекции."""
    from app.core.memory import vector_memory

    if not vector_memory._initialized:
        await vector_memory.initialize()

    results = await vector_memory.search_conversations(
        query=query,
        user_id=user_id,
        n_results=RAG_N_RESULTS
    )
    return results  # [{content, distance, metadata}, ...]


# ------------------------------------------------------------------
# Шаг 1 — консенсус-кластеризация
# ------------------------------------------------------------------

def _cosine_similarity(a: List[float], b: List[float]) -> float:
    """Cosine similarity между двумя векторами."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x ** 2 for x in a) ** 0.5
    norm_b = sum(x ** 2 for x in b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def _embed_texts(texts: List[str]) -> List[List[float]]:
    """
    Embeddings общей моделью VectorMemory (синхронно). Раньше здесь на КАЖДЫЙ вызов
    создавалась вторая копия модели (~0.5 ГБ) — теперь модель одна на процесс.
    """
    from app.core.memory import vector_memory

    return vector_memory.embed_texts_sync(texts)


def _cluster_fragments(
    fragments: List[Dict],
    threshold: float = CLUSTER_SIMILARITY_THRESHOLD
) -> List[List[int]]:
    """
    Жадная кластеризация: фрагмент идёт в первый кластер, с центроидом
    которого similarity ≥ threshold. Иначе — новый кластер.

    Возвращает список кластеров, каждый — список индексов фрагментов.
    """
    texts = [f["content"] for f in fragments]
    embeddings = _embed_texts(texts)

    clusters: List[List[int]] = []          # список индексов
    centroids: List[List[float]] = []       # усреднённый embedding кластера

    for idx, emb in enumerate(embeddings):
        placed = False
        for c_idx, centroid in enumerate(centroids):
            if _cosine_similarity(emb, centroid) >= threshold:
                clusters[c_idx].append(idx)
                # обновляем centroid как среднее
                n = len(clusters[c_idx])
                centroids[c_idx] = [
                    (centroid[i] * (n - 1) + emb[i]) / n
                    for i in range(len(emb))
                ]
                placed = True
                break
        if not placed:
            clusters.append([idx])
            centroids.append(emb[:])  # копия

    return clusters


def _pick_consensus(
    fragments: List[Dict],
    clusters: List[List[int]]
) -> Tuple[List[Dict], float]:
    """
    Выбирает лидирующий кластер и считает confidence:
      confidence = votes_in_leader / total_fragments

    Возвращает (фрагменты лидера, confidence_score).
    """
    if not clusters:
        return fragments, 0.0

    # сортируем кластеры по количеству голосов
    sorted_clusters = sorted(clusters, key=lambda c: len(c), reverse=True)
    leader_indices = sorted_clusters[0]
    confidence = len(leader_indices) / len(fragments)

    leader_fragments = [fragments[i] for i in leader_indices]
    return leader_fragments, confidence


# ------------------------------------------------------------------
# Шаг 2 — web-верификация (опциональная)
# ------------------------------------------------------------------

async def _web_verify(
    query: str,
    consensus_text: str
) -> Dict[str, Any]:
    """
    Ищем в интернете, просим LLM сравнить консенсус с результатами поиска.

    Возвращает:
      {
        "verdict": "verified" | "contradicted" | "unverifiable",
        "confidence_delta": float,   # +0.2 / -0.3 / 0.0
        "web_summary": str,
        "sources": [str, ...]
      }
    """
    from app.core.llm_client_v2 import Message, llm_client
    from app.core.web_search import web_search

    # --- поиск ---
    try:
        results = await web_search.search(query, num_results=WEB_SEARCH_N)
    except Exception as e:
        logger.warning(f"Web search failed during validation: {e}")
        return {
            "verdict": "unverifiable",
            "confidence_delta": 0.0,
            "web_summary": "Поиск недоступен",
            "sources": []
        }

    if not results:
        return {
            "verdict": "unverifiable",
            "confidence_delta": 0.0,
            "web_summary": "Результатов поиска нет",
            "sources": []
        }

    web_snippets = "\n".join(
        f"[{r.source or r.link}]: {r.snippet}"
        for r in results[:WEB_SEARCH_N]
    )
    sources = [r.link for r in results[:WEB_SEARCH_N]]

    # --- LLM-верификатор ---
    verify_prompt = f"""Задача: проверить корректность ответа из базы знаний относительно результатов поиска.

ВОПРОС: {query}

ОТВЕТ ИЗ БАЗЫ:
{consensus_text}

РЕЗУЛЬТАТЫ ПОИСКА:
{web_snippets}

Оцени:
1. Соответствует ли ответ из базы результатам поиска?
2. Есть ли противоречия или устаревшая информация?

Ответь СТРОГО в формате JSON (без markdown, без пояснений):
{{
  "verdict": "verified" | "contradicted" | "unverifiable",
  "reason": "одно предложение",
  "corrected_info": "если contradicted — что именно неверно и как правильно; иначе null"
}}"""

    try:
        response = await llm_client.chat(
            messages=[Message(role="user", content=verify_prompt)],
            model="default",
            max_tokens=300,
            temperature=0.1
        )

        import json
        raw = response.content.strip()
        # убираем возможные markdown-обёртки
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        data = json.loads(raw.strip())
        verdict = data.get("verdict", "unverifiable")
        reason = data.get("reason", "")
        corrected = data.get("corrected_info")

    except Exception as e:
        logger.warning(f"LLM verifier parse error: {e}")
        verdict = "unverifiable"
        reason = str(e)
        corrected = None

    delta_map = {
        "verified": 0.25,
        "contradicted": -0.35,
        "unverifiable": 0.0
    }

    summary_parts = [reason]
    if corrected:
        summary_parts.append(f"Актуально: {corrected}")

    return {
        "verdict": verdict,
        "confidence_delta": delta_map.get(verdict, 0.0),
        "web_summary": " | ".join(summary_parts),
        "sources": sources
    }


# ------------------------------------------------------------------
# Шаг 3 — финальный LLM-синтез
# ------------------------------------------------------------------

async def _synthesize(
    query: str,
    fragments: List[Dict],
    web_context: Optional[str] = None,
    web_verdict: Optional[str] = None
) -> str:
    """
    Синтезирует итоговый ответ из фрагментов (+ web-контекст если есть).
    """
    from app.core.llm_client_v2 import Message, llm_client

    fragments_text = "\n---\n".join(
        f["content"] for f in fragments[:5]  # не больше 5
    )

    system = (
        "Ты — ассистент, синтезирующий точный ответ из фрагментов переписки. "
        "Отвечай только на основе предоставленных источников. "
        "Если источники противоречат друг другу — укажи это. "
        "Отвечай на русском языке, кратко и по делу."
    )

    user_parts = [
        f"Вопрос: {query}\n",
        f"Фрагменты из базы знаний:\n{fragments_text}"
    ]

    if web_context:
        correction_note = ""
        if web_verdict == "contradicted":
            correction_note = "\n⚠️ ВАЖНО: веб-поиск показал расхождение с базой — приоритет за актуальными данными:"
        elif web_verdict == "verified":
            correction_note = "\nДополнительно — подтверждающий контекст из интернета:"
        else:
            correction_note = "\nДополнительный контекст (не удалось верифицировать):"
        user_parts.append(f"{correction_note}\n{web_context}")

    user_parts.append(
        "\nДай точный, лаконичный ответ. "
        "Если данные могут быть устаревшими — предупреди об этом."
    )

    response = await llm_client.chat(
        messages=[
            Message(role="system", content=system),
            Message(role="user", content="\n".join(user_parts))
        ],
        model="default",
        max_tokens=600,
        temperature=0.3
    )
    return response.content.strip()


# ------------------------------------------------------------------
# Главная функция — оркестрация
# ------------------------------------------------------------------

async def validate_and_answer(
    query: str,
    user_id: int,
    fragments: Optional[List[Dict]] = None
) -> Dict[str, Any]:
    """
    Полный пайплайн: retrieval → кластеризация → (web-verify) → синтез.

    Возвращает:
    {
        "answer": str,
        "confidence": float,           # итоговая уверенность 0..1
        "confidence_label": str,       # HIGH / MEDIUM / LOW
        "cluster_votes": int,          # голосов у лидирующего кластера
        "total_fragments": int,
        "web_verdict": str | None,     # verified / contradicted / unverifiable / None
        "web_sources": [str],
        "web_summary": str | None
    }
    """

    # 0. Retrieval
    if fragments is None:
        logger.info(f"validated_answer: fetching RAG fragments for query='{query[:60]}'")
        fragments = await _fetch_fragments(query, user_id)

    fragments = [
        f for f in fragments
        if len(f.get("content", "")) >= MIN_FRAGMENT_LEN
    ]

    if not fragments:
        return {
            "answer": "В базе знаний не найдено подходящих фрагментов для этого запроса.",
            "confidence": 0.0,
            "confidence_label": "LOW",
            "cluster_votes": 0,
            "total_fragments": 0,
            "web_verdict": None,
            "web_sources": [],
            "web_summary": None
        }

    # 1. Кластеризация (в executor чтобы не блокировать event loop)
    logger.info(f"validated_answer: clustering {len(fragments)} fragments")
    loop = asyncio.get_event_loop()
    clusters = await loop.run_in_executor(
        None, _cluster_fragments, fragments, CLUSTER_SIMILARITY_THRESHOLD
    )

    leader_fragments, consensus_score = _pick_consensus(fragments, clusters)

    logger.info(
        f"validated_answer: consensus={consensus_score:.2f}, "
        f"leader_cluster={len(leader_fragments)}/{len(fragments)} fragments"
    )

    # 2. Web-верификация — только если консенсус слабый
    web_result = None
    final_confidence = consensus_score

    if consensus_score < CONSENSUS_CONFIDENCE_THRESHOLD:
        logger.info("validated_answer: weak consensus → triggering web verify")
        consensus_text = "\n".join(f["content"] for f in leader_fragments[:3])
        web_result = await _web_verify(query, consensus_text)
        final_confidence = max(
            0.0,
            min(1.0, consensus_score + web_result["confidence_delta"])
        )
        logger.info(
            f"validated_answer: web_verdict={web_result['verdict']}, "
            f"confidence {consensus_score:.2f} → {final_confidence:.2f}"
        )

    # 3. Синтез
    web_context = web_result["web_summary"] if web_result else None
    web_verdict = web_result["verdict"] if web_result else None

    answer = await _synthesize(
        query=query,
        fragments=leader_fragments,
        web_context=web_context,
        web_verdict=web_verdict
    )

    # 4. Метка уверенности
    if final_confidence >= 0.65:
        label = "HIGH"
    elif final_confidence >= 0.35:
        label = "MEDIUM"
    else:
        label = "LOW"

    return {
        "answer": answer,
        "confidence": round(final_confidence, 2),
        "confidence_label": label,
        "cluster_votes": len(leader_fragments),
        "total_fragments": len(fragments),
        "web_verdict": web_verdict,
        "web_sources": web_result["sources"] if web_result else [],
        "web_summary": web_result["web_summary"] if web_result else None
    }


# ------------------------------------------------------------------
# Форматирование ответа для Telegram
# ------------------------------------------------------------------

def _format_result(result: Dict[str, Any]) -> str:
    """Формирует читаемый ответ с тегом уверенности."""
    label = result["confidence_label"]
    confidence = result["confidence"]
    votes = result["cluster_votes"]
    total = result["total_fragments"]

    badge = {
        "HIGH": "✅ Высокая уверенность",
        "MEDIUM": "⚠️ Средняя уверенность",
        "LOW": "❓ Низкая уверенность"
    }.get(label, "❓")

    lines = [
        f"{badge} ({confidence:.0%})",
        f"<i>Консенсус: {votes} из {total} фрагментов</i>",
        "",
        result["answer"]
    ]

    # web-блок
    if result.get("web_verdict"):
        verdict_icon = {
            "verified": "🌐 Подтверждено поиском",
            "contradicted": "🌐 ⚠️ Расхождение с поиском",
            "unverifiable": "🌐 Не удалось проверить"
        }.get(result["web_verdict"], "🌐")

        lines.append(f"\n{verdict_icon}")

        if result.get("web_summary"):
            lines.append(f"<i>{result['web_summary']}</i>")

        if result.get("web_sources"):
            sources_str = "\n".join(f"  • {s}" for s in result["web_sources"][:3])
            lines.append(f"Источники:\n{sources_str}")

    return "\n".join(lines)


# ------------------------------------------------------------------
# Точка входа скилла
# ------------------------------------------------------------------

async def run(context: Dict[str, Any]) -> str:
    """
    Точка входа.

    context ожидает:
      - user_id: int
      - query: str  (или message_text как fallback)
      - fragments: list (опционально — если RAG уже сделан снаружи)
    """
    user_id = context.get("user_id")
    query = context.get("query") or context.get("message_text", "").strip()
    fragments = context.get("fragments")  # опционально

    if not query:
        return (
            "❓ <b>validated_answer</b>\n\n"
            "Укажите запрос в поле <code>query</code>.\n\n"
            "Пример использования:\n"
            "<code>validated_answer: Как настроить nginx для FastAPI?</code>"
        )

    if not user_id:
        return "❌ user_id не передан в контексте скилла."

    try:
        result = await validate_and_answer(
            query=query,
            user_id=user_id,
            fragments=fragments
        )
        return _format_result(result)

    except Exception as e:
        logger.error(f"validated_answer error: {e}", exc_info=True)
        return f"❌ Ошибка валидации: {e}"
