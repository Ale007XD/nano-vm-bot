# Alex-Nano-Bot

Self-hosted Telegram AI-бот с детерминированным Program-driven рантаймом, векторной памятью (RAG), базой знаний, multi-provider LLM и мультиюзерным режимом с лимитами.

**Версия:** 1.10.1 · **Тесты:** 656 passed 🟢 · ruff: clean · Python 3.12

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
- **BYOK:** пользователь подключает свои API-ключи (`/byok`) — запросы идут на его ключ, лимиты бота не применяются
- **Кредиты и платежи:** пакеты запросов сверх бесплатного суточного лимита, оплата Telegram Stars / ЮKassa / Stripe (`/buy`, `/balance`)

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
- **Несколько витрин в одном процессе** (`SHOWCASES`): у каждой свой бот (токен), своя коллекция знаний, свой режим доступа (`invite` — по приглашению, `open`), своя группа оператора, промпт, модель и приветствие. Эмбеддинг-модель и БД общие, поэтому ещё одна витрина стоит единицы МиБ, а не отдельного контейнера. Витрина определяется по боту, принявшему сообщение, а не по данным пользователя: корпуса, диалоги и обращения к оператору разных витрин не смешиваются.
- **Доступ по приглашению:** администратор в самой витрине создаёт ссылку-приглашение (`/invite 5 3` — до 5 человек, 3 дня; `0` — без ограничения) или выдаёт доступ по ID (`/allow`). Приглашение действует только в той витрине, где создано; код погашается атомарно, отзывается `/revoke_invite`.
- **Эскалация на оператора:** кнопка «Позвать оператора» под каждым ответом (и `/operator`) создаёт тему в супергруппе поддержки, пересылает сообщения в обе стороны; в карточку темы попадает переписка клиента с ботом и резюме от LLM. `/close` в теме или `/endchat` у клиента завершает обращение.
- **Доступ и бюджет:** основной бот остаётся приватным независимо от витрин; суточный лимит LLM-ответов на пользователя — `SHOWCASE_DAILY_LIMIT` (по умолчанию 20), общий для всех витрин. Без `SHOWCASES` работает одна витрина с прежними переменными (`SHOWCASE_OPEN_ACCESS`).
- **Память:** эмбеддинг-модель грузится при первом вопросе и выгружается после простоя (~270 МиБ в покое вместо ~800 МиБ).

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
| `/byok set <провайдер> <ключ>`, `/byok` | Свои API-ключи (BYOK): подключить, статус, удалить |
| `/buy`, `/balance`, `/paysupport` | Пакеты запросов (кредиты): купить, баланс, поддержка по платежам |
| `/grant <id> <n>`, `/refund <№ платежа>` | Начислить кредиты / вернуть платёж (admin) |
| `/operator`, `/endchat` | Витрина: позвать оператора / завершить обращение (клиент) |
| `/invite [N] [дней]`, `/invites`, `/revoke_invite <код>` | Витрина: создать ссылку-приглашение (N человек, срок; 0 — без ограничения), список действующих, отозвать (admin) |
| `/allow <id>`, `/deny <id>`, `/members` | Витрина: выдать/отозвать доступ по ID, список участников (admin) |
| `/close` | Витрина: закрыть обращение из темы оператора |
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
│   │   │                     # net_guard (SSRF), web_search, crypto (Fernet),
│   │   │                     # byok, billing/ (кредиты, платежи), ext_db (таблицы
│   │   │                     # расширений + автодобавление колонок),
│   │   │                     # operator, dialog, handoff, support_events (эскалация),
│   │   │                     # showcases (реестр витрин, контекст), showcase_access
│   │   │                     # (участники и приглашения)
│   ├── runtime/              # VM, Planner, StateContext, instructions/
│   │   └── instructions/     # call_llm, call_tool, respond, store_memory
│   ├── handlers/             # commands, messages, providers, skills, memory,
│   │                         # channel, reminders, showcase, operator, byok, billing, fallback
│   ├── utils/                # keyboards, states, helpers (sanitize_html, typing)
│   ├── bot.py
│   └── bot_showcase.py       # демо-бот витрины службы поддержки (отдельная точка входа)
├── skills/system/ custom/ external/   # external: .py-навыки и папки с SKILL.md
├── data/                     # bot.db, vector_store/ (volume-mounted)
├── logs/                     # (volume-mounted)
├── docs/                     # support-bot-design.md — проект бота поддержки на llm-nano-vm
├── tests/                    # 630 тестов: runtime, access, usage, mcp, net_guard (SSRF),
│                             # kb_isolation, tool_policy, skills_admin, skillmd, corpus, bot,
│                             # byok, billing, operator/handoff, embedding_lifecycle, ext_migration, config_env,
│                             # showcases, showcase_flow, bot_showcase_entry
├── requirements.txt          # runtime-зависимости (aiogram 3.31+, chromadb 1.5+, mcp 1.x/2.x)
├── requirements-dev.txt      # pytest, pytest-asyncio, ruff, mypy (для CI и разработки)
├── ruff.toml                 # зафиксированный набор правил линтера
├── docker-compose.yml
├── Dockerfile
└── .env.example
```

---

## CI/CD

GitHub Actions · Python 3.12 · установка `requirements-dev.txt` (runtime + pytest/ruff/mypy) · `ruff check app/ tests/` · `pytest tests/ -v` · **630/630 passed 🟢** (проверено на pytest 9.1 и 9.2)

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
| `SHOWCASES` | — | пусто | Раздельные витрины: JSON-список одной строкой, объект на витрину: `slug` (a-z0-9_, обязателен), `token` или `token_env` (имя переменной с токеном), `collection` (по умолчанию `corpus_<slug>`; для уже загруженного корпуса — `corpus`), `access` (`invite` по умолчанию или `open`), `operator_chat_id`, `name`, `welcome`, `system_prompt`, `model`. Опечатки в ключах, дубли slug/токена/коллекции и неверные значения — ошибка запуска. Пусто — одна витрина по `SHOWCASE_BOT_TOKEN` |
| `SHOWCASE_OPEN_ACCESS` | — | не задано | Только без `SHOWCASES`: витрина отвечает всем независимо от основного бота (он остаётся приватным). Не задано или пусто — как `BOT_OPEN_ACCESS` |
| `SHOWCASE_DAILY_LIMIT` | — | `20` | Суточный лимит LLM-ответов витрины на пользователя (UTC; `0` — безлимит; админы не ограничены). Вопросы без ответа в базе знаний лимит не тратят. Учёт ведётся по `usage_daily` |
| `USER_DAILY_MESSAGE_LIMIT` | — | `50` | Суточный лимит сообщений (0 — безлимит; меняется и через `/setlimit`) |
| `KB_CHANNEL_IDS` | — | — | Каналы для базы знаний |
| `KB_STALE_DAYS` | — | `30` | Статьи базы знаний старше N дней считаются устаревшими и обновляются при обращении |
| `MCP_SERVERS` | — | — | JSON-список MCP-серверов одной строкой: `[{"name":"fetch","command":"uvx","args":["mcp-server-fetch"]}]` или `[{"name":"x","url":"https://host/mcp"}]` (stdio требует `uv`, уже в образе) |
| `RUNTIME_USER_TOOLS` | — | `validated_answer` | Инструменты `call_tool` в Runtime VM, доступные не-админам (через запятую; MCP — `server__tool`). Админам доступны все; `user_id` в навык всегда подставляется из контекста, а не из ответа planner |
| `ALLOW_PRIVATE_URLS` | — | `false` | `true` — разрешить `knowledge_base`/`site_monitor` ходить на внутренние адреса (localhost, 10.x, 192.168.x, 169.254.x). По умолчанию запрещено (SSRF-защита) |
| `EMBEDDING_IDLE_UNLOAD_SECONDS` | — | `600` | Эмбеддинг-модель (~0.5–0.8 ГБ ОЗУ) грузится при первом использовании и выгружается после стольких секунд простоя; первый запрос после выгрузки медленнее на несколько секунд. `0` — грузить при старте и не выгружать. Потолки памяти контейнеров: `BOT_MEM_LIMIT`, `SHOWCASE_MEM_LIMIT` (по умолчанию `1280m`) |
| `BYOK_ENABLED` | — | `true` | Разрешить пользователям свои API-ключи (`/byok set groq <ключ>`; провайдеры: groq, openrouter, anthropic, openai). Требует `ENCRYPTION_KEY`. С ключом запросы идут только на него (без фолбэка на ключи бота), суточные лимиты не применяются; голос и фото — только с ключом Groq |
| `OPERATOR_CHAT_ID` | — | `0` | Витрина: ID супергруппы поддержки (темы включены, бот — админ с правом управлять темами). Клиент зовёт оператора через `/operator` или кнопку под ответом «не нашёл»; для каждого клиента создаётся тема, сообщения пересылаются в обе стороны, `/close` в теме или `/endchat` у клиента завершает обращение. В карточку темы попадает переписка клиента с ботом (последние реплики, в памяти процесса, не в БД), следом — резюме от LLM (`SHOWCASE_MODEL`; при сбое остаётся только переписка). 0 — выключено. Развитие (тикеты, память клиента, веб): `docs/support-bot-design.md` |
| `BILLING_PROVIDERS` | — | пусто | Платежи за пакеты запросов (кредиты): `stars`, `yookassa`, `stripe` через запятую. Сверх бесплатного суточного лимита списывается 1 кредит за сообщение (админы и BYOK не списываются). Команды: `/buy`, `/balance`, `/paysupport`; админ: `/grant <id> <n>`, `/refund <№ платежа>` (Stars — автоматически, ЮKassa/Stripe — вручную в кабинете; возврат отклоняется, если кредиты потрачены). Пусто — выключено |
| `YOOKASSA_PROVIDER_TOKEN` / `STRIPE_PROVIDER_TOKEN` | — | пусто | Токены Telegram Payments из @BotFather; без токена провайдер не включается |
| `CREDIT_PACKAGES` | — | 2 пакета | JSON: `[{"id":"s50","credits":50,"title":"50 запросов","prices":{"XTR":100,"RUB":19900,"USD":249}}]` (цены в минимальных единицах: звёзды / копейки / центы) |
| `ENABLE_WEB_SEARCH` | — | `true` | Веб-поиск в FastBot/PlanBot и навыках |
| `SERPER_API_KEY` / `SERPAPI_KEY` / `BING_API_KEY` | — | — | Ключи поисковых API (рекомендуется Serper); без ключей — бесплатный DuckDuckGo |
| `WEB_SEARCH_RESULTS` | — | `3` | Результатов поиска на запрос |
| `EMBEDDING_MODEL` | — | `sentence-transformers/all-MiniLM-L6-v2` | Модель эмбеддингов (fastembed). Для русского лучше `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`; после смены модели векторное хранилище нужно переиндексировать |
| `SHOWCASE_BOT_TOKEN` | — | — | Токен демо-бота витрины (пусто и нет `SHOWCASES` — сервис витрины не запускается) |
| `SHOWCASE_MODEL` | — | `openai/gpt-oss-20b` | Модель витрин по умолчанию (поле `model` витрины переопределяет) |
| `SHOWCASE_SYSTEM_PROMPT` | — | вшитый | Системный промпт витрин по умолчанию (поле `system_prompt` витрины переопределяет) |
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

### Несколько витрин

```bash
# .env — токен второго бота кладём отдельной переменной, в SHOWCASES — только её имя
SHOWCASES=[{"slug":"reklama","token_env":"SHOWCASE_BOT_TOKEN","collection":"corpus","operator_chat_id":-1001111111111},{"slug":"jurist","token_env":"JURIST_BOT_TOKEN","operator_chat_id":-1002222222222}]
JURIST_BOT_TOKEN=123456:AAA...

