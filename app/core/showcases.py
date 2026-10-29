"""
Раздельные витрины: несколько ботов-витрин в одном процессе.

Каждая витрина — свой Telegram-бот (токен), своя коллекция знаний ChromaDB,
свой режим доступа (open | invite), своя группа оператора, промпт и модель.
Тяжёлые ресурсы (эмбеддинг-модель, БД, LLM-клиент) общие на процесс.

Витрина определяется по боту, принявшему сообщение, а не по данным
пользователя: так граница между витринами не зависит от пользовательского
состояния. Текущая витрина доступна через current() (ContextVar,
выставляется middleware на каждое обновление).

Конфиг — env SHOWCASES (JSON-список). Если не задан, работает одна «наследная»
витрина с токеном SHOWCASE_BOT_TOKEN и прежними переменными
(OPERATOR_CHAT_ID, SHOWCASE_OPEN_ACCESS, коллекция «corpus»).
"""
from __future__ import annotations

import json
import logging
import os
import re
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional

logger = logging.getLogger(__name__)

ACCESS_OPEN = "open"
ACCESS_INVITE = "invite"
_ACCESS_VALUES = (ACCESS_OPEN, ACCESS_INVITE)

SLUG_RE = re.compile(r"^[a-z0-9_]{2,24}$")
# Имя коллекции обязано начинаться с «corpus» — иначе витрина могла бы
# указать на служебные коллекции (memories, skills, conversations).
COLLECTION_RE = re.compile(r"^corpus(_[a-z0-9_]{1,40})?$")

LEGACY_SLUG = ""
LEGACY_COLLECTION = "corpus"

_ALLOWED_KEYS = {
    "slug", "token", "token_env", "collection", "access", "operator_chat_id",
    "name", "system_prompt", "model", "welcome",
}


class ShowcaseConfigError(ValueError):
    """Неверная конфигурация витрин (сообщение — для администратора)."""


@dataclass(frozen=True)
class Showcase:
    slug: str
    token: str = field(repr=False)
    collection: str = LEGACY_COLLECTION
    access: str = ACCESS_INVITE
    operator_chat_id: int = 0
    name: str = ""
    system_prompt: str = ""
    model: str = ""
    welcome: str = ""

    @property
    def bot_id(self) -> int:
        try:
            return int(self.token.split(":", 1)[0])
        except (ValueError, AttributeError):
            return 0

    @property
    def operator_enabled(self) -> bool:
        return self.operator_chat_id != 0

    def client_key(self, user_id: int):
        """Ключ клиента во внутрипроцессных структурах (история, антифлуд).

        У наследной витрины — просто id (как было до разделения витрин).
        """
        return user_id if self.slug == LEGACY_SLUG else (self.slug, user_id)


class ShowcaseRegistry:
    def __init__(self, showcases: List[Showcase]):
        if not showcases:
            raise ShowcaseConfigError("Не настроено ни одной витрины")
        self._list = list(showcases)
        self._by_slug: Dict[str, Showcase] = {s.slug: s for s in self._list}
        self._by_bot_id: Dict[int, Showcase] = {s.bot_id: s for s in self._list}

    def __iter__(self) -> Iterator[Showcase]:
        return iter(self._list)

    def __len__(self) -> int:
        return len(self._list)

    def get(self, slug: str) -> Optional[Showcase]:
        return self._by_slug.get(slug)

    def by_bot_id(self, bot_id: int) -> Optional[Showcase]:
        return self._by_bot_id.get(bot_id)

    @property
    def first(self) -> Showcase:
        return self._list[0]


# --------------------------------------------------------------------------- #
# Загрузка конфигурации
# --------------------------------------------------------------------------- #
def _env_lookup(name: str) -> str:
    """Переменная из окружения, затем из .env (pydantic не экспортирует .env в os.environ)."""
    val = os.environ.get(name)
    if val:
        return val
    try:
        from dotenv import dotenv_values

        return (dotenv_values(".env").get(name) or "").strip()
    except Exception:
        return ""


