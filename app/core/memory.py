"""
Vector Memory System for RAG (Retrieval Augmented Generation)
Uses ChromaDB for vector storage and fastembed for embeddings (multilingual)
"""
import asyncio
import contextlib
import gc
import hashlib
import logging
import os
import re
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

import chromadb
from chromadb.config import Settings as ChromaSettings
from fastembed import TextEmbedding

from app.core.config import settings

try:  # межпроцессная блокировка перестройки индекса (бот и витрина делят один vector_store)
    import fcntl
except ImportError:  # не POSIX
    fcntl = None

logger = logging.getLogger(__name__)

# Модель, которой до появления настройки EMBEDDING_MODEL были построены все коллекции.
# Коллекции без отметки модели считаются построенными ею.
LEGACY_EMBEDDING_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
EMBEDDING_MODEL = LEGACY_EMBEDDING_MODEL  # обратная совместимость импорта

# E5-семейство обучено с префиксами: без них качество поиска заметно падает
# (fastembed их сам не добавляет). Остальные модели префиксов не требуют.
_MODEL_PREFIXES = (
    ("intfloat/multilingual-e5", ("query: ", "passage: ")),
    ("intfloat/e5", ("query: ", "passage: ")),
)

_MULTILINGUAL_HINTS = ("multilingual", "e5", "labse", "bge-m3", "jina-embeddings-v3")
_REINDEX_SUFFIX = "__reindex"
_REINDEX_BATCH = 256


# Прежний «умолчательный» EMBEDDING_MODEL из .env.example: настройка тогда не читалась, а эта
# модель английская — на русском ищет плохо. Не даём ей молча вступить в силу.
_DEAD_ENGLISH_DEFAULT = "sentence-transformers/all-MiniLM-L6-v2"
_warned_dead_default = False


def _model_name() -> str:
    """Модель эмбеддингов из настроек (EMBEDDING_MODEL)."""
    global _warned_dead_default
    val = getattr(settings, "EMBEDDING_MODEL", None)
    if not (isinstance(val, str) and val.strip()):
        return LEGACY_EMBEDDING_MODEL
    val = val.strip()
    if val == _DEAD_ENGLISH_DEFAULT:
        if not _warned_dead_default:
            _warned_dead_default = True
            logger.warning(
                f"EMBEDDING_MODEL={val} (устаревшее значение из старого .env.example, оно не "
                f"применялось) игнорируется — используется {LEGACY_EMBEDDING_MODEL}. Задайте модель "
                "явно, например intfloat/multilingual-e5-large"
            )
        return LEGACY_EMBEDDING_MODEL
    return val


def _cache_dir() -> Optional[str]:
    """Каталог кэша модели (на томе — переживает пересоздание контейнера)."""
    val = getattr(settings, "EMBEDDING_CACHE_DIR", None)
    if isinstance(val, str) and val.strip():
        os.makedirs(val, exist_ok=True)
        return val
    return None


def _prefixes_for(model_name: str):
    name = (model_name or "").lower()
    for start, prefixes in _MODEL_PREFIXES:
        if name.startswith(start):
            return prefixes
    return ("", "")


def _trim_memory() -> None:
    """Вернуть освобождённую кучу ОС (glibc): без этого RSS после выгрузки модели не падает."""
    try:
        import ctypes

        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except Exception:  # не glibc / нет libc.so.6 — не критично
        pass


def _idle_unload_seconds() -> int:
    """0 — модель грузится при старте и не выгружается (прежнее поведение)."""
    val = getattr(settings, "EMBEDDING_IDLE_UNLOAD_SECONDS", 0)
    return val if isinstance(val, int) and not isinstance(val, bool) and val > 0 else 0


def _sanitize_metadata(metadata: Dict[str, Any]) -> Dict[str, Any]:
    """Убирает None-значения из metadata — ChromaDB принимает только str/int/float/bool"""
    return {
        k: v for k, v in metadata.items()
        if v is not None and isinstance(v, (str, int, float, bool))
    }


DEFAULT_CORPUS = "corpus"
_CORPUS_NAME_RE = re.compile(r"^corpus(_[a-z0-9_]{1,40})?$")


