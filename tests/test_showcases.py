"""
Раздельные витрины: конфиг, реестр/контекст, изоляция корпусов, сессии оператора
по витринам, приглашения и доступ, миграция ключа operator_sessions.
"""
import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core import access, ext_db, showcase_access, showcases
from app.core import operator as store
from app.core.ext_db import ExtBase, init_ext_tables
from app.core.showcases import (
    ShowcaseConfigError,
    ShowcaseRegistry,
    legacy_showcase,
    parse_showcases,
    use_showcase,
)

TOKEN_A = "111:AAA"
TOKEN_B = "222:BBB"


def cfg(*items):
    import json

    return json.dumps(list(items))


def item(slug, token, **kw):
    return {"slug": slug, "token": token, **kw}


# ----------------------------- конфиг ------------------------------------ #
class TestParse:
    def test_defaults(self):
        (sc,) = parse_showcases(cfg(item("beauty", TOKEN_A)))
        assert sc.collection == "corpus_beauty"
        assert sc.access == "invite"
        assert sc.operator_chat_id == 0 and not sc.operator_enabled
        assert sc.bot_id == 111

    def test_two_showcases_full(self):
        a, b = parse_showcases(cfg(
            item("reklama", TOKEN_A, collection="corpus", operator_chat_id=-1001, name="Реклама"),
            item("jurist", TOKEN_B, operator_chat_id=-1002, access="open", model="m", system_prompt="p"),
        ))
        assert (a.collection, a.operator_chat_id, a.name) == ("corpus", -1001, "Реклама")
        assert (b.collection, b.access, b.model, b.system_prompt) == ("corpus_jurist", "open", "m", "p")

    def test_token_env(self, monkeypatch):
        monkeypatch.setenv("JURIST_BOT_TOKEN", TOKEN_B)
        (sc,) = parse_showcases(cfg({"slug": "jurist", "token_env": "JURIST_BOT_TOKEN"}))
        assert sc.token == TOKEN_B

    def test_token_env_missing(self, monkeypatch):
        monkeypatch.delenv("NOPE_TOKEN", raising=False)
        with pytest.raises(ShowcaseConfigError, match="NOPE_TOKEN"):
            parse_showcases(cfg({"slug": "x1", "token_env": "NOPE_TOKEN"}))

    def test_token_not_in_repr(self):
        (sc,) = parse_showcases(cfg(item("beauty", TOKEN_A)))
        assert TOKEN_A not in repr(sc)

    @pytest.mark.parametrize(
        "raw",
        [
            "не json",
            "{}",
            "[]",
            "[1]",
            cfg(item("A", TOKEN_A)),                              # заглавные в slug
            cfg(item("x", TOKEN_A)),                              # слишком короткий
            cfg({"slug": "beauty"}),                              # нет токена
            cfg(item("beauty", "не-токен")),
            cfg(item("beauty", TOKEN_A, access="private")),
            cfg(item("beauty", TOKEN_A, acces="open")),           # опечатка в ключе
            cfg(item("beauty", TOKEN_A, operator_chat_id="-100")),
            cfg(item("beauty", TOKEN_A, operator_chat_id=True)),
            cfg(item("beauty", TOKEN_A, name=5)),
            cfg(item("beauty", TOKEN_A), item("beauty", TOKEN_B)),            # дубль slug
            cfg(item("aa", TOKEN_A), item("bb", TOKEN_A)),                    # дубль токена
            cfg(item("aa", TOKEN_A, collection="corpus"), item("bb", TOKEN_B, collection="corpus")),
        ],
    )
    def test_invalid(self, raw):
        with pytest.raises(ShowcaseConfigError):
            parse_showcases(raw)

    @pytest.mark.parametrize("name", ["memories", "skills", "conversations", "corpus-x", "corpus_", "Corpus"])
    def test_collection_cannot_point_to_service_collections(self, name):
        with pytest.raises(ShowcaseConfigError, match="collection"):
            parse_showcases(cfg(item("beauty", TOKEN_A, collection=name)))


