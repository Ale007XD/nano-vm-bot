"""Отчёты пространства по расписанию: еженедельные и ежемесячные, на любых показателях.

Отчёт — это описание (ReportSpec): какой показатель, за какой период, когда и кому слать.
Описания лежат в `settings["reports"]` пространства; без них действуют умолчания из .env
(`SPACE_REPORT_DEFAULTS`). Числа считает SQL, текст собирает код — LLM в отчётах не участвует,
поэтому цифры нельзя «придумать», а работа бесплатна. Для нестандартных отчётов (не «сумма/число
по показателю») есть реестр типов: модуль из `SPACE_REPORT_PLUGINS` вызывает
`register_report_type(имя, построитель)`, а в описании указывается `"type": "<имя>"`.

Расписание хранится как «слот» (период + дата срабатывания). Каждый слот отправляется один раз
независимо от перезапусков (таблица report_runs); после простоя уходит только последний слот,
а слот, пропущенный более чем на сутки с лишним, не досылается задним числом.
"""
from __future__ import annotations

import html
import importlib
import logging
import re
from calendar import monthrange
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

import pytz
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.core.config import settings as app_settings
from app.core.ext_db import resolve_session_maker
from app.spaces import ledger, metrics, service
from app.spaces.intake import WEEKDAYS, member_names
from app.spaces.models import ReportRun, Space, SpaceMember
from app.spaces.repo import SpaceScope

logger = logging.getLogger(__name__)

GRACE = timedelta(hours=36)
MAX_ATTEMPTS = 3
MAX_REPORTS = 10
_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
Send = Callable[[int, str], Awaitable[None]]


class ReportError(Exception):
    """Отчёт нельзя построить (сообщение — для журнала)."""


@dataclass(frozen=True)
class ReportSpec:
    id: str
    title: str
    metric: str = metrics.EXPENSE
    period: str = "month"  # month | week
    day: int = 1  # число месяца для month (1–28)
    weekday: int = 0  # день недели для week (0 — понедельник)
    hour: int = 9
    minute: int = 0
    audience: str = "all"  # all | admins
    send_empty: bool = False
    type: str = "metric"
    options: Dict[str, Any] = field(default_factory=dict, compare=False)


@dataclass(frozen=True)
class Window:
    start: datetime  # UTC naive, [start, end)
    end: datetime
    prev_start: datetime
    prev_end: datetime
    label: str
    days: int


Builder = Callable[[SpaceScope, Space, ReportSpec, Window], Awaitable[Optional[str]]]
BUILDERS: Dict[str, Builder] = {}


def register_report_type(name: str, builder: Builder) -> None:
    """Зарегистрировать свой тип отчёта (вызывается из модуля-плагина)."""
    if not _ID_RE.match(name) or name == "metric":
        raise ValueError("имя типа отчёта: латиница/цифры/_, не «metric»")
    BUILDERS[name] = builder


def load_plugins(modules_csv: str) -> List[str]:
    """Импортировать модули-плагины из списка через запятую; возвращает успешно загруженные."""
    loaded = []
    for name in [m.strip() for m in (modules_csv or "").split(",") if m.strip()]:
        try:
            importlib.import_module(name)
            loaded.append(name)
        except Exception as e:  # noqa: BLE001 — сломанный плагин не должен ронять бота
            logger.error(f"report plugin {name!r} failed to load: {e}")
    return loaded


# ----------------------------- настройки ----------------------------------- #
def enabled() -> bool:
    return getattr(app_settings, "SPACE_REPORTS_ENABLED", True) is True


def _env_hour() -> int:
    v = getattr(app_settings, "SPACE_REPORT_HOUR", 9)
    return v if isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 23 else 9


def default_specs() -> List[ReportSpec]:
    raw = getattr(app_settings, "SPACE_REPORT_DEFAULTS", "monthly")
    names = [n.strip().lower() for n in raw.split(",")] if isinstance(raw, str) else []
    hour, out = _env_hour(), []
    if "monthly" in names:
        out.append(ReportSpec(id="monthly", title="Расходы за месяц", period="month", hour=hour))
    if "weekly" in names:
        out.append(ReportSpec(id="weekly", title="Расходы за неделю", period="week", hour=hour))
    return out


