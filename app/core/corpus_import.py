"""Пакетный импорт документов в корпус витрины: план (dry-run), применение, отчёты.

Идемпотентен: документ с тем же именем и тем же содержимым пропускается; изменившийся
добавляется новой версией, а старая удаляется только после успешного добавления.
Режим синхронизации дополнительно удаляет из корпуса документы, которых нет в архиве.
"""
import asyncio
import hashlib
import html
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Dict, List, Optional, Tuple

from app.core.memory import vector_memory
from app.utils.corpus_archive import ArchiveDoc, ArchiveError, ArchiveScan

_locks: Dict[str, asyncio.Lock] = {}


def import_lock(corpus: str) -> asyncio.Lock:
    """Один импорт на корпус одновременно (иначе замена и синхронизация перемешаются)."""
    return _locks.setdefault(corpus, asyncio.Lock())


@dataclass
class ImportPlan:
    add: List[ArchiveDoc] = field(default_factory=list)
    update: List[ArchiveDoc] = field(default_factory=list)
    unchanged: List[str] = field(default_factory=list)
    remove: List[str] = field(default_factory=list)
    sync: bool = False


@dataclass
class ImportReport:
    added: List[str] = field(default_factory=list)
    updated: List[str] = field(default_factory=list)
    removed: List[str] = field(default_factory=list)
    failed: List[Tuple[str, str]] = field(default_factory=list)


async def plan_import(scan: ArchiveScan, corpus: str, sync: bool = False) -> ImportPlan:
    if sync and not scan.seen:
        raise ArchiveError(
            "в архиве нет ни одного документа .md/.txt — синхронизацию не выполняю, "
            "иначе корпус был бы очищен"
        )
    index = await vector_memory.get_corpus_index(corpus=corpus)
    plan = ImportPlan(sync=sync)
    for doc in scan.docs:
        entry = index.get(doc.name)
        if entry is None:
            plan.add.append(doc)
        elif len(entry["source_ids"]) == 1 and entry["hashes"] == {doc.sha}:
            plan.unchanged.append(doc.name)
        else:
            plan.update.append(doc)
    if sync:
        # «seen» включает и файлы, которые не прочитались: их прежние версии не трогаем
        plan.remove = sorted(n for n in index if n not in scan.seen)
    return plan


def source_id_for(doc: ArchiveDoc) -> str:
    return f"zip_{hashlib.sha1(doc.name.encode('utf-8')).hexdigest()[:12]}_{doc.sha[:10]}"


async def apply_import(
    plan: ImportPlan,
    corpus: str,
    progress: Optional[Callable[[int, int, str], Awaitable[None]]] = None,
    min_progress_interval: float = 3.0,
) -> ImportReport:
    report = ImportReport()
    todo = [(d, False) for d in plan.add] + [(d, True) for d in plan.update]
    last = 0.0
    for i, (doc, is_update) in enumerate(todo, 1):
        sid = source_id_for(doc)
        try:
            await vector_memory.add_corpus_document(
                source_id=sid, source_name=doc.name, content=doc.text, corpus=corpus, content_hash=doc.sha
            )
            # старая версия уходит только после успешного добавления новой
            await vector_memory.delete_corpus_by_name(doc.name, corpus=corpus, keep_source_id=sid)
        except Exception as e:  # noqa: BLE001 — один плохой документ не должен остановить пакет
            report.failed.append((doc.name, f"{e.__class__.__name__}: {str(e)[:80]}"))
            continue
        (report.updated if is_update else report.added).append(doc.name)
        now = time.monotonic()
        if progress and now - last >= min_progress_interval:
            last = now
            try:
                await progress(i, len(todo), doc.name)
            except Exception:  # noqa: BLE001 — статус не критичен
                pass
    for name in plan.remove:
        if await vector_memory.delete_corpus_by_name(name, corpus=corpus):
            report.removed.append(name)
    return report


# ----------------------------- отчёты (HTML для Telegram) ----------------- #
def _names(items: List[str], limit: int = 8) -> str:
    shown = ", ".join(f"<code>{html.escape(n)}</code>" for n in items[:limit])
    return shown + (f" … и ещё {len(items) - limit}" if len(items) > limit else "")


def _skipped_lines(scan: ArchiveScan, limit: int = 8) -> List[str]:
    lines = []
    if scan.skipped:
        lines.append(f"⚠️ Пропущено файлов: {len(scan.skipped)}")
        for name, why in scan.skipped[:limit]:
            lines.append(f"  – <code>{html.escape(name)}</code>: {html.escape(why)}")
        if len(scan.skipped) > limit:
            lines.append(f"  … и ещё {len(scan.skipped) - limit}")
    if scan.unsupported:
        kinds = ", ".join(f".{ext} ×{n}" for ext, n in scan.unsupported.most_common(6))
        lines.append(f"ℹ️ Другие форматы проигнорированы: {html.escape(kinds)}")
    return lines


def format_plan(plan: ImportPlan, scan: ArchiveScan) -> str:
    lines = [
        "🔍 <b>Проверка архива</b> (ничего не изменено)",
        f"Новых: <b>{len(plan.add)}</b> · обновится: <b>{len(plan.update)}</b> · "
        f"без изменений: <b>{len(plan.unchanged)}</b>"
        + (f" · удалится: <b>{len(plan.remove)}</b>" if plan.sync else ""),
    ]
    if plan.add:
        lines.append("➕ Новые: " + _names([d.name for d in plan.add]))
    if plan.update:
        lines.append("♻️ Обновятся: " + _names([d.name for d in plan.update]))
    if plan.remove:
        lines.append("🗑 Удалятся (нет в архиве): " + _names(plan.remove))
    lines += _skipped_lines(scan)
    lines.append("Чтобы применить — отправьте тот же архив без слова «dry» в подписи.")
    return "\n".join(lines)


def format_report(report: ImportReport, plan: ImportPlan, scan: ArchiveScan) -> str:
    head = "✅" if not report.failed else "⚠️"
    lines = [
        f"{head} <b>Импорт завершён</b>",
        f"Добавлено: <b>{len(report.added)}</b> · обновлено: <b>{len(report.updated)}</b> · "
        f"без изменений: <b>{len(plan.unchanged)}</b>"
        + (f" · удалено: <b>{len(report.removed)}</b>" if plan.sync else ""),
    ]
    if report.removed:
        lines.append("🗑 Удалены: " + _names(report.removed))
    if report.failed:
        lines.append(f"❌ Ошибок: {len(report.failed)} (прежние версии этих файлов сохранены)")
        for name, why in report.failed[:8]:
            lines.append(f"  – <code>{html.escape(name)}</code>: {html.escape(why)}")
    lines += _skipped_lines(scan)
    return "\n".join(lines)