class TestLegacy:
    def test_from_settings(self):
        st = SimpleNamespace(
            SHOWCASE_BOT_TOKEN=TOKEN_A, OPERATOR_CHAT_ID=-1009,
            SHOWCASE_OPEN_ACCESS=True, BOT_OPEN_ACCESS=False,
        )
        sc = legacy_showcase(st)
        assert (sc.slug, sc.collection, sc.operator_chat_id, sc.access) == ("", "corpus", -1009, "open")

    def test_open_inherits_bot_flag(self):
        st = SimpleNamespace(SHOWCASE_BOT_TOKEN=TOKEN_A, OPERATOR_CHAT_ID=0,
                             SHOWCASE_OPEN_ACCESS=None, BOT_OPEN_ACCESS=True)
        assert legacy_showcase(st).access == "open"

    def test_client_key_legacy_is_plain_id(self):
        st = SimpleNamespace(SHOWCASE_BOT_TOKEN=TOKEN_A, OPERATOR_CHAT_ID=0,
                             SHOWCASE_OPEN_ACCESS=None, BOT_OPEN_ACCESS=False)
        assert legacy_showcase(st).client_key(5) == 5
        (named,) = parse_showcases(cfg(item("beauty", TOKEN_A)))
        assert named.client_key(5) == ("beauty", 5)

    def test_load_registry_prefers_showcases(self):
        st = SimpleNamespace(SHOWCASES=cfg(item("beauty", TOKEN_A)), SHOWCASE_BOT_TOKEN="")
        assert [s.slug for s in showcases.load_registry(st)] == ["beauty"]

    def test_load_registry_nothing_configured(self):
        st = SimpleNamespace(SHOWCASES="", SHOWCASE_BOT_TOKEN="", OPERATOR_CHAT_ID=0,
                             SHOWCASE_OPEN_ACCESS=None, BOT_OPEN_ACCESS=False)
        with pytest.raises(ShowcaseConfigError):
            showcases.load_registry(st)


# ----------------------------- контекст ---------------------------------- #
@pytest.fixture
def two():
    a, b = parse_showcases(cfg(item("reklama", TOKEN_A), item("jurist", TOKEN_B)))
    return a, b


@pytest.fixture(autouse=True)
def _reset_registry():
    showcases.set_registry(None)
    yield
    showcases.set_registry(None)


class TestContext:
    def test_registry_lookup(self, two):
        reg = ShowcaseRegistry(list(two))
        assert reg.by_bot_id(111).slug == "reklama" and reg.by_bot_id(222).slug == "jurist"
        assert reg.by_bot_id(999) is None and reg.first.slug == "reklama" and len(reg) == 2

    def test_current_follows_context(self, two):
        showcases.set_registry(ShowcaseRegistry(list(two)))
        with use_showcase(two[1]):
            assert showcases.current().slug == "jurist"
            with use_showcase(two[0]):
                assert showcases.current().slug == "reklama"
            assert showcases.current().slug == "jurist"

    def test_no_context_with_several_showcases_is_an_error(self, two):
        showcases.set_registry(ShowcaseRegistry(list(two)))
        with pytest.raises(RuntimeError):
            showcases.current()

    def test_no_context_single_showcase_is_that_showcase(self, two):
        showcases.set_registry(ShowcaseRegistry([two[0]]))
        assert showcases.current().slug == "reklama"

    @pytest.mark.asyncio
    async def test_middleware_selects_by_bot_and_isolates_concurrent_updates(self, two):
        mw = showcases.showcase_middleware(ShowcaseRegistry(list(two)))
        seen = []

        async def handler(event, data):
            await asyncio.sleep(0.01 * event)  # перемежаем обработку
            seen.append((event, showcases.current().slug))

        await asyncio.gather(
            mw(handler, 2, {"bot": SimpleNamespace(id=111)}),
            mw(handler, 1, {"bot": SimpleNamespace(id=222)}),
        )
        assert sorted(seen) == [(1, "jurist"), (2, "reklama")]

    @pytest.mark.asyncio
    async def test_middleware_drops_unknown_bot(self, two):
        mw = showcases.showcase_middleware(ShowcaseRegistry(list(two)))
        handler = AsyncMock()
        assert await mw(handler, object(), {"bot": SimpleNamespace(id=5)}) is None
        handler.assert_not_awaited()