def parse_specs(raw: Any) -> Tuple[List[ReportSpec], List[str]]:
    """Проверить описания отчётов. Возвращает (годные, замечания); негодные пропускаются."""
    specs: List[ReportSpec] = []
    errors: List[str] = []
    if not isinstance(raw, list):
        return specs, ["reports должен быть списком"]
    seen = set()
    for i, it in enumerate(raw[:MAX_REPORTS]):
        if not isinstance(it, dict):
            errors.append(f"reports[{i}]: ожидался объект")
            continue
        if it.get("enabled") is False:
            continue
        rid = str(it.get("id") or "").strip().lower()
        if not _ID_RE.match(rid) or rid in seen:
            errors.append(f"reports[{i}]: id «{rid}» некорректен или повторяется")
            continue
        rtype = str(it.get("type") or "metric")
        if rtype != "metric" and rtype not in BUILDERS:
            errors.append(f"reports[{i}] ({rid}): неизвестный тип «{rtype}»")
            continue
        period = str(it.get("period") or "month").lower()
        if period not in ("month", "week"):
            errors.append(f"reports[{i}] ({rid}): period — month или week")
            continue
        day = it.get("day", 1)
        if period == "month" and not (isinstance(day, int) and not isinstance(day, bool) and 1 <= day <= 28):
            errors.append(f"reports[{i}] ({rid}): day — число от 1 до 28")
            continue
        wd = it.get("weekday", "mon")
        wd = WEEKDAYS.get(str(wd).lower(), wd if isinstance(wd, int) and not isinstance(wd, bool) else None)
        if period == "week" and wd not in range(7):
            errors.append(f"reports[{i}] ({rid}): weekday — mon…sun")
            continue
        hhmm = re.fullmatch(r"(\d{1,2}):(\d{2})", str(it.get("time") or f"{_env_hour()}:00"))
        if not hhmm or int(hhmm.group(1)) > 23 or int(hhmm.group(2)) > 59:
            errors.append(f"reports[{i}] ({rid}): time — HH:MM")
            continue
        audience = str(it.get("audience") or "all").lower()
        if audience not in ("all", "admins"):
            errors.append(f"reports[{i}] ({rid}): audience — all или admins")
            continue
        seen.add(rid)
        opts = it.get("options")
        specs.append(ReportSpec(
            id=rid, title=" ".join(str(it.get("title") or rid).split())[:60],
            metric=str(it.get("metric") or metrics.EXPENSE), period=period,
            day=day if period == "month" else 1, weekday=wd if period == "week" else 0,
            hour=int(hhmm.group(1)), minute=int(hhmm.group(2)), audience=audience,
            send_empty=bool(it.get("send_empty", False)), type=rtype,
            options=opts if isinstance(opts, dict) else {},
        ))
    if len(raw) > MAX_REPORTS:
        errors.append(f"отчётов больше {MAX_REPORTS} — лишние отброшены")
    return specs, errors


_warned: set = set()


def specs_for(space: Space) -> List[ReportSpec]:
    """Отчёты пространства: свои из settings["reports"] (пусто — отчётов нет) или умолчания из .env."""
    if not enabled():
        return []
    raw = space.settings.get("reports") if isinstance(space.settings, dict) else None
    if raw is None:
        return default_specs()
    specs, errors = parse_specs(raw)
    for e in errors:
        if (space.id, e) not in _warned:
            _warned.add((space.id, e))
            logger.warning(f"space {space.id}: {e}")
    return specs


# ----------------------------- слоты и окна -------------------------------- #
def slot_for(spec: ReportSpec, now_local: datetime) -> datetime:
    """Последний момент расписания, не позже now_local (aware, в зоне пространства)."""
    zone = now_local.tzinfo
    n = now_local.replace(tzinfo=None)
    if spec.period == "month":
        cand = datetime(n.year, n.month, spec.day, spec.hour, spec.minute)
        if cand > n:
            y, m = ledger.previous_month(n.year, n.month)
            cand = datetime(y, m, spec.day, spec.hour, spec.minute)
    else:
        cand = (n - timedelta(days=(n.weekday() - spec.weekday) % 7)).replace(
            hour=spec.hour, minute=spec.minute, second=0, microsecond=0)
        if cand > n:
            cand -= timedelta(days=7)
    return zone.localize(cand)


def slot_key(spec: ReportSpec, slot: datetime) -> str:
    return f"{spec.period}:{slot:%Y-%m-%d}"


def window_for(spec: ReportSpec, slot: datetime, tz: str) -> Window:
    """Отчётный период — полный месяц (неделя) перед слотом, и такой же перед ним для сравнения."""
    zone = pytz.timezone(tz)
    utc = lambda d: zone.localize(d).astimezone(timezone.utc).replace(tzinfo=None)  # noqa: E731
    if spec.period == "month":
        y, m = ledger.previous_month(slot.year, slot.month)
        start, end = ledger.month_bounds(y, m, tz)
        py, pm = ledger.previous_month(y, m)
        ps, pe = ledger.month_bounds(py, pm, tz)
        return Window(start, end, ps, pe, f"{ledger.MONTHS_RU[m - 1]} {y}", monthrange(y, m)[1])
    monday = datetime(slot.year, slot.month, slot.day) - timedelta(days=slot.weekday())
    first, last = monday - timedelta(days=7), monday - timedelta(days=1)
    return Window(utc(first), utc(monday), utc(first - timedelta(days=7)), utc(first),
                  f"{first:%d.%m}–{last:%d.%m}", 7)