def _check_corpus_name(name: str) -> str:
    """Имя коллекции корпуса: только «corpus» или «corpus_<имя>».

    Не даёт указать служебные коллекции (memories, skills, conversations).
    """
    if not isinstance(name, str) or not _CORPUS_NAME_RE.match(name):
        raise ValueError(f"Недопустимое имя коллекции корпуса: {name!r}")
    return name


class VectorMemory:
    """Vector memory system for storing and retrieving relevant context"""

    def __init__(self):
        self.embedding_model = None
        self.chroma_client = None
        self.collections = {}
        self._initialized = False
        # Эмбеддинг-модель — самый тяжёлый объект (~0.5–0.8 ГБ RSS): грузим лениво
        # при первом использовании и выгружаем после простоя (EMBEDDING_IDLE_UNLOAD_SECONDS)
        self._model_lock = threading.Lock()
        self._model_busy = 0
        self._model_last_used = 0.0
        self._unload_task: Optional[asyncio.Task] = None

    # --- ленивая модель ---------------------------------------------------
    def _acquire_model(self):
        """Вернуть модель (загрузив при необходимости) и пометить занятой. Парный _release_model."""
        with self._model_lock:
            if self.embedding_model is None:
                name, cache = _model_name(), _cache_dir()
                logger.info(f"Loading embedding model: {name}")
                kwargs = {"cache_dir": cache} if cache else {}
                self.embedding_model = TextEmbedding(model_name=name, **kwargs)
            self._model_busy += 1
            self._model_last_used = time.monotonic()
            return self.embedding_model

    def _release_model(self) -> None:
        with self._model_lock:
            self._model_busy -= 1
            self._model_last_used = time.monotonic()

    def unload_model_if_idle(self, idle_seconds: float, now: Optional[float] = None) -> bool:
        """Выгрузить модель, если ею не пользуются дольше idle_seconds. True — выгружена."""
        now = time.monotonic() if now is None else now
        with self._model_lock:
            if self.embedding_model is None or self._model_busy > 0:
                return False
            if now - self._model_last_used < idle_seconds:
                return False
            self.embedding_model = None
        gc.collect()
        _trim_memory()
        logger.info("Embedding model unloaded (idle)")
        return True

    async def _unload_loop(self, idle_seconds: int, interval: float = 30.0) -> None:
        while True:
            await asyncio.sleep(interval)
            try:
                self.unload_model_if_idle(idle_seconds)
            except Exception as e:  # выгрузка — оптимизация, не должна ронять цикл
                logger.warning(f"Embedding unload failed: {e}")

    def embed_texts_sync(self, texts: List[str], kind: str = "query") -> List[List[float]]:
        """Пакетные эмбеддинги общей моделью (для навыков; синхронно).

        kind: "query" | "passage" — для моделей с префиксами (E5). Симметричное сравнение
        текстов между собой по рекомендации E5 идёт с «query: ».
        """
        model = self._acquire_model()
        try:
            prefix = _prefixes_for(_model_name())[0 if kind == "query" else 1]
            return [vec.tolist() for vec in model.embed([prefix + t for t in texts])]
        finally:
            self._release_model()

    async def initialize(self):
        """Initialize vector memory components"""
        if self._initialized:
            return

        try:
            idle = _idle_unload_seconds()
            if idle:
                logger.info(
                    f"Embedding model is lazy: loads on first use, unloads after {idle}s idle"
                )
                self._unload_task = asyncio.create_task(self._unload_loop(idle))
            else:
                # fastembed и ChromaDB — синхронные: уводим из event loop
                await asyncio.to_thread(self._acquire_model)
                self._release_model()
                logger.info("Memory initialized with embeddings")

            os.makedirs(settings.VECTOR_STORE_PATH, exist_ok=True)

            self.chroma_client = await asyncio.to_thread(
                chromadb.PersistentClient,
                path=settings.VECTOR_STORE_PATH,
                settings=ChromaSettings(
                    anonymized_telemetry=False,
                    allow_reset=True
                )
            )

            logger.info(f"Embedding model: {_model_name()}")
            if not any(h in _model_name().lower() for h in _MULTILINGUAL_HINTS):
                logger.warning(
                    f"EMBEDDING_MODEL={_model_name()} не похожа на многоязычную: русский текст "
                    "будет искаться плохо. Рекомендуется intfloat/multilingual-e5-large "
                    "или sentence-transformers/paraphrase-multilingual-mpnet-base-v2"
                )

            for key, description in (
                ("memories", "User memories and notes"),
                ("skills", "Skills documentation and code"),
                ("conversations", "Important conversation fragments"),
                ("corpus", "Showcase support knowledge corpus"),
            ):
                self.collections[key] = await self._open_collection(key, description)

            self._initialized = True
            logger.info("Vector memory initialized successfully")

        except Exception as e:
            logger.error(f"Failed to initialize vector memory: {e}")
            raise

    # --- модель коллекций и перестройка индекса ---------------------------
    async def _open_collection(self, name: str, description: str):
        """Открыть коллекцию и привести её векторы к текущей EMBEDDING_MODEL."""
        col = await asyncio.to_thread(
            self.chroma_client.get_or_create_collection,
            name=name,
            metadata={"description": description},
        )
        try:
            return await self._ensure_collection_model(col, name, description)
        except Exception as e:  # не роняем бота: поиск вернёт пусто, в логе — причина
            logger.error(
                f"Не удалось привести коллекцию «{name}» к модели {_model_name()}: {e}. "
                "Поиск по ней может работать неверно до успешной перестройки (перезапустите бота)"
            )
            return col

    async def _ensure_collection_model(self, col, name: str, description: str):
        target = _model_name()
        md = getattr(col, "metadata", None)
        if not isinstance(md, dict):
            md = {}
        stamped = md.get("embedding_model")
        if stamped == target:
            return col
        if stamped is None and LEGACY_EMBEDDING_MODEL == target:
            # коллекция построена прежней (зашитой) моделью — только ставим отметку
            await asyncio.to_thread(col.modify, metadata={**md, "embedding_model": target})
            return col
        count = await asyncio.to_thread(col.count)
        if not isinstance(count, int):
            return col
        if count == 0 and stamped is None:
            # новая пустая коллекция — просто помечаем моделью
            await asyncio.to_thread(col.modify, metadata={**md, "embedding_model": target})
            return col
        return await self._reindex_collection(name, description, target)

    @contextlib.asynccontextmanager
    async def _reindex_guard(self):
        """Один перестраивает — остальные ждут (в процессе и между контейнерами бота и витрины)."""
        if not hasattr(self, "_reindex_alock"):
            self._reindex_alock = asyncio.Lock()
        async with self._reindex_alock:
            fd = None
            if fcntl is not None:
                try:
                    os.makedirs(settings.VECTOR_STORE_PATH, exist_ok=True)
                    fd = os.open(os.path.join(settings.VECTOR_STORE_PATH, ".reindex.lock"),
                                 os.O_CREAT | os.O_RDWR)
                    await asyncio.to_thread(fcntl.flock, fd, fcntl.LOCK_EX)
                except OSError as e:
                    logger.warning(f"Не удалось взять блокировку перестройки: {e}")
            try:
                yield
            finally:
                if fd is not None:
                    with contextlib.suppress(OSError):
                        fcntl.flock(fd, fcntl.LOCK_UN)
                    os.close(fd)

    async def _reindex_collection(self, name: str, description: str, target: str):
        """Пересчитать векторы коллекции моделью target из сохранённых текстов.

        Новая коллекция строится под временным именем и подменяет старую только после полного
        заполнения: при сбое данные не теряются (остаётся старая коллекция либо готовая копия).
        """
        client = self.chroma_client
        tmp_name = name + _REINDEX_SUFFIX
        async with self._reindex_guard():
            existing = {c if isinstance(c, str) else c.name
                        for c in await asyncio.to_thread(client.list_collections)}

            # восстановление после прерванной подмены: старой нет, а готовая копия есть
            if name not in existing and tmp_name in existing:
                tmp = await asyncio.to_thread(client.get_collection, tmp_name)
                await asyncio.to_thread(tmp.modify, name=name)
                logger.warning(f"Коллекция «{name}» восстановлена из копии перестройки")
                return await asyncio.to_thread(client.get_collection, name)

            col = await asyncio.to_thread(client.get_collection, name)
            md = col.metadata if isinstance(col.metadata, dict) else {}
            if md.get("embedding_model") == target:  # другой процесс уже перестроил
                return col
            old_model = md.get("embedding_model") or LEGACY_EMBEDDING_MODEL
            if tmp_name in existing:  # недоделанная прошлая попытка
                await asyncio.to_thread(client.delete_collection, tmp_name)

            ids: List[str] = []
            docs: List[str] = []
            metas: List[Dict[str, Any]] = []
            offset = 0
            while True:
                part = await asyncio.to_thread(
                    col.get, include=["documents", "metadatas"], limit=_REINDEX_BATCH, offset=offset
                )
                got = part.get("ids") or []
                if not got:
                    break
                ids += got
                docs += part.get("documents") or [""] * len(got)
                metas += part.get("metadatas") or [{}] * len(got)
                offset += len(got)

            logger.warning(
                f"Перестройка индекса «{name}»: {old_model} → {target}, записей: {len(ids)}"
                + (" (может занять минуты)" if len(ids) > 50 else "")
            )
            started = time.monotonic()
            vectors = await asyncio.to_thread(self._embed_many, docs, "passage") if ids else []

            new_md = {**md, "description": md.get("description", description), "embedding_model": target}
            tmp = await asyncio.to_thread(client.create_collection, name=tmp_name, metadata=new_md)
            for i in range(0, len(ids), _REINDEX_BATCH):
                sl = slice(i, i + _REINDEX_BATCH)
                await asyncio.to_thread(
                    tmp.add, ids=ids[sl], embeddings=vectors[sl], documents=docs[sl],
                    metadatas=[m or None for m in metas[sl]],
                )
            await asyncio.to_thread(client.delete_collection, name)
            await asyncio.to_thread(tmp.modify, name=name)
            logger.warning(
                f"Индекс «{name}» перестроен за {time.monotonic() - started:.0f} с, записей: {len(ids)}"
            )
            return await asyncio.to_thread(client.get_collection, name)

    def _generate_id(self, content: str, user_id: Optional[int] = None) -> str:
        """Generate unique ID for document"""
        data = f"{user_id}:{content}" if user_id else content
        return hashlib.md5(data.encode()).hexdigest()

    def _embed_text(self, text: str, kind: str = "passage") -> List[float]:
        """Generate embedding for text using fastembed.

        kind="query" — поисковый запрос, "passage" — сохраняемый документ (важно для E5-префиксов).
        """
        model = self._acquire_model()
        try:
            prefix = _prefixes_for(_model_name())[0 if kind == "query" else 1]
            embeddings = list(model.embed([prefix + text]))
        finally:
            self._release_model()
        return embeddings[0].tolist()

    def _embed_many(self, texts: List[str], kind: str = "passage") -> List[List[float]]:
        """Эмбеддинги пачками (для перестройки индекса); модель держится занятой на всё время."""
        model = self._acquire_model()
        try:
            prefix = _prefixes_for(_model_name())[0 if kind == "query" else 1]
            out: List[List[float]] = []
            step = 16
            for i in range(0, len(texts), step):
                batch = [prefix + t for t in texts[i:i + step]]
                out.extend(vec.tolist() for vec in model.embed(batch))
                if len(texts) > step and (i // step) % 10 == 9:
                    logger.info(f"Перестройка индекса: {min(i + step, len(texts))}/{len(texts)}")
            return out
        finally:
            self._release_model()

    async def add_memory(
        self,
        content: str,
        user_id: int,
        memory_type: str = "note",
        metadata: Optional[Dict[str, Any]] = None
    ) -> str:
        """Add a memory to vector store"""
        await self.initialize()

        try:
            doc_id = self._generate_id(content, user_id)
            embedding = await asyncio.to_thread(self._embed_text, content)

            doc_metadata = _sanitize_metadata({
                "user_id": user_id,
                "memory_type": memory_type,
                "created_at": datetime.now().isoformat(),
                **(metadata or {})
            })

            await asyncio.to_thread(
                self.collections['memories'].add,
                ids=[doc_id],
                embeddings=[embedding],
                documents=[content],
                metadatas=[doc_metadata]
            )

            logger.debug(f"Added memory {doc_id} for user {user_id}")
            return doc_id

        except Exception as e:
            logger.error(f"Failed to add memory: {e}")
            raise

    async def search_memories(
        self,
        query: str,
        user_id: int,
        n_results: int = 5,
        memory_type: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Search for relevant memories"""
        await self.initialize()

        try:
            query_embedding = await asyncio.to_thread(self._embed_text, query, "query")

            where_filter = {"user_id": user_id}
            if memory_type:
                where_filter["memory_type"] = memory_type

            results = await asyncio.to_thread(
                self.collections['memories'].query,
                query_embeddings=[query_embedding],
                n_results=n_results,
                where=where_filter,
                include=["documents", "metadatas", "distances"]
            )

            memories = []
            if results['ids'] and results['ids'][0]:
                for i, doc_id in enumerate(results['ids'][0]):
                    memories.append({
                        'id': doc_id,
                        'content': results['documents'][0][i],
                        'metadata': results['metadatas'][0][i],
                        'distance': results['distances'][0][i]
                    })

            return memories

        except Exception as e:
            logger.error(f"Failed to search memories: {e}")
            return []

    async def add_skill_documentation(
        self,
        skill_name: str,
        description: str,
        code: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> str:
        """Add skill documentation to vector store"""
        await self.initialize()

        try:
            content = f"Skill: {skill_name}\nDescription: {description}"
            if code:
                content += f"\nCode:\n{code[:2000]}"

            doc_id = self._generate_id(skill_name)
            embedding = await asyncio.to_thread(self._embed_text, content)

            doc_metadata = _sanitize_metadata({
                "skill_name": skill_name,
                "has_code": bool(code),
                "created_at": datetime.now().isoformat(),
                **(metadata or {})
            })

            await asyncio.to_thread(
                self.collections['skills'].add,
                ids=[doc_id],
                embeddings=[embedding],
                documents=[content],
                metadatas=[doc_metadata]
            )

            return doc_id

        except Exception as e:
            logger.error(f"Failed to add skill documentation: {e}")
            raise

    async def search_skills(
        self,
        query: str,
        n_results: int = 5
    ) -> List[Dict[str, Any]]:
        """Search for relevant skills"""
        await self.initialize()

        try:
            query_embedding = await asyncio.to_thread(self._embed_text, query, "query")

            results = await asyncio.to_thread(
                self.collections['skills'].query,
                query_embeddings=[query_embedding],
                n_results=n_results,
                include=["documents", "metadatas", "distances"]
            )

            skills = []
            if results['ids'] and results['ids'][0]:
                for i, doc_id in enumerate(results['ids'][0]):
                    skills.append({
                        'id': doc_id,
                        'content': results['documents'][0][i],
                        'metadata': results['metadatas'][0][i],
                        'distance': results['distances'][0][i]
                    })

            return skills

        except Exception as e:
            logger.error(f"Failed to search skills: {e}")
            return []

    async def add_conversation_fragment(
        self,
        content: str,
        user_id: int,
        importance: float = 0.5,
        metadata: Optional[Dict[str, Any]] = None
    ) -> str:
        """Add important conversation fragment"""
        await self.initialize()

        try:
            doc_id = self._generate_id(content, user_id)
            embedding = await asyncio.to_thread(self._embed_text, content)

            doc_metadata = _sanitize_metadata({
                "user_id": user_id,
                "importance": importance,
                "created_at": datetime.now().isoformat(),
                **(metadata or {})
            })

            await asyncio.to_thread(
                self.collections['conversations'].add,
                ids=[doc_id],
                embeddings=[embedding],
                documents=[content],
                metadatas=[doc_metadata]
            )

            return doc_id

        except Exception as e:
            logger.error(f"Failed to add conversation fragment: {e}")
            raise

    async def get_relevant_context(
        self,
        query: str,
        user_id: int,
        n_memories: int = 3,
        n_conversations: int = 2
    ) -> Dict[str, List[Dict[str, Any]]]:
        """Get all relevant context for a query"""
        await self.initialize()

        memories = await self.search_memories(query, user_id, n_results=n_memories)
        conversations = await self.search_conversations(query, user_id, n_results=n_conversations)

        return {
            'memories': memories,
            'conversations': conversations
        }

    async def search_conversations(
        self,
        query: str,
        user_id: int,
        n_results: int = 5
    ) -> List[Dict[str, Any]]:
        """Search conversation fragments"""
        await self.initialize()

        try:
            query_embedding = await asyncio.to_thread(self._embed_text, query, "query")

            results = await asyncio.to_thread(
                self.collections['conversations'].query,
                query_embeddings=[query_embedding],
                n_results=n_results,
                where={"user_id": user_id},
                include=["documents", "metadatas", "distances"]
            )

            conversations = []
            if results['ids'] and results['ids'][0]:
                for i, doc_id in enumerate(results['ids'][0]):
                    conversations.append({
                        'id': doc_id,
                        'content': results['documents'][0][i],
                        'metadata': results['metadatas'][0][i],
                        'distance': results['distances'][0][i]
                    })

            return conversations

        except Exception as e:
            logger.error(f"Failed to search conversations: {e}")
            return []

    # ------------------------------------------------------------------
    # Showcase corpus management
    # ------------------------------------------------------------------

    @staticmethod
    def _split_units(text: str, limit: int) -> List[str]:
        """Текст → «единицы» не длиннее limit: строки, предложения, затем группы слов.

        Режет по границам слов; посимвольно — только одно «слово» длиннее limit (например, URL).
        """
        units: List[str] = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            for sentence in re.split(r'(?<=[.!?…])\s+', line):
                sentence = sentence.strip()
                if not sentence:
                    continue
                if len(sentence) <= limit:
                    units.append(sentence)
                    continue
                piece = ""
                for word in sentence.split():
                    if len(word) > limit:  # «слово» длиннее лимита — режем посимвольно
                        if piece:
                            units.append(piece)
                            piece = ""
                        units.extend(word[i:i + limit] for i in range(0, len(word), limit))
                    elif piece and len(piece) + 1 + len(word) > limit:
                        units.append(piece)
                        piece = word
                    else:
                        piece = f"{piece} {word}" if piece else word
                if piece:
                    units.append(piece)
        return units

    @staticmethod
    def _chunk_text(
        text: str,
        chunk_size: int,
        overlap: int
    ) -> List[str]:
        """Split text into overlapping chunks on line/sentence/word boundaries.

        Every returned chunk is guaranteed to be <= chunk_size characters. Текст режется по
        строкам (заголовки и пункты списков без точки не склеиваются в одно «предложение»),
        затем по предложениям, затем по границам слов. overlap — максимум символов,
        повторяемых из конца предыдущего чанка в начале следующего.
        """
        text = text.strip()
        if not text or chunk_size <= 0:
            return []

        chunks: List[str] = []
        current = ""

        for unit in VectorMemory._split_units(text, chunk_size):
            if current and len(current) + 1 + len(unit) > chunk_size:
                chunks.append(current.strip())
                # Начало следующего чанка — наложение из конца текущего (в пределах overlap
                # символов и так, чтобы чанк с новой единицей не превысил chunk_size).
                tail: List[str] = []
                used = 0
                room = min(max(0, overlap), chunk_size - len(unit) - 1)
                for w in reversed(current.split()):
                    if used + len(w) + (1 if tail else 0) > room:
                        break
                    tail.insert(0, w)
                    used += len(w) + (1 if len(tail) > 1 else 0)
                current = (" ".join(tail) + " " + unit) if tail else unit
            else:
                current = f"{current} {unit}" if current else unit

        if current.strip():
            chunks.append(current.strip())

        return [c for c in chunks if c]

    async def ensure_corpus(self, corpus: str = DEFAULT_CORPUS):
        """Коллекция корпуса витрины (создаётся при первом обращении)."""
        _check_corpus_name(corpus)
        await self.initialize()
        col = self.collections.get(corpus)
        if col is None:
            col = await self._open_collection(corpus, f"Showcase knowledge corpus: {corpus}")
            self.collections[corpus] = col
        return col

    async def add_corpus_document(
        self,
        source_id: str,
        source_name: str,
        content: str,
        source_url: Optional[str] = None,
        chunk_size: Optional[int] = None,
        chunk_overlap: Optional[int] = None,
        corpus: str = DEFAULT_CORPUS,
        content_hash: Optional[str] = None,
    ) -> str:
        """Add a document to the showcase knowledge corpus.

        Splits content into overlapping chunks and stores each chunk as a
        separate ChromaDB document with shared source metadata.
        Returns the source_id.
        """
        collection = await self.ensure_corpus(corpus)

        chunk_size = chunk_size or settings.CORPUS_CHUNK_SIZE
        if chunk_overlap is None:  # явный 0 — «без наложения», а не «взять из настроек»
            chunk_overlap = settings.CORPUS_CHUNK_OVERLAP

        chunks = self._chunk_text(content, chunk_size, chunk_overlap)
        if not chunks:
            raise ValueError("Document produced no chunks")

        try:
            ids = []
            embeddings = []
            documents = []
            metadatas = []

            now = datetime.now().isoformat()
            # все чанки документа — одним переходом в поток (а не по одному)
            embeddings = await asyncio.to_thread(lambda: [self._embed_text(c) for c in chunks])
            for idx, chunk in enumerate(chunks):
                doc_id = hashlib.md5(
                    f"{source_id}:{idx}:{chunk[:80]}".encode()
                ).hexdigest()

                ids.append(doc_id)
                documents.append(chunk)
                metadatas.append(_sanitize_metadata({
                    "source_id": source_id,
                    "source_name": source_name,
                    "source_url": source_url or "",
                    "chunk_index": idx,
                    "chunk_count": len(chunks),
                    "added_at": now,
                    "content_hash": content_hash or "",
                }))

            await asyncio.to_thread(
                collection.add,
                ids=ids,
                embeddings=embeddings,
                documents=documents,
                metadatas=metadatas
            )

            logger.info(
                f"Added corpus document {source_id}: {len(chunks)} chunks"
            )
            return source_id

        except Exception as e:
            logger.error(f"Failed to add corpus document {source_id}: {e}")
            raise

    async def search_corpus(
        self,
        query: str,
        n_results: Optional[int] = None,
        corpus: str = DEFAULT_CORPUS,
    ) -> List[Dict[str, Any]]:
        """Semantic search in the showcase knowledge corpus (одной витрины)."""
        collection = await self.ensure_corpus(corpus)

        n_results = n_results or settings.CORPUS_TOP_K

        try:
            query_embedding = await asyncio.to_thread(self._embed_text, query, "query")

            results = await asyncio.to_thread(
                collection.query,
                query_embeddings=[query_embedding],
                n_results=n_results,
                include=["documents", "metadatas", "distances"]
            )

            corpus_docs = []
            if results['ids'] and results['ids'][0]:
                for i, doc_id in enumerate(results['ids'][0]):
                    corpus_docs.append({
                        'id': doc_id,
                        'content': results['documents'][0][i],
                        'metadata': results['metadatas'][0][i],
                        'distance': results['distances'][0][i]
                    })

            return corpus_docs

        except Exception as e:
            logger.error(f"Failed to search corpus: {e}")
            return []

    async def delete_corpus_by_name(
        self, source_name: str, corpus: str = DEFAULT_CORPUS, keep_source_id: Optional[str] = None
    ) -> int:
        """Удалить все чанки документов с данным именем файла (кроме keep_source_id).

        Возвращает число удалённых чанков. Нужно, чтобы повторная загрузка файла заменяла
        прежнюю версию, а не дублировала её.
        """
        collection = await self.ensure_corpus(corpus)
        where: Dict[str, Any] = {"source_name": source_name}
        if keep_source_id:
            where = {"$and": [{"source_name": source_name}, {"source_id": {"$ne": keep_source_id}}]}
        try:
            found = await asyncio.to_thread(collection.get, where=where, include=[])
            ids = found.get("ids") or []
            if ids:
                await asyncio.to_thread(collection.delete, ids=ids)
            return len(ids)
        except Exception as e:
            logger.error(f"Failed to delete corpus documents named {source_name!r}: {e}")
            return 0

    async def get_corpus_index(self, corpus: str = DEFAULT_CORPUS, limit: int = 50000) -> Dict[str, Dict[str, Any]]:
        """Имя документа -> {chunks, source_ids, hashes}: для сверки корпуса с архивом."""
        collection = await self.ensure_corpus(corpus)
        index: Dict[str, Dict[str, Any]] = {}
        try:
            offset = 0
            while offset < limit:
                part = await asyncio.to_thread(
                    collection.get, include=["metadatas"], limit=min(1000, limit - offset), offset=offset
                )
                ids = part.get("ids")
                if not isinstance(ids, list) or not ids:
                    break
                for meta in part.get("metadatas") or []:
                    meta = meta or {}
                    name = meta.get("source_name") or "без названия"
                    entry = index.setdefault(name, {"chunks": 0, "source_ids": set(), "hashes": set()})
                    entry["chunks"] += 1
                    entry["source_ids"].add(meta.get("source_id", ""))
                    entry["hashes"].add(meta.get("content_hash", ""))
                offset += len(ids)
        except Exception as e:
            logger.error(f"Failed to index corpus: {e}")
        return index

    async def get_corpus_sources(self, corpus: str = DEFAULT_CORPUS, limit: int = 5000) -> List[Dict[str, Any]]:
        """Документы корпуса: имя, число чанков, средняя длина чанка (до limit чанков)."""
        collection = await self.ensure_corpus(corpus)
        stats: Dict[str, Dict[str, int]] = {}
        try:
            offset = 0
            while offset < limit:
                part = await asyncio.to_thread(
                    collection.get, include=["documents", "metadatas"], limit=min(500, limit - offset), offset=offset
                )
                ids = part.get("ids")
                if not isinstance(ids, list) or not ids:
                    break
                for doc, meta in zip(part.get("documents") or [], part.get("metadatas") or []):
                    name = (meta or {}).get("source_name") or "без названия"
                    st = stats.setdefault(name, {"chunks": 0, "chars": 0})
                    st["chunks"] += 1
                    st["chars"] += len(doc or "")
                offset += len(ids)
        except Exception as e:
            logger.error(f"Failed to list corpus sources: {e}")
        return sorted(
            ({"name": n, "chunks": v["chunks"], "avg_chars": v["chars"] // max(1, v["chunks"])}
             for n, v in stats.items()),
            key=lambda x: (-x["chunks"], x["name"]),
        )

    async def delete_corpus_document(
        self, source_id: str, corpus: str = DEFAULT_CORPUS
    ) -> bool:
        """Delete all chunks of a corpus document by source_id."""
        collection = await self.ensure_corpus(corpus)

        try:
            await asyncio.to_thread(
                collection.delete,
                where={"source_id": source_id}
            )
            logger.info(f"Deleted corpus document {source_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to delete corpus document {source_id}: {e}")
            return False

    async def get_corpus_stats(self, corpus: str = DEFAULT_CORPUS) -> Dict[str, int]:
        """Get corpus statistics (document count and chunk count)."""
        collection = await self.ensure_corpus(corpus)

        try:
            count = collection.count()
            return {
                "documents": count,  # Chroma count = chunks; docs ≈ chunks
                "chunks": count
            }
        except Exception as e:
            logger.error(f"Failed to get corpus stats: {e}")
            return {"documents": 0, "chunks": 0}

    async def delete_memory(self, doc_id: str) -> bool:
        """Delete a memory by ID"""
        await self.initialize()

        try:
            await asyncio.to_thread(self.collections['memories'].delete, ids=[doc_id])
            return True
        except Exception as e:
            logger.error(f"Failed to delete memory: {e}")
            return False

    async def get_stats(self) -> Dict[str, int]:
        """Get vector store statistics"""
        await self.initialize()

        try:
            stats = {}
            for name, collection in self.collections.items():
                stats[name] = await asyncio.to_thread(collection.count)
            return stats
        except Exception as e:
            logger.error(f"Failed to get stats: {e}")
            return {}


# Global vector memory instance
vector_memory = VectorMemory()