# ----------------------------- корпуса ----------------------------------- #
class FakeCollection:
    def __init__(self):
        self.docs = {}
        self.metadata = {}

    def modify(self, metadata=None, name=None):
        if metadata is not None:
            self.metadata = dict(metadata)

    def add(self, ids, embeddings, documents, metadatas):
        for i, d, m in zip(ids, documents, metadatas):
            self.docs[i] = (d, m)

    def query(self, query_embeddings, n_results, include):
        items = list(self.docs.items())[:n_results]
        return {
            "ids": [[i for i, _ in items]],
            "documents": [[d for _, (d, _) in items]],
            "metadatas": [[m for _, (_, m) in items]],
            "distances": [[0.1 for _ in items]],
        }

    def delete(self, where):
        sid = where["source_id"]
        self.docs = {i: v for i, v in self.docs.items() if v[1].get("source_id") != sid}

    def count(self):
        return len(self.docs)


class FakeChroma:
    def __init__(self):
        self.created = {}

    def get_or_create_collection(self, name, metadata=None):
        return self.created.setdefault(name, FakeCollection())


@pytest.fixture
def memory(monkeypatch):
    from app.core.memory import VectorMemory

    vm = VectorMemory()
    vm._initialized = True
    vm.chroma_client = FakeChroma()
    vm.collections = {"corpus": vm.chroma_client.get_or_create_collection("corpus")}
    monkeypatch.setattr(VectorMemory, "_embed_text", lambda self, t, kind="passage": [0.1, 0.2])
    st = sys.modules["app.core.config"].settings
    monkeypatch.setattr(st, "CORPUS_CHUNK_SIZE", 1200)
    monkeypatch.setattr(st, "CORPUS_CHUNK_OVERLAP", 200)
    monkeypatch.setattr(st, "CORPUS_TOP_K", 5)
    return vm


class TestCorpusIsolation:
    @pytest.mark.asyncio
    async def test_documents_are_not_visible_across_corpora(self, memory):
        await memory.add_corpus_document("d1", "Банкротство.md", "Процедура банкротства.", corpus="corpus_jurist")
        await memory.add_corpus_document("d2", "Баннеры.md", "Печать баннеров.", corpus="corpus")

        jurist = await memory.search_corpus("банкротство", corpus="corpus_jurist")
        reklama = await memory.search_corpus("банкротство", corpus="corpus")
        beauty = await memory.search_corpus("банкротство", corpus="corpus_beauty")

        assert [d["metadata"]["source_name"] for d in jurist] == ["Банкротство.md"]
        assert [d["metadata"]["source_name"] for d in reklama] == ["Баннеры.md"]
        assert beauty == []

    @pytest.mark.asyncio
    async def test_stats_and_delete_are_per_corpus(self, memory):
        await memory.add_corpus_document("d1", "a", "Один.", corpus="corpus_a")
        await memory.add_corpus_document("d1", "b", "Два.", corpus="corpus_b")
        assert (await memory.get_corpus_stats(corpus="corpus_a"))["chunks"] == 1
        await memory.delete_corpus_document("d1", corpus="corpus_a")
        assert (await memory.get_corpus_stats(corpus="corpus_a"))["chunks"] == 0
        assert (await memory.get_corpus_stats(corpus="corpus_b"))["chunks"] == 1

    @pytest.mark.asyncio
    async def test_default_corpus_is_legacy_collection(self, memory):
        await memory.add_corpus_document("d1", "a", "Текст.")
        assert memory.chroma_client.created["corpus"].count() == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize("name", ["memories", "skills", "conversations", "../x", "corpus x", ""])
    async def test_service_collections_are_unreachable(self, memory, name):
        with pytest.raises(ValueError):
            await memory.ensure_corpus(name)
        with pytest.raises(ValueError):
            await memory.search_corpus("q", corpus=name)