docker compose build showcase
docker compose --profile showcase up -d --force-recreate showcase
```

- Группы операторов — свои у каждой витрины (супергруппа с темами, бот — админ с правом управлять темами). Бот в чужой группе ничего не увидит.
- Наследный корпус остаётся доступным: коллекция `corpus` указывается в `collection`, переиндексация не нужна. Открытые обращения к оператору, созданные до перехода, передаются первой витрине списка.
- **Режим `invite` закрывает витрину для всех, кроме админов**, в том числе для тех, кто пользовался ей раньше. Выдайте доступ: `/invite 20 7` (ссылка на 20 человек, 7 дней) или `/allow <id>`.
- Новую витрину можно добавить, не трогая остальные; её корпус наполняется документами `.md`/`.txt`, отправленными админом этому боту (`/corpus_upload`).
- Токен, который не принимает Telegram, пропускается с записью в лог — остальные витрины работают. Если не работает ни одна, процесс завершается с ошибкой.
- Если переходите с одной витрины и не хотите менять конфигурацию, оставьте `SHOWCASES` пустым: всё продолжит работать как раньше.

> ⚠️ Код копируется в образ (`COPY . .`), поэтому после обновления нужна пересборка: `docker compose build bot showcase`. Команда `up -d` без `build` образ не пересобирает; если контейнер не пересоздался, добавьте `--force-recreate`.

Запуск витрины службы поддержки (отдельный процесс, свой токен):

```bash
docker compose --profile showcase up -d showcase
```

Витрина не зависит от основного бота: её можно держать открытой для пользователей (`SHOWCASE_OPEN_ACCESS=true`), а основной бот остановить (`docker compose stop bot`). Вместе с ним остановятся планировщик (напоминания) и остальные функции основного бота. Проверка памяти: `docker stats --no-stream`.

---

## Безопасность

- Закрытый режим по умолчанию: только `ADMIN_IDS`; открытый — с бан-листом и лимитами
- API-ключи Fernet-зашифрованы в БД; сообщение с ключом удаляется из чата
- PII не в логах — только `user_id`
- Суточные лимиты (сообщения / LLM-запросы / токены; для открытой витрины — `SHOWCASE_DAILY_LIMIT`) защищают бюджет ключей; лимит действует на пользователя, общего потолка расхода по витрине нет
- SSRF-защита (`app/core/net_guard.py`): сетевые навыки не ходят на приватные адреса (localhost, RFC1918, 169.254.169.254); отключается `ALLOW_PRIVATE_URLS=true`
- Политика инструментов Runtime VM: не-админам доступен только allowlist `RUNTIME_USER_TOOLS`; `user_id` подставляется навыку из контекста VM, а не из ответа planner
- Раздельные витрины: корпус выбирается по боту-получателю (не по данным пользователя), имя коллекции строго `corpus` или `corpus_<имя>` — витрина не может обратиться к служебным коллекциям памяти; сессии оператора, история диалога и антифлуд ключуются витриной; доступ по приглашению закрыт по умолчанию и закрывается при сбое БД
- `.dockerignore`: `.env`, `.git`, `data/`, `logs/` не попадают в образ (раньше `COPY . .` клали туда секреты сервера)
- Админ-гейт на создание, редактирование и удаление Python-навыков (`/skills`) и установку SKILL.md
- Изоляция базы знаний per-user: статьи и выборки по ID привязаны к владельцу; заголовки и саммари из интернета HTML-экранируются перед отправкой в Telegram

---

## Лицензия

MIT