# ----------------------------- построение ---------------------------------- #
async def build_text(scope: SpaceScope, space: Space, spec: ReportSpec, win: Window) -> Optional[str]:
    """HTML-текст отчёта или None, если слать нечего."""
    if spec.type != "metric":
        return await BUILDERS[spec.type](scope, space, spec, win)
    metric = metrics.metrics_for(space).get(spec.metric)
    if metric is None:
        raise ReportError(f"показатель «{spec.metric}» не описан в настройках пространства")
    s = await ledger.summarize(scope, metric=metric.key, unit=metric.unit, start=win.start, end=win.end,
                               prev_start=win.prev_start, prev_end=win.prev_end, label=win.label, days=win.days)
    if not s.count and not spec.send_empty:
        return None
    names = await member_names(space.id)
    head = f"📊 <b>{html.escape(spec.title)}: {html.escape(win.label)}</b>"
    return ledger.format_period(
        s, names, head, metrics.formatter(metric),
        biggest_title="Крупнейшие траты" if metric.kind == "money" else "Крупнейшие записи",
        authors_label="Кто тратил" if metric.kind == "money" else "Кто вносил")


# ----------------------------- исполнение ---------------------------------- #
_done: set = set()  # слоты, которые в этом процессе точно закрыты (чтобы не ходить в БД каждую минуту)


async def _claim(space_id: int, report_id: str, slot: str, stale: bool, session_maker=None) -> bool:
    """Занять слот. True — слать должен этот процесс. Устаревший слот записывается как пропущенный."""
    sm = resolve_session_maker(session_maker)
    async with sm() as s:
        s.add(ReportRun(space_id=space_id, report_id=report_id, slot=slot,
                        status="skipped" if stale else "sending"))
        try:
            await s.commit()
            return not stale
        except IntegrityError:
            await s.rollback()
        row = await s.get(ReportRun, (space_id, report_id, slot))
        if row is None or row.status != "failed" or row.attempts >= MAX_ATTEMPTS:
            return False
        res = await s.execute(update(ReportRun).where(
            ReportRun.space_id == space_id, ReportRun.report_id == report_id, ReportRun.slot == slot,
            ReportRun.status == "failed", ReportRun.attempts == row.attempts
        ).values(status="sending", attempts=row.attempts + 1))
        await s.commit()
        return bool(res.rowcount)


async def _finish(space_id: int, report_id: str, slot: str, status: str, session_maker=None) -> None:
    sm = resolve_session_maker(session_maker)
    async with sm() as s:
        await s.execute(update(ReportRun).where(
            ReportRun.space_id == space_id, ReportRun.report_id == report_id, ReportRun.slot == slot
        ).values(status=status, updated_at=datetime.now(timezone.utc).replace(tzinfo=None)))
        await s.commit()


async def _recipients(space_id: int, audience: str, session_maker=None) -> List[int]:
    sm = resolve_session_maker(session_maker)
    async with sm() as s:
        q = select(SpaceMember.user_id).where(SpaceMember.space_id == space_id)
        if audience == "admins":
            q = q.where(SpaceMember.role.in_(("owner", "admin")))
        return [r[0] for r in (await s.execute(q.order_by(SpaceMember.joined_at, SpaceMember.user_id))).all()]


async def _active_spaces(session_maker=None) -> List[Space]:
    sm = resolve_session_maker(session_maker)
    async with sm() as s:
        rows = await s.execute(select(Space).where(Space.is_active.is_(True), Space.kind != service.PERSONAL_KIND))
        return list(rows.scalars())


async def run_due(send: Send, now: Optional[datetime] = None, session_maker=None) -> int:
    """Отправить созревшие отчёты всех пространств; возвращает число отправленных. Не бросает исключений."""
    if not enabled():
        return 0
    now = (now or datetime.now(timezone.utc))
    now = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    sent = 0
    try:
        spaces = await _active_spaces(session_maker)
    except Exception as e:  # noqa: BLE001
        logger.error(f"reports: cannot list spaces: {e}")
        return 0
    for space in spaces:
        try:
            zone = pytz.timezone(space.timezone or "UTC")
            now_local = now.astimezone(zone)
            for spec in specs_for(space):
                slot = slot_for(spec, now_local)
                key = slot_key(spec, slot)
                if (space.id, spec.id, key) in _done:
                    continue
                if not await _claim(space.id, spec.id, key, now_local - slot > GRACE, session_maker):
                    _done.add((space.id, spec.id, key))
                    continue
                status = "failed"
                try:
                    scope = SpaceScope(space.id, 0, "owner", session_maker)
                    text = await build_text(scope, space, spec, window_for(spec, slot, space.timezone or "UTC"))
                    if text is None:
                        status = "skipped"
                    else:
                        delivered = 0
                        for chat_id in await _recipients(space.id, spec.audience, session_maker):
                            try:
                                await send(chat_id, text)
                                delivered += 1
                            except Exception as e:  # noqa: BLE001 — недоступный адресат не мешает остальным
                                logger.warning(f"report {spec.id}: send to {chat_id} failed: {e}")
                        status = "sent" if delivered else "failed"
                        sent += bool(delivered)
                except Exception as e:  # noqa: BLE001
                    logger.error(f"report {spec.id} for space {space.id}: {e}")
                await _finish(space.id, spec.id, key, status, session_maker)
                if status != "failed":
                    _done.add((space.id, spec.id, key))
        except Exception as e:  # noqa: BLE001
            logger.error(f"reports: space {space.id}: {e}")
    return sent
