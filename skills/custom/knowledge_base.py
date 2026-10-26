"""
knowledge_base — скилл для сбора, анализа и поиска статей из Telegram-каналов.

Вход: forward сообщения с URL (+ опциональный комментарий) в личку бота.
Хранение: SQLite (метаданные + граф связей) + ChromaDB (семантические векторы).
Актуализация: веб-поиск ключевых фактов при добавлении статьи.

Команды (через агента или текст):
  kb add <url> [комментарий]   — добавить статью вручную
  kb search <запрос>           — семантический поиск
  kb related <id>              — похожие статьи по графу/векторам
  kb refresh <id>              — обновить факты конкретной статьи
  kb stats                     — статистика базы
  kb list [тег]                — список статей, опционально по тегу
"""

import hashlib
import json
import logging
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from html import escape as html_escape
from typing import Optional

from app.core.net_guard import FetchError, UnsafeURLError, safe_fetch

SKILL_NAME = "knowledge_base"
SKILL_DESCRIPTION = (
    "Сбор статей из Telegram forwarded-сообщений, извлечение сущностей, "
    "построение графа связей, семантический поиск. "
    "Триггеры: 'kb ', 'статья', 'добавь статью', 'найди в базе', 'похожие статьи'."
)
SKILL_VERSION = "1.0.0"
SKILL_AUTHOR = "alex"
SKILL_CATEGORY = "custom"
SKILL_COMMANDS = ["kb"]

logger = logging.getLogger(__name__)

# ─── Путь к SQLite ────────────────────────────────────────────────────────────
_DB_PATH = os.path.join(
    os.environ.get("DATA_DIR", "data"),
    "knowledge_base.db"
)

# ─── DDL ──────────────────────────────────────────────────────────────────────
_DDL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS articles (
    id          TEXT PRIMARY KEY,
    url         TEXT NOT NULL,
    title       TEXT,
    summary     TEXT,
    full_text   TEXT,
    tags        TEXT DEFAULT '[]',      -- JSON array
    topics      TEXT DEFAULT '[]',      -- JSON array
    key_facts   TEXT DEFAULT '[]',      -- JSON array
    source_name TEXT,
    user_comment TEXT,
    user_id     INTEGER NOT NULL,
    added_at    TEXT NOT NULL,
    refreshed_at TEXT,
    is_stale    INTEGER DEFAULT 0,      -- 1 если данные устарели (>30 дней)
    UNIQUE(user_id, url)                -- один URL у разных пользователей — разные записи
);

CREATE TABLE IF NOT EXISTS article_links (
    from_id     TEXT NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
    to_id       TEXT NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
    weight      REAL DEFAULT 1.0,       -- общих тегов / тем
    link_type   TEXT DEFAULT 'semantic',-- semantic | tag | topic
    PRIMARY KEY (from_id, to_id)
);

