# Changelog

All notable changes to Alex-Nano-Bot will be documented in this file.

## [1.8.0] - 2026-10-27 (витрина, MCP, SKILL.md, безопасность, модернизация зависимостей)

### Added — Витрина службы поддержки (отдельная точка входа)
- **`app/bot_showcase.py` + `app/handlers/showcase.py`**: демо-бот со своим токеном (`SHOWCASE_BOT_TOKEN`) отвечает клиентам строго по загруженному корпусу — чанки из отдельной коллекции ChromaDB, перечень источников, честный отказ при отсутствии данных
- Команды: `/start`, `/help`, `/corpus_stats`, `/corpus_upload` (загрузка `.md`/`.txt` документов, admin); запуск — `docker compose --profile showcase up -d showcase`

### Added — Внешние инструменты и навыки
- **MCP-клиент `app/core/mcp.py`** (SDK 1.x/2.x; stdio / Streamable HTTP): инструменты внешних серверов доступны VM как `call_tool <server>__<tool>`, Planner видит их список; `/mcp` (admin), конфиг `MCP_SERVERS`
- **Agent Skills (SKILL.md)**: инструкционные навыки открытого стандарта [agentskills.io](https://agentskills.io/specification) — папка в `skills/external/` или установка документом `.md` (admin), запуск из `/skills`

### Security
- **SSRF-защита `app/core/net_guard.py`**: `knowledge_base`/`site_monitor` не ходят на приватные адреса (localhost, RFC1918, 169.254.169.254); исключение — `ALLOW_PRIVATE_URLS=true`
- **Политика инструментов**: не-админам в Runtime VM доступен только allowlist `RUNTIME_USER_TOOLS`; `user_id` навыку подставляется из контекста VM, а не из ответа planner
- **Админ-гейт** на создание/редактирование/удаление Python-навыков и установку SKILL.md
- **Изоляция базы знаний per-user**: ID статьи от пары «пользователь+URL», `UNIQUE(user_id, url)`, выборки и обновления только по владельцу; HTML-экранирование заголовков/саммари из интернета

### Changed — Зависимости (requirements.txt переписан)
- `aiogram` 3.4.1 → `>=3.31,<4`; `pydantic` 2.5.3 → `>=2.13`; `httpx` 0.27.0 → `>=0.28`; `pydantic-settings` → `>=2.16`; `chromadb` 0.4.24 → `>=1.5`; `fastembed` → `>=0.9`; `sqlalchemy[asyncio]` → `>=2.1`; `mcp` → `>=1.2,<3`
- Разработческие инструменты (`pytest`, `pytest-asyncio`, `ruff`, `mypy`) вынесены в `requirements-dev.txt`
- CI: Python 3.12, установка `requirements-dev.txt`, `ruff check app/ tests/` + `pytest tests/ -v`
- Тесты: 126 → **290** (runtime, access, usage, mcp, net_guard, kb_isolation, tool_policy, skills_admin, skillmd, corpus, bot)

### Fixed
- Конфликт пинов `aiogram`×`mcp` (pydantic `<2.6` против `>=2.12`), ломавший установку зависимостей и Docker-сборку

## [1.6.0] - 2026-10-26 (мультиюзерность, лимиты, учёт токенов, UX)

### Added — Мультиюзерность
- **`app/core/access.py`**: закрытый режим по умолчанию (только `ADMIN_IDS`), открытый — `BOT_OPEN_ACCESS=true`; бан-лист; `check_daily_limit`; админы — bypass; сбой БД не запирает пользователей
- Команды `/ban <id>`, `/unban <id>`, `/users` (admin); колонка `users.is_banned` с автомиграцией
- **`app/core/usage.py`**: per-user учёт LLM-токенов (ContextVar-сессии, таблица `usage_daily`), команда `/usage`
- Динамические лимиты: таблица `bot_settings`, `/limits`, `/setlimit <messages|requests|tokens> <N>` — перекрывают env без рестарта

### Added
- Системные промпты читаются из env (`*_SYSTEM_PROMPT`, одной строкой); JSON-mode + `reasoning_effort` для planner на Groq
- Фото: подпись с URL → статья в базу знаний, без URL → резюме текста, без подписи → vision-анализ; фото-посты в отслеживаемых каналах → резюме владельцу
- Статусные стадии PlanBot («Составляю план… → Выполняю шаг 2/4 → Формирую ответ»)

### Fixed
- Модели Groq после Enterprise-only ротации llama-3.x: дефолты `openai/gpt-oss-20b/120b`, vision `qwen/qwen3.8-27b`; тело API-ошибок логируется
- UX: продлеваемый индикатор «печатает…», текущие дата/время в промпт (модель не галлюцинирует дату), Markdown → Telegram HTML, чанки по 4000

### Changed
- README переписан по текущему состоянию; `APP_VERSION` 1.5.0 → 1.6.0

## [1.5.0] - 2026-04-25 (patch 6: Planner JSON fix + RAG pipeline)

### Fixed — Planner JSON parser (критический)
- **`app/runtime/planner.py`**: удалён `re.sub(r'(?<!\\)\n', r'\\n', json_str)` — ломал структуру JSON при каждом запросе, Planner падал в fallback на 100% запросов
- Добавлен `_fix_newlines_in_strings()` — посимвольный проход, экранирует `\n`/`\r` только внутри строковых значений, не трогает структуру JSON
- Добавлен `_ensure_rag_in_system()` — детерминированная гарантия контракта: если Planner не положил RAG-блок в `params.system` первого `call_llm` шага — подставляется автоматически. Не хардкод логики, страховка контракта между `messages.py` и VM

### Fixed — RAG pipeline
- **`app/handlers/messages.py`**: `n_memories=4` → `n_memories=8` — ChromaDB возвращает больше релевантных записей
- **`app/handlers/messages.py`**: `mem_texts` дополнен датой: `"- {content} (сохранено: {created_at[:10]})"` — LLM видит хронологию и корректно разрешает конфликты между записями с одинаковым номером сообщения

### Verified — store_memory работает end-to-end
- ChromaDB `memories` collection: 10+ записей с корректным `user_id=1` (db_user.id)
- RAG читает и пишет с одним `user_id` — несоответствия нет
- `_ensure_rag_in_system` подтверждён в проде: контекст доходит до `call_llm`

### Result
```
Planner failed → 0 (было: 100% запросов)
Injected memories: 8 (было: 4)
store_memory: работает, данные персистируются в ./data/vector_store
```

---

## [1.5.0] - 2026-04-24 (patch 5: ToolRegistry + P-6 + deprecation cleanup)

### Added — ToolRegistry (P-6 завершён)
- **`app/runtime/tool_registry.py`** — `ToolRegistry` адаптирует `SkillLoader` к `ctx.tools.execute()`
- **`app/runtime/__init__.py`** — `ToolRegistry` в публичном API

### Changed — CallToolInstruction, messages.py, skills_loader.py
- Guard на отсутствие `tool` param и `ctx.tools is None`
- `tools=None` → `tools=_tool_registry` в `messages.py`
- `SkillLoader` и `OpenClawExecutor` разделены (SRP)

### Fixed — DeprecationWarnings (5 штук)
- aiogram: `DefaultBotProperties`
- Pydantic v2: `ConfigDict` в `state_context.py`, `step_result.py`
- Python 3.12: `datetime.now(timezone.utc)` в `builder.py`

### Tests
- `test_runtime.py`: 51 → 60 тестов
- Итого: **79/79 passed, 1 warning**

---

## [1.5.0] - 2026-04-22 (patch 4)

### Added — CI/CD pipeline
- `.github/workflows/python-tests.yml` — GitHub Actions, Python 3.12

### Added — OpenClaw + MCP архитектура (scaffold)
### Changed — Ренейминг агентов (P-rename): Nanobot→FastBot, Claudbot→PlanBot, Moltbot→SkillBot

---

## [1.5.0] - 2026-04-21 (patch 3)
### Added — Тесты runtime: 51/51 passed, `tests/conftest.py`

## [1.5.0] - 2026-04-21 (patch 2)
### Fixed — StateContext персистентность: `from_db()` + `to_db_context()` + `flush()`

## [1.5.0] - 2026-04-21
### Added — Runtime VM: δ(S, Program) → S', DSL v0.1, `/mode` → ⚙️ Runtime VM

## [1.5.0] - 2026-04-20
### Fixed — LLM-модели (Groq mixtral → llama), `/providers` UI

## [1.4.0] - 2026-04-19
### Added — Knowledge Base скилл
### Fixed — BUG-1 scheduler, BUG-2 coroutine, BUG-3 двойной singleton

## [1.3.0] - 2026-02-08
### Added — Голосовые сообщения (Groq Whisper), Multi-provider LLM v2, APScheduler

## [1.2.0] - 2026-02
### Added — Hot-swap провайдеров, Fernet-шифрование, access control

## [1.1.0] - 2026-02-07
### Added — Русский интерфейс, Groq API

## [1.0.0] - 2026-02
### Added — FastBot/PlanBot/SkillBot, ChromaDB, динамические навыки, Docker Compose
