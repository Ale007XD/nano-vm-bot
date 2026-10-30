"""Чанки корпуса под окно модели эмбеддингов, замена документа по имени, список источников."""
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture(autouse=True)
def _stubs():
    sys.modules.setdefault("chromadb", MagicMock())
    sys.modules.setdefault("chromadb.config", MagicMock())
    sys.modules.setdefault("fastembed", MagicMock())
    yield


@pytest.fixture
def vm():
    from app.core.memory import VectorMemory

    return VectorMemory()


PARA = (
    "Возврат товара возможен в течение четырнадцати дней. Деньги возвращаются на карту за три дня! "
    "Нужен ли чек? Да, чек обязателен, без него возврат невозможен… Исключения: товары надлежащего "
    "качества, сделанные на заказ."
)


class TestChunker:
    @pytest.mark.parametrize("size,overlap", [(60, 0), (60, 20), (120, 40), (450, 80), (30, 29)])
    def test_chunks_never_exceed_size(self, vm, size, overlap):
        text = "\n".join([PARA] * 12 + ["Слово" * 3] * 3)
        chunks = vm._chunk_text(text, size, overlap)
        assert chunks
        assert all(0 < len(c) <= size for c in chunks)

    def test_no_word_is_cut_when_words_fit(self, vm):
        words = set(PARA.replace("…", " ").split())
        for c in vm._chunk_text(" ".join([PARA] * 5), 80, 20):
            for w in c.split():
                assert w in words or w.strip(".,!?:…") in {x.strip(".,!?:…") for x in words}

    def test_all_content_is_covered(self, vm):
        text = " ".join(f"факт{i}." for i in range(200))
        chunks = vm._chunk_text(text, 100, 0)
        joined = " ".join(chunks)
        for i in range(200):
            assert f"факт{i}." in joined

    def test_overwide_word_is_split_by_chars(self, vm):
        chunks = vm._chunk_text("а" * 250, 100, 0)
        assert [len(c) for c in chunks] == [100, 100, 50]

    def test_default_chunk_fits_embedding_window(self):
        # app.core.config замокан в conftest — читаем настоящие умолчания по пути файла
        import importlib.util
        import pathlib

        spec = importlib.util.spec_from_file_location(
            "real_config_corpus", pathlib.Path(__file__).parent.parent / "app" / "core" / "config.py"
        )
        real = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(real)
        fields = real.Settings.model_fields
        assert fields["CORPUS_CHUNK_SIZE"].default == 450
        assert fields["CORPUS_CHUNK_OVERLAP"].default == 80
        assert fields["CORPUS_TOP_K"].default == 8


class FakeColl:
    def __init__(self):
        self.rows = {}

    def add(self, ids, embeddings=None, documents=None, metadatas=None):
        for i, d, m in zip(ids, documents, metadatas):
            self.rows[i] = (d, m)

    def _match(self, meta, where):
        if "$and" in where:
            return all(self._match(meta, w) for w in where["$and"])
        (k, v), = where.items()
        if isinstance(v, dict):
            return meta.get(k) != v["$ne"]
        return meta.get(k) == v

    def get(self, where=None, include=None, limit=None, offset=0):
        items = [(i, v) for i, v in self.rows.items() if where is None or self._match(v[1], where)]
        items = items[offset: offset + limit if limit else None]
        return {"ids": [i for i, _ in items], "documents": [v[0] for _, v in items],
                "metadatas": [v[1] for _, v in items]}

    def delete(self, ids=None, where=None):
        for i in ids or []:
            self.rows.pop(i, None)

    def count(self):
        return len(self.rows)


@pytest.fixture
def mem(monkeypatch, vm):
    from app.core.memory import VectorMemory

    coll = FakeColl()
    vm._initialized = True
    vm.collections = {"corpus": coll}
    monkeypatch.setattr(VectorMemory, "_embed_text", lambda self, t, kind="passage": [0.1])
    st = sys.modules["app.core.config"].settings
    monkeypatch.setattr(st, "CORPUS_CHUNK_SIZE", 100)
    monkeypatch.setattr(st, "CORPUS_CHUNK_OVERLAP", 10)
    return vm, coll


