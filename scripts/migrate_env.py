#!/usr/bin/env python3
"""Приводит серверный .env к текущей раскладке .env.example, сохраняя ВАШИ значения.

Запуск (из каталога проекта, где лежат .env и свежий .env.example):
    python3 migrate_env.py            # читает .env, пишет .env.new, .env не трогает
    python3 migrate_env.py --old путь --template путь --out путь

Что делает
- берёт порядок/комментарии из .env.example; ваши значения (токены, ключи, модели, промпты,
  ID групп) переносятся дословно, без перекодировки кавычек и \\n;
- ключи, которых у вас не было, добавляются со значением по умолчанию (плейсхолдеры
  «your_..._here» — закомментированными);
- дубли ключей: действует ПОСЛЕДНЕЕ значение (так читает dotenv), остальные отбрасываются;
- APP_VERSION берётся из шаблона (закреплённая в .env версия устаревает);
- ваши ключи, которых нет в шаблоне, дописываются в конец;
- значения секретов в отчёт не выводятся.
"""
import argparse
import os
import re
import sys

ACTIVE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
COMMENTED = re.compile(r"^#\s?([A-Z][A-Z0-9_]*)=(.*)$")
PLACEHOLDER = re.compile(r"your_.*_here|^123456:AAA")
FORCE_FROM_TEMPLATE = {"APP_VERSION"}


def read_lines(path):
    with open(path, encoding="utf-8") as f:
        return [ln.rstrip("\r\n") for ln in f]


def parse_old(lines):
    values, order, dups, warnings = {}, [], {}, []
    for n, ln in enumerate(lines, 1):
        s = ln.strip()
        if not s or s.startswith("#"):
            continue
        m = ACTIVE.match(s)
        if not m:
            warnings.append(f"строка {n}: не «KEY=VALUE» — пропущена (многострочное значение?)")
            continue
        k, v = m.group(1), m.group(2)
        if len(v) > 1 and v[0] in "\"'" and v[-1] != v[0]:
            warnings.append(f"{k}: кавычка не закрыта на той же строке — многострочные значения не поддерживаются")
        if k in values:
            dups.setdefault(k, [values[k]]).append(v)
        else:
            order.append(k)
        values[k] = v
    return values, order, dups, warnings


def merge(template_lines, old, ):
    active_idx, commented_idx = {}, {}
    for i, ln in enumerate(template_lines):
        m = ACTIVE.match(ln)
        if m:
            active_idx.setdefault(m.group(1), i)
            continue
        m = COMMENTED.match(ln)
        if m:
            commented_idx.setdefault(m.group(1), i)

    out = list(template_lines)
    kept, added, activated, commented_placeholders = [], [], [], []
    used = set()
    for k, v in old.items():
        if k in FORCE_FROM_TEMPLATE and k in active_idx:
            used.add(k)
            continue
        if k in active_idx:
            out[active_idx[k]] = f"{k}={v}"
            kept.append(k)
            used.add(k)
        elif k in commented_idx:
            out[commented_idx[k]] = f"{k}={v}"
            activated.append(k)
            used.add(k)

    for k, i in active_idx.items():
        if k in old:
            continue
        val = ACTIVE.match(template_lines[i]).group(2)
        if PLACEHOLDER.search(val):
            out[i] = "# " + template_lines[i]
            commented_placeholders.append(k)
        elif k not in FORCE_FROM_TEMPLATE:
            added.append(k)

    unknown = [k for k in old if k not in used]
    if unknown:
        out += ["", "# ============================================",
                "# Прочее (из прежнего .env; в .env.example не описано)",
                "# ============================================"]
        out += [f"{k}={old[k]}" for k in unknown]
    return out, kept, added, activated, commented_placeholders, unknown


