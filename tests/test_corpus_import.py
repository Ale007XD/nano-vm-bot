"""Пакетная загрузка корпуса из ZIP: безопасное чтение, план/применение, sync, dry-run, хендлер."""
import io
import sys
import zipfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

LIM = dict(max_files=50, max_file_bytes=10_000, max_total_bytes=100_000)


def make_zip(files, *, symlinks=(), encrypt_flag=()):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, content in files.items():
            data = content.encode("utf-8") if isinstance(content, str) else content
            info = zipfile.ZipInfo(name)
            if name in symlinks:
                info.external_attr = (0o120777 << 16)
            if name in encrypt_flag:
                info.flag_bits |= 0x1
            z.writestr(info, data)
    return buf.getvalue()


@pytest.fixture
def scan():
    from app.utils.corpus_archive import scan_archive

    return lambda data, **kw: scan_archive(data, **{**LIM, **kw})


class TestScan:
    def test_basic_and_common_dir_stripped(self, scan):
        r = scan(make_zip({"docs/a.md": "Альфа", "docs/sub/b.txt": "Бета"}))
        assert sorted(d.name for d in r.docs) == ["a.md", "sub/b.txt"]

    def test_common_dir_kept_when_not_shared(self, scan):
        r = scan(make_zip({"x/a.md": "А", "y/a.md": "Б"}))
        assert sorted(d.name for d in r.docs) == ["x/a.md", "y/a.md"]  # одинаковые basename не сливаются

    @pytest.mark.parametrize("evil", ["../evil.md", "a/../../evil.md", "/abs.md", "C:/win.md", "..\\evil.md"])
    def test_path_traversal_rejected(self, scan, evil):
        r = scan(make_zip({evil: "x", "ok.md": "ok"}))
        assert [d.name for d in r.docs] == ["ok.md"]
        assert any("небезопасный" in why for _, why in r.skipped)

    def test_symlink_and_encrypted_skipped_but_seen(self, scan, monkeypatch):
        orig = zipfile.ZipFile.infolist

        def with_encrypted(self):  # writestr сбрасывает flag_bits — выставляем «зашифрован» при чтении
            infos = orig(self)
            for i in infos:
                if i.filename == "e.md":
                    i.flag_bits |= 0x1
            return infos

        monkeypatch.setattr(zipfile.ZipFile, "infolist", with_encrypted)
        r = scan(make_zip({"l.md": "target", "e.md": "secret", "ok.md": "ok"}, symlinks={"l.md"}))
        assert [d.name for d in r.docs] == ["ok.md"]
        reasons = dict(r.skipped)
        assert "символическая" in reasons["l.md"] and "зашифрован" in reasons["e.md"]
        assert {"l.md", "e.md", "ok.md"} <= r.seen  # sync не должен удалять их прежние версии

    def test_service_and_unsupported(self, scan):
        r = scan(make_zip({".DS_Store": "x", "__MACOSX/._a.md": "x", ".hidden/a.md": "x",
                           "pic.png": b"\x89PNG", "a.md": "A", "b.pdf": b"%PDF"}))
        assert [d.name for d in r.docs] == ["a.md"]
        assert r.unsupported == {"png": 1, "pdf": 1}
        assert not r.skipped

    def test_file_too_big_and_lying_header_bounded(self, scan):
        r = scan(make_zip({"big.md": "я" * 20_000, "ok.md": "ok"}), max_total_bytes=10**6)
        assert [d.name for d in r.docs] == ["ok.md"]
        assert "КБ" in dict(r.skipped)["big.md"]

    def test_total_limit_declared(self):
        from app.utils.corpus_archive import ArchiveError, scan_archive

        data = make_zip({f"{i}.md": "z" * 5000 for i in range(10)})
        with pytest.raises(ArchiveError, match="распакованном"):
            scan_archive(data, max_files=50, max_file_bytes=10_000, max_total_bytes=20_000)

    def test_too_many_files(self):
        from app.utils.corpus_archive import ArchiveError, scan_archive

        with pytest.raises(ArchiveError, match="больше допустимых"):
            scan_archive(make_zip({f"{i}.md": "t" for i in range(6)}),
                         max_files=5, max_file_bytes=1000, max_total_bytes=10**6)

    def test_not_a_zip(self, scan):
        from app.utils.corpus_archive import ArchiveError

        with pytest.raises(ArchiveError, match="не удалось открыть"):
            scan("это не архив".encode())

    def test_encodings_and_empty_and_binary(self, scan):
        r = scan(make_zip({
            "u8.md": "\ufeffПривет",
            "cp.txt": "Привет, мир".encode("cp1251"),
            "empty.md": "  \n",
            "bin.md": b"abc\x00def",
        }))
        by = {d.name: d.text for d in r.docs}
        assert by["u8.md"] == "Привет" and by["cp.txt"] == "Привет, мир"
        reasons = dict(r.skipped)
        assert "пустой" in reasons["empty.md"] and "не текст" in reasons["bin.md"]

    @pytest.mark.filterwarnings("ignore:Duplicate name")
    def test_duplicate_names_last_wins(self, scan):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("a.md", "первый")
            z.writestr("a.md", "второй")
            z.writestr("b.md", "b")
        r = scan(buf.getvalue())
        assert {d.name: d.text for d in r.docs}["a.md"] == "второй"
        assert any("дубль" in why for _, why in r.skipped)

    def test_hash_is_stable_per_content(self, scan):
        a = scan(make_zip({"a.md": "один"})).docs[0].sha
        b = scan(make_zip({"zzz.md": "один"})).docs[0].sha
        c = scan(make_zip({"a.md": "другой"})).docs[0].sha
        assert a == b != c


