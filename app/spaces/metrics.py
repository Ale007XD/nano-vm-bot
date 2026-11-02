"""Показатели пространства: что считаем (расходы, проданные штуки, часы…) и как это показывать.

Все значения хранятся в `ledger_entries` целым ×100 (деньги — копейки, штуки — сотые доли), а единица
измерения — в колонке `currency`. Показатель `expense` есть всегда; свои описываются в
`settings["metrics"]` пространства: [{"key","title","unit","kind": "money"|"count"}].
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Callable, Dict, List, Tuple

from app.spaces import ledger

EXPENSE = "expense"
KINDS = ("money", "count")
MAX_METRICS = 20
MAX_CATEGORIES = 30
MAX_VALUE = Decimal(10) ** 9
_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


@dataclass(frozen=True)
class Metric:
    key: str
    title: str
    unit: str  # валюта (money) или единица («шт», «ч»)
    kind: str = "count"
    categories: Tuple[str, ...] = ()  # необязательный список категорий показателя


def default_expense(currency: str) -> Metric:
    return Metric(EXPENSE, "Расходы", currency.upper(), "money")


def parse_metrics(raw: Any) -> Tuple[List[Metric], List[str]]:
    """Проверить описание показателей. Возвращает (годные, замечания); негодные пропускаются."""
    out: List[Metric] = []
    errors: List[str] = []
    if raw is None:
        return out, errors
    if not isinstance(raw, list):
        return out, ["metrics должен быть списком"]
    seen = {EXPENSE}
    for i, it in enumerate(raw[:MAX_METRICS]):
        if not isinstance(it, dict):
            errors.append(f"metrics[{i}]: ожидался объект")
            continue
        key = str(it.get("key") or "").strip().lower()
        title = " ".join(str(it.get("title") or "").split())[:60]
        unit = " ".join(str(it.get("unit") or "").split())[:8]
        kind = str(it.get("kind") or "count").lower()
        if not _KEY_RE.match(key) or key in seen:
            errors.append(f"metrics[{i}]: ключ «{key}» некорректен или занят (латиница/цифры/_, с буквы)")
        elif not title or not unit or kind not in KINDS:
            errors.append(f"metrics[{i}] ({key}): нужны title, unit (до 8 символов) и kind money|count")
        else:
            raw_cats = it.get("categories")
            cats = tuple(dict.fromkeys(
                " ".join(str(c).split())[:50] for c in raw_cats if str(c).strip())) if isinstance(raw_cats, list) else ()
            if len(cats) > MAX_CATEGORIES:
                errors.append(f"metrics[{i}] ({key}): категорий больше {MAX_CATEGORIES} — лишние отброшены")
            seen.add(key)
            out.append(Metric(key, title, unit, kind, cats[:MAX_CATEGORIES]))
    if len(raw) > MAX_METRICS:
        errors.append(f"показателей больше {MAX_METRICS} — лишние отброшены")
    return out, errors


def metrics_for(space) -> Dict[str, Metric]:
    """Показатели пространства: расходы + описанные в настройках."""
    result = {EXPENSE: default_expense(space.currency)}
    raw = space.settings.get("metrics") if isinstance(space.settings, dict) else None
    for m in parse_metrics(raw)[0]:
        result[m.key] = m
    return result


def format_count(stored: int, unit: str) -> str:
    v = Decimal(int(stored)) / 100
    num = f"{int(v):,}".replace(",", " ") if v == v.to_integral() else f"{v:.2f}".replace(".", ",")
    return f"{num} {unit}"


def formatter(metric: Metric) -> Callable[[int], str]:
    if metric.kind == "money":
        return lambda v: ledger.format_money(v, metric.unit)
    return lambda v: format_count(v, metric.unit)


def to_stored(metric: Metric, value: Any) -> int:
    """Значение из текста/LLM → целое ×100. LedgerError, если оно не годится."""
    if metric.kind == "money":
        return ledger.to_minor(value)
    try:
        d = Decimal(str(value).replace(",", ".").replace(" ", "").replace(" ", ""))
    except (InvalidOperation, ValueError):
        raise ledger.LedgerError("не удалось понять число") from None
    if not d.is_finite() or d <= 0:
        raise ledger.LedgerError("число должно быть больше нуля")
    if d > MAX_VALUE:
        raise ledger.LedgerError("слишком большое число — проверьте, не опечатка ли")
    stored = int((d * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    if stored <= 0:
        raise ledger.LedgerError("число слишком мало")
    return stored
