"""
Vector Memory System for RAG (Retrieval Augmented Generation)
Uses ChromaDB for vector storage and fastembed for embeddings (multilingual)
"""
import asyncio
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

logger = logging.getLogger(__name__)

EMBEDDING_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"


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
                logger.info(f"Loading embedding model: {EMBEDDING_MODEL}")
                self.embedding_model = TextEmbedding(model_name=EMBEDDING_MODEL)
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

    def embed_texts_sync(self, texts: List[str]) -> List[List[float]]:
        """Пакетные эмбеддинги общей моделью (для навыков; синхронно)."""
        model = self._acquire_model()
        try:
            return [vec.tolist() for vec in model.embed(texts)]
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

            self.collections['memories'] = await asyncio.to_thread(
                self.chroma_client.get_or_create_collection,
                name="memories",
                metadata={"description": "User memories and notes"}
            )
            self.collections['skills'] = await asyncio.to_thread(
                self.chroma_client.get_or_create_collection,
                name="skills",
                metadata={"description": "Skills documentation and code"}
            )
            self.collections['conversations'] = await asyncio.to_thread(
                self.chroma_client.get_or_create_collection,
                name="conversations",
                metadata={"description": "Important conversation fragments"}
            )
            self.collections['corpus'] = await asyncio.to_thread(
                self.chroma_client.get_or_create_collection,
                name="corpus",
                metadata={"description": "Showcase support knowledge corpus"}
            )

            self._initialized = True
            logger.info("Vector memory initialized successfully")

        except Exception as e:
            logger.error(f"Failed to initialize vector memory: {e}")
            raise

    def _generate_id(self, content: str, user_id: Optional[int] = None) -> str:
        """Generate unique ID for document"""
        data = f"{user_id}:{content}" if user_id else content
        return hashlib.md5(data.encode()).hexdigest()

    def _embed_text(self, text: str) -> List[float]:
        """Generate embedding for text using fastembed"""
        model = self._acquire_model()
        try:
            embeddings = list(model.embed([text]))
        finally:
            self._release_model()
        return embeddings[0].tolist()

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
            query_embedding = await asyncio.to_thread(self._embed_text, query)

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
            query_embedding = await asyncio.to_thread(self._embed_text, query)

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
            query_embedding = await asyncio.to_thread(self._embed_text, query)

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
    def _chunk_text(
        text: str,
        chunk_size: int,
        overlap: int
    ) -> List[str]:
        """Split text into overlapping chunks on sentence boundaries.

        A single sentence longer than chunk_size is hard-split by characters so
        every returned chunk is guaranteed to be <= chunk_size characters.
        """
        text = text.strip()
        if not text or chunk_size <= 0:
            return []

        sentences = re.split(r'(?<=[.!?…])\s+', text)
        chunks: List[str] = []
        current = ""

        for sentence in sentences:
            sentence = sentence.strip()
            if not sentence:
                continue

            # Одно предложение длиннее лимита — режем посимвольно.
            if len(sentence) > chunk_size:
                if current:
                    chunks.append(current.strip())
                    current = ""
                for i in range(0, len(sentence), chunk_size):
                    chunks.append(sentence[i:i + chunk_size])
                continue

            if current and len(current) + 1 + len(sentence) > chunk_size:
                chunks.append(current.strip())
                # Начало следующего чанка — наложение из конца текущего.
                words = current.split()
                keep = max(0, overlap // 8)
                if keep:
                    current = " ".join(words[-keep:]) + " " + sentence
                else:
                    current = sentence
            else:
                current = (current + " " + sentence).strip() if current else sentence

        if current.strip():
            chunks.append(current.strip())

        return [c for c in chunks if c]

    async def add_corpus_document(
        self,
        source_id: str,
        source_name: str,
        content: str,
        source_url: Optional[str] = None,
        chunk_size: Optional[int] = None,
        chunk_overlap: Optional[int] = None
    ) -> str:
        """Add a document to the showcase knowledge corpus.

        Splits content into overlapping chunks and stores each chunk as a
        separate ChromaDB document with shared source metadata.
        Returns the source_id.
        """
        await self.initialize()

        chunk_size = chunk_size or settings.CORPUS_CHUNK_SIZE
        chunk_overlap = chunk_overlap or settings.CORPUS_CHUNK_OVERLAP

        chunks = self._chunk_text(content, chunk_size, chunk_overlap)
        if not chunks:
            raise ValueError("Document produced no chunks")

        try:
            ids = []
            embeddings = []
            documents = []
            metadatas = []

            now = datetime.now().isoformat()
            for idx, chunk in enumerate(chunks):
                doc_id = hashlib.md5(
                    f"{source_id}:{idx}:{chunk[:80]}".encode()
                ).hexdigest()
                embedding = await asyncio.to_thread(self._embed_text, chunk)

                ids.append(doc_id)
                embeddings.append(embedding)
                documents.append(chunk)
                metadatas.append(_sanitize_metadata({
                    "source_id": source_id,
                    "source_name": source_name,
                    "source_url": source_url or "",
                    "chunk_index": idx,
                    "chunk_count": len(chunks),
                    "added_at": now,
                }))

            await asyncio.to_thread(
                self.collections['corpus'].add,
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
        n_results: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """Semantic search in the showcase knowledge corpus."""
        await self.initialize()

        n_results = n_results or settings.CORPUS_TOP_K

        try:
            query_embedding = await asyncio.to_thread(self._embed_text, query)

            results = await asyncio.to_thread(
                self.collections['corpus'].query,
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

    async def delete_corpus_document(self, source_id: str) -> bool:
        """Delete all chunks of a corpus document by source_id."""
        await self.initialize()

        try:
            await asyncio.to_thread(
                self.collections['corpus'].delete,
                where={"source_id": source_id}
            )
            logger.info(f"Deleted corpus document {source_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to delete corpus document {source_id}: {e}")
            return False

    async def get_corpus_stats(self) -> Dict[str, int]:
        """Get corpus statistics (document count and chunk count)."""
        await self.initialize()

        try:
            count = self.collections['corpus'].count()
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
