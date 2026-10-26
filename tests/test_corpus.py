"""
Showcase corpus management: chunking, add/search/delete/stats (app/core/memory.py).
ChromaDB mocked — no external service needed.
"""
import sys
from unittest.mock import MagicMock

import pytest


@pytest.fixture(autouse=True)
def mock_chromadb():
    """Stub chromadb before importing app.core.memory."""
    mock_chroma = MagicMock()
    mock_chroma.config = MagicMock()
    mock_chroma.config.Settings = MagicMock()
    sys.modules["chromadb"] = mock_chroma
    sys.modules["chromadb.config"] = mock_chroma.config

    mock_fastembed = MagicMock()
    mock_fastembed.TextEmbedding = MagicMock()
    sys.modules["fastembed"] = mock_fastembed

    yield


class TestChunkText:
    @pytest.fixture
    def memory(self):
        from app.core.memory import VectorMemory
        return VectorMemory()

    def test_plain_text(self, memory):
        # Два коротких предложения помещаются в один чанк.
        chunks = memory._chunk_text("Первое предложение. Второе предложение.", 100, 20)
        assert chunks == ["Первое предложение. Второе предложение."]

    def test_empty_text(self, memory):
        assert memory._chunk_text("", 100, 20) == []
        assert memory._chunk_text("   ", 100, 20) == []

    def test_chunk_size_respected(self, memory):
        # Одно длинное предложение без препинания — жёсткий символьный разрез.
        long_text = "слово " * 200
        chunks = memory._chunk_text(long_text, 100, 0)
        assert len(chunks) > 1
        for c in chunks:
            assert len(c) <= 100

    def test_overlap(self, memory):
        # Предложения с точкой — проверяем наложение между чанками.
        text = ". ".join(f"слово{i}" for i in range(60)) + "."
        chunks = memory._chunk_text(text, 100, 20)
        assert len(chunks) > 1
        assert all(len(c) <= 100 for c in chunks)
        # Начало второго чанка пересекается с концом первого (последние слова).
        first_words = chunks[0].split()[-5:]
        second_start = chunks[1].split()[:8]
        assert set(first_words) & set(second_start)


class TestCorpusCRUD:
    @pytest.fixture
    async def memory(self):
        from app.core.memory import VectorMemory
        vm = VectorMemory()
        vm._initialized = True
        coll = MagicMock()
        coll.count.return_value = 0
        vm.collections = {"corpus": coll}
        return vm, coll

    @pytest.mark.asyncio
    async def test_add_and_search(self, memory):
        vm, coll = memory
        # Эмбеддинг-модель подменяется моком (ChromaDB тоже мок).
        emb = MagicMock()
        emb.tolist.return_value = [0.1] * 384
        vm.embedding_model = MagicMock()
        vm.embedding_model.embed.return_value = [emb]

        coll.query.return_value = {
            "ids": [["doc1:0"]],
            "documents": [["фрагмент про возврат товара"]],
            "metadatas": [[{"source_id": "doc1", "source_name": "Возвраты", "chunk_index": 0}]],
            "distances": [[0.1]],
        }

        source_id = await vm.add_corpus_document(
            source_id="doc1",
            source_name="Возвраты",
            content="Возврат товара возможен в течение 14 дней с момента покупки.",
            chunk_size=20,
            chunk_overlap=0,
        )
        assert source_id == "doc1"
        coll.add.assert_called_once()
        add_kwargs = coll.add.call_args.kwargs
        assert len(add_kwargs["documents"]) >= 1

        found = await vm.search_corpus("вернуть товар через 14 дней")
        assert len(found) == 1
        assert found[0]["content"] == "фрагмент про возврат товара"
        assert found[0]["metadata"]["source_id"] == "doc1"

    @pytest.mark.asyncio
    async def test_delete_corpus_document(self, memory):
        vm, coll = memory
        result = await vm.delete_corpus_document("doc1")
        assert result is True
        coll.delete.assert_called_once_with(where={"source_id": "doc1"})

    @pytest.mark.asyncio
    async def test_get_corpus_stats(self, memory):
        vm, coll = memory
        coll.count.return_value = 42
        stats = await vm.get_corpus_stats()
        assert stats == {"documents": 42, "chunks": 42}

    @pytest.mark.asyncio
    async def test_search_empty_corpus(self, memory):
        vm, coll = memory
        coll.query.return_value = {"ids": [[]], "documents": [[]], "metadatas": [[]], "distances": [[]]}
        found = await vm.search_corpus("что угодно")
        assert found == []
