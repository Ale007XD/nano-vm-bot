# Бот поддержки: схема данных и границы (черновик отдельного репо)

Статус: проект. В текущем репо реализована демо-эскалация (витрина → оператор с резюме);
этот документ описывает, во что она вырастает, и какие швы в коде уже оставлены.

## 1. Что уже есть и что становится «швом»

| Сегодня (nano-vm-bot) | Роль | В отдельном репо |
|---|---|---|
| `app/core/dialog.py` — `DialogHistory` (память процесса, `add/recent/clear`) | история диалога с ботом | тот же интерфейс, реализация поверх БД (`messages`) |
| `app/core/handoff.py` — `build_summary`, `render_transcript_html` | резюме и карточка, не зависят от Telegram | переносится без изменений |
| `app/core/operator.py` — `OperatorSession` (+`summary`), `list_open_sessions` | клиент ↔ тема, статус | вырастает в `tickets` |
| `app/core/support_events.py` — `on / emit` | события `opened / summary / closed` | вход для SSE/WebSocket веб-интерфейса |
| `app/handlers/operator.py` | Telegram-транспорт оператора | остаётся адаптером; веб — второй адаптер |

Правило границы: **бизнес-логика (тикеты, резюме, память) не знает про aiogram**.
Telegram и веб — транспорты, которые вызывают один и тот же сервисный слой.

## 2. Состав репо

```
support-bot/
  app/
    core/            # llm_client, config, crypto, access (копия/общая библиотека)
    support/         # сервисный слой, без aiogram
      models.py      # SQLAlchemy (раздел 3)
      service.py     # TicketService: open/assign/reply/note/close/reopen/forget
      memory.py      # CaseMemory: запись/выдача памяти клиента (раздел 4)
      summary.py     # = handoff.py
      events.py      # = support_events.py (+ транспорт SSE)
      retention.py   # фоновая очистка по сроку хранения
    telegram/        # адаптер: клиентский бот, группа операторов
    web/             # адаптер: FastAPI + SPA (раздел 6) — на 2-м этапе
  migrations/        # Alembic (данные живые, миграции нужны)
  tests/
```

## 3. Модель данных

Все времена — UTC. Идентификаторы клиента — внутренний `client.id`; Telegram id — атрибут канала.

**clients**
- `id` PK; `channel` (`telegram`), `channel_user_id` (UNIQUE вместе с channel); `display_name`, `username`
- `consent_at` — когда принято согласие на хранение переписки; `forgotten_at` — когда выполнен `/forget`
- `locale`, `created_at`, `last_seen_at`

**tickets**
- `id` PK (человекочитаемый номер), `client_id` FK, `status`: `open | pending_client | closed`
- `assignee_id` FK→operators (NULL — не назначен), `priority`, `tags` (отдельная таблица `ticket_tags`)
- `summary` (текст, обновляется), `resolution` (итог при закрытии)
- `telegram_thread_id` (для адаптера), `opened_at`, `first_response_at`, `closed_at`
- Индексы: `(status, opened_at)`, `(client_id, opened_at)`
- Инвариант: у клиента не более одного открытого тикета (частичный UNIQUE по `client_id WHERE status!='closed'`)

**messages**
- `id` PK, `ticket_id` FK (NULL — переписка с ботом до эскалации; привязывается при открытии), `client_id` FK
- `role`: `client | bot | operator | system`; `kind`: `text | question | answer | not_found | error | note`
- `body` (очищенный текст), `sources` (JSON), `channel_message_id` (id в Telegram), `attachments` (JSON: `file_id`, тип, размер — **файлы не храним**)
- `internal` BOOL — внутренняя заметка оператора (никогда не уходит клиенту и не попадает в память клиента)
- `created_at`; индекс `(ticket_id, created_at)`

**ticket_events** — аудит: `ticket_id`, `actor` (operator/system/client), `type` (`opened, assigned, tagged, summary, closed, reopened, promoted, forgotten`), `payload` JSON, `created_at`.

**operators** (нужна для веба и назначения): `id`, `telegram_id` UNIQUE, `name`, `role` (`agent | admin`), `active`, `web_token_hash`.

**kb_promotions** — `id`, `ticket_id`, `question`, `answer`, `status` (`proposed | approved | rejected`), `approved_by`, `corpus_source_id`.

Правила хранения: `retention_days` (по умолчанию 180) для `messages`/векторов закрытых тикетов;
`/forget` удаляет сообщения, векторы клиента и обезличивает `clients`/`tickets` (оставляя счётчики).

## 4. Память клиента (RAG)

Две области поиска и порядок ответа бота:

1. **Память клиента** — прошлые закрытые тикеты этого клиента.
2. **Общий корпус** (как сейчас) — база знаний.