# ----------------------------- БД-фикстура ------------------------------- #
@pytest_asyncio.fixture
async def sm(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(ExtBase.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(ext_db, "default_session_maker", lambda: maker)
    yield maker
    await engine.dispose()


# ----------------------------- сессии оператора -------------------------- #
class TestOperatorTenants:
    @pytest.mark.asyncio
    async def test_same_client_has_independent_sessions_per_showcase(self, sm):
        await store.open_session(7, 10, "Иван", tenant="reklama")
        assert await store.get_open_by_client(7, tenant="jurist") is None
        await store.open_session(7, 10, "Иван", tenant="jurist")
        assert await store.close_session(7, tenant="reklama") is True
        assert await store.get_open_by_client(7, tenant="reklama") is None
        assert await store.get_open_by_client(7, tenant="jurist") is not None

    @pytest.mark.asyncio
    async def test_same_thread_id_in_two_groups_is_not_confused(self, sm):
        await store.open_session(1, 5, "A", tenant="reklama")
        await store.open_session(2, 5, "B", tenant="jurist")
        assert (await store.get_open_by_thread(5, tenant="reklama")).client_id == 1
        assert (await store.get_open_by_thread(5, tenant="jurist")).client_id == 2
        assert await store.get_open_by_thread(5, tenant="beauty") is None

    @pytest.mark.asyncio
    async def test_summary_and_thread_update_only_own_tenant(self, sm):
        await store.open_session(1, 5, "A", tenant="reklama")
        await store.open_session(1, 6, "A", tenant="jurist")
        await store.set_summary(1, "резюме", tenant="jurist")
        await store.set_thread(1, 99, tenant="reklama")
        assert (await store.get_open_by_client(1, tenant="reklama")).summary == ""
        assert (await store.get_open_by_client(1, tenant="jurist")).summary == "резюме"
        assert (await store.get_open_by_client(1, tenant="jurist")).thread_id == 6

    @pytest.mark.asyncio
    async def test_list_open_filters_by_tenant(self, sm):
        await store.open_session(1, 5, "A", tenant="reklama")
        await store.open_session(2, 6, "B", tenant="jurist")
        assert len(await store.list_open_sessions()) == 2
        assert [r.client_id for r in await store.list_open_sessions(tenant="jurist")] == [2]

    @pytest.mark.asyncio
    async def test_adopt_legacy_sessions(self, sm):
        await store.open_session(1, 5, "A")            # без витрины (старые данные)
        await store.open_session(2, 6, "B", tenant="jurist")
        assert await store.adopt_legacy_sessions("reklama") == 1
        assert await store.get_open_by_client(1, tenant="reklama") is not None
        assert await store.get_open_by_client(2, tenant="jurist") is not None
        assert await store.adopt_legacy_sessions("reklama") == 0
        assert await store.adopt_legacy_sessions("") == 0

    @pytest.mark.asyncio
    async def test_adopt_skips_conflicting_rows(self, sm):
        await store.open_session(1, 5, "A")
        await store.open_session(1, 7, "A", tenant="reklama")  # уже есть у витрины
        await store.adopt_legacy_sessions("reklama")
        assert (await store.get_open_by_client(1, tenant="reklama")).thread_id == 7


# ----------------------------- миграция ключа ---------------------------- #
OLD_DDL = (
    "CREATE TABLE operator_sessions (client_id BIGINT NOT NULL PRIMARY KEY, "
    "thread_id INTEGER NOT NULL, status VARCHAR(8) NOT NULL, client_name VARCHAR(128) NOT NULL, "
    "summary TEXT NOT NULL DEFAULT '', opened_at DATETIME, updated_at DATETIME)"
)


@pytest_asyncio.fixture
async def old_engine(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/old.db")
    async with engine.begin() as conn:
        await conn.execute(text(OLD_DDL))
        await conn.execute(text("CREATE INDEX ix_operator_sessions_thread_id ON operator_sessions (thread_id)"))
        await conn.execute(text("CREATE INDEX ix_operator_sessions_status ON operator_sessions (status)"))
        await conn.execute(text(
            "INSERT INTO operator_sessions VALUES (1, 10, 'open', 'Иван', 'старое резюме', "
            "'2026-10-01 10:00:00', '2026-10-01 10:00:00')"
        ))
        await conn.execute(text(
            "INSERT INTO operator_sessions VALUES (2, 11, 'closed', 'Мария', '', NULL, NULL)"
        ))
    yield engine
    await engine.dispose()


async def _pk_and_rows(engine):
    async with engine.connect() as conn:
        info = (await conn.execute(text('PRAGMA table_info("operator_sessions")'))).fetchall()
        rows = (await conn.execute(
            text("SELECT tenant, client_id, thread_id, status, summary FROM operator_sessions ORDER BY client_id")
        )).fetchall()
        names = {r[0] for r in (await conn.execute(text(
            "SELECT name FROM sqlite_master WHERE tbl_name='operator_sessions' AND type='index'"
        ))).fetchall()}
        tables = {r[0] for r in (await conn.execute(text(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ))).fetchall()}
    pk = [r[1] for r in sorted((r for r in info if r[5]), key=lambda r: r[5])]
    return pk, [tuple(r) for r in rows], names, tables


class TestPkMigration:
    @pytest.mark.asyncio
    async def test_rebuilds_with_composite_key_and_keeps_data(self, old_engine):
        await init_ext_tables(old_engine)
        pk, rows, names, tables = await _pk_and_rows(old_engine)
        assert pk == ["tenant", "client_id"]
        assert rows == [("", 1, 10, "open", "старое резюме"), ("", 2, 11, "closed", "")]
        assert {"ix_operator_sessions_thread_id", "ix_operator_sessions_status"} <= names
        assert not {"operator_sessions_old", "operator_sessions_new"} & tables

    @pytest.mark.asyncio
    async def test_is_idempotent(self, old_engine):
        await init_ext_tables(old_engine)
        await init_ext_tables(old_engine)
        _, rows, _, _ = await _pk_and_rows(old_engine)
        assert len(rows) == 2

    @pytest.mark.asyncio
    async def test_work_after_migration_per_tenant(self, old_engine, monkeypatch):
        await init_ext_tables(old_engine)
        maker = async_sessionmaker(old_engine, expire_on_commit=False)
        monkeypatch.setattr(ext_db, "default_session_maker", lambda: maker)
        assert await store.adopt_legacy_sessions("reklama") == 2
        await store.open_session(1, 50, "Иван", tenant="jurist")
        assert (await store.get_open_by_client(1, tenant="reklama")).thread_id == 10
        assert (await store.get_open_by_client(1, tenant="jurist")).thread_id == 50

    @pytest.mark.asyncio
    async def test_stray_new_table_from_failed_attempt_is_cleaned(self, old_engine):
        async with old_engine.begin() as conn:
            await conn.execute(text("CREATE TABLE operator_sessions_new (junk INTEGER)"))
        await init_ext_tables(old_engine)
        pk, rows, _, tables = await _pk_and_rows(old_engine)
        assert pk == ["tenant", "client_id"] and len(rows) == 2
        assert "operator_sessions_new" not in tables

    @pytest.mark.asyncio
    async def test_fresh_database_needs_no_migration(self, tmp_path):
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/fresh.db")
        await init_ext_tables(engine)
        pk, rows, _, _ = await _pk_and_rows(engine)
        await engine.dispose()
        assert pk == ["tenant", "client_id"] and rows == []


# ----------------------------- приглашения ------------------------------- #
class TestInvites:
    @pytest.mark.asyncio
    async def test_redeem_ok_makes_member_and_consumes_use(self, sm):
        code = await showcase_access.create_invite("jurist", created_by=1, max_uses=2)
        assert await showcase_access.redeem_invite("jurist", code, 10) == "ok"
        assert await showcase_access.is_member("jurist", 10)
        assert not await showcase_access.is_member("reklama", 10)
        invite = (await showcase_access.list_invites("jurist"))[0]
        assert invite.uses == 1

    @pytest.mark.asyncio
    async def test_existing_member_does_not_consume(self, sm):
        code = await showcase_access.create_invite("jurist", max_uses=1)
        assert await showcase_access.redeem_invite("jurist", code, 10) == "ok"
        assert await showcase_access.redeem_invite("jurist", code, 10) == "already_member"
        assert await showcase_access.redeem_invite("jurist", code, 11) == "exhausted"

    @pytest.mark.asyncio
    async def test_code_of_other_showcase_is_invalid(self, sm):
        code = await showcase_access.create_invite("jurist")
        assert await showcase_access.redeem_invite("reklama", code, 10) == "invalid"
        assert not await showcase_access.is_member("reklama", 10)
        assert not await showcase_access.is_member("jurist", 10)

    @pytest.mark.asyncio
    async def test_unknown_revoked_expired(self, sm):
        assert await showcase_access.redeem_invite("jurist", "нет-такого", 10) == "invalid"
        code = await showcase_access.create_invite("jurist", max_uses=5)
        assert await showcase_access.revoke_invite("jurist", code)
        assert await showcase_access.redeem_invite("jurist", code, 10) == "revoked"

        code2 = await showcase_access.create_invite("jurist", max_uses=5, days=1)
        async with sm() as s:
            inv = await s.get(showcase_access.ShowcaseInvite, code2)
            inv.expires_at = showcase_access._now() - showcase_access.timedelta(minutes=1)
            await s.commit()
        assert await showcase_access.redeem_invite("jurist", code2, 10) == "expired"

    @pytest.mark.asyncio
    async def test_revoke_from_other_showcase_does_nothing(self, sm):
        code = await showcase_access.create_invite("jurist")
        assert not await showcase_access.revoke_invite("reklama", code)
        assert await showcase_access.redeem_invite("jurist", code, 10) == "ok"

    @pytest.mark.asyncio
    async def test_unlimited_uses(self, sm):
        code = await showcase_access.create_invite("jurist", max_uses=0)
        results = [await showcase_access.redeem_invite("jurist", code, u) for u in range(20, 30)]
        assert results == ["ok"] * 10

    @pytest.mark.asyncio
    async def test_limits_are_clamped(self, sm):
        code = await showcase_access.create_invite("jurist", max_uses=10**9, days=10**6)
        async with sm() as s:
            inv = await s.get(showcase_access.ShowcaseInvite, code)
        assert inv.max_uses == showcase_access.MAX_USES_LIMIT
        assert inv.expires_at < showcase_access._now() + showcase_access.timedelta(days=366)

    @pytest.mark.asyncio
    async def test_code_is_deeplink_safe(self, sm):
        import re

        for _ in range(20):
            assert re.fullmatch(r"[A-Za-z0-9_-]{6,32}", await showcase_access.create_invite("jurist"))

    @pytest.mark.asyncio
    async def test_list_invites_hides_inactive(self, sm):
        used = await showcase_access.create_invite("jurist", max_uses=1)
        await showcase_access.redeem_invite("jurist", used, 10)
        revoked = await showcase_access.create_invite("jurist")
        await showcase_access.revoke_invite("jurist", revoked)
        live = await showcase_access.create_invite("jurist", max_uses=3)
        other = await showcase_access.create_invite("reklama")
        codes = [i.code for i in await showcase_access.list_invites("jurist")]
        assert codes == [live] and other not in codes

    @pytest.mark.asyncio
    async def test_members_add_remove_list(self, sm):
        assert await showcase_access.add_member("jurist", 5, added_by=1) is True
        assert await showcase_access.add_member("jurist", 5) is False
        await showcase_access.add_member("reklama", 6)
        assert [m.user_id for m in await showcase_access.list_members("jurist")] == [5]
        assert await showcase_access.remove_member("jurist", 5) is True
        assert await showcase_access.remove_member("jurist", 5) is False
        assert not await showcase_access.is_member("jurist", 5)

    @pytest.mark.asyncio
    async def test_parallel_redeem_never_exceeds_limit(self, tmp_path, monkeypatch):
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/race.db")
        async with engine.begin() as conn:
            await conn.run_sync(ExtBase.metadata.create_all)
        maker = async_sessionmaker(engine, expire_on_commit=False)
        monkeypatch.setattr(ext_db, "default_session_maker", lambda: maker)
        code = await showcase_access.create_invite("jurist", max_uses=3)
        results = await asyncio.gather(
            *[showcase_access.redeem_invite("jurist", code, 100 + i) for i in range(12)]
        )
        await engine.dispose()
        assert results.count("ok") == 3
        assert set(results) <= {"ok", "exhausted"}


# ----------------------------- проверка доступа -------------------------- #
@pytest.fixture
def acc(monkeypatch):
    st = sys.modules["app.core.config"].settings
    monkeypatch.setattr(st, "ADMIN_IDS", [1])
    db = sys.modules["app.core.database"]
    monkeypatch.setattr(db, "is_user_banned", AsyncMock(return_value=False))
    return db


def sc_of(access_mode, slug="jurist"):
    (sc,) = parse_showcases(cfg(item(slug, TOKEN_A, access=access_mode)))
    return sc


class TestShowcaseAllowed:
    @pytest.mark.asyncio
    async def test_admin_always(self, acc, sm):
        assert await access.showcase_allowed(sc_of("invite"), 1)

    @pytest.mark.asyncio
    async def test_open_allows_stranger_but_not_banned(self, acc, sm):
        assert await access.showcase_allowed(sc_of("open"), 50)
        acc.is_user_banned = AsyncMock(return_value=True)
        assert not await access.showcase_allowed(sc_of("open"), 50)

    @pytest.mark.asyncio
    async def test_invite_only_members(self, acc, sm):
        sc = sc_of("invite")
        assert not await access.showcase_allowed(sc, 50)
        await showcase_access.add_member("jurist", 50)
        assert await access.showcase_allowed(sc, 50)

    @pytest.mark.asyncio
    async def test_membership_is_per_showcase(self, acc, sm):
        await showcase_access.add_member("jurist", 50)
        assert not await access.showcase_allowed(sc_of("invite", "reklama"), 50)

    @pytest.mark.asyncio
    async def test_banned_member_denied(self, acc, sm):
        await showcase_access.add_member("jurist", 50)
        acc.is_user_banned = AsyncMock(return_value=True)
        assert not await access.showcase_allowed(sc_of("invite"), 50)

    @pytest.mark.asyncio
    async def test_db_failure_fails_closed_for_invite_open_for_open(self, acc, monkeypatch):
        acc.is_user_banned = AsyncMock(side_effect=RuntimeError("db"))
        assert await access.showcase_allowed(sc_of("open"), 50) is True
        assert await access.showcase_allowed(sc_of("invite"), 50) is False

        acc.is_user_banned = AsyncMock(return_value=False)
        monkeypatch.setattr(showcase_access, "is_member", AsyncMock(side_effect=RuntimeError("db")))
        assert await access.showcase_allowed(sc_of("invite"), 50) is False
