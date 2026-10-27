# Alex-Nano-Bot

Self-hosted Telegram AI-бот с детерминированным Program-driven рантаймом, векторной памятью (RAG), базой знаний, multi-provider LLM и мультиюзерным режимом с лимитами.

**Версия:** 1.8.0 · **Тесты:** 290 passed 🟢 · ruff: clean · Python 3.12

---

## Возможности

🤖 **Три агента + Runtime VM:**
- ⚡ **FastBot** — быстрый помощник, RAG из переписки и заметок
- 🧩 **PlanBot** — многошаговое рассуждение с прогресс-стадиями («Составляю план… → Выполняю шаг 2/4 → Формирую ответ»)
- 🔧 **SkillBot** — менеджер навыков, генерация кода
- ⚙️ **Runtime VM** — детерминированная Program-driven система δ(S, Program) → S'

⚙️ **Runtime VM:**
- Planner генерирует JSON-программу (JSON-mode + `reasoning_effort` на Groq)
- VM исполняет: `call_llm`, `respond`, `store_memory`, `call_tool`
- Персистентный `StateContext` через `UserState.context`
- Устойчивость к мусору от LLM: битые шаги/params не роняют прогон

🧠 **Векторная память (RAG):** ChromaDB + fastembed, изолирована per-user

👥 **Мультиюзерность (один админ):**
- Открытый режим (`BOT_OPEN_ACCESS=true`) — бот отвечает всем
- Бан-лист: `/ban`, `/unban`
- Суточные лимиты на пользователя (UTC): сообщения / LLM-запросы / токены
- Учёт расхода токенов per-user: `/usage` (у админа — тотал по всем)
- Динамическое управление лимитами: `/setlimit`, `/limits` — без рестарта

📚 **Контент:**
- Пересланное фото с подписью: подпись с URL → статья в базу знаний + резюме; подпись без URL → резюме текста; без подписи → vision-анализ (Groq)
- Посты в отслеживаемых каналах: с URL → в базу знаний; фото + подпись → резюме владельцу
- Импорт JSON-экспорта Telegram-чата, YouTube-транскрипты
- Веб-поиск (`app/core/web_search.py`): Serper → SerpAPI → Bing → DuckDuckGo (без ключей); FastBot и PlanBot автоматически ищут в интернете, когда вопрос того требует; результаты доступны навыкам `validated_answer` и `knowledge_base`

🛠 **UX:**
- Индикатор «печатает…» продлевается на всё время обработки
- Текущие дата/время инжектируются в промпт — модель не галлюцинирует дату
- Markdown в ответах LLM автоматически конвертируется в Telegram HTML
- Длинные ответы корректно режутся по лимиту 4096

🔌 **Multi-provider LLM:** Groq → OpenRouter → Anthropic → OpenAI, фоллбэк с ретраями, горячая замена ключей (`/providers`), выбор моделей по ролям (`/models`, персистентно в БД). Тело API-ошибок логируется.

🧩 **Внешние навыки:**
- **Agent Skills (SKILL.md)** — инструкционные навыки открытого стандарта [agentskills.io](https://agentskills.io/specification): папка в `skills/external/` или установка документом `.md` (админ); запуск из `/skills` — исполняет LLM по инструкции
- **MCP-инструменты** ([Model Context Protocol](https://registry.modelcontextprotocol.io)) — внешние серверы (stdio / Streamable HTTP, SDK 1.x/2.x) через `MCP_SERVERS`; их инструменты доступны VM как `call_tool <server>__<tool>`, Planner видит их список; статус — `/mcp` (admin)

📚 **Витрина службы поддержки (отдельная точка входа):**
- Демо-бот с собственным токеном (`SHOWCASE_BOT_TOKEN`) отвечает клиентам **строго по загруженному корпусу** базы знаний и ничего не выдумывает.
- Администратор загружает документы (`.md`, `.txt`) боту — они режутся на чанки и индексируются в отдельной коллекции ChromaDB.
- Ответ строится только из релевантных чанков с перечнем источников; при отсутствии данных — честный отказ.
- Команды: `/start`, `/help`, `/corpus_stats` (статус корпуса), `/corpus_upload` — загрузка документов в корпус (админ).

---

## Быстрый старт

```bash
git clone https://github.com/Ale007XD/Alex-Nano-Bot.git
cd Alex-Nano-Bot
cp .env.example .env
# заполнить .env
docker compose build && docker compose up -d
docker compose logs -f
```

> ⚠️ Имя сервиса в docker-compose: `bot`. Команды: `docker compose build bot && docker compose up -d bot`

---

## Команды

| Команда | Описание |
|---------|----------|
| `/start` | Запуск |
| `/mode` | Смена агента (FastBot / PlanBot / SkillBot / Runtime VM) |
| `/skills` | Менеджер навыков (в т.ч. SKILL.md-навыки) |
| `/mcp` | Статус внешних MCP-инструментов, `/mcp refresh` (admin) |
| `/memory` | Управление памятью |
| `/providers` | Управление ключами LLM-провайдеров (admin) |
| `/models` | Выбор моделей по ролям (admin) |
| `/remind`, `/daily`, `/weekly`, `/tasks` | Планировщик |
| `/usage` | Ваш расход LLM-токенов за сегодня (UTC) |
| `/limits` | Текущие лимиты + расход по всем (admin) |
| `/setlimit <messages\|requests\|tokens> <N>` | Задать суточный лимит, 0 — снять (admin) |
| `/ban <id>`, `/unban <id>` | Бан-лист (admin) |
| `/users` | Активность пользователей за сегодня (admin) |
| `/clear` | Очистить историю |

---

## Runtime VM

```
User input → Planner (JSON-mode) → Program (JSON) → VM → StepResult[] → Response
                                                     ↓
                                              StateContext (персистентный)
                                                     ↓
                                         store_memory → ChromaDB
```

**DSL v0.1:** `call_llm`, `respond`, `store_memory`, `call_tool`  
**call_tool:** сначала локальные навыки (`.py`, `run(context)`), при отсутствии — внешние MCP-инструменты `<server>__<tool>`  
**RAG:** воспоминания пользователя + даты → `_ensure_rag_in_system` → `call_llm.params.system`

---

## Структура проекта

```
Alex-Nano-Bot/
├── app/
│   ├── agents/               # FastBot, PlanBot, SkillBot, router
│   ├── core/                 # config, database, llm_client_v2, memory, scheduler,
│   │   │                     # skills_loader (+SKILL.md), access (гейт+лимиты),
│   │   │                     # usage (учёт токенов), mcp (внешние инструменты),
│   │   │                     # net_guard (SSRF), web_search, crypto (Fernet)
│   ├── runtime/              # VM, Planner, StateContext, instructions/
│   │   └── instructions/     # call_llm, call_tool, respond, store_memory
│   ├── handlers/             # commands, messages, providers, skills, memory,
│   │                         # channel, reminders, showcase, fallback
│   ├── utils/                # keyboards, states, helpers (sanitize_html, typing)
│   ├── bot.py
│   └── bot_showcase.py       # демо-бот витрины службы поддержки (отдельная точка входа)
├── skills/system/ custom/ external/   # external: .py-навыки и папки с SKILL.md
├── data/                     # bot.db, vector_store/ (volume-mounted)
├── logs/                     # (volume-mounted)
├── tests/                    # 290 тестов: runtime, access, usage, mcp, net_guard (SSRF),
│                             # kb_isolation, tool_policy, skills_admin, skillmd, corpus, bot
├── requirements.txt          # runtime-зависимости (aiogram 3.31+, chromadb 1.5+, mcp 1.x/2.x)
├── requirements-dev.txt      # pytest, pytest-asyncio, ruff, mypy (для CI и разработки)
├── ruff.toml                 # зафиксированный набор правил линтера
├── docker-compose.yml
├── Dockerfile
└── .env.example
```

---

## CI/CD

GitHub Actions · Python 3.12 · установка `requirements-dev.txt` (runtime + pytest/ruff/mypy) · `ruff check app/ tests/` · `pytest tests/ -v` · **290/290 passed 🟢**

---

## Переменные окружения

| Переменная | Обязательная | По умолчанию | Описание |
|---|---|---|---|
| `BOT_TOKEN` | ✅ | — | Токен бота |
| `ADMIN_IDS` | ✅ | — | ID админов (через запятую); безлимит по лимитам |
| `OPENROUTER_API_KEY` | ✅ | — | Основной fallback-провайдер |
| `GROQ_API_KEY` | — | — | Основной провайдер + vision + whisper |
| `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` | — | — | Дополнительные провайдеры |
| `ENCRYPTION_KEY` | — | — | Fernet-ключ для хранения API-ключей в БД (`/providers`) |
| `BOT_OPEN_ACCESS` | — | `false` | `true` — открытый режим: бот отвечает всем |
| `USER_DAILY_MESSAGE_LIMIT` | — | `50` | Суточный лимит сообщений (0 — безлимит; меняется и через `/setlimit`) |
| `KB_CHANNEL_IDS` | — | — | Каналы для базы знаний |
| `KB_STALE_DAYS` | — | `30` | Статьи базы знаний старше N дней считаются устаревшими и обновляются при обращении |
| `MCP_SERVERS` | — | — | JSON-список MCP-серверов одной строкой: `[{"name":"fetch","command":"uvx","args":["mcp-server-fetch"]}]` или `[{"name":"x","url":"https://host/mcp"}]` (stdio требует `uv`, уже в образе) |
| `RUNTIME_USER_TOOLS` | — | `validated_answer` | Инструменты `call_tool` в Runtime VM, доступные не-админам (через запятую; MCP — `server__tool`). Админам доступны все; `user_id` в навык всегда подставляется из контекста, а не из ответа planner |
| `ALLOW_PRIVATE_URLS` | — | `false` | `true` — разрешить `knowledge_base`/`site_monitor` ходить на внутренние адреса (localhost, 10.x, 192.168.x, 169.254.x). По умолчанию запрещено (SSRF-защита) |
| `BYOK_ENABLED` | — | `true` | Разрешить пользователям свои API-ключи (`/byok set groq <ключ>`; провайдеры: groq, openrouter, anthropic, openai). Требует `ENCRYPTION_KEY`. С ключом запросы идут только на него (без фолбэка на ключи бота), суточные лимиты не применяются; голос и фото — только с ключом Groq |
| `OPERATOR_CHAT_ID` | — | `0` | Витрина: ID супергруппы поддержки (темы включены, бот — админ с правом управлять темами). Клиент зовёт оператора через `/operator` или кнопку под ответом «не нашёл»; для каждого клиента создаётся тема, сообщения пересылаются в обе стороны, `/close` в теме или `/endchat` у клиента завершает обращение. В карточку темы попадает переписка клиента с ботом (последние реплики, в памяти процесса, не в БД), следом — резюме от LLM (`SHOWCASE_MODEL`; при сбое остаётся только переписка). 0 — выключено. Развитие (тикеты, память клиента, веб): `docs/support-bot-design.md` |
| `BILLING_PROVIDERS` | — | пусто | Платежи за пакеты запросов (кредиты): `stars`, `yookassa`, `stripe` через запятую. Сверх бесплатного суточного лимита списывается 1 кредит за сообщение (админы и BYOK не списываются). Команды: `/buy`, `/balance`, `/paysupport`; админ: `/grant <id> <n>`, `/refund <№ платежа>` (Stars — автоматически, ЮKassa/Stripe — вручную в кабинете; возврат отклоняется, если кредиты потрачены). Пусто — выключено |
| `YOOKASSA_PROVIDER_TOKEN` / `STRIPE_PROVIDER_TOKEN` | — | пусто | Токены Telegram Payments из @BotFather; без токена провайдер не включается |
| `CREDIT_PACKAGES` | — | 2 пакета | JSON: `[{"id":"s50","credits":50,"title":"50 запросов","prices":{"XTR":100,"RUB":19900,"USD":249}}]` (цены в минимальных единицах: звёзды / копейки / центы) |
| `ENABLE_WEB_SEARCH` | — | `true` | Веб-поиск в FastBot/PlanBot и навыках |
| `SERPER_API_KEY` / `SERPAPI_KEY` / `BING_API_KEY` | — | — | Ключи поисковых API (рекомендуется Serper); без ключей — бесплатный DuckDuckGo |
| `WEB_SEARCH_RESULTS` | — | `3` | Результатов поиска на запрос |
| `EMBEDDING_MODEL` | — | `sentence-transformers/all-MiniLM-L6-v2` | Модель эмбеддингов (fastembed) |
| `SHOWCASE_BOT_TOKEN` | — | — | Токен демо-бота витрины (пусто — сервис витрины не запускается) |
| `SHOWCASE_MODEL` | — | `openai/gpt-oss-20b` | Модель демо-бота витрины |
| `SHOWCASE_SYSTEM_PROMPT` | — | вшитый | Системный промпт демо-бота витрины |
| `CORPUS_CHUNK_SIZE` | — | `1200` | Размер чанка корпуса (символы) |
| `CORPUS_CHUNK_OVERLAP` | — | `200` | Перекрытие чанков корпуса (символы) |
| `CORPUS_TOP_K` | — | `5` | Чанков в контексте ответа |
| `BOT_TIMEZONE` | — | `Europe/Moscow` | Зона времени в ответах и планировщике |
| `FASTBOT_MODEL` / `SKILLBOT_MODEL` / `PLANBOT_MODEL` | — | gpt-oss-20b/20b/120b | Модели агентов (Groq) |
| `VISION_MODEL` | — | `qwen/qwen3.8-27b` | Мультимодальная модель Groq для фото |
| `WHISPER_MODEL` | — | `whisper-large-v3` | Распознавание голоса |
| `FASTBOT_SYSTEM_PROMPT` / `PLANBOT_SYSTEM_PROMPT` / `SKILLBOT_SYSTEM_PROMPT` / `PLANNER_SYSTEM_PROMPT` | — | вшитые | Системные промпты (одной строкой, переносы `\n`) |
| `DATABASE_URL` | — | `sqlite+aiosqlite:///data/bot.db` | |
| `VECTOR_STORE_PATH` | — | `data/vector_store` | |

> **Модели:** актуальны на октябрь 2026 — Groq перевёл llama-3.x в Enterprise-only, self-serve: `openai/gpt-oss-20b`, `openai/gpt-oss-120b`, vision — `qwen/qwen3.8-27b`. Список для вашего ключа: `curl https://api.groq.com/openai/v1/models -H "Authorization: Bearer $GROQ_API_KEY"`.

> **Лимиты:** значения из БД (`/setlimit`) перекрывают env. Порог сбрасывается в 00:00 UTC.

---

## Деплой на VPS

```bash
rsync -av --exclude='data/' --exclude='logs/' --exclude='.env' \
  ./ user@host:~/my-bots/Alex-Nano-Bot/

cd ~/my-bots/Alex-Nano-Bot
docker compose build bot && docker compose up -d bot
docker compose logs -f
```

Запуск витрины службы поддержки (отдельный процесс, свой токен):

```bash
docker compose --profile showcase up -d showcase
```

---

## Безопасность

- Закрытый режим по умолчанию: только `ADMIN_IDS`; открытый — с бан-листом и лимитами
- API-ключи Fernet-зашифрованы в БД; сообщение с ключом удаляется из чата
- PII не в логах — только `user_id`
- Суточные лимиты (сообщения / LLM-запросы / токены) защищают бюджет ключей
- SSRF-защита (`app/core/net_guard.py`): сетевые навыки не ходят на приватные адреса (localhost, RFC1918, 169.254.169.254); отключается `ALLOW_PRIVATE_URLS=true`
- Политика инструментов Runtime VM: не-админам доступен только allowlist `RUNTIME_USER_TOOLS`; `user_id` подставляется навыку из контекста VM, а не из ответа planner
- Админ-гейт на создание, редактирование и удаление Python-навыков (`/skills`) и установку SKILL.md
- Изоляция базы знаний per-user: статьи и выборки по ID привязаны к владельцу; заголовки и саммари из интернета HTML-экранируются перед отправкой в Telegram

---

## Лицензия

MIT