# ----------------------------- корпус (фейк Chroma) ------------------------ #
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
        return meta.get(k) != v["$ne"] if isinstance(v, dict) else meta.get(k) == v

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
def corpus(monkeypatch):
    sys.modules.setdefault("chromadb", MagicMock())
    from app.core.memory import VectorMemory, vector_memory

    coll = FakeColl()
    monkeypatch.setattr(vector_memory, "_initialized", True, raising=False)
    monkeypatch.setattr(vector_memory, "collections", {"corpus": coll}, raising=False)
    monkeypatch.setattr(VectorMemory, "_embed_text", lambda self, t, kind="passage": [0.1])
    st = sys.modules["app.core.config"].settings
    monkeypatch.setattr(st, "CORPUS_CHUNK_SIZE", 100)
    monkeypatch.setattr(st, "CORPUS_CHUNK_OVERLAP", 10)
    return coll


def S(files, **kw):
    from app.utils.corpus_archive import scan_archive

    return scan_archive(make_zip(files), **{**LIM, **kw})


def names(coll):
    return sorted({m["source_name"] for _, m in coll.rows.values()})


class TestImport:
    @pytest.mark.asyncio
    async def test_first_import_then_idempotent_then_update(self, corpus):
        from app.core import corpus_import as ci

        scan = S({"a.md": "Альфа. " * 30, "b/c.md": "Гамма."})
        plan = await ci.plan_import(scan, "corpus")
        assert [d.name for d in plan.add] == ["a.md", "b/c.md"] and not plan.update
        rep = await ci.apply_import(plan, "corpus")
        assert sorted(rep.added) == ["a.md", "b/c.md"] and not rep.failed
        before = dict(corpus.rows)

        # повтор того же архива — ничего не делает
        plan2 = await ci.plan_import(S({"a.md": "Альфа. " * 30, "b/c.md": "Гамма."}), "corpus")
        assert sorted(plan2.unchanged) == ["a.md", "b/c.md"] and not plan2.add and not plan2.update
        await ci.apply_import(plan2, "corpus")
        assert corpus.rows == before

        # изменили один документ — заменяется только он, без дублей
        plan3 = await ci.plan_import(S({"a.md": "Новая версия.", "b/c.md": "Гамма."}), "corpus")
        assert [d.name for d in plan3.update] == ["a.md"] and plan3.unchanged == ["b/c.md"]
        rep3 = await ci.apply_import(plan3, "corpus")
        assert rep3.updated == ["a.md"]
        a_docs = [d for d, m in corpus.rows.values() if m["source_name"] == "a.md"]
        assert a_docs == ["Новая версия."]

    @pytest.mark.asyncio
    async def test_legacy_doc_without_hash_is_updated(self, corpus):
        from app.core import corpus_import as ci
        from app.core.memory import vector_memory

        await vector_memory.add_corpus_document("old_1", "a.md", "Текст.", corpus="corpus")  # без content_hash
        plan = await ci.plan_import(S({"a.md": "Текст."}), "corpus")
        assert [d.name for d in plan.update] == ["a.md"]
        await ci.apply_import(plan, "corpus")
        assert {m["source_id"] for _, m in corpus.rows.values()} == {ci.source_id_for(plan.update[0])}

    @pytest.mark.asyncio
    async def test_sync_removes_absent_but_keeps_unreadable(self, corpus):
        from app.core import corpus_import as ci
        from app.core.memory import vector_memory

        for n in ("gone.md", "bad.md", "keep.md"):
            await vector_memory.add_corpus_document(f"s_{n}", n, "Текст.", corpus="corpus")
        scan = S({"keep.md": "Текст.", "bad.md": b"\x00\x01binary"})
        plan = await ci.plan_import(scan, "corpus", sync=True)
        assert plan.remove == ["gone.md"]  # bad.md в архиве есть, просто не прочитался
        rep = await ci.apply_import(plan, "corpus")
        assert rep.removed == ["gone.md"]
        assert names(corpus) == ["bad.md", "keep.md"]

    @pytest.mark.asyncio
    async def test_sync_without_documents_is_refused(self, corpus):
        from app.core import corpus_import as ci
        from app.utils.corpus_archive import ArchiveError

        with pytest.raises(ArchiveError, match="очищен"):
            await ci.plan_import(S({"pic.png": b"x"}), "corpus", sync=True)

    @pytest.mark.asyncio
    async def test_failure_keeps_old_version_and_continues(self, corpus, monkeypatch):
        from app.core import corpus_import as ci
        from app.core.memory import vector_memory

        await vector_memory.add_corpus_document("old", "a.md", "Старый текст.", corpus="corpus")
        real = vector_memory.add_corpus_document

        async def flaky(*a, **kw):
            if kw.get("source_name") == "a.md":
                raise RuntimeError("embed down")
            return await real(*a, **kw)

        monkeypatch.setattr(vector_memory, "add_corpus_document", flaky)
        plan = await ci.plan_import(S({"a.md": "Новый.", "b.md": "Другой."}), "corpus")
        rep = await ci.apply_import(plan, "corpus")
        assert [n for n, _ in rep.failed] == ["a.md"] and rep.added == ["b.md"]
        assert [d for d, m in corpus.rows.values() if m["source_name"] == "a.md"] == ["Старый текст."]

    @pytest.mark.asyncio
    async def test_progress_callback_and_reports(self, corpus):
        from app.core import corpus_import as ci

        scan = S({"a.md": "А.", "b.md": "Б.", "x.png": b"1"})
        plan = await ci.plan_import(scan, "corpus", sync=True)
        calls = []

        async def progress(i, n, name):
            calls.append((i, n))

        rep = await ci.apply_import(plan, "corpus", progress=progress, min_progress_interval=0)
        assert calls == [(1, 2), (2, 2)]
        text = ci.format_plan(plan, scan)
        assert "ничего не изменено" in text and "png" in text
        assert "Импорт завершён" in ci.format_report(rep, plan, scan)

    @pytest.mark.asyncio
    async def test_index_terminates_on_odd_result(self, corpus):
        from app.core.memory import vector_memory

        corpus.get = MagicMock()
        assert await vector_memory.get_corpus_index(corpus="corpus") == {}


