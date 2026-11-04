#!/usr/bin/env python3
"""Диагностика и обслуживание витрин: сессии оператора по витринам, проверка SHOWCASES, закрытие зависших.

Только стандартная библиотека: работает и в контейнере, и на хосте с системным python.

    python3 scripts/showcase_ops.py                      # status: витрины, сессии, предупреждения
    python3 scripts/showcase_ops.py close --tenant jurist                    # что БУДЕТ закрыто (ничего не меняет)
    python3 scripts/showcase_ops.py close --tenant jurist --yes              # закрыть все открытые сессии витрины
    python3 scripts/showcase_ops.py close --tenant jurist --older-hours 24 --yes   # только неактивные сутки
    python3 scripts/showcase_ops.py close --tenant jurist --client 12345 --yes     # одного клиента
    docker exec alex-nano-showcase python scripts/showcase_ops.py

Опции: --db путь (по умолчанию из DATABASE_URL, .env или data/bot.db), --env путь (по умолчанию .env).
Токены нигде не печатаются. Коды выхода: 0 — всё в порядке, 1 — есть предупреждения (status), 2 — ошибка.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

DEFAULT_DB = "data/bot.db"
STALE_HOURS = 24


def read_env(path: str) -> Dict[str, str]:
    """Простой разбор .env (KEY=VALUE, без подстановок); окружение процесса главнее файла."""
    out: Dict[str, str] = {}
    if path and os.path.exists(path):
        for ln in open(path, encoding="utf-8"):
            m = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)=(.*)$", ln.rstrip("\r\n"))
            if m:
                v = m.group(2).strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
                    v = v[1:-1]
                out[m.group(1)] = v
    for k in ("DATABASE_URL", "SHOWCASES"):
        if os.environ.get(k):
            out[k] = os.environ[k]
    return out


def db_path(env: Dict[str, str], override: Optional[str] = None) -> str:
    if override:
        return override
    url = env.get("DATABASE_URL", "")
    m = re.match(r"^sqlite(?:\+aiosqlite)?:///(.+)$", url)
    return m.group(1) if m else DEFAULT_DB


def parse_showcases(raw: str) -> Tuple[List[dict], Optional[str]]:
    """(витрины без токенов, ошибка). Пустой SHOWCASES — одна наследная витрина (tenant «»)."""
    if not raw.strip():
        return [{"slug": "", "operator_chat_id": 0, "access": "—", "collection": "corpus", "legacy": True}], None
    try:
        data = json.loads(raw)
    except ValueError as e:
        return [], f"SHOWCASES — неверный JSON: {e}"
    if not isinstance(data, list):
        return [], "SHOWCASES должен быть списком"
    out = []
    for i, it in enumerate(data):
        if not isinstance(it, dict):
            return [], f"SHOWCASES[{i}] — не объект"
        out.append({"slug": str(it.get("slug", "")), "operator_chat_id": int(it.get("operator_chat_id", 0) or 0),
                    "access": str(it.get("access", "invite")), "collection": str(it.get("collection", "")
                                                                                   or f"corpus_{it.get('slug', '')}")})
    return out, None


def sessions(con: sqlite3.Connection) -> List[sqlite3.Row]:
    con.row_factory = sqlite3.Row
    return con.execute(
        "SELECT tenant, status, COUNT(*) AS n, MIN(opened_at) AS first_opened, MAX(updated_at) AS last_update "
        "FROM operator_sessions GROUP BY tenant, status ORDER BY tenant, status").fetchall()


def warnings(shows: List[dict], rows: List[sqlite3.Row], now: datetime, con: sqlite3.Connection,
             stale_hours: int = STALE_HOURS) -> List[str]:
    w: List[str] = []
    slugs = {s["slug"] for s in shows}
    chats: Dict[int, List[str]] = {}
    for s in shows:
        if s["operator_chat_id"]:
            chats.setdefault(s["operator_chat_id"], []).append(s["slug"] or "default")
        elif not s.get("legacy"):
            w.append(f"витрина «{s['slug']}»: operator_chat_id=0 — эскалация выключена")
    for chat, names in chats.items():
        if len(names) > 1:
            w.append(f"одна группа оператора {chat} у нескольких витрин ({', '.join(names)}): темы и ответы смешаются")
    for r in rows:
        if r["status"] != "open":
            continue
        if r["tenant"] not in slugs:
            w.append(f"открытых сессий {r['n']} у витрины «{r['tenant']}», которой нет в конфигурации "
                     f"(после перехода на SHOWCASES старые сессии с tenant='' достаются ПЕРВОЙ витрине)")
    cut = (now - timedelta(hours=stale_hours)).isoformat(sep=" ")
    for r in con.execute("SELECT tenant, COUNT(*) AS n FROM operator_sessions WHERE status='open' AND updated_at < ? "
                         "GROUP BY tenant", (cut,)):
        arg = r["tenant"] or '""'
        w.append(f"витрина «{r['tenant']}»: {r['n']} открытых сессий без обновлений больше {stale_hours} ч — "
                 f"клиенты в них пишут оператору, а не боту (закрыть: close --tenant {arg} "
                 f"--older-hours {stale_hours} --yes)")
    return w


def render_status(shows: List[dict], rows: List[sqlite3.Row], warns: List[str], path: str) -> str:
    L = [f"БД: {path}", "", "Витрины (из SHOWCASES):"]
    for s in shows:
        L.append(f"  • {s['slug'] or '(наследная)'}: доступ={s['access']}, корпус={s['collection']}, "
                 f"оператор={s['operator_chat_id'] or 'выкл'}")
    L += ["", "Сессии оператора:"]
    if not rows:
        L.append("  нет")
    for r in rows:
        L.append(f"  • {r['tenant'] or '(пустой tenant)'} / {r['status']}: {r['n']} "
                 f"(открыта с {r['first_opened']}, последнее обновление {r['last_update']})")
    L += ["", "Предупреждения:"] + (["  " + x for x in warns] if warns else ["  нет"])
    return "\n".join(L)


def select_open(con: sqlite3.Connection, tenant: str, older_hours: Optional[int], client: Optional[int],
                now: datetime) -> List[sqlite3.Row]:
    con.row_factory = sqlite3.Row
    q, args = "SELECT tenant, client_id, thread_id, client_name, opened_at, updated_at FROM operator_sessions " \
              "WHERE status='open' AND tenant=?", [tenant]
    if older_hours is not None:
        q += " AND updated_at < ?"
        args.append((now - timedelta(hours=older_hours)).isoformat(sep=" "))
    if client is not None:
        q += " AND client_id=?"
        args.append(client)
    return con.execute(q + " ORDER BY updated_at", args).fetchall()


def close_sessions(con: sqlite3.Connection, picked: List[sqlite3.Row]) -> int:
    n = 0
    for r in picked:
        n += con.execute("UPDATE operator_sessions SET status='closed' WHERE tenant=? AND client_id=? AND status='open'",
                         (r["tenant"], r["client_id"])).rowcount
    con.commit()
    return n


def main(argv: Optional[List[str]] = None, now: Optional[datetime] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", default="")
    p.add_argument("--env", default=".env")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("status")
    c = sub.add_parser("close")
    c.add_argument("--tenant", required=True, help="slug витрины (для наследной — пустая строка)")
    c.add_argument("--older-hours", type=int, default=None)
    c.add_argument("--client", type=int, default=None)
    c.add_argument("--yes", action="store_true", help="выполнить (без него — только показать)")
    a = p.parse_args(argv)
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)  # в БД время UTC без зоны
    env = read_env(a.env)
    path = db_path(env, a.db)
    if not os.path.exists(path):
        print(f"БД не найдена: {path} (укажите --db)", file=sys.stderr)
        return 2
    con = sqlite3.connect(path)
    try:
        con.row_factory = sqlite3.Row
        if a.cmd == "close":
            picked = select_open(con, a.tenant, a.older_hours, a.client, now)
            for r in picked:
                print(f"  {r['tenant'] or '(пустой)'} клиент {r['client_id']} «{r['client_name']}» "
                      f"тема {r['thread_id']}, обновлена {r['updated_at']}")
            if not picked:
                print("Открытых сессий по условию нет.")
            elif a.yes:
                print(f"Закрыто: {close_sessions(con, picked)}. Клиенты снова пишут боту.")
            else:
                print(f"Будет закрыто: {len(picked)}. Добавьте --yes, чтобы выполнить.")
            return 0
        shows, err = parse_showcases(env.get("SHOWCASES", ""))
        if err:
            print(err, file=sys.stderr)
            return 2
        rows = sessions(con)
        warns = warnings(shows, rows, now, con)
        print(render_status(shows, rows, warns, path))
        return 1 if warns else 0
    except sqlite3.Error as e:
        print(f"ошибка БД: {e}", file=sys.stderr)
        return 2
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