class TestReplaceByName:
    @pytest.mark.asyncio
    async def test_reupload_replaces_old_version_only(self, mem):
        vm, coll = mem
        await vm.add_corpus_document("up_1_a", "faq.md", " ".join([PARA] * 3))
        await vm.add_corpus_document("up_1_x", "other.md", "Другой документ.")
        old = sum(1 for _, m in coll.rows.values() if m["source_id"] == "up_1_a")
        assert old > 1

        await vm.add_corpus_document("up_1_b", "faq.md", "Новая версия.")
        removed = await vm.delete_corpus_by_name("faq.md", keep_source_id="up_1_b")

        assert removed == old
        ids = {m["source_id"] for _, m in coll.rows.values()}
        assert ids == {"up_1_b", "up_1_x"}

    @pytest.mark.asyncio
    async def test_delete_by_name_without_keep_and_missing(self, mem):
        vm, coll = mem
        await vm.add_corpus_document("s1", "faq.md", "Текст.")
        assert await vm.delete_corpus_by_name("нет-такого.md") == 0
        assert await vm.delete_corpus_by_name("faq.md") == 1
        assert coll.count() == 0

    @pytest.mark.asyncio
    async def test_failure_returns_zero(self, mem):
        vm, coll = mem
        coll.get = MagicMock(side_effect=RuntimeError("boom"))
        assert await vm.delete_corpus_by_name("faq.md") == 0


class TestSources:
    @pytest.mark.asyncio
    async def test_sources_listing(self, mem):
        vm, _ = mem
        await vm.add_corpus_document("a", "big.md", " ".join([PARA] * 4))
        await vm.add_corpus_document("b", "small.md", "Коротко.")
        src = await vm.get_corpus_sources()
        assert [s["name"] for s in src] == ["big.md", "small.md"]
        assert src[0]["chunks"] > 1 and 0 < src[0]["avg_chars"] <= 100
        assert src[1] == {"name": "small.md", "chunks": 1, "avg_chars": len("Коротко.")}

    @pytest.mark.asyncio
    async def test_sources_terminates_on_unexpected_get_result(self, mem):
        vm, coll = mem
        coll.get = MagicMock()  # MagicMock вместо словаря не должен зацикливать выборку
        assert await vm.get_corpus_sources() == []


class TestCorpusDeleteCommand:
    @pytest.mark.asyncio
    async def test_delete_command(self, monkeypatch):
        sys.modules["app.core.config"].settings.BOT_TIMEZONE = "UTC"
        commands = pytest.importorskip("app.handlers.commands")
        from app.core import showcases
        from app.handlers import showcase as sc

        monkeypatch.setattr(commands, "is_admin", lambda uid: uid == 1)
        delete = AsyncMock(side_effect=[3, 0])
        monkeypatch.setattr(sc.vector_memory, "delete_corpus_by_name", delete)
        show = showcases.Showcase(slug="j", token="1:x", collection="corpus_j")

        def m(uid):
            return SimpleNamespace(from_user=SimpleNamespace(id=uid), answer=AsyncMock())

        cmd = lambda a: SimpleNamespace(args=a)  # noqa: E731
        with showcases.use_showcase(show):
            stranger = m(2)
            await sc.cmd_corpus_delete(stranger, cmd("faq.md"))
            delete.assert_not_awaited()

            empty = m(1)
            await sc.cmd_corpus_delete(empty, cmd(None))
            assert "/corpus_delete" in empty.answer.await_args.args[0]

            ok = m(1)
            await sc.cmd_corpus_delete(ok, cmd("faq.md"))
            assert delete.await_args.args[0] == "faq.md"
            assert delete.await_args.kwargs["corpus"] == "corpus_j"
            assert "3" in ok.answer.await_args.args[0]

            missing = m(1)
            await sc.cmd_corpus_delete(missing, cmd("faq.md"))
            assert "не найден" in missing.answer.await_args.args[0]