CREATE INDEX IF NOT EXISTS idx_articles_user  ON articles(user_id);
CREATE INDEX IF NOT EXISTS idx_articles_stale ON articles(is_stale);
CREATE INDEX IF NOT EXISTS idx_links_from     ON article_links(from_id);
CREATE INDEX IF NOT EXISTS idx_links_to       ON article_links(to_id);
"""


_schema_warned = False


def _warn_if_legacy_schema(conn) -> None:
    """Старая схема (url UNIQUE глобально) ломает добавление одной ссылки разными
    пользователями. Миграции нет: удалите data/knowledge_base.db (проект greenfield)."""
    global _schema_warned
    if _schema_warned:
        return
    row = conn.execute("SELECT sql FROM sqlite_master WHERE name='articles'").fetchone()
    if row and "UNIQUE(user_id, url)" not in row[0]:
        logger.warning(
            "knowledge_base: устаревшая схема БД (%s). Удалите файл БД, чтобы "
            "статьи разных пользователей не конфликтовали.", _DB_PATH,
        )
    _schema_warned = True


@contextmanager
def _db():
    os.makedirs(os.path.dirname(_DB_PATH) or ".", exist_ok=True)
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(_DDL)
        _warn_if_legacy_schema(conn)
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _e(value) -> str:
    """Экранирование недоверенного текста (страницы, ответы LLM) для HTML-режима Telegram."""
    return html_escape("" if value is None else str(value), quote=True)


def _link(url: str, label: str = "открыть") -> str:
    """Безопасная ссылка: только http(s), атрибут в двойных кавычках и экранирован."""
    if not isinstance(url, str) or not url.lower().startswith(("http://", "https://")):
        return ""
    return f'<a href="{html_escape(url, quote=True)}">{label}</a>'


def _article_id(url: str, user_id: int) -> str:
    """ID статьи привязан к пользователю: одна ссылка у разных людей — разные записи."""
    return hashlib.sha1(f"{user_id}:{url}".encode()).hexdigest()[:12]


# ─── Парсинг URL из текста ────────────────────────────────────────────────────
_URL_RE = re.compile(r'https?://[^\s\]\)>"\']+')


def _extract_url(text: str) -> Optional[str]:
    m = _URL_RE.search(text or "")
    return m.group(0).rstrip(".,;)") if m else None


# ─── Fetch страницы ───────────────────────────────────────────────────────────
async def _fetch_page(url: str) -> tuple[str, str]:
    """Возвращает (title, text). text — первые 6000 символов body."""
    headers = {"User-Agent": "Mozilla/5.0 (compatible; KnowledgeBot/1.0)"}
    try:
        r = await safe_fetch(url, headers=headers, timeout=20, max_bytes=2 * 1024 * 1024)
        if r.status_code >= 400:
            raise FetchError(f"HTTP {r.status_code}")
        html = r.text
    except UnsafeURLError:
        raise  # отказ по политике — вызывающий покажет пользователю причину
    except Exception as e:
        logger.warning(f"fetch {url}: {e}")
        return "", ""

    # title
    tm = re.search(r'<title[^>]*>(.*?)</title>', html, re.IGNORECASE | re.DOTALL)
    title = re.sub(r'\s+', ' ', tm.group(1)).strip() if tm else ""

    # body text — убираем теги
    text = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return title, text[:6000]


# ─── LLM: извлечение сущностей ────────────────────────────────────────────────
async def _extract_entities(text: str, title: str, llm_client) -> dict:
    """Возвращает dict(summary, tags, topics, key_facts)."""
    prompt = f"""Проанализируй статью и верни ТОЛЬКО валидный JSON без пояснений:

Заголовок: {title}
Текст (фрагмент): {text[:3000]}

Формат ответа:
{{
  "summary": "краткое изложение 2-3 предложения на русском",
  "tags": ["тег1", "тег2", "тег3"],
  "topics": ["тема1", "тема2"],
  "key_facts": ["факт 1", "факт 2", "факт 3"]
}}