def _int(value: Any, field_name: str, slug: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ShowcaseConfigError(f"Витрина «{slug}»: {field_name} должен быть числом")
    return value


def parse_showcases(raw: str) -> List[Showcase]:
    """Разобрать env SHOWCASES. Любая ошибка — ShowcaseConfigError (запуск не продолжается)."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ShowcaseConfigError(f"SHOWCASES: некорректный JSON ({e})") from e
    if not isinstance(data, list) or not data:
        raise ShowcaseConfigError("SHOWCASES: ожидается непустой JSON-список витрин")

    result: List[Showcase] = []
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            raise ShowcaseConfigError(f"SHOWCASES[{i}]: ожидается объект")
        unknown = set(item) - _ALLOWED_KEYS
        if unknown:
            raise ShowcaseConfigError(
                f"SHOWCASES[{i}]: неизвестные поля {sorted(unknown)} (опечатка?)"
            )

        slug = item.get("slug")
        if not isinstance(slug, str) or not SLUG_RE.match(slug):
            raise ShowcaseConfigError(
                f"SHOWCASES[{i}]: slug обязателен, [a-z0-9_], 2–24 символа"
            )

        token = item.get("token") or ""
        if not token and item.get("token_env"):
            token = _env_lookup(str(item["token_env"]))
            if not token:
                raise ShowcaseConfigError(
                    f"Витрина «{slug}»: переменная {item['token_env']} не задана"
                )
        if not isinstance(token, str) or not re.match(r"^\d+:[\w-]+$", token):
            raise ShowcaseConfigError(
                f"Витрина «{slug}»: нужен token (или token_env) в формате 123456:ABC…"
            )

        collection = item.get("collection") or f"corpus_{slug}"
        if not isinstance(collection, str) or not COLLECTION_RE.match(collection):
            raise ShowcaseConfigError(
                f"Витрина «{slug}»: collection должна быть «corpus» или «corpus_<имя>» "
                "([a-z0-9_])"
            )

        access = item.get("access", ACCESS_INVITE)
        if access not in _ACCESS_VALUES:
            raise ShowcaseConfigError(
                f"Витрина «{slug}»: access должен быть одним из {list(_ACCESS_VALUES)}"
            )

        chat = _int(item.get("operator_chat_id", 0), "operator_chat_id", slug)
        for key in ("name", "system_prompt", "model", "welcome"):
            if not isinstance(item.get(key, ""), str):
                raise ShowcaseConfigError(f"Витрина «{slug}»: {key} должен быть строкой")

        result.append(
            Showcase(
                slug=slug,
                token=token,
                collection=collection,
                access=access,
                operator_chat_id=chat,
                name=item.get("name", ""),
                system_prompt=item.get("system_prompt", ""),
                model=item.get("model", ""),
                welcome=item.get("welcome", ""),
            )
        )

    for attr, label in (("slug", "slug"), ("token", "токен"), ("collection", "collection")):
        seen: set = set()
        for sc in result:
            val = getattr(sc, attr)
            if val in seen:
                raise ShowcaseConfigError(f"SHOWCASES: повторяется {label} витрины («{sc.slug}»)")
            seen.add(val)
    return result


def legacy_showcase(settings: Any = None) -> Showcase:
    """Единственная витрина по прежним переменным (SHOWCASES не задан)."""
    if settings is None:
        from app.core.config import settings as _settings

        settings = _settings
    chat = getattr(settings, "OPERATOR_CHAT_ID", 0)
    chat = chat if isinstance(chat, int) and not isinstance(chat, bool) else 0
    own = getattr(settings, "SHOWCASE_OPEN_ACCESS", None)
    if isinstance(own, bool):
        is_open = own
    else:
        is_open = bool(getattr(settings, "BOT_OPEN_ACCESS", False))
    token = getattr(settings, "SHOWCASE_BOT_TOKEN", "")
    return Showcase(
        slug=LEGACY_SLUG,
        token=token if isinstance(token, str) else "",
        collection=LEGACY_COLLECTION,
        access=ACCESS_OPEN if is_open else ACCESS_INVITE,
        operator_chat_id=chat,
    )


def load_registry(settings: Any = None) -> ShowcaseRegistry:
    if settings is None:
        from app.core.config import settings as _settings

        settings = _settings
    raw = getattr(settings, "SHOWCASES", "")
    if isinstance(raw, str) and raw.strip():
        return ShowcaseRegistry(parse_showcases(raw))
    sc = legacy_showcase(settings)
    if not sc.bot_id:
        raise ShowcaseConfigError(
            "Не задан SHOWCASES и SHOWCASE_BOT_TOKEN — нечего запускать"
        )
    return ShowcaseRegistry([sc])


# --------------------------------------------------------------------------- #
# Текущая витрина (контекст обработки обновления)
# --------------------------------------------------------------------------- #
_current: ContextVar[Optional[Showcase]] = ContextVar("current_showcase", default=None)
_registry: Optional[ShowcaseRegistry] = None


def set_registry(registry: Optional[ShowcaseRegistry]) -> None:
    global _registry
    _registry = registry


def current() -> Showcase:
    """Витрина, обрабатывающая текущее обновление.

    Без контекста: единственная витрина реестра; без реестра (тесты, dev) —
    наследная витрина по настройкам. При нескольких витринах отсутствие контекста
    — ошибка, а не тихий выбор «первой»: иначе данные разных витрин смешались бы.
    """
    sc = _current.get()
    if sc is not None:
        return sc
    if _registry is None:
        return legacy_showcase()
    if len(_registry) == 1:
        return _registry.first
    raise RuntimeError("showcase context is not set (multiple showcases configured)")


@contextmanager
def use_showcase(sc: Showcase):
    tok = _current.set(sc)
    try:
        yield sc
    finally:
        _current.reset(tok)


def showcase_middleware(registry: ShowcaseRegistry):
    """Outer-middleware уровня update: выбирает витрину по боту-получателю."""

    async def middleware(handler, event, data):
        bot = data.get("bot")
        sc = registry.by_bot_id(getattr(bot, "id", 0))
        if sc is None:
            logger.warning("update for unknown bot id=%s dropped", getattr(bot, "id", None))
            return None
        with use_showcase(sc):
            return await handler(event, data)

    return middleware
