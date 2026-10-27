"""
BYOK (Bring Your Own Key): пользовательские API-ключи LLM-провайдеров.

Хранение: таблица user_api_keys, ключ шифруется Fernet (app/core/crypto.py,
ENCRYPTION_KEY обязателен; без него сохранить ключ нельзя — fail closed).

Семантика (решение владельца): если у пользователя есть хотя бы один ключ,
ВСЕ его LLM-запросы идут только на его ключи (без фолбэка на общие ключи
бота), суточные лимиты на него не распространяются.

Модуль без зависимостей от aiogram; сессии БД передаются параметром
(по умолчанию — основная фабрика приложения).
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone
from typing import Dict, Optional, Tuple

import httpx
from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Integer,
    String,
    Text,
    UniqueConstraint,
    delete,
    select,
)

from app.core.ext_db import ExtBase, resolve_session_maker

logger = logging.getLogger(__name__)

SUPPORTED_PROVIDERS = ("groq", "openrouter", "anthropic", "openai")
CACHE_TTL = 60.0

# Эндпоинты проверки ключа: дешёвый GET списка моделей
_VALIDATION = {
    "groq": ("https://api.groq.com/openai/v1/models", "bearer"),
    "openrouter": ("https://openrouter.ai/api/v1/key", "bearer"),
    "openai": ("https://api.openai.com/v1/models", "bearer"),
    "anthropic": ("https://api.anthropic.com/v1/models", "anthropic"),
}

_KEY_RE = re.compile(r"^[\x21-\x7e]{16,400}$")  # печатные ASCII без пробелов


class BYOKUnavailable(RuntimeError):
    """Шифрование не настроено (нет ENCRYPTION_KEY) — хранить ключи нельзя."""


class BYOKError(Exception):
    """Запрос на пользовательский ключ не удался (без фолбэка на общие ключи)."""


class UserApiKey(ExtBase):
    __tablename__ = "user_api_keys"
    __table_args__ = (UniqueConstraint("telegram_id", "provider", name="uq_user_provider"),)

    id = Column(Integer, primary_key=True)
    telegram_id = Column(BigInteger, nullable=False, index=True)
    provider = Column(String(32), nullable=False)
    encrypted_key = Column(Text, nullable=False)
    key_hint = Column(String(8), nullable=False, default="")  # последние 4 символа
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))


# --------------------------------------------------------------------------- #
# Кэш: {telegram_id: (expires_monotonic, {provider: plaintext_key})}
# Ключи в памяти процесса — как и ключи из .env; TTL ограничивает «хвост» после удаления.
# --------------------------------------------------------------------------- #
_cache: Dict[int, Tuple[float, Dict[str, str]]] = {}


def invalidate(telegram_id: Optional[int] = None) -> None:
    if telegram_id is None:
        _cache.clear()
    else:
        _cache.pop(telegram_id, None)


def validate_key_format(provider: str, key: str) -> Optional[str]:
    """Вернёт текст ошибки или None. Это только sanity-проверка, не аутентификация."""
    if provider not in SUPPORTED_PROVIDERS:
        return f"Неизвестный провайдер. Доступны: {', '.join(SUPPORTED_PROVIDERS)}"
    if not _KEY_RE.match(key or ""):
        return "Ключ выглядит некорректно (16–400 печатных символов без пробелов)"
    return None


def _crypto():
    from app.core import crypto

    try:
        crypto._get_fernet()
    except RuntimeError as e:
        raise BYOKUnavailable(str(e)) from e
    return crypto


async def set_user_key(
    telegram_id: int, provider: str, key: str, session_maker=None
) -> str:
    """Сохранить (или заменить) ключ. Возвращает подсказку (последние 4 символа)."""
    err = validate_key_format(provider, key)
    if err:
        raise ValueError(err)
    crypto = _crypto()
    encrypted = crypto.encrypt_key(key)
    hint = key[-4:]
    sm = resolve_session_maker(session_maker)
    async with sm() as session:
        row = (
            await session.execute(
                select(UserApiKey).where(
                    UserApiKey.telegram_id == telegram_id, UserApiKey.provider == provider
                )
            )
        ).scalar_one_or_none()
        if row:
            row.encrypted_key = encrypted
            row.key_hint = hint
        else:
            session.add(
                UserApiKey(
                    telegram_id=telegram_id,
                    provider=provider,
                    encrypted_key=encrypted,
                    key_hint=hint,
                )
            )
        await session.commit()
    invalidate(telegram_id)
    return hint


async def delete_user_key(
    telegram_id: int, provider: Optional[str] = None, session_maker=None
) -> int:
    """Удалить ключ провайдера (или все). Возвращает число удалённых."""
    sm = resolve_session_maker(session_maker)
    stmt = delete(UserApiKey).where(UserApiKey.telegram_id == telegram_id)
    if provider:
        stmt = stmt.where(UserApiKey.provider == provider)
    async with sm() as session:
        res = await session.execute(stmt)
        await session.commit()
    invalidate(telegram_id)
    return res.rowcount or 0


async def list_user_keys(telegram_id: int, session_maker=None) -> Dict[str, str]:
    """{provider: hint} — без расшифровки, для показа пользователю."""
    sm = resolve_session_maker(session_maker)
    async with sm() as session:
        rows = (
            await session.execute(
                select(UserApiKey.provider, UserApiKey.key_hint).where(
                    UserApiKey.telegram_id == telegram_id
                )
            )
        ).all()
    return {p: h for p, h in rows}


async def get_user_keys(telegram_id: int, session_maker=None) -> Dict[str, str]:
    """{provider: plaintext_key} пользователя (с кэшем на CACHE_TTL)."""
    hit = _cache.get(telegram_id)
    now = time.monotonic()
    if hit and hit[0] > now:
        return dict(hit[1])

    sm = resolve_session_maker(session_maker)
    async with sm() as session:
        rows = (
            await session.execute(
                select(UserApiKey.provider, UserApiKey.encrypted_key).where(
                    UserApiKey.telegram_id == telegram_id
                )
            )
        ).all()
    keys: Dict[str, str] = {}
    if rows:
        crypto = _crypto()
        for provider, enc in rows:
            try:
                keys[provider] = crypto.decrypt_key(enc)
            except ValueError:
                logger.warning("BYOK: cannot decrypt %s key of user %s", provider, telegram_id)
    _cache[telegram_id] = (now + CACHE_TTL, keys)
    return dict(keys)


async def has_user_keys(telegram_id: int, session_maker=None) -> bool:
    """Есть ли у пользователя BYOK-ключи. Любая ошибка → False (не блокируем работу)."""
    try:
        return bool(await get_user_keys(telegram_id, session_maker))
    except Exception as e:
        logger.warning("BYOK lookup failed for %s: %s", telegram_id, e)
        return False


async def current_user_keys() -> Dict[str, str]:
    """Ключи пользователя текущей сессии учёта (usage ContextVar). Вне сессии — {}."""
    from app.core.usage import get_session_user

    uid = get_session_user()
    if uid is None:
        return {}
    try:
        return await get_user_keys(uid)
    except Exception as e:
        logger.warning("BYOK lookup failed for %s: %s", uid, e)
        return {}


async def resolve_provider_key(provider: str, shared_key: Optional[str]) -> Tuple[Optional[str], bool]:
    """Ключ для побочных функций (vision, голос), идущих мимо chat().

    Возвращает (ключ, byok_активен). При активном BYOK — только ключ пользователя
    (или None, если у него нет ключа этого провайдера): общий ключ не используется.
    """
    user_keys = await current_user_keys()
    if user_keys:
        return user_keys.get(provider), True
    return (shared_key or None), False


async def validate_key_live(
    provider: str, key: str, transport: Optional[httpx.AsyncBaseTransport] = None
) -> Tuple[Optional[bool], str]:
    """Проверка ключа живым запросом. (True, ok) / (False, причина) / (None, «не удалось проверить»)."""
    url, style = _VALIDATION[provider]
    headers = (
        {"x-api-key": key, "anthropic-version": "2023-06-01"}
        if style == "anthropic"
        else {"Authorization": f"Bearer {key}"}
    )
    try:
        async with httpx.AsyncClient(timeout=15.0, trust_env=False, transport=transport) as c:
            r = await c.get(url, headers=headers)
    except httpx.HTTPError as e:
        return None, f"сеть: {type(e).__name__}"
    if r.status_code == 200:
        return True, "ok"
    if r.status_code in (401, 403):
        return False, "провайдер отклонил ключ"
    return None, f"провайдер ответил HTTP {r.status_code}"
