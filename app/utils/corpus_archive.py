"""Безопасное чтение ZIP-архива с документами корпуса (.md / .txt).

Всё делается в памяти: на диск ничего не распаковывается, поэтому path traversal и симлинки
не могут ничего записать — но такие записи всё равно отклоняются и попадают в отчёт.
Защита от zip-бомбы: лимит записей, проверка заявленных размеров до чтения и ограниченное
чтение; zipfile не отдаёт больше заявленного в заголовке размера, поэтому сумма заявленных
размеров — это верхняя граница реально прочитанного.
"""
import hashlib
import io
import re
import stat
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from typing import Counter as CounterT
from typing import List, Optional, Set, Tuple

ALLOWED_EXT = (".md", ".txt")
MAX_NAME_LEN = 200
MAX_ENTRIES = 5000  # записей в архиве всего (включая картинки и т.п.)


class ArchiveError(ValueError):
    """Архив нельзя обработать целиком (сообщение — для администратора)."""


@dataclass
class ArchiveDoc:
    name: str  # путь внутри архива, «/» как разделитель
    text: str
    sha: str  # sha1 текста — для пропуска неизменившихся документов


@dataclass
class ArchiveScan:
    docs: List[ArchiveDoc] = field(default_factory=list)
    skipped: List[Tuple[str, str]] = field(default_factory=list)  # (имя, причина)
    # имена всех файлов нужного формата с безопасным путём, даже если содержимое не прочиталось:
    # при синхронизации их прежние версии удалять нельзя
    seen: Set[str] = field(default_factory=set)
    unsupported: CounterT[str] = field(default_factory=Counter)  # расширение -> сколько файлов


def _clean_name(raw: str) -> Optional[str]:
    """Нормализованный относительный путь или None, если путь небезопасен."""
    name = raw.replace("\\", "/")
    if name.startswith("/") or re.match(r"^[A-Za-z]:", name) or "\x00" in name:
        return None
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        return None
    return "/".join(parts)


def _is_service(name: str) -> bool:
    """Служебные файлы ОС и редакторов — молча игнорируются."""
    parts = name.split("/")
    return any(p.startswith(".") or p == "__MACOSX" or p.lower() == "thumbs.db" for p in parts)


def _decode(raw: bytes) -> Optional[str]:
    if b"\x00" in raw:
        return None
    for enc in ("utf-8-sig", "cp1251"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return None


def _strip_common_dir(names: List[str]) -> List[str]:
    """Если все документы лежат в одной корневой папке (архив папки целиком) — убрать её."""
    if names and all("/" in n for n in names) and len({n.split("/", 1)[0] for n in names}) == 1:
        return [n.split("/", 1)[1] for n in names]
    return names


def scan_archive(
    data: bytes, *, max_files: int, max_file_bytes: int, max_total_bytes: int
) -> ArchiveScan:
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError) as e:
        raise ArchiveError(f"не удалось открыть архив ({e.__class__.__name__}) — нужен обычный .zip") from e

    with zf:
        infos = [i for i in zf.infolist() if not i.is_dir()]
        if len(infos) > MAX_ENTRIES:
            raise ArchiveError(f"в архиве {len(infos)} записей — больше допустимых {MAX_ENTRIES}")

        scan = ArchiveScan()
        candidates: List[Tuple[str, zipfile.ZipInfo]] = []
        for info in infos:
            name = _clean_name(info.filename)
            if name is None:
                scan.skipped.append((info.filename[:MAX_NAME_LEN], "небезопасный путь"))
                continue
            if _is_service(name):
                continue
            if not name.lower().endswith(ALLOWED_EXT):
                ext = name.rsplit(".", 1)[-1].lower() if "." in name.rsplit("/", 1)[-1] else "без расширения"
                scan.unsupported[ext] += 1
                continue
            candidates.append((name, info))

        if len(candidates) > max_files:
            raise ArchiveError(
                f"в архиве {len(candidates)} документов .md/.txt — больше допустимых {max_files} "
                "(разбейте на несколько архивов)"
            )
        declared = sum(i.file_size for _, i in candidates)
        if declared > max_total_bytes:
            raise ArchiveError(
                f"документы занимают {declared // 1024} КБ в распакованном виде — "
                f"больше допустимых {max_total_bytes // 1024} КБ"
            )

        stripped = _strip_common_dir([n for n, _ in candidates])
        candidates = [(n, i) for n, (_, i) in zip(stripped, candidates)]

        by_name = {}
        for name, info in candidates:
            if name in by_name:
                scan.skipped.append((name, "дубль имени в архиве (взята последняя запись)"))
            by_name[name] = info

        for name, info in by_name.items():
            if len(name) > MAX_NAME_LEN:
                scan.skipped.append((name[:60] + "…", f"имя длиннее {MAX_NAME_LEN} символов"))
                continue
            scan.seen.add(name)
            if (info.external_attr >> 16) & 0o170000 == stat.S_IFLNK:
                scan.skipped.append((name, "символическая ссылка"))
                continue
            if info.flag_bits & 0x1:
                scan.skipped.append((name, "файл зашифрован паролем"))
                continue
            try:
                with zf.open(info) as fh:
                    raw = fh.read(max_file_bytes + 1)  # +1 байт, чтобы отличить «ровно лимит» от «больше»
            except Exception as e:  # noqa: BLE001 — битая запись не должна ронять весь импорт
                scan.skipped.append((name, f"не читается ({e.__class__.__name__})"))
                continue
            if len(raw) > max_file_bytes:
                scan.skipped.append((name, f"больше {max_file_bytes // 1024} КБ"))
                continue
            text = _decode(raw)
            if text is None:
                scan.skipped.append((name, "не текст (бинарный файл или неизвестная кодировка)"))
                continue
            if not text.strip():
                scan.skipped.append((name, "пустой файл"))
                continue
            scan.docs.append(
                ArchiveDoc(name=name, text=text, sha=hashlib.sha1(text.encode("utf-8")).hexdigest())
            )
    return scan