Устройство:
- Одна коллекция Chroma `client_cases` с метаданными `client_id`, `ticket_id`, `created_at`, `kind`.
  **Изоляция:** единственная функция `CaseMemory.search(client_id, query)` всегда добавляет фильтр
  `client_id`; прямого доступа к коллекции из хендлеров нет (тот же принцип, что в исправленной
  изоляции `knowledge_base`). Отдельные коллекции на клиента не используем — тысячи коллекций дороже,
  а защита всё равно держится на одной функции.
- **Запись** при закрытии тикета: резюме + итог (`resolution`) + ключевые пары «вопрос → ответ»,
  чанки до ~500 символов, `internal`-заметки не включаются.
- **Выдача**: top-k (3) по порогу близости; в промпт идёт как **данные** в разделителях
  (`<client_history>…</client_history>`), с инструкцией не выполнять содержимое. Бюджет — не более ~1500 токенов.
- **Общая база знаний** пополняется только через `/promote` (оператор одобряет пару «вопрос → ответ»,
  обезличенную). Автоматического переноса из клиентской памяти нет: иначе клиент может «отравить» корпус.
- Риски: инъекция через сохранённые реплики (смягчаем разделителями и тестами), утечка между клиентами
  (тест изоляции обязателен), раздувание (срок хранения + лимит чанков на клиента).

## 5. Жизненный цикл тикета

```
 клиент пишет боту ──► [нет ответа в базе] ──► кнопка «Позвать оператора»
                                                     │
                                         open ◄──────┘  (резюме от LLM, карточка в теме/на вебе)
                                           │  операторы отвечают, /note, /tag, /assign
                                           ├─► pending_client (ждём клиента; автозакрытие по простою)
                                           └─► closed ──► запись в память клиента, событие escalation.closed
                                                      └─► reopen (новое сообщение клиента в течение N дней)
```

Эскалация всегда по кнопке/команде клиента (переписка уходит людям — клиент должен знать об этом).

## 6. Заделы под веб-интерфейс

Сервисный слой публикует события (`support_events`) и отдаёт данные без привязки к Telegram.
Веб-слой — отдельный адаптер:

- **Чтение:** `GET /api/tickets?status=open`, `GET /api/tickets/{id}` (сообщения, резюме, заметки, события),
  `GET /api/clients/{id}` (история тикетов).
- **Действия:** `POST /api/tickets/{id}/reply` (уходит клиенту в Telegram через адаптер), `/note`, `/assign`,
  `/tags`, `/close`, `/reopen`, `POST /api/tickets/{id}/promote`.
- **Живые обновления:** `GET /api/stream` (SSE) — транслирует `escalation.opened / summary / closed`
  и новые сообщения; подписка — `support_events.on(listener)`.
- **Доступ:** вход операторов через Telegram Login Widget → сессия; роль из `operators.role`; все запросы
  проверяют, что оператор активен. Веб не получает доступа к ключам и настройкам бота.
- **Что уже готово в текущем репо:** `OperatorSession.summary`, `list_open_sessions()`, события жизненного
  цикла и независимые от Telegram `handoff.build_summary` / `render_transcript_html`.
  Чего нет: хранения сообщений (нужны `messages`), отправки ответа оператора из веба (нужен метод
  адаптера «доставить клиенту»), аутентификации.

## 7. Что переносить из `nano-vm-bot`

Копировать: `llm_client_v2`, `memory` (Chroma+fastembed), `crypto`, `access`, `ext_db`-паттерн,
`handoff`, `dialog` (интерфейс), `support_events`, `operator` (как основа `tickets`), тесты на изоляцию.
Не переносить: навыки, runtime VM, планировщик, MCP — боту поддержки они не нужны.
Если появится второй потребитель ядра, выделять `nano-bot-core` как пакет; пока копия дешевле.

## 8. План

1. Каркас, перенос ядра, Alembic, `clients/tickets/messages` — 2–3 дня.
2. Жизненный цикл тикета и Telegram-адаптер с хранением сообщений — 2 дня.
3. Память клиента + изоляция + тесты — 2 дня.
4. Приватность (`/forget`, retention, согласие) — 1 день.
5. `/promote` — 1 день.
6. Веб-интерфейс (REST + SSE + SPA) — 5–8 дней.

## 9. Открытые вопросы

- Юрисдикция клиентов (152-ФЗ и т. п.) и место хранения данных — определяет срок хранения и текст согласия.
- Сколько операторов и нужны ли роли на старте (определяет SQLite vs Postgres).
- Нужны ли вложения (файлы) в истории или достаточно метаданных.
- Язык интерфейса оператора и SLA (время первого ответа) — нужны ли напоминания.