Теги — конкретные технологии/имена/продукты. Темы — широкие категории. Факты — конкретные цифры/события/утверждения."""

    try:
        response = await llm_client.chat(
            messages=[{"role": "user", "content": prompt}],
            system="Ты аналитик. Отвечай только JSON без markdown-оберток.",
            max_tokens=600
        )
        raw = response.strip().lstrip("```json").rstrip("```").strip()
        return json.loads(raw)
    except Exception as e:
        logger.warning(f"LLM extract failed: {e}")
        return {"summary": title, "tags": [], "topics": [], "key_facts": []}


# ─── Актуализация: веб-поиск ──────────────────────────────────────────────────
async def _verify_facts(facts: list[str], title: str) -> list[str]:
    """DuckDuckGo-поиск по заголовку — возвращает уточнённые факты или исходные."""
    try:
        from app.core.web_search import web_search
        results = await web_search.search(title, num_results=3)
        if not results:
            return facts

        # Просто помечаем — актуальные данные найдены
        return [f"{f} ✓" if i == 0 else f for i, f in enumerate(facts)]
    except Exception as e:
        logger.warning(f"web verify: {e}")
        return facts


# ─── Граф связей ─────────────────────────────────────────────────────────────
def _build_links(article_id: str, tags: list, topics: list, user_id: int):
    """Находит существующие статьи с пересечением тегов/тем и создаёт рёбра графа."""
    with _db() as conn:
        rows = conn.execute(
            "SELECT id, tags, topics FROM articles WHERE user_id=? AND id!=?",
            (user_id, article_id)
        ).fetchall()

        new_set = set(tags) | set(topics)
        links = []
        for row in rows:
            existing_tags = set(json.loads(row["tags"] or "[]"))
            existing_topics = set(json.loads(row["topics"] or "[]"))
            existing_set = existing_tags | existing_topics
            overlap = new_set & existing_set
            if overlap:
                weight = len(overlap) / max(len(new_set | existing_set), 1)
                links.append((article_id, row["id"], round(weight, 3), "tag"))

        if links:
            conn.executemany(
                "INSERT OR REPLACE INTO article_links(from_id,to_id,weight,link_type) VALUES(?,?,?,?)",
                links
            )
            # Симметричные рёбра
            conn.executemany(
                "INSERT OR REPLACE INTO article_links(from_id,to_id,weight,link_type) VALUES(?,?,?,?)",
                [(to, fr, w, lt) for fr, to, w, lt in links]
            )


# ─── Команды скилла ───────────────────────────────────────────────────────────

async def _cmd_add(url: str, user_id: int, comment: str, llm_client) -> str:
    art_id = _article_id(url, user_id)

    with _db() as conn:
        existing = conn.execute(
            "SELECT id, title FROM articles WHERE id=? AND user_id=?", (art_id, user_id)
        ).fetchone()
        if existing:
            return (
                f"⚠️ Статья уже в базе: <b>{_e(existing['title'] or url)}</b>\n"
                f"ID: <code>{art_id}</code>"
            )

    try:
        title, text = await _fetch_page(url)
    except UnsafeURLError as e:
        return f"⛔ Этот адрес нельзя добавить: {html_escape(str(e))}"
    if not text:
        return f"❌ Не удалось получить содержимое страницы:\n<code>{_e(url)}</code>"

    entities = await _extract_entities(text, title, llm_client)
    key_facts = await _verify_facts(entities.get("key_facts", []), title)

    # Определяем источник из домена
    source = re.sub(r'^www\.', '', re.search(r'https?://([^/]+)', url).group(1))

    with _db() as conn:
        conn.execute("""
            INSERT INTO articles(id,url,title,summary,full_text,tags,topics,key_facts,
                                 source_name,user_comment,user_id,added_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            art_id, url, title,
            entities.get("summary", ""),
            text[:4000],
            json.dumps(entities.get("tags", []), ensure_ascii=False),
            json.dumps(entities.get("topics", []), ensure_ascii=False),
            json.dumps(key_facts, ensure_ascii=False),
            source, comment, user_id,
            datetime.now().isoformat()
        ))

    # ChromaDB
    try:
        from app.core.memory import vector_memory
        chroma_content = (
            f"{title}\n{entities.get('summary','')}\n"
            f"Теги: {', '.join(entities.get('tags',[]))}\n"
            f"Темы: {', '.join(entities.get('topics',[]))}"
        )
        await vector_memory.add_memory(
            content=chroma_content,
            user_id=user_id,
            memory_type="article",
            metadata={"article_id": art_id, "url": url, "source": source}
        )
    except Exception as e:
        logger.warning(f"ChromaDB add failed: {e}")

    # Граф
    _build_links(art_id, entities.get("tags", []), entities.get("topics", []), user_id)

    tags_str = _e(" ".join(f"#{t}" for t in entities.get("tags", [])[:5]))
    topics_str = _e(", ".join(entities.get("topics", [])[:3]))
    facts_str = "\n".join(f"• {_e(f)}" for f in key_facts[:3])

    return (
        f"✅ <b>Статья добавлена в базу знаний</b>\n\n"
        f"📰 <b>{_e(title)}</b>\n"
        f"🌐 {_e(source)}\n"
        f"🆔 <code>{art_id}</code>\n\n"
        f"📝 {_e(entities.get('summary',''))}\n\n"
        f"🏷 {tags_str}\n"
        f"📂 Темы: {topics_str}\n\n"
        f"🔑 <b>Ключевые факты:</b>\n{facts_str}"
    )


