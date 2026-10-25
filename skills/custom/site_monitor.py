"""
Skill: site_monitor
Description: Мониторинг доступности сайтов с периодическими проверками и защитой от флуд-алертов.
Workflow:
  1. Отправьте ссылку → бот запросит интервал (1-360 мин)
  2. Отправьте число → мониторинг активируется
  3. Фоновый цикл проверяет сайты. При падении → алерт в личку (не чаще 1 раза/час на сайт)
  4. При восстановлении → опциональное уведомление 🟢
Usage: /monitor add https://example.com
       Или просто отправьте ссылку.
       Команды: monitor list | monitor stop <id> | monitor
"""
import os
import sqlite3
import asyncio
import httpx
import logging
import re
from datetime import datetime
from contextlib import contextmanager
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)

SKILL_NAME = "site_monitor"
SKILL_DESCRIPTION = (
    "Мониторинг доступности сайтов. Периодические проверки (1-360 мин), "
    "автоматическое оповещение в Telegram при падении. Встроенная защита от флуда."
)
SKILL_VERSION = "1.1.0"
SKILL_CATEGORY = "utility"
SKILL_AUTHOR = "User"
SKILL_COMMANDS = ["monitor"]

# ── Настройки флуд-защиты ─────────────────────────────────────────────────
ALERT_COOLDOWN_MINUTES = 60  # Минимум минут между повторными алертами по одному сайту

# ── Состояние скилла ──────────────────────────────────────────────────────
_pending_adds: Dict[int, str] = {}  # user_id -> url
_monitor_task: Optional[asyncio.Task] = None
_notify_func = None
_notify_lock = asyncio.Lock()

# ── База данных ───────────────────────────────────────────────────────────
_DB_PATH = os.path.join(os.environ.get("DATA_DIR", "data"), "site_monitor.db")

