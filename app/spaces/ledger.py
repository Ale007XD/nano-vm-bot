"""Журнал расходов/доходов пространства: запись, отмена, месячная сводка и отчёт.

Суммы — целые минорные единицы (копейки), валюта с двумя знаками. Время хранится в UTC,
границы месяца считаются в часовом поясе пространства. Сводка считается SQL-агрегатами,
а не LLM: цифры в отчёте не могут быть «придуманы».
"""
from __future__ import annotations

import html
from calendar import monthrange
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Callable, Dict, List, Optional, Tuple

import pytz
from sqlalchemy import func

from app.spaces.models import Category, LedgerEntry
from app.spaces.repo import SpaceAccessError, SpaceScope

MAX_AMOUNT_MINOR = 10**12  # 10 млрд в основных единицах — заведомо опечатка выше
FALLBACK_CATEGORY = "Прочее"
DEFAULT_CATEGORIES: Dict[str, List[str]] = {
    "family": ["Продукты", "Дом", "Транспорт", "Здоровье", "Дети", "Одежда", "Связь", "Развлечения", FALLBACK_CATEGORY],
    "generic": [FALLBACK_CATEGORY],
}
CURRENCY_SIGN = {"RUB": "₽", "USD": "$", "EUR": "€", "THB": "฿", "KZT": "₸", "UAH": "₴", "BYN": "Br"}