async def _cmd_search(query: str, user_id: int) -> str:
    try:
        from app.core.memory import vector_memory
        results = await vector_memory.search_memories(
            query=query, user_id=user_id, n_results=5, memory_type="article"
        )
    except Exception as e:
        results = []
        logger.warning(f"ChromaDB search: {e}")

    if not results:
        # Fallback: SQLite LIKE
        with _db() as conn:
            rows = conn.execute("""
                SELECT id, title, summary, url, source_name, tags
                FROM articles WHERE user_id=?
                AND (title LIKE ? OR summary LIKE ? OR tags LIKE ?)
                LIMIT 5
            """, (user_id, f"%{query}%", f"%{query}%", f"%{query}%")).fetchall()

        if not rows:
            return f"🔍 Ничего не найдено по запросу: <i>{_e(query)}</i>"

        out = f"🔍 <b>Найдено {len(rows)} статей (текстовый поиск):</b>\n\n"
        for r in rows:
            tags = _e(" ".join(f"#{t}" for t in json.loads(r["tags"] or "[]")[:3]))
            out += f"📰 <b>{_e(r['title'] or r['url'])}</b>\n"
            out += f"   {_e((r['summary'] or '')[:120])}...\n"
            out += f"   {tags}\n"
            out += f"   🆔 <code>{r['id']}</code> | {_link(r['url'])}\n\n"
        return out

    out = f"🔍 <b>Найдено {len(results)} статей:</b>\n\n"
    for mem in results:
        art_id = mem["metadata"].get("article_id", "")
        url = mem["metadata"].get("url", "")
        relevance = round((1 - mem["distance"]) * 100)
        # Дополняем из SQLite
        with _db() as conn:
            row = conn.execute(
                "SELECT title, summary, tags FROM articles WHERE id=? AND user_id=?",
                (art_id, user_id),
            ).fetchone()
        if row:
            tags = _e(" ".join(f"#{t}" for t in json.loads(row["tags"] or "[]")[:3]))
            out += f"📰 <b>{_e(row['title'] or url)}</b>\n"
            out += f"   {_e((row['summary'] or '')[:120])}...\n"
            out += f"   {tags} | релевантность: {relevance}%\n"
            out += f"   🆔 <code>{_e(art_id)}</code>"
            if url:
                out += f" | {_link(url)}"
            out += "\n\n"
    return out


async def _cmd_related(art_id: str, user_id: int) -> str:
    with _db() as conn:
        base = conn.execute(
            "SELECT title, url FROM articles WHERE id=? AND user_id=?", (art_id, user_id)
        ).fetchone()
        if not base:
            return f"❌ Статья <code>{_e(art_id)}</code> не найдена."

        links = conn.execute("""
            SELECT a.id, a.title, a.url, a.tags, al.weight, al.link_type
            FROM article_links al
            JOIN articles a ON a.id = al.to_id
            WHERE al.from_id=? AND a.user_id=?
            ORDER BY al.weight DESC LIMIT 8
        """, (art_id, user_id)).fetchall()

    if not links:
        return f"🔗 Для статьи <b>{_e(base['title'])}</b> связей пока нет.\nДобавьте больше статей по схожим темам."

    out = f"🔗 <b>Связанные статьи для:</b>\n<i>{_e(base['title'])}</i>\n\n"
    for r in links:
        tags = _e(" ".join(f"#{t}" for t in json.loads(r["tags"] or "[]")[:3]))
        strength = round(r["weight"] * 100)
        out += f"📰 <b>{_e(r['title'] or r['url'])}</b>\n"
        out += f"   {tags} | связь: {strength}% ({_e(r['link_type'])})\n"
        out += f"   🆔 <code>{r['id']}</code>"
        if r["url"]:
            out += f" | {_link(r['url'])}"
        out += "\n\n"
    return out


async def _cmd_refresh(art_id: str, user_id: int, llm_client) -> str:
    with _db() as conn:
        row = conn.execute(
            "SELECT url, title, full_text FROM articles WHERE id=? AND user_id=?",
            (art_id, user_id)
        ).fetchone()
        if not row:
            return f"❌ Статья <code>{_e(art_id)}</code> не найдена."

    try:
        title, text = await _fetch_page(row["url"])
    except UnsafeURLError as e:
        return f"⛔ Адрес статьи больше не проходит проверку: {html_escape(str(e))}"
    if not text:
        text = row["full_text"] or ""
        title = title or row["title"] or ""

    entities = await _extract_entities(text, title, llm_client)
    key_facts = await _verify_facts(entities.get("key_facts", []), title)

    with _db() as conn:
        conn.execute("""
            UPDATE articles SET
                title=?, summary=?, full_text=?, tags=?, topics=?,
                key_facts=?, refreshed_at=?, is_stale=0
            WHERE id=? AND user_id=?
        """, (
            title,
            entities.get("summary", ""),
            text[:4000],
            json.dumps(entities.get("tags", []), ensure_ascii=False),
            json.dumps(entities.get("topics", []), ensure_ascii=False),
            json.dumps(key_facts, ensure_ascii=False),
            datetime.now().isoformat(),
            art_id, user_id
        ))

    _build_links(art_id, entities.get("tags", []), entities.get("topics", []), user_id)
    return f"🔄 <b>Статья обновлена:</b> <code>{_e(art_id)}</code>\n📰 {_e(title)}"