@contextmanager
def _db():
    os.makedirs(os.path.dirname(_DB_PATH) or ".", exist_ok=True)
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS monitors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            url TEXT NOT NULL,
            interval_min INTEGER NOT NULL CHECK(interval_min BETWEEN 1 AND 360),
            is_active INTEGER DEFAULT 1,
            last_status TEXT DEFAULT 'pending',
            last_checked TEXT,
            last_alert_at TEXT,
            added_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
    """)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

# ── Проверка URL ──────────────────────────────────────────────────────────
async def _check_url(url: str) -> tuple[bool, str]:
    """Проверяет доступность URL. Возвращает (ok, status_text)."""
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True, http2=True) as client:
            resp = await client.head(url)
            return resp.status_code < 400, f"HTTP {resp.status_code}"
    except httpx.ConnectError:
        return False, "Соединение отклонено / таймаут"
    except httpx.HTTPStatusError as e:
        return False, f"HTTP {e.response.status_code}"
    except httpx.RequestError as e:
        return False, str(e)[:60]
    except Exception as e:
        return False, f"Неизвестная ошибка: {e}"

# ── Фоновый монитор ───────────────────────────────────────────────────────
async def _safe_notify(user_id: int, text: str):
    """Обёртка над _notify_func с защитой от 429/403."""
    if not _notify_func:
        return
    try:
        await _notify_func(user_id, text)
    except Exception as e:
        err = str(e).lower()
        if "403" in err or "forbidden" in err:
            logger.warning(f"Bot blocked by user {user_id}. Disabling all their monitors.")
            with _db() as c:
                c.execute("UPDATE monitors SET is_active=0 WHERE user_id=?", (user_id,))
        elif "429" in err or "too many" in err:
            logger.warning("Telegram rate limit hit. Waiting 30s before retrying alert.")
            await asyncio.sleep(30)
            try:
                await _notify_func(user_id, text)
            except Exception:
                pass
        else:
            logger.error(f"Failed to send alert to {user_id}: {e}")

async def _monitor_loop():
    """Цикл проверки сайтов каждую минуту."""
    while True:
        try:
            async with _notify_lock:
                with _db() as conn:
                    monitors = conn.execute(
                        "SELECT id, user_id, url, interval_min, last_status, last_checked, last_alert_at "
                        "FROM monitors WHERE is_active=1"
                    ).fetchall()

            now = datetime.now()
            for m in monitors:
                last = datetime.fromisoformat(m["last_checked"]) if m["last_checked"] else datetime.min
                if (now - last).total_seconds() < m["interval_min"] * 60:
                    continue

                is_ok, status = await _check_url(m["url"])
                with _db() as conn:
                    conn.execute(
                        "UPDATE monitors SET last_status=?, last_checked=? WHERE id=?",
                        ("ok" if is_ok else "error", now.isoformat(), m["id"])
                    )

                prev_status = m["last_status"]
                if not is_ok:
                    last_alert = datetime.fromisoformat(m["last_alert_at"]) if m["last_alert_at"] else datetime.min
                    if (now - last_alert).total_seconds() >= ALERT_COOLDOWN_MINUTES * 60:
                        msg = (
                            f"🚨 <b>Сайт недоступен!</b>\n\n"
                            f"🌐 <code>{m['url']}</code>\n"
                            f"⚠️ Ошибка: {status}\n"
                            f"🕒 Проверено: {now.strftime('%H:%M')}\n"
                            f"🔁 Следующий алерт не ранее чем через {ALERT_COOLDOWN_MINUTES} мин."
                        )
                        await _safe_notify(m["user_id"], msg)
                        with _db() as c:
                            c.execute("UPDATE monitors SET last_alert_at=? WHERE id=?", (now.isoformat(), m["id"]))
                elif is_ok and prev_status == "error":
                    msg = (
                        f"🟢 <b>Сайт восстановлен</b>\n\n"
                        f"🌐 <code>{m['url']}</code>\n"
                        f"✅ Статус: OK\n"
                        f"🕒 Проверено: {now.strftime('%H:%M')}"
                    )
                    await _safe_notify(m["user_id"], msg)
                    with _db() as c:
                        c.execute("UPDATE monitors SET last_alert_at=NULL WHERE id=?", (m["id"],))

            await asyncio.sleep(60)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Monitor loop error: {e}")
            await asyncio.sleep(60)

def _ensure_monitor_running(notify_func):
    global _monitor_task, _notify_func
    if (_monitor_task is None or _monitor_task.done()) and notify_func:
        _notify_func = notify_func
        _monitor_task = asyncio.create_task(_monitor_loop())
        logger.info("🌐 Site monitor background loop started.")

# ── Утилиты ───────────────────────────────────────────────────────────────
_URL_RE = re.compile(r'https?://[^\s)]>')

def _extract_urls(text: str) -> list[str]:
    return _URL_RE.findall(text)

# ── Точка входа ───────────────────────────────────────────────────────────
async def run(context: Dict[str, Any]) -> str:
    user_id = context.get("user_id", 0)
    text = (context.get("message_text") or context.get("args", {}).get("message_text", "")).strip()
    notify_func = context.get("send_message") or context.get("notify_user")
    _ensure_monitor_running(notify_func)

    if not text:
        return (
            "🌐 <b>Мониторинг сайтов</b>\n\n"
            "Как использовать:\n"
            "• Отправьте ссылку → бот запросит интервал\n"
            "• <code>monitor list</code> — список мониторингов\n"
            "• <code>monitor stop &lt;id&gt;</code> — остановить\n\n"
            "Интервал проверки: от 1 до 360 минут. Защита от флуда: 1 алерт/час."
        )

    # 1. Ожидание интервала
    if user_id in _pending_adds:
        try:
            interval = int(text)
            if not (1 <= interval <= 360):
                return "❌ Интервал должен быть от 1 до 360 минут."
            
            url = _pending_adds.pop(user_id)
            with _db() as conn:
                conn.execute(
                    "INSERT INTO monitors(user_id, url, interval_min) VALUES(?,?,?)",
                    (user_id, url, interval)
                )
            return (
                f"✅ Мониторинг активирован!\n"
                f"🌐 <code>{url}</code>\n"
                f"⏱ Интервал: {interval} мин.\n\n"
                "Я пришлю уведомление в личку, если сайт перестанет отвечать."
            )
        except ValueError:
            return "❌ Отправьте только число (например: <code>5</code> или <code>30</code>)."

    # 2. Команды
    parts = text.split()
    cmd = parts[0].lower() if parts else ""
    sub = ""
    if cmd == "monitor" and len(parts) > 1:
        sub = parts[1].lower()
    elif _extract_urls(text):
        sub = "add"

    if sub == "add" or _extract_urls(text):
        urls = _extract_urls(text)
        if not urls:
            return "❌ Не удалось найти ссылку. Отправьте URL, начиная с <code>http://</code> или <code>https://</code>."
        
        url = urls[0]
        _pending_adds[user_id] = url
        return (
            f"📝 Сайт <code>{url}</code> готов к добавлению.\n\n"
            "Укажите периодичность проверки в минутах (от 1 до 360):\n"
            "Пример: <code>5</code>, <code>15</code>, <code>60</code>"
        )

    elif sub == "list":
        with _db() as conn:
            rows = conn.execute(
                "SELECT id, url, interval_min, last_status, last_checked FROM monitors WHERE user_id=? ORDER BY id DESC",
                (user_id,)
            ).fetchall()
        if not rows:
            return "📭 У вас нет активных мониторингов."
        
        out = "📋 <b>Ваши мониторинги:</b>\n\n"
        for r in rows:
            icon = "🟢" if r["last_status"] == "ok" else ("🔴" if r["last_status"] == "error" else "⏳")
            last = r["last_checked"][:16] if r["last_checked"] else "—"
            out += f"🆔 <code>{r['id']}</code> | {icon} <code>{r['url']}</code>\n   ⏱ {r['interval_min']} мин | Последняя: {last}\n\n"
        return out

    elif sub == "stop":
        if len(parts) < 3:
            return "❌ Укажите ID: <code>monitor stop &lt;id&gt;</code>"
        try:
            mid = int(parts[2])
        except ValueError:
            return "❌ ID должен быть числом."
        
        with _db() as conn:
            cur = conn.execute("UPDATE monitors SET is_active=0 WHERE id=? AND user_id=?", (mid, user_id))
            if cur.rowcount == 0:
                return f"❌ Мониторинг #{mid} не найден или не принадлежит вам."
        return f"🛑 Мониторинг #{mid} остановлен."

    return "❓ Неизвестная команда. Напишите <code>monitor</code> для справки."