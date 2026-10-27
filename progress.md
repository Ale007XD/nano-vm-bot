# Прогресс разработки Alex-Nano-Bot

## Последнее обновление: 27.10.2026 — v1.8.0 (витрина, MCP, SKILL.md, безопасность, зависимости)

---

## ✅ Выполнено (40 фич)

### v1.8.0 — витрина, MCP, SKILL.md, безопасность, зависимости (26–27.10.2026)

**Витрина службы поддержки** — отдельная точка входа `app/bot_showcase.py` + `app/handlers/showcase.py`:
демо-бот со своим токеном (`SHOWCASE_BOT_TOKEN`) отвечает строго по загруженному корпусу
(отдельная коллекция ChromaDB, перечень источников, честный отказ при отсутствии данных).
`/corpus_stats`, `/corpus_upload` (admin), запуск `docker compose --profile showcase up -d showcase`.

**Внешние инструменты и навыки:**
- MCP-клиент `app/core/mcp.py` (SDK 1.x/2.x; stdio / Streamable HTTP): `call_tool <server>__<tool>`,
  `/mcp` (admin), конфиг `MCP_SERVERS` — закрыт P-mcp
- Agent Skills (SKILL.md): инструкционные навыки agentskills.io, установка документом `.md` (admin)

**Безопасность:**
- SSRF-защита `app/core/net_guard.py` — блок приватных адресов для `knowledge_base`/`site_monitor`
- Allowlist `RUNTIME_USER_TOOLS` для не-админов; `user_id` навыку — из контекста VM
- Админ-гейт на создание/редактирование/удаление `.py`-навыков и SKILL.md
- Изоляция базы знаний per-user: `UNIQUE(user_id, url)`, выборки по владельцу, HTML-экранирование

**Зависимости** — requirements.txt переписан: aiogram ≥3.31, pydantic ≥2.13, chromadb ≥1.5,
fastembed ≥0.9, sqlalchemy[asyncio] ≥2.1, mcp ≥1.2,<3; dev-инструменты → `requirements-dev.txt`;
CI — Python 3.12 + `requirements-dev.txt`. Фикс конфликта пинов aiogram×mcp.

### v1.6.0 — мультиюзерность, лимиты, учёт токенов, UX (25–26.10.2026)

- `app/core/access.py`: открытый доступ (`BOT_OPEN_ACCESS`), бан-лист, суточные лимиты; `/ban`, `/unban`, `/users`
- `app/core/usage.py`: per-user учёт LLM-токенов (ContextVar-сессии, `usage_daily`), `/usage`
- Динамические лимиты: `bot_settings`, `/limits`, `/setlimit <messages|requests|tokens> <N>` — без рестарта
- Промпты из env (`*_SYSTEM_PROMPT`), JSON-mode + `reasoning_effort` для planner на Groq
- Groq-модели после Enterprise-only ротации llama-3.x: `openai/gpt-oss-20b/120b`, vision `qwen/qwen3.8-27b`
- UX: typing-индикатор, дата/время в промпт, Markdown→HTML, статусные стадии PlanBot
- Фото: подпись+URL → база знаний, без URL → резюме, без подписи → vision; фото-посты в каналах → резюме владельцу

### patch 6 — Planner JSON fix + RAG pipeline (25.04.2026)

`app/runtime/planner.py`: `_fix_newlines_in_strings()` вместо сломанного `re.sub`,
`_ensure_rag_in_system()` — страховка контракта RAG→VM. `messages.py`: n_memories 4→8 + даты.
Верифицировано в проде: Planner failed — 0, store_memory персистирует.

### patch 1–5, v1.0.0–v1.5.0 (фев–апр 2026)

FastBot/PlanBot/SkillBot, ChromaDB, навыки, Docker, голос, планировщик, KB, Runtime VM (DSL v0.1),
ToolRegistry, StateContext персистентность, CI/CD.

---

## 🐛 Известные дефекты

Активных блокирующих дефектов нет.

| ID | Описание | Severity | Статус |
|----|----------|----------|--------|
| ~~BUG-4~~ | ImportError на ToolError | HIGH | ✅ patch 5 |
| ~~BUG-planner~~ | Planner failed 100% — re.sub ломал JSON | CRITICAL | ✅ patch 6 |
| ~~BUG-rag~~ | RAG контекст не доходил до call_llm | HIGH | ✅ patch 6 |
| BUG-20…25 | LOW-блок из реестра аудита Фазы 1 | LOW | 📋 pending |

---

## 📋 Pending

| ID | Задача | Приоритет |
|----|--------|-----------|
| P-byok | BYOK: свой ключ LLM-провайдера у пользователя (без лимитов, без фоллбэка) | high |
| P-escalation | Витрина: эскалация на живого оператора (группа поддержки, топик на клиента) | high |
| P-billing | Биллинг: пакеты запросов (кредиты); Telegram Stars первым адаптером, ЮKassa/Stripe — заглушки | high |
| P-5 | `asyncio.gather` для параллельных skills | low |
| P-dsl-4 | DSL v0.4: `$step.output.field`, `$memories[0]` | low |
| P-dsl-5 | DSL v0.5: typed conditions (AST) | low |
| P-dag | MFDBA-DAG: graph runtime + Redis queue | low |
| P-sandbox | Изоляция `vm.py` (DinD / WASM) | low |
| P-decouple | Миграция `app/runtime/` в библиотеку MFDBA | low |
| P-volume | `./app:/app/app` volume mount в `docker-compose.yml` | low |
| P-planner-v2 | Planner v2: `retrieve_memory` перед генерацией программы | low |
| P-bugs-low | BUG-20…25 — LOW-блок из реестра аудита Фазы 1 | low |

Закрыто с прошлого обновления: P-mcp (реальный MCP-клиент), P-2 (BOT_TOKEN мокается в conftest),
P-3 (aiohttp убран из app), P-yt (`yt_skill({...})`), P-voice (runtime-ветка в `handle_voice`),
P-7 (модели в ProviderConfig, БД), P-4 (`VISION_MODEL` в settings), P-pytest-marks (маркеры в pytest.ini),
P-deploy-name (сервис `bot`).

---

## 🔧 Текущая конфигурация

**LLM:** Groq p1 → OpenRouter p2 → Anthropic p3 → OpenAI p4  
**Groq:** `openai/gpt-oss-20b` (default/coder), `openai/gpt-oss-120b` (planner), `qwen/qwen3.8-27b` (vision)

**Runtime VM:** DSL v0.1 · JSON-mode + reasoning_effort · RAG: 8 memories + даты ·
tool policy (`RUNTIME_USER_TOOLS`) · MCP 1.x/2.x

**Skills:** 8 .py-навыков (calculator, echo, reminder, import_chat, knowledge_base, site_monitor,
validated_answer, youtube_transcript) + SKILL.md в `skills/external/`

**Безопасность:** net_guard (SSRF) · tool policy · админ-гейт навыков · KB per-user ·
мультиюзерность с лимитами и бан-листом

**Витрина:** `bot_showcase.py` · корпус в ChromaDB · `--profile showcase`

**Тесты:** 290 (полный набор; локально в WSL без aiogram — 274 + 16 setup-errors) · ruff: clean  
**Память:** ChromaDB `./data/vector_store` · персистентна

**Deploy:**
```bash
docker compose build bot && docker compose up -d bot
# витрина:
docker compose --profile showcase up -d showcase
```
