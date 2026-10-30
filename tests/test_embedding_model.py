"""Модель эмбеддингов из настроек: префиксы E5, кэш, перестройка индекса при смене модели.

ChromaDB и fastembed замоканы (conftest): проверяется логика перестройки на правдоподобной
in-memory-имитации; поведение настоящего Chroma проверено отдельным прогоном.
"""
import sys
from unittest.mock import MagicMock

import pytest

mem = sys.modules["app.core.memory"]
OLD = mem.LEGACY_EMBEDDING_MODEL
E5 = "intfloat/multilingual-e5-large"


class FCol:
    def __init__(self, store, name, metadata):
        self.store, self.name, self.metadata = store, name, dict(metadata or {})
        self.rows = {}  # id -> (vec, doc, meta)

    def count(self):
        return len(self.rows)

    def modify(self, metadata=None, name=None):
        if metadata is not None:
            self.metadata = dict(metadata)
        if name is not None:
            self.store.cols.pop(self.name, None)
            self.name = name
            self.store.cols[name] = self

    def add(self, ids, embeddings, documents, metadatas):
        dims = {len(r[0]) for r in self.rows.values()} | {len(e) for e in embeddings}
        assert len(dims) <= 1, "разная размерность"
        for i, e, d, m in zip(ids, embeddings, documents, metadatas):
            self.rows[i] = (e, d, m)

    def get(self, include=None, limit=None, offset=0):
        items = list(self.rows.items())[offset:offset + (limit or len(self.rows))]
        return {"ids": [i for i, _ in items], "documents": [r[1] for _, r in items],
                "metadatas": [r[2] for _, r in items]}


class FChroma:
    def __init__(self):
        self.cols = {}
        self.fail_add_for = None

    def get_or_create_collection(self, name, metadata=None):
        return self.cols.setdefault(name, FCol(self, name, metadata))

    def get_collection(self, name):
        return self.cols[name]

    def create_collection(self, name, metadata=None):
        assert name not in self.cols
        col = FCol(self, name, metadata)
        self.cols[name] = col
        return col

    def delete_collection(self, name):
        del self.cols[name]

    def list_collections(self):
        return list(self.cols)


@pytest.fixture
def vm(monkeypatch, tmp_path):
    st = sys.modules["app.core.config"].settings
    monkeypatch.setattr(st, "VECTOR_STORE_PATH", str(tmp_path / "vs"))
    monkeypatch.setattr(st, "EMBEDDING_MODEL", OLD, raising=False)
    v = mem.VectorMemory()
    v.chroma_client = FChroma()
    seen = {"calls": []}

    def embed_many(self, texts, kind="passage"):
        seen["calls"].append((mem._model_name(), len(texts), kind))
        dim = 4 if mem._model_name() == E5 else 2
        return [[float(len(t))] * dim for t in texts]

    monkeypatch.setattr(mem.VectorMemory, "_embed_many", embed_many)
    v._seen = seen
    return v, st


def _legacy_collection(vm, name="corpus", n=3):
    col = vm.chroma_client.get_or_create_collection(name, {"description": "d"})
    for i in range(n):
        col.add([f"id{i}"], [[0.1, 0.2]], [f"текст {i}"], [{"source_id": f"s{i}"}])
    return col


class TestPrefixes:
    def test_e5_prefixes_only_for_e5(self):
        assert mem._prefixes_for(E5) == ("query: ", "passage: ")
        assert mem._prefixes_for("intfloat/e5-base") == ("query: ", "passage: ")
        assert mem._prefixes_for(OLD) == ("", "")

    def test_query_and_passage_get_different_prefix(self, monkeypatch):
        st = sys.modules["app.core.config"].settings
        monkeypatch.setattr(st, "EMBEDDING_MODEL", E5, raising=False)
        sent = []

        class M:
            def embed(self, texts):
                sent.extend(texts)
                for _ in texts:
                    v = MagicMock()
                    v.tolist.return_value = [1.0]
                    yield v

        v = mem.VectorMemory()
        v.embedding_model = M()
        v._embed_text("как оформить возврат", "query")
        v._embed_text("Возврат оформляется в течение 14 дней")
        assert sent == ["query: как оформить возврат", "passage: Возврат оформляется в течение 14 дней"]


