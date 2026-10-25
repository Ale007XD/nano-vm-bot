"""
Vector Memory System for RAG (Retrieval Augmented Generation)
Uses ChromaDB for vector storage and fastembed for embeddings (multilingual)
"""
import asyncio
import os
import hashlib
from typing import List, Dict, Optional, Any
from datetime import datetime
import chromadb
from chromadb.config import Settings as ChromaSettings
from fastembed import TextEmbedding

from app.core.config import settings
import logging

logger = logging.getLogger(__name__)

EMBEDDING_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"


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

    async def initialize(self):
        """Initialize vector memory components"""
        if self._initialized:
            return

        try:
            logger.info(f"Loading embedding model: {EMBEDDING_MODEL}")
            # fastembed и ChromaDB — синхронные: уводим из event loop
            self.embedding_model = await asyncio.to_thread(
                TextEmbedding, model_name=EMBEDDING_MODEL
            )
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
        if not self.embedding_model:
            raise RuntimeError("Embedding model not initialized")
        embeddings = list(self.embedding_model.embed([text]))
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
