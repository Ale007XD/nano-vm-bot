"""
conftest_mfdba.py — изоляция для test_mfdba_core.py.

Кладётся в корень репо. CI копирует его в tests/ и подключает через
  -p tests.conftest_mfdba --noconftest

Проблема (три слоя):
  1. app/__init__.py и app/core/__init__.py делают eager-import при любом
     обращении к пакету — тянут config, database, memory, sqlalchemy, …
  2. Settings() требует BOT_TOKEN / OPENROUTER_API_KEY из env.
  3. app/core/__init__.py импортирует skill_loader, SkillInfo из skills_loader,
     но эти имена были удалены при рефакторинге на OpenClawExecutor.

Решение:
  • Заглушаем все тяжёлые внешние пакеты через sys.modules.
  • Ставим env-переменные-заглушки для Settings().
  • Загружаем РЕАЛЬНЫЙ skills_loader.py через importlib.util напрямую
    (в обход app/__init__ chain) и дополняем его алиасами
    skill_loader / SkillInfo, которые ожидает app/core/__init__.py.
  • app и app.core НЕ перекрываем — Python находит реальные пакеты на диске.
"""
from __future__ import annotations

import importlib.util
import os
import pathlib
import sys
import types
from unittest.mock import AsyncMock, MagicMock

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _stub(name: str) -> types.ModuleType:
    if name not in sys.modules:
        m = types.ModuleType(name)
        sys.modules[name] = m
    return sys.modules[name]


def _install_stubs() -> None:
    # ── внешние тяжёлые пакеты ──────────────────────────────────────────────
    for name in [
        "chromadb", "chromadb.config", "chromadb.api", "chromadb.api.types",
        "fastembed",
        "aiogram", "aiogram.types", "aiogram.filters",
        "aiogram.fsm", "aiogram.fsm.context",
        "aiogram.fsm.storage", "aiogram.fsm.storage.memory",
        "apscheduler", "apscheduler.schedulers",
        "apscheduler.schedulers.asyncio",
        "apscheduler.triggers", "apscheduler.triggers.cron",
        "cryptography", "cryptography.fernet",
        "httpx", "aiohttp", "aiofiles",
        "sqlalchemy", "sqlalchemy.orm", "sqlalchemy.ext",
        "sqlalchemy.ext.asyncio", "sqlalchemy.future",
        "sqlalchemy.orm.decl_api",
        "aiosqlite",
    ]:
        _stub(name)

    sa_orm = sys.modules["sqlalchemy.orm"]
    sa_orm.DeclarativeBase = MagicMock
    sa_orm.Mapped = MagicMock
    sa_orm.mapped_column = MagicMock(return_value=MagicMock())
    sa_orm.relationship = MagicMock(return_value=MagicMock())

    sa = sys.modules["sqlalchemy"]
    for attr in ["Column", "Integer", "String", "Boolean", "DateTime",
                 "Text", "JSON", "ForeignKey", "create_engine",
                 "event", "inspect", "select", "func"]:
        setattr(sa, attr, MagicMock())
    sa.orm = sa_orm

    sa_ext = sys.modules["sqlalchemy.ext.asyncio"]
    sa_ext.AsyncSession = MagicMock
    sa_ext.create_async_engine = MagicMock(return_value=MagicMock())
    sa_ext.async_sessionmaker = MagicMock(return_value=MagicMock())

    # ── env-заглушки — Settings() не упадёт на отсутствующих полях ──────────
    os.environ.setdefault("BOT_TOKEN", "fake-ci-token")
    os.environ.setdefault("OPENROUTER_API_KEY", "fake-ci-key")

    # ── app.core.* leaf-модули (cascade stubs) ───────────────────────────────
    # Регистрируем ДО того, как Python выполнит app/core/__init__.py

    _make_stub("app.core.database", {
        "init_db": AsyncMock(),
        "get_db": MagicMock(),
        "get_or_create_user": AsyncMock(),
        "save_message": AsyncMock(),
        "User": MagicMock(),
        "Message": MagicMock(),
        "UserState": MagicMock(),
    })
    _make_stub("app.core.memory", {
        "vector_memory": MagicMock(),
        "VectorMemory": MagicMock(),
    })
    _make_stub("app.core.llm_client_v2", {
        "llm_client": MagicMock(),
        "Message": MagicMock(),
        "LLMResponse": MagicMock(),
    })
    _make_stub("app.core.llm_client", {
        "Message": MagicMock(),
        "LLMResponse": MagicMock(),
    })
    _make_stub("app.core.scheduler",   {"scheduler": MagicMock()})
    _make_stub("app.core.logger",      {"setup_logging": MagicMock()})
    _make_stub("app.core.crypto",      {"encrypt": MagicMock(return_value=b"enc"),
                                        "decrypt": MagicMock(return_value="dec")})
    _make_stub("app.core.web_search",  {"web_search": AsyncMock(return_value=[])})

    # ── РЕАЛЬНЫЙ skills_loader — загружаем через spec, минуя __init__ chain ──
    # app/core/__init__.py ожидает skill_loader и SkillInfo,
    # которых нет в файле после рефакторинга — добавляем алиасы.
    _load_real_skills_loader()


def _make_stub(name: str, attrs: dict) -> None:
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m


def _load_real_skills_loader() -> None:
    """Загрузить реальный skills_loader.py напрямую через importlib.util."""
    sl_path = pathlib.Path("app/core/skills_loader.py")
    if not sl_path.exists():
        raise FileNotFoundError(f"Cannot find {sl_path} — run pytest from repo root")

    spec = importlib.util.spec_from_file_location("app.core.skills_loader", sl_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    # Добавляем алиасы, которые app/core/__init__.py ещё ожидает
    if not hasattr(module, "skill_loader"):
        module.skill_loader = module.OpenClawExecutor()
    if not hasattr(module, "SkillInfo"):
        module.SkillInfo = MagicMock()
    if not hasattr(module, "SkillLoader"):
        module.SkillLoader = module.OpenClawExecutor

    sys.modules["app.core.skills_loader"] = module


# Устанавливаем всё ДО любого импорта из app.*
_install_stubs()