# ----------------------------- хендлер ------------------------------------ #
class TestHandler:
    @pytest.fixture
    def env(self, monkeypatch):
        sys.modules["app.core.config"].settings.BOT_TIMEZONE = "UTC"
        commands = pytest.importorskip("app.handlers.commands")
        from app.core import showcases
        from app.handlers import showcase as sc

        monkeypatch.setattr(commands, "is_admin", lambda uid: uid == 1)
        st = sc.settings
        for k, v in dict(CORPUS_ZIP_MAX_ARCHIVE_BYTES=10**6, CORPUS_ZIP_MAX_TOTAL_BYTES=10**6,
                         CORPUS_ZIP_MAX_FILE_BYTES=10**5, CORPUS_ZIP_MAX_FILES=50).items():
            monkeypatch.setattr(st, k, v, raising=False)
        return sc, showcases.Showcase(slug="j", token="1:x", collection="corpus_j"), showcases

    def message(self, data, caption=None, uid=1, name="kb.zip"):
        async def download(path, dest):
            with open(dest, "wb") as fh:
                fh.write(data)

        bot = SimpleNamespace(get_file=AsyncMock(return_value=SimpleNamespace(file_path="p")),
                              download_file=AsyncMock(side_effect=download))
        processing = SimpleNamespace(edit_text=AsyncMock())
        m = SimpleNamespace(
            bot=bot, from_user=SimpleNamespace(id=uid), caption=caption,
            document=SimpleNamespace(file_name=name, file_size=len(data), file_id="f1"),
            answer=AsyncMock(return_value=processing),
        )
        m.processing = processing
        return m

    @pytest.mark.asyncio
    async def test_dry_run_changes_nothing(self, env, corpus, monkeypatch):
        sc, show, showcases = env
        from app.core.memory import vector_memory
        monkeypatch.setattr(vector_memory, "collections", {"corpus_j": corpus}, raising=False)
        m = self.message(make_zip({"a.md": "Текст."}), caption="dry sync")
        with showcases.use_showcase(show):
            await sc.handle_document(m)
        assert corpus.count() == 0
        assert "Проверка архива" in m.processing.edit_text.await_args.args[0]

    @pytest.mark.asyncio
    async def test_apply_targets_showcase_collection(self, env, corpus, monkeypatch):
        sc, show, showcases = env
        from app.core.memory import vector_memory
        monkeypatch.setattr(vector_memory, "collections", {"corpus_j": corpus}, raising=False)
        m = self.message(make_zip({"a.md": "Текст.", "b.md": "Ещё."}))
        with showcases.use_showcase(show):
            await sc.handle_document(m)
        assert names(corpus) == ["a.md", "b.md"]
        assert "Импорт завершён" in m.processing.edit_text.await_args.args[0]

    @pytest.mark.asyncio
    async def test_non_admin_denied_and_bad_archive_reported(self, env, corpus, monkeypatch):
        sc, show, showcases = env
        stranger = self.message(make_zip({"a.md": "x"}), uid=2)
        with showcases.use_showcase(show):
            await sc.handle_document(stranger)
        assert "Только для администраторов" in stranger.answer.await_args.args[0]
        stranger.bot.download_file.assert_not_awaited()

        bad = self.message(b"not a zip")
        with showcases.use_showcase(show):
            await sc.handle_document(bad)
        assert "Архив отклонён" in bad.processing.edit_text.await_args.args[0]

    @pytest.mark.asyncio
    async def test_oversized_archive_and_busy_lock(self, env, corpus):
        sc, show, showcases = env
        from app.core import corpus_import as ci

        big = self.message(b"x")
        big.document.file_size = 10**7
        with showcases.use_showcase(show):
            await sc.handle_document(big)
        assert "больше" in big.answer.await_args.args[0]

        busy = self.message(make_zip({"a.md": "x"}))
        async with ci.import_lock("corpus_j"):
            with showcases.use_showcase(show):
                await sc.handle_document(busy)
        assert "уже идёт" in busy.answer.await_args.args[0]
        busy.bot.download_file.assert_not_awaited()

    def test_caption_flags(self, env):
        sc, _, _ = env
        assert sc._zip_flags("dry") == (True, False)
        assert sc._zip_flags("Sync, DRY") == (True, True)
        assert sc._zip_flags("синхронизация") == (False, True)
        assert sc._zip_flags(None) == (False, False)
