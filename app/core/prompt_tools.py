"""Общие инструменты системных промптов: чтение из файла, выбор действующего, запись в .env.

Промпты задаются тремя способами (по убыванию приоритета у конкретной витрины/агента):
поле витрины -> файл (*_FILE) -> значение в .env -> встроенный. Файл — самый надёжный способ:
без кавычек, \\n и экранирования `$`, правится обычным редактором и подхватывается без перезапуска.

Почему не просто значение в .env: docker compose подставляет `$имя` в значениях без кавычек и в
двойных кавычках (`$step1` в схеме DSL превращалось в пустую строку), а python-dotenv и compose
по-разному читают `\\\\` и `${...}`. Поэтому to_env_line пишет значение в одинарных кавычках
(там `$` и `\\n` не трогает ни один) и отказывается от текстов, где парсеры расходятся.

Модуль зависит только от stdlib: его грузят конфиг, planner, хендлеры и scripts/migrate_env.py.
"""
import logging
import os
from typing import Any, Callable, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

MAX_PROMPT_FILE_BYTES = 64 * 1024

COMPOSE_HINT = (
    "Если промпт задан в .env и содержит $: docker compose подставляет $имя в значениях без кавычек и "
    "в двойных кавычках (оно становится пустым) — используйте одинарные кавычки (python3 "
    "scripts/migrate_env.py --print-prompt ИМЯ) или файл (*_FILE)"
)

_reported: set = set()
_file_cache: dict = {}  # путь -> ((mtime_ns, size), текст)


def log_once(level: int, key: Any, message: str) -> None:
    k = (level, key)
    if k not in _reported:
        _reported.add(k)
        logger.log(level, message)


def read_prompt_file(path: Any) -> Optional[str]:
    """Текст промпта из файла или None (пусто/нет файла/ошибка — с одной записью в лог).

    Путь относительный — от рабочей папки (в контейнере /app; том ./data доступен как data/...).
    Результат кэшируется по (mtime, размер): правка файла подхватывается без перезапуска.
    """
    if not (isinstance(path, str) and path.strip()):
        return None
    path = path.strip()
    try:
        st = os.stat(path)
        if not os.path.isfile(path):
            raise OSError("это не файл")
        if st.st_size > MAX_PROMPT_FILE_BYTES:
            log_once(logging.ERROR, ("big", path, st.st_size),
                     f"Файл промпта {path} больше {MAX_PROMPT_FILE_BYTES // 1024} КБ — пропущен")
            return None
        sig = (st.st_mtime_ns, st.st_size)
        cached = _file_cache.get(path)
        if cached and cached[0] == sig:
            return cached[1]
        with open(path, encoding="utf-8-sig") as fh:
            text = fh.read()
        _file_cache[path] = (sig, text)
        return text
    except (OSError, UnicodeDecodeError) as e:
        log_once(logging.ERROR, ("read", path, str(e)),
                 f"Не удалось прочитать файл промпта {path}: {e} — пропущен, берётся следующий источник")
        return None


def resolve_prompt(
    candidates: Iterable[Tuple[str, Any]],
    default: str,
    validate: Callable[[str], Any],
    how_to_edit: str,
) -> str:
    """Первый непустой и корректный промпт из [(источник, текст)], иначе default.

    validate(text) -> объект с .errors и .warnings. Пустые источники пропускаются молча,
    некорректный — громкая ошибка в логе (один раз на источник и текст) и переход к следующему.
    """
    for source, text in candidates:
        if not (isinstance(text, str) and text.strip()):
            continue
        if text == default:
            return text
        check = validate(text)
        if check.errors:
            log_once(
                logging.ERROR, (source, text),
                f"Системный промпт ({source}) отвергнут: {'; '.join(check.errors)}. Берётся следующий источник "
                f"или встроенный промпт. {COMPOSE_HINT}. Встроенный для правки: {how_to_edit}",
            )
            continue
        if check.warnings:
            log_once(logging.WARNING, (source, text, "w"), f"Системный промпт ({source}): " + "; ".join(check.warnings))
        return text
    return default


def to_env_line(name: str, value: str) -> str:
    """Строка .env с многострочным значением в одинарных кавычках.

    Проверено на docker compose v5 и python-dotenv: в одинарных кавычках оба читают `$имя` и
    перевод строки буквально, `\\'` даёт апостроф. Расходятся они на `\\\\` (compose оставляет два
    символа, dotenv — один) и на `${...}` (dotenv подставляет, compose нет), поэтому такие тексты
    не поддерживаются: ValueError с советом использовать файл.
    """
    if "\\" in value:
        raise ValueError("текст содержит обратный слэш — в .env его читают по-разному, используйте файл (*_FILE)")
    if "${" in value:
        raise ValueError("текст содержит ${...} — python-dotenv подставит переменную, используйте файл (*_FILE)")
    return f"{name}='" + value.rstrip("\n").replace("'", "\\'") + "'"


def candidates_list(*pairs: Tuple[str, Any]) -> List[Tuple[str, Any]]:
    return list(pairs)
