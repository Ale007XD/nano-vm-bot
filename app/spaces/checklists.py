"""Списки пространства (покупки, сборы, задания): добавление пачкой, вычёркивание, очистка.

Все операции идут через SpaceScope: чужой список недоступен даже по прямому id.
Текст позиции нормализуется (пробелы, длина), дубль среди невычеркнутых не добавляется.
"""
from __future__ import annotations

import html
import re
from datetime import datetime, timezone
from typing import Iterable, List, Optional, Tuple

from app.spaces.models import Checklist, ChecklistItem
from app.spaces.repo import SpaceAccessError, SpaceScope

MAX_TEXT = 100
MAX_QTY = 32
MAX_BATCH = 50
MAX_OPEN_ITEMS = 500


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()[:MAX_TEXT]


async def default_list(scope: SpaceScope, kind: str = "shopping", title: str = "") -> Checklist:
    """Действующий (не в архиве) список вида kind; если нет — создаётся."""
    found = await scope.list(Checklist, Checklist.kind == kind, Checklist.archived.is_(False),
                             order_by=Checklist.id, limit=1)
    if found:
        return found[0]
    return await scope.add(Checklist(kind=kind, title=title or kind, created_by=scope.user_id))


async def add_items(
    scope: SpaceScope, checklist_id: int, items: Iterable[Tuple[str, str]]
) -> Tuple[List[ChecklistItem], List[str]]:
    """Добавить позиции [(текст, количество)]. Возвращает (добавленные, пропущенные как дубли/лишние).

    Дубль — такой же текст (без учёта регистра) среди невычеркнутых или в этой же пачке.
    """
    if await scope.get(Checklist, checklist_id) is None:
        raise SpaceAccessError("список не найден в этом пространстве")
    existing = {i.text.casefold() for i in await scope.list(
        ChecklistItem, ChecklistItem.checklist_id == checklist_id, ChecklistItem.done.is_(False))}
    room = MAX_OPEN_ITEMS - len(existing)
    added: List[ChecklistItem] = []
    skipped: List[str] = []
    for raw_text, qty in list(items)[:MAX_BATCH]:
        text = _norm(raw_text)
        if not text:
            continue
        key = text.casefold()
        if key in existing or room <= 0:
            skipped.append(text)
            continue
        existing.add(key)
        room -= 1
        added.append(await scope.add(ChecklistItem(
            checklist_id=checklist_id, text=text, qty=_norm(qty)[:MAX_QTY], added_by=scope.user_id)))
    return added, skipped


async def list_items(scope: SpaceScope, checklist_id: int) -> List[ChecklistItem]:
    """Сначала невычеркнутые (по порядку добавления), затем вычеркнутые."""
    rows = await scope.list(ChecklistItem, ChecklistItem.checklist_id == checklist_id,
                            order_by=(ChecklistItem.done, ChecklistItem.id))
    return rows


async def set_done(scope: SpaceScope, item_id: int, done: bool = True) -> bool:
    return await scope.update(
        ChecklistItem, item_id, done=done,
        done_by=scope.user_id if done else None, done_at=_now() if done else None,
    )


async def toggle(scope: SpaceScope, item_id: int) -> Optional[bool]:
    """Переключить вычеркнутость; возвращает новое состояние или None, если позиции нет."""
    item = await scope.get(ChecklistItem, item_id)
    if item is None:
        return None
    await set_done(scope, item_id, not item.done)
    return not item.done


async def clear_done(scope: SpaceScope, checklist_id: int) -> int:
    return await scope.delete_where(
        ChecklistItem, ChecklistItem.checklist_id == checklist_id, ChecklistItem.done.is_(True))


def render(title: str, items: List[ChecklistItem]) -> str:
    """HTML-текст списка для Telegram."""
    if not items:
        return f"🛒 <b>{html.escape(title)}</b>\n\nСписок пуст."
    lines = [f"🛒 <b>{html.escape(title)}</b>"]
    for i in items:
        qty = f" — {html.escape(i.qty)}" if i.qty else ""
        lines.append(("✅ <s>" + html.escape(i.text) + "</s>" if i.done else "▫️ " + html.escape(i.text)) + qty)
    return "\n".join(lines)