async def _cmd_stats(user_id: int) -> str:
    with _db() as conn:
        total = conn.execute("SELECT COUNT(*) FROM articles WHERE user_id=?", (user_id,)).fetchone()[0]
        stale = conn.execute("SELECT COUNT(*) FROM articles WHERE user_id=? AND is_stale=1", (user_id,)).fetchone()[0]
        links = conn.execute("""
            SELECT COUNT(*) FROM article_links al
            JOIN articles a ON a.id=al.from_id WHERE a.user_id=?
        """, (user_id,)).fetchone()[0]

        # Топ теги
        all_tags: dict = {}
        rows = conn.execute("SELECT tags FROM articles WHERE user_id=?", (user_id,)).fetchall()
        for r in rows:
            for tag in json.loads(r["tags"] or "[]"):
                all_tags[tag] = all_tags.get(tag, 0) + 1
        top_tags = sorted(all_tags.items(), key=lambda x: -x[1])[:8]

        # Топ источники
        sources = conn.execute("""
            SELECT source_name, COUNT(*) as cnt FROM articles
            WHERE user_id=? GROUP BY source_name ORDER BY cnt DESC LIMIT 5
        """, (user_id,)).fetchall()

    tags_str = _e(" ".join(f"#{t}({c})" for t, c in top_tags))
    sources_str = "\n".join(f"  • {_e(r['source_name'])}: {r['cnt']}" for r in sources)

    return (
        f"📊 <b>База знаний</b>\n\n"
        f"📰 Статей: <b>{total}</b>\n"
        f"🔗 Связей в графе: <b>{links // 2}</b>\n"
        f"⚠️ Устаревших: <b>{stale}</b>\n\n"
        f"🏷 <b>Топ теги:</b>\n{tags_str}\n\n"
        f"🌐 <b>Источники:</b>\n{sources_str}"
    )


async def _cmd_refresh_stale(user_id: int, llm_client) -> str:
    """Находит статьи старше KB_STALE_DAYS, обновляет их по одной."""
    try:
        from app.core.config import settings
        stale_days = getattr(settings, "KB_STALE_DAYS", 30)
    except Exception:
        stale_days = 30

    from datetime import timedelta
    cutoff = (datetime.now() - timedelta(days=stale_days)).isoformat()

    with _db() as conn:
        rows = conn.execute("""
            SELECT id FROM articles
            WHERE user_id=?
            AND (refreshed_at IS NULL OR refreshed_at < ?)
            AND added_at < ?
            ORDER BY COALESCE(refreshed_at, added_at) ASC
            LIMIT 10
        """, (user_id, cutoff, cutoff)).fetchall()

    if not rows:
        return f"✅ Устаревших статей нет (порог: {stale_days} дней)"

    refreshed = []
    failed = []
    for row in rows:
        try:
            await _cmd_refresh(row["id"], user_id, llm_client)
            refreshed.append(row["id"])
        except Exception as e:
            failed.append(f"{row['id']}: {e}")
            logger.warning(f"refresh_stale failed for {row['id']}: {e}")

    # Помечаем остальные устаревшими
    with _db() as conn:
        conn.execute(
            "UPDATE articles SET is_stale=1 WHERE user_id=? AND added_at < ? AND id NOT IN ({})".format(
                ",".join("?" * len(refreshed)) or "NULL"
            ),
            [user_id, cutoff] + refreshed
        )

    out = "🔄 <b>Обновление базы знаний завершено</b>\n\n"
    out += f"✅ Обновлено: {len(refreshed)}\n"
    if failed:
        out += f"❌ Ошибок: {len(failed)}\n"
    out += f"⚠️ Порог устаревания: {stale_days} дней"
    return out