def lint(old):
    warns = []
    planner = old.get("PLANNER_SYSTEM_PROMPT", "")
    if planner.strip().strip("\"'"):
        # кавычки в .env могут быть экранированы (\"plan\"), поэтому ищем слова, а не "plan"
        need = {"plan": r"\bplan\b", "call_llm": r"\bcall_llm\b", "respond": r"\brespond\b", "params": r"\bparams\b"}
        missing = [name for name, rx in need.items() if not re.search(rx, planner)]
        if missing:
            warns.append(
                "PLANNER_SYSTEM_PROMPT ЗАМЕНЯЕТ встроенный промпт целиком, но не содержит схему DSL "
                f"(нет: {', '.join(missing)}). Приложение схему отдельно не передаёт — Planner не знает "
                "имён параметров (prompt/text/content...), программы не проходят и работает "
                "запасной вариант. Уберите переменную — применится встроенный промпт."
            )
    show = old.get("SHOWCASE_SYSTEM_PROMPT", "")
    if show.strip().strip("\"'") and "БАЗ" not in show.upper():
        warns.append(
            "SHOWCASE_SYSTEM_PROMPT не упоминает базу знаний. Поиск по корпусу всегда возвращает "
            "ближайшие фрагменты (порога релевантности нет), поэтому единственная защита от ответов "
            "«из головы» — этот промпт. Добавьте правило «отвечай строго по блоку БАЗА ЗНАНИЙ; "
            "если ответа там нет — так и скажи»."
        )
    emb = old.get("EMBEDDING_MODEL", "").strip("\"'")
    if emb == "sentence-transformers/all-MiniLM-L6-v2":
        warns.append(
            "EMBEDDING_MODEL=all-MiniLM-L6-v2 — устаревшее значение из старого шаблона (оно не читалось, "
            "модель английская): теперь игнорируется. Для русского задайте "
            "intfloat/multilingual-e5-large (~3 ГБ ОЗУ) или оставьте paraphrase-multilingual-MiniLM-L12-v2"
        )
    if emb.startswith("intfloat/multilingual-e5-large") and not (old.get("BOT_MEM_LIMIT") and old.get("SHOWCASE_MEM_LIMIT")):
        warns.append(
            "EMBEDDING_MODEL=multilingual-e5-large требует ~3 ГБ ОЗУ на процесс, а потолок контейнеров по "
            "умолчанию 1280m — задайте BOT_MEM_LIMIT и SHOWCASE_MEM_LIMIT (например 3500m), проверьте `free -h`"
        )
    chunk = old.get("CORPUS_CHUNK_SIZE", "").strip("\"'")
    if chunk.isdigit() and int(chunk) > 600 and (not emb or "MiniLM" in emb):
        warns.append(
            f"CORPUS_CHUNK_SIZE={chunk}: модель эмбеддингов (MiniLM-L12) видит только первые 128 токенов "
            "(~400–500 символов), остальная часть каждого чанка не участвует в поиске. Поставьте 450 "
            "(CORPUS_CHUNK_OVERLAP=80, CORPUS_TOP_K=8) и перезагрузите документы"
        )
    if old.get("BYOK_ENABLED", "").lower() == "true" and not old.get("ENCRYPTION_KEY", "").strip("\"'"):
        warns.append("BYOK_ENABLED=true, но ENCRYPTION_KEY пуст — /byok и /providers не заработают")
    if (old.get("SHOWCASE_BOT_TOKEN", "") and not old.get("SHOWCASES", "").strip("\"'")
            and old.get("SHOWCASE_OPEN_ACCESS", "").lower() == "true"):
        warns.append("Витрина (одна, без SHOWCASES) открыта ВСЕМ: приглашения не действуют. "
                     "Для доступа по приглашению задайте SHOWCASES c access=invite")
    return warns


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--old", default=".env")
    ap.add_argument("--template", default=".env.example")
    ap.add_argument("--out", default=".env.new")
    a = ap.parse_args()

    for p in (a.old, a.template):
        if not os.path.isfile(p):
            sys.exit(f"Не найден файл: {p}")
    if os.path.abspath(a.out) == os.path.abspath(a.old):
        sys.exit("--out не должен совпадать с исходным .env")

    old, order, dups, warns = parse_old(read_lines(a.old))
    tpl = read_lines(a.template)
    out, kept, added, activated, placeholders, unknown = merge(tpl, old)

    # самопроверка: каждое ваше значение попало в результат дословно
    produced, _, _, _ = parse_old(out)
    bad = [k for k, v in old.items() if k not in FORCE_FROM_TEMPLATE and produced.get(k) != v]
    if bad:
        sys.exit("Самопроверка не пройдена, значения изменились: " + ", ".join(bad))

    with open(a.out, "w", encoding="utf-8") as f:
        f.write("\n".join(out).rstrip("\n") + "\n")
    os.chmod(a.out, 0o600)

    print(f"Готово: {a.out} (права 600). Исходный {a.old} не изменён.\n")
    print(f"Перенесено ваших значений: {len(kept) + len(activated)}")
    if activated:
        print("  из закомментированных примеров шаблона: " + ", ".join(activated))
    if added:
        print("Добавлено со значением по умолчанию: " + ", ".join(added))
    if placeholders:
        print("Не заданы у вас, оставлены закомментированными: " + ", ".join(placeholders))
    if unknown:
        print("Нет в шаблоне, дописаны в конец: " + ", ".join(unknown))
    if "APP_VERSION" in old:
        tv = next((ACTIVE.match(t).group(2) for t in tpl if ACTIVE.match(t) and ACTIVE.match(t).group(1) == "APP_VERSION"), "?")
        print(f"APP_VERSION: {old['APP_VERSION']} → {tv} (из шаблона)")
    for k, vals in dups.items():
        same = len(set(vals)) == 1
        print(f"ДУБЛЬ {k}: {len(vals)} раза, значения {'одинаковые' if same else 'РАЗНЫЕ'} — "
              "действует последнее" + ("" if same else "; предыдущие отброшены"))
    found = warns + lint(old)
    if found:
        print("\nПроверка:")
        for w in found:
            print(" • " + w)
    print("\nДальше: просмотрите различия  diff <(sort .env) <(sort .env.new) | head -50\n"
          "        затем  mv .env .env.bak && mv .env.new .env  и пересоздайте контейнеры.")


if __name__ == "__main__":
    main()