class LedgerError(ValueError):
    """Некорректная запись (сообщение — для пользователя)."""


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ----------------------------- деньги -------------------------------------- #
def to_minor(value: Any) -> int:
    """Сумма в основных единицах (str/int/float/Decimal) → минорные; > 0 и разумный предел."""
    try:
        d = Decimal(str(value).replace(",", ".").replace(" ", "").replace(" ", ""))
    except (InvalidOperation, ValueError):
        raise LedgerError("не удалось понять сумму") from None
    if not d.is_finite():
        raise LedgerError("не удалось понять сумму")
    if d <= 0:
        raise LedgerError("сумма должна быть больше нуля")
    if d * 100 > MAX_AMOUNT_MINOR:  # до quantize: на огромных числах он падает с InvalidOperation
        raise LedgerError("слишком большая сумма — проверьте, не опечатка ли")
    minor = int((d * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    if minor <= 0:
        raise LedgerError("сумма слишком мала")
    return minor


def format_money(minor: int, currency: str = "RUB") -> str:
    sign = "-" if minor < 0 else ""
    minor = abs(int(minor))
    whole, frac = divmod(minor, 100)
    num = f"{whole:,}".replace(",", " ") + (f",{frac:02d}" if frac else "")
    return f"{sign}{num} {CURRENCY_SIGN.get(currency.upper(), currency)}"


# ----------------------------- категории ----------------------------------- #
def category_names(kind: str, settings: Optional[dict] = None) -> List[str]:
    custom = (settings or {}).get("categories")
    if isinstance(custom, list):
        names = [str(n).strip()[:50] for n in custom if str(n).strip()]
        if names:
            return names
    return list(DEFAULT_CATEGORIES.get(kind, DEFAULT_CATEGORIES["generic"]))


async def ensure_categories(
    scope: SpaceScope, space_kind: str, space_settings: Optional[dict] = None, *,
    kind: str = "expense", names: Optional[List[str]] = None,
) -> List[Category]:
    """Создать недостающие категории (идемпотентно). kind — «expense» или ключ показателя;
    names — свой список (для показателей), иначе набор расходов по виду пространства."""
    wanted = names if names is not None else category_names(space_kind, space_settings)
    have = {c.name_key for c in await scope.list(Category, Category.kind == kind)}
    for n, name in enumerate(wanted):
        if name.casefold() not in have:
            have.add(name.casefold())
            await scope.add(Category(kind=kind, name=name, name_key=name.casefold(), sort=n))
    return await scope.list(Category, Category.kind == kind, order_by=(Category.sort, Category.id))


async def resolve_category(
    scope: SpaceScope, name: Optional[str], *, kind: str = "expense", fallback: bool = True
) -> Optional[Category]:
    """Категория по названию (без регистра; точное совпадение, затем по началу слова).
    Не найдена: для расходов — «Прочее» (fallback), для остальных показателей — None.
    Новые категории из текста LLM не создаются — список задаёт пространство."""
    cats = await scope.list(Category, Category.kind == kind, order_by=(Category.sort, Category.id))
    key = (name or "").strip().casefold()
    if key:
        for c in cats:
            if c.name_key == key:
                return c
        for c in cats:
            if len(key) >= 3 and (c.name_key.startswith(key) or key.startswith(c.name_key)):
                return c
    if not fallback:
        return None
    for c in cats:
        if c.name_key == FALLBACK_CATEGORY.casefold():
            return c
    return await scope.add(Category(kind=kind, name=FALLBACK_CATEGORY,
                                    name_key=FALLBACK_CATEGORY.casefold(), sort=999))


# ----------------------------- записи -------------------------------------- #
async def add_entry(
    scope: SpaceScope, amount: Any, *, currency: str, category: Optional[str] = None, note: str = "",
    occurred_at: Optional[datetime] = None, source: str = "text", raw_text: str = "",
    metric: str = "expense", stored: Optional[int] = None,
) -> LedgerEntry:
    """Записать расход (или значение другого показателя). amount — в основных единицах (или
    stored — уже готовое целое ×100); occurred_at — UTC (naive) или aware."""
    if occurred_at is not None and occurred_at.tzinfo is not None:
        occurred_at = occurred_at.astimezone(timezone.utc).replace(tzinfo=None)
    is_expense = metric == "expense"
    cat = await resolve_category(scope, category, kind="expense" if is_expense else metric, fallback=is_expense)
    return await scope.add(LedgerEntry(
        author_id=scope.user_id, metric=metric, amount_minor=stored if stored is not None else to_minor(amount),
        currency=(currency.upper() if metric == "expense" else currency)[:8],  # единицы вроде «шт» — как заданы
        category_id=cat.id if cat else None, note=" ".join((note or "").split())[:200],
        occurred_at=occurred_at or _now(), source="voice" if source == "voice" else "text",
        raw_text=(raw_text or "")[:2000],
    ))


async def undo_last(scope: SpaceScope) -> Optional[LedgerEntry]:
    """Удалить последнюю запись ТЕКУЩЕГО пользователя (по времени создания); None, если записей нет."""
    rows = await scope.list(LedgerEntry, LedgerEntry.author_id == scope.user_id,
                            order_by=(LedgerEntry.id.desc(),), limit=1)
    if not rows:
        return None
    await scope.delete(LedgerEntry, rows[0].id)
    return rows[0]


async def delete_entry(scope: SpaceScope, entry_id: int) -> bool:
    """Удалить запись: автор — свою, админ/владелец — любую в пространстве."""
    entry = await scope.get(LedgerEntry, entry_id)
    if entry is None:
        return False
    if entry.author_id != scope.user_id and not scope.can("admin"):
        raise SpaceAccessError("удалять чужие записи могут только администраторы")
    return await scope.delete(LedgerEntry, entry_id)


async def list_entries(scope: SpaceScope, start: datetime, end: datetime, limit: int = 50) -> List[LedgerEntry]:
    return await scope.list(LedgerEntry, LedgerEntry.occurred_at >= start, LedgerEntry.occurred_at < end,
                            order_by=(LedgerEntry.occurred_at.desc(), LedgerEntry.id.desc()), limit=limit)


# ----------------------------- сводка -------------------------------------- #
def month_bounds(year: int, month: int, tz: str) -> Tuple[datetime, datetime]:
    """[начало, конец) месяца в UTC (naive) для границ по местному времени пространства."""
    if not (1 <= month <= 12):
        raise LedgerError("месяц должен быть от 1 до 12")
    try:
        zone = pytz.timezone(tz or "UTC")
    except pytz.UnknownTimeZoneError:
        raise LedgerError("неизвестный часовой пояс") from None
    ny, nm = (year + 1, 1) if month == 12 else (year, month + 1)
    start, end = zone.localize(datetime(year, month, 1)), zone.localize(datetime(ny, nm, 1))
    to_utc = lambda d: d.astimezone(timezone.utc).replace(tzinfo=None)  # noqa: E731
    return to_utc(start), to_utc(end)


def previous_month(year: int, month: int) -> Tuple[int, int]:
    return (year - 1, 12) if month == 1 else (year, month - 1)


@dataclass
class PeriodSummary:
    metric: str
    unit: str
    label: str = ""
    total: int = 0
    count: int = 0
    prev_total: int = 0
    by_category: List[Tuple[str, int]] = field(default_factory=list)  # по убыванию суммы
    by_author: List[Tuple[int, int]] = field(default_factory=list)  # (telegram id, сумма)
    biggest: List[LedgerEntry] = field(default_factory=list)
    days: int = 1


@dataclass
class MonthSummary(PeriodSummary):
    year: int = 0
    month: int = 0
    currency: str = ""


async def summarize(
    scope: SpaceScope, *, metric: str, unit: str, start: datetime, end: datetime,
    prev_start: Optional[datetime] = None, prev_end: Optional[datetime] = None, label: str = "", days: int = 1,
) -> PeriodSummary:
    """Сводка показателя за [start, end) (UTC naive) в единице unit; записи в других единицах не входят."""
    cur = unit.upper() if metric == "expense" else unit

    def window(a, b):
        return (LedgerEntry.metric == metric, LedgerEntry.currency == cur,
                LedgerEntry.occurred_at >= a, LedgerEntry.occurred_at < b)

    async def total_of(w) -> Tuple[int, int]:
        r = await scope.rows(LedgerEntry, func.coalesce(func.sum(LedgerEntry.amount_minor), 0), func.count(), where=w)
        return int(r[0][0]), int(r[0][1])

    base = window(start, end)
    total, count = await total_of(base)
    prev_total = (await total_of(window(prev_start, prev_end)))[0] if prev_start and prev_end else 0
    s = PeriodSummary(metric=metric, unit=cur, label=label, total=total, count=count, prev_total=prev_total, days=days)
    if count:
        names = {c.id: c.name for c in await scope.list(Category)}
        by_cat = await scope.rows(LedgerEntry, LedgerEntry.category_id, func.sum(LedgerEntry.amount_minor),
                                  where=base, group_by=(LedgerEntry.category_id,),
                                  order_by=(func.sum(LedgerEntry.amount_minor).desc(),))
        if any(cid is not None for cid, _ in by_cat):  # у показателей без категорий блока нет
            s.by_category = [(names.get(cid, "Без категории"), int(v)) for cid, v in by_cat]
        by_auth = await scope.rows(LedgerEntry, LedgerEntry.author_id, func.sum(LedgerEntry.amount_minor),
                                   where=base, group_by=(LedgerEntry.author_id,),
                                   order_by=(func.sum(LedgerEntry.amount_minor).desc(),))
        s.by_author = [(int(a), int(v)) for a, v in by_auth]
        s.biggest = await scope.list(LedgerEntry, *base, order_by=(LedgerEntry.amount_minor.desc(), LedgerEntry.id), limit=3)
    return s


async def monthly_summary(scope: SpaceScope, year: int, month: int, *, tz: str, currency: str) -> MonthSummary:
    """Сводка расходов за месяц по валюте пространства. Записи в других валютах в сумму не входят."""
    start, end = month_bounds(year, month, tz)
    py, pm = previous_month(year, month)
    ps, pe = month_bounds(py, pm, tz)
    s = await summarize(scope, metric="expense", unit=currency, start=start, end=end, prev_start=ps, prev_end=pe,
                        days=monthrange(year, month)[1])
    return MonthSummary(**{**s.__dict__, "year": year, "month": month, "currency": s.unit})


MONTHS_RU = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь"]


def format_period(
    s: PeriodSummary, member_names: Dict[int, str], head: str, fmt: Callable[[int], str],
    *, biggest_title: str = "Крупнейшие записи", empty: str = "За этот период записей нет.", show_daily: bool = True,
    prev_label: str = "К прошлому периоду", authors_label: str = "Кто вносил",
) -> str:
    """HTML-отчёт по сводке; fmt форматирует значение (деньги/штуки). head уже безопасный HTML."""
    if not s.count:
        return head + "\n\n" + empty
    lines = [head, ""]
    avg = f", в среднем {fmt(s.total // s.days)}/день" if show_daily and s.days > 1 else ""
    lines.append(f"Всего: <b>{fmt(s.total)}</b> ({s.count} зап.{avg})")
    if s.prev_total:
        diff = s.total - s.prev_total
        pct = round(diff * 100 / s.prev_total)
        arrow = "▲" if diff > 0 else ("▼" if diff < 0 else "=")
        lines.append(f"{prev_label}: {arrow} {fmt(abs(diff))} ({pct:+d}%)")
    if s.by_category:
        lines += ["", "<b>По категориям</b>"]
        for name, v in s.by_category:
            lines.append(f"• {html.escape(name)} — {fmt(v)} ({round(v * 100 / s.total)}%)")
    if len(s.by_author) > 1:
        lines += ["", f"<b>{html.escape(authors_label)}</b>"]
        for uid, v in s.by_author:
            lines.append(f"• {html.escape(member_names.get(uid, str(uid)))} — {fmt(v)}")
    if s.biggest:
        lines += ["", f"<b>{html.escape(biggest_title)}</b>"]
        for e in s.biggest:
            note = f" — {html.escape(e.note)}" if e.note else ""
            lines.append(f"• {fmt(e.amount_minor)} ({e.occurred_at:%d.%m}){note}")
    return "\n".join(lines)


def format_report(s: MonthSummary, member_names: Dict[int, str], title: str = "Расходы") -> str:
    """HTML-отчёт по расходам за месяц для Telegram. member_names: telegram id → имя."""
    head = f"📊 <b>{html.escape(title)}: {MONTHS_RU[s.month - 1]} {s.year}</b>"
    return format_period(
        s, member_names, head, lambda v: format_money(v, s.currency),
        biggest_title="Крупнейшие траты", empty="За этот месяц расходов нет.",
        prev_label="К прошлому месяцу", authors_label="Кто тратил",
    )
