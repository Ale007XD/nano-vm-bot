"""
knowledge_base: изоляция пользователей и экранирование HTML.

Регрессия: id статьи = sha1(url) и url UNIQUE были глобальными, поэтому
  * `kb add` существующей у другого пользователя ссылки раскрывал её название и id;
  * второй пользователь не мог добавить ту же ссылку (IntegrityError / «уже в базе»).
"""
import importlib.util
import logging
import os
import sqlite3
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

A, B = 1001, 2002


@pytest.fixture
def kb(tmp_path, monkeypatch):
    path = os.path.join("skills", "custom", "knowledge_base.py")
    spec = importlib.util.spec_from_file_location("_t_kb_iso", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    monkeypatch.setattr(mod, "_DB_PATH", str(tmp_path / "kb.db"))
    monkeypatch.setattr(mod, "_schema_warned", False)

    state = {"title": "T", "summary": "S", "tags": ["t1"]}

    async def fetch(url):
        return state["title"], "page text " + url

    async def entities(text, title, llm):
        return {"summary": state["summary"], "tags": state["tags"], "topics": ["x"], "key_facts": []}

    async def facts(f, title):
        return f

    monkeypatch.setattr(mod, "_fetch_page", fetch)
    monkeypatch.setattr(mod, "_extract_entities", entities)
    monkeypatch.setattr(mod, "_verify_facts", facts)
    mod._state = state
    return mod


def _vector(monkeypatch, hits):
    vm = MagicMock()
    vm.add_memory = AsyncMock()
    vm.search_memories = AsyncMock(return_value=hits)
    monkeypatch.setattr(sys.modules["app.core.memory"], "vector_memory", vm)
    return vm


URL = "https://example.com/post"


class TestAddIsolation:
    @pytest.mark.asyncio
    async def test_two_users_can_add_same_url(self, kb, monkeypatch):
        _vector(monkeypatch, [])
        a = await kb._cmd_add(URL, A, "", None)
        b = await kb._cmd_add(URL, B, "", None)
        assert "✅" in a and "✅" in b
        with kb._db() as c:
            rows = c.execute("SELECT id, user_id FROM articles ORDER BY user_id").fetchall()
        assert [r["user_id"] for r in rows] == [A, B]
        assert rows[0]["id"] != rows[1]["id"]

    @pytest.mark.asyncio
    async def test_other_users_title_not_leaked(self, kb, monkeypatch):
        _vector(monkeypatch, [])
        kb._state["title"] = "SECRET-TITLE-OF-A"
        await kb._cmd_add(URL, A, "", None)

        kb._state["title"] = "B-own-title"
        out = await kb._cmd_add(URL, B, "", None)
        assert "SECRET-TITLE-OF-A" not in out
        assert "B-own-title" in out
        assert "уже в базе" not in out

    @pytest.mark.asyncio
    async def test_same_user_duplicate_detected(self, kb, monkeypatch):
        _vector(monkeypatch, [])
        await kb._cmd_add(URL, A, "", None)
        out = await kb._cmd_add(URL, A, "", None)
        assert "уже в базе" in out
        with kb._db() as c:
            assert c.execute("SELECT COUNT(*) FROM articles").fetchone()[0] == 1

    def test_article_id_depends_on_user(self, kb):
        assert kb._article_id(URL, A) != kb._article_id(URL, B)
        assert kb._article_id(URL, A) == kb._article_id(URL, A)

    @pytest.mark.asyncio
    async def test_db_constraint_unique_per_user(self, kb, monkeypatch):
        _vector(monkeypatch, [])
        await kb._cmd_add(URL, A, "", None)
        with pytest.raises(sqlite3.IntegrityError):
            with kb._db() as c:
                c.execute(
                    "INSERT INTO articles(id,url,user_id,added_at) VALUES('zz',?,?,'now')",
                    (URL, A),
                )


class TestCrossUserAccess:
    async def _seed(self, kb, monkeypatch):
        _vector(monkeypatch, [])
        kb._state["title"] = "SECRET-TITLE-OF-A"
        await kb._cmd_add(URL, A, "", None)
        return kb._article_id(URL, A)

    @pytest.mark.asyncio
    async def test_related_foreign_id_not_found(self, kb, monkeypatch):
        art = await self._seed(kb, monkeypatch)
        out = await kb._cmd_related(art, B)
        assert "не найдена" in out and "SECRET" not in out

    @pytest.mark.asyncio
    async def test_refresh_foreign_id_not_found_and_untouched(self, kb, monkeypatch):
        art = await self._seed(kb, monkeypatch)
        kb._state["title"] = "HIJACKED"
        out = await kb._cmd_refresh(art, B, None)
        assert "не найдена" in out
        with kb._db() as c:
            assert c.execute("SELECT title FROM articles WHERE id=?", (art,)).fetchone()[0] == "SECRET-TITLE-OF-A"

    @pytest.mark.asyncio
    async def test_search_hit_for_foreign_article_not_shown(self, kb, monkeypatch):
        """Даже если векторный поиск вернул чужой article_id, SQLite-подгрузка его не отдаст."""
        art = await self._seed(kb, monkeypatch)
        _vector(monkeypatch, [{"metadata": {"article_id": art, "url": URL}, "distance": 0.1}])
        out = await kb._cmd_search("secret", B)
        assert "SECRET-TITLE-OF-A" not in out

    @pytest.mark.asyncio
    async def test_list_and_stats_scoped(self, kb, monkeypatch):
        await self._seed(kb, monkeypatch)
        assert "SECRET" not in await kb._cmd_list(B)
        assert "Статей: <b>0</b>" in await kb._cmd_stats(B)


class TestHtmlEscaping:
    @pytest.mark.asyncio
    async def test_title_summary_tags_escaped_on_add(self, kb, monkeypatch):
        _vector(monkeypatch, [])
        kb._state.update(
            title='<a href="https://evil.test">free money</a>',
            summary="<b>x</b> & <script>",
            tags=["<i>t</i>"],
        )
        out = await kb._cmd_add(URL, A, "", None)
        assert "<a href" not in out and "<script>" not in out and "<i>t</i>" not in out
        assert "&lt;a href" in out

    @pytest.mark.asyncio
    async def test_list_escapes_title(self, kb, monkeypatch):
        _vector(monkeypatch, [])
        kb._state["title"] = "<b>boom"
        await kb._cmd_add(URL, A, "", None)
        out = await kb._cmd_list(A)
        assert "&lt;b&gt;boom" in out

    def test_link_helper_rejects_bad_schemes_and_escapes_quotes(self, kb):
        assert kb._link("javascript:alert(1)") == ""
        assert kb._link("tg://resolve?domain=x") == ""
        out = kb._link("https://e.com/?q='\"><b>")
        assert out.startswith('<a href="https://e.com/?q=') and "<b>" not in out


class TestLegacySchemaWarning:
    def test_warns_on_old_schema(self, kb, tmp_path, monkeypatch, caplog):
        legacy = str(tmp_path / "old.db")
        c = sqlite3.connect(legacy)
        legacy_ddl = kb._DDL.replace("    url         TEXT NOT NULL,", "    url         TEXT UNIQUE NOT NULL,").replace(
            "    UNIQUE(user_id, url)                -- один URL у разных пользователей — разные записи\n", ""
        ).replace("    is_stale    INTEGER DEFAULT 0,", "    is_stale    INTEGER DEFAULT 0")
        assert "UNIQUE(user_id, url)" not in legacy_ddl
        c.executescript(legacy_ddl)
        c.close()
        monkeypatch.setattr(kb, "_DB_PATH", legacy)
        with caplog.at_level(logging.WARNING):
            with kb._db():
                pass
        assert any("устаревшая схема" in r.message for r in caplog.records)

    def test_no_warning_on_fresh_db(self, kb, caplog):
        with caplog.at_level(logging.WARNING):
            with kb._db():
                pass
        assert not any("устаревшая схема" in r.message for r in caplog.records)