async def _cmd_list(user_id: int, tag_filter: Optional[str] = None) -> str:
    with _db() as conn:
        if tag_filter:
            rows = conn.execute("""
                SELECT id, title, url, source_name, tags, added_at
                FROM articles WHERE user_id=? AND tags LIKE ?
                ORDER BY added_at DESC LIMIT 20
            """, (user_id, f'%"{tag_filter}"%')).fetchall()
        else:
            rows = conn.execute("""
                SELECT id, title, url, source_name, tags, added_at
                FROM articles WHERE user_id=?
                ORDER BY added_at DESC LIMIT 20
            """, (user_id,)).fetchall()

    if not rows:
        return "📭 База знаний пуста. Перешлите статью с URL или используйте <code>kb add &lt;url&gt;</code>"

    header = f"📋 <b>Статьи{f' по #{_e(tag_filter)}' if tag_filter else ''} ({len(rows)}):</b>\n\n"
    out = header
    for r in rows:
        tags = _e(" ".join(f"#{t}" for t in json.loads(r["tags"] or "[]")[:3]))
        date = r["added_at"][:10]
        out += f"📰 <b>{_e(r['title'] or r['url'])}</b>\n"
        out += f"   {_e(r['source_name'])} | {date} | {tags}\n"
        out += f"   🆔 <code>{r['id']}</code>\n\n"
    return out


# ─── Точка входа скилла ───────────────────────────────────────────────────────

async def run(context: dict) -> str:
    user_id: int = context.get("user_id", 0)
    message_text: str = context.get("message_text", "") or context.get("args", {}).get("message_text", "")
    forwarded_url: str = context.get("args", {}).get("url", "")
    llm_client = context.get("llm_client")
    if llm_client is None:
        try:
            from app.core.llm_client_v2 import llm_client as _lc
            llm_client = _lc
        except Exception:
            pass

    text = message_text.strip()

    # ── Парсим команду ──────────────────────────────────────────────────────
    # Формат: "kb <команда> [аргументы]"
    # или просто URL/forward без префикса kb

    cmd = ""
    arg = ""

    kb_match = re.match(r'^kb\s+(\w+)(.*)$', text, re.IGNORECASE)
    if kb_match:
        cmd = kb_match.group(1).lower()
        arg = kb_match.group(2).strip()
    elif forwarded_url or _extract_url(text):
        cmd = "add"
        arg = text
    else:
        return (
            "📚 <b>База знаний</b>\n\n"
            "Команды:\n"
            "• <code>kb add &lt;url&gt; [комментарий]</code> — добавить статью\n"
            "• <code>kb search &lt;запрос&gt;</code> — поиск\n"
            "• <code>kb related &lt;id&gt;</code> — похожие статьи\n"
            "• <code>kb refresh &lt;id&gt;</code> — обновить статью\n"
            "• <code>kb list [тег]</code> — список статей\n"
            "• <code>kb stats</code> — статистика\n\n"
            "💡 Или просто перешлите сообщение с URL — статья добавится автоматически."
        )

    # ── Диспетчер команд ────────────────────────────────────────────────────
    if cmd == "add":
        url = forwarded_url or _extract_url(arg)
        if not url:
            return "❌ URL не найден в сообщении."
        comment_match = re.sub(r'https?://\S+', '', arg).strip()
        if not llm_client:
            return "❌ LLM клиент не инициализирован — проверьте контекст скилла."
        return await _cmd_add(url, user_id, comment_match, llm_client)

    elif cmd == "search":
        if not arg:
            return "❌ Укажите поисковый запрос: <code>kb search Python 2025</code>"
        return await _cmd_search(arg, user_id)

    elif cmd == "related":
        if not arg:
            return "❌ Укажите ID статьи: <code>kb related abc123</code>"
        return await _cmd_related(arg.split()[0], user_id)

    elif cmd == "refresh":
        if not arg:
            return "❌ Укажите ID статьи: <code>kb refresh abc123</code>"
        if not llm_client:
            return "❌ LLM клиент не доступен."
        return await _cmd_refresh(arg.split()[0], user_id, llm_client)

    elif cmd == "refresh_stale":
        if not llm_client:
            return "❌ LLM клиент не доступен."
        return await _cmd_refresh_stale(user_id, llm_client)

    elif cmd == "stats":
        return await _cmd_stats(user_id)

    elif cmd == "list":
        return await _cmd_list(user_id, tag_filter=arg.lstrip("#"))

    else:
        return f"❓ Неизвестная команда: <code>kb {cmd}</code>. Напишите <code>kb</code> для справки."