class TestModelName:
    def test_setting_used_not_hardcoded(self, monkeypatch):
        st = sys.modules["app.core.config"].settings
        monkeypatch.setattr(st, "EMBEDDING_MODEL", E5, raising=False)
        assert mem._model_name() == E5

    def test_unusable_value_falls_back(self, monkeypatch):
        st = sys.modules["app.core.config"].settings
        monkeypatch.setattr(st, "EMBEDDING_MODEL", "  ", raising=False)
        assert mem._model_name() == OLD

    def test_dead_english_default_is_ignored(self, monkeypatch):
        st = sys.modules["app.core.config"].settings
        monkeypatch.setattr(st, "EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2", raising=False)
        assert mem._model_name() == OLD

    def test_cache_dir_passed_to_fastembed(self, monkeypatch, tmp_path):
        st = sys.modules["app.core.config"].settings
        monkeypatch.setattr(st, "EMBEDDING_CACHE_DIR", str(tmp_path / "models"), raising=False)
        monkeypatch.setattr(st, "EMBEDDING_MODEL", E5, raising=False)
        got = {}
        monkeypatch.setattr(mem, "TextEmbedding", lambda model_name, **kw: got.update(name=model_name, **kw) or MagicMock())
        mem.VectorMemory()._acquire_model()
        assert got == {"name": E5, "cache_dir": str(tmp_path / "models")}
        assert (tmp_path / "models").is_dir()


class TestReindex:
    @pytest.mark.asyncio
    async def test_legacy_collection_same_model_only_stamped(self, vm):
        v, _ = vm
        _legacy_collection(v)
        col = await v._open_collection("corpus", "d")
        assert col.metadata["embedding_model"] == OLD
        assert v._seen["calls"] == []                      # ничего не пересчитывалось

    @pytest.mark.asyncio
    async def test_model_change_reembeds_from_stored_texts(self, vm):
        v, st = vm
        _legacy_collection(v)
        st.EMBEDDING_MODEL = E5
        col = await v._open_collection("corpus", "d")
        assert col.name == "corpus" and col.metadata["embedding_model"] == E5
        assert col.metadata["description"] == "d"
        assert {r[1] for r in col.rows.values()} == {"текст 0", "текст 1", "текст 2"}   # тексты целы
        assert all(len(r[0]) == 4 for r in col.rows.values())                          # новые векторы
        assert {r[2]["source_id"] for r in col.rows.values()} == {"s0", "s1", "s2"}    # метаданные целы
        assert v._seen["calls"] == [(E5, 3, "passage")]
        assert list(v.chroma_client.cols) == ["corpus"]                                # временной нет

    @pytest.mark.asyncio
    async def test_second_open_is_noop(self, vm):
        v, st = vm
        _legacy_collection(v)
        st.EMBEDDING_MODEL = E5
        await v._open_collection("corpus", "d")
        await v._open_collection("corpus", "d")
        assert len(v._seen["calls"]) == 1

    @pytest.mark.asyncio
    async def test_new_empty_collection_just_stamped(self, vm):
        v, st = vm
        st.EMBEDDING_MODEL = E5
        col = await v._open_collection("memories", "m")
        assert col.metadata["embedding_model"] == E5 and v._seen["calls"] == []

    @pytest.mark.asyncio
    async def test_embedding_failure_keeps_old_data(self, vm, monkeypatch):
        v, st = vm
        old = _legacy_collection(v)
        st.EMBEDDING_MODEL = E5

        def boom(self, texts, kind="passage"):
            raise RuntimeError("HF недоступен")

        monkeypatch.setattr(mem.VectorMemory, "_embed_many", boom)
        col = await v._open_collection("corpus", "d")        # не падает
        assert col is old and len(old.rows) == 3 and "embedding_model" not in old.metadata
        assert list(v.chroma_client.cols) == ["corpus"]

    @pytest.mark.asyncio
    async def test_recovers_after_interrupted_swap(self, vm):
        v, st = vm
        st.EMBEDDING_MODEL = E5
        done = v.chroma_client.create_collection("corpus__reindex", {"embedding_model": E5})
        done.add(["a"], [[1.0] * 4], ["готовая копия"], [{}])
        # старая коллекция уже удалена, переименование не случилось
        col = await v._reindex_collection("corpus", "d", E5)
        assert col.name == "corpus" and col.rows["a"][1] == "готовая копия"
        assert v._seen["calls"] == []

    @pytest.mark.asyncio
    async def test_stale_partial_copy_is_discarded(self, vm):
        v, st = vm
        _legacy_collection(v)
        st.EMBEDDING_MODEL = E5
        stale = v.chroma_client.create_collection("corpus__reindex", {})
        stale.add(["zzz"], [[1.0] * 4], ["огрызок"], [{}])
        col = await v._open_collection("corpus", "d")
        assert "zzz" not in col.rows and len(col.rows) == 3

    @pytest.mark.asyncio
    async def test_large_collection_paged(self, vm, monkeypatch):
        v, st = vm
        monkeypatch.setattr(mem, "_REINDEX_BATCH", 4)
        _legacy_collection(v, n=11)
        st.EMBEDDING_MODEL = E5
        col = await v._open_collection("corpus", "d")
        assert len(col.rows) == 11

    @pytest.mark.asyncio
    async def test_corpus_collections_go_through_same_path(self, vm):
        v, st = vm
        v._initialized = True
        _legacy_collection(v, "corpus_jurist", 2)
        st.EMBEDDING_MODEL = E5
        col = await v.ensure_corpus("corpus_jurist")
        assert col.metadata["embedding_model"] == E5 and len(col.rows) == 2
