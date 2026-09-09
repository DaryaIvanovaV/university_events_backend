# University Events Extractor

Система автоматичного наповнення календаря подій ЗВО: бот отримує
оголошення з Telegram-каналу → локальна LLM видобуває структуровані
дані → адміністратор підтверджує (етап модерації зі статті) → подія
потрапляє в мобільний застосунок через REST + push-сповіщення (FCM).

Ядро (схеми, валідація JSON, дедуплікація, пайплайн, модерація) покрите
тестами і запускається без зовнішніх сервісів. Для повного циклу потрібні Ollama і
Telegram-бот; FCM опційний.

## Архітектура

```
[Telegram-канал]  ──бот-адмін──►  aiogram (Bot API)     нові дописи
[result.json]     ──експорт────►  scripts/import_history  стара історія
[приклади]        ──симуляція──►  scripts/simulate        розробка/демо
                                          │
                                POST /events/ingest
                                          │
                   ┌──────────────────────▼─────────────────────┐
                   │  FastAPI backend  ──► 202 {job_id}          │
                   │  кладе задачу в чергу і відпускає клієнта   │
                   └──────────────────────┬─────────────────────┘
                                          │
                              таблиця ingest_jobs (черга)
                                          │  SELECT ... FOR UPDATE SKIP LOCKED
                   ┌──────────────────────▼─────────────────────┐
                   │  worker (python -m app.worker)              │
                   │  prefilter → LLM extract → фільтри → upsert │
                   └───────┬───────────────────────┬────────────┘
                           │                       │
                Ollama (gemma3:4b)          PostgreSQL / SQLite
                format= + Pydantic          (dedup за hash + msg_id)
                                                   │
                                                PENDING
                           │
                модерація (бот / API): approve / reject / edit
                           │  approve + майбутня дата
                           ▼
                   FCM topic push  ───►  Мобільний застосунок (GET /events)
```

Дві ключові ідеї.

**Тільки майбутні дописи** ловляться ботом автоматично; стара історія
підтягується разово з JSON-експорту Telegram Desktop (без окремого
акаунта). Кожна подія проходить модерацію перед публікацією.

**API не чекає модель.** Видобування забирає в LLM секунди (таймаут — до 300 с
на холодний старт), тому приймання повідомлення і його обробка розведені: HTTP-шар
лише ставить задачу в чергу, а важку частину виконує окремий процес.
Інакше під час імпорту історії робочі потоки FastAPI пішли б в інференс цілком і
фід для мобільного застосунку перестав би відповідати. Побічна вигода: повідомлення
не губиться, якщо модель тимчасово недоступна, — задача повернеться в чергу.

## Стек

| Шар | Технологія |
|---|---|
| Live-захоплення нових дописів | aiogram (Bot API, бот-адмін каналу) |
| Імпорт історії | JSON-експорт Telegram Desktop → `scripts/import_history` |
| Backend | FastAPI (приймає і віддає; інференс не виконує) |
| Черга задач | Таблиця `ingest_jobs` у PostgreSQL, `FOR UPDATE SKIP LOCKED` |
| Обробка | Окремий процес `python -m app.worker` |
| LLM | Ollama + `gemma3:4b` (легка; альтернативи нижче) |
| Гарантія JSON | Pydantic + Ollama `format=` (constrained decoding) + retry |
| Модерація | pending → approve/reject/edit (бот або API) |
| БД | PostgreSQL (JSONB) / SQLite для dev; міграції — Alembic |
| Автентифікація | API-ключ у заголовку `X-API-Key` (ingest / admin) |
| Push | Firebase Cloud Messaging (topic-based) |
| Розгортання | Docker Compose: `db` + `api` + `worker` (+ `bot`, `ollama`) |

Модель змінюється одним рядком `OLLAMA_MODEL` у `.env`. Легші: `qwen2.5:3b`,
`gemma3:1b`. Точніші (якщо вистачає заліза): `gemma3:12b`, `qwen2.5:14b`,
Lapa-12B / MamayLM-9B (максимум для української).

## Структура

```
app/
  config.py                налаштування (pydantic-settings)
  worker.py                ПРОЦЕС-ВОРКЕР: розбирає чергу (python -m app.worker)
  api/
    security.py            перевірка X-API-Key (ingest / admin)
  models/
    schemas.py             Pydantic: ExtractedEvent, EventList, EventOut,
                           EventUpdate, IngestAccepted, JobOut, Stats
    db.py                  SQLAlchemy ORM: Event (+ статус модерації), IngestJob
  db/
    database.py            engine (pool_pre_ping), сесії, init_db
    crud.py                upsert + дедуплікація + статуси + запити
    jobs.py                черга: enqueue / claim (SKIP LOCKED) / retry /
                           повернення зависань / статистика
  llm/
    prompts.py             системний промпт + few-shot (укр)
    extractor.py           виклик Ollama + валідація + retry
  eval/
    grounding.py           перевірка «чи підтверджується поле текстом?» (вигадки)
    pipeline_sim.py        чи потрапить подія в БД і у фід (без БД)
    report.py              HTML-звіт перевірки промта
  services/
    clock.py               єдине «зараз»: today() за APP_TIMEZONE, utc_now()
    prefilter.py           дешевий відсів "це анонс?"
    meeting.py             посилання / код зустрічі / місце по своїх полях
    audience.py            охоплення: масове чи індивідуальна консультація
    relevance.py           чи не застаріла подія вже під час публікації
    language.py            тільки українські повідомлення (російські не йдуть)
    schedule.py            дні тижня і «11:00-16:00» → HH:MM
    pipeline.py            оркестрація → pending
    moderation.py          approve / reject / edit (+ push майбутніх)
  notifications/
    fcm.py                 push через firebase-admin
  ingestion/
    aiogram_bot.py         бот: live-захоплення + картки модерації
    tg_export.py           парсер result.json (Telegram Desktop)
    samples.py             парсер прикладів (JSONL) + автовизначення формату
  main.py                  FastAPI-застосунок
data/
  sample_messages.jsonl    12 коротких прикладів (укр/рос) — швидкий smoke
  test_messages.json       35 довгих укр. повідомлень: 20 анонсів, 10 пасток,
                           3 індивідуальні + 2 масові консультації;
                           ключ _expect — еталон для звірки
migrations/                Alembic: env.py + versions/0001, 0002
scripts/
  _api.py                  wait_for_job — спільне очікування результату з черги
  _console.py              спільні консольні утиліти (UTF-8, heartbeat)
  check_prompt.py          перевірка промта на пачці повідомлень → HTML-звіт
  demo_extract.py          демо видобування на одному прикладі
  import_history.py        імпорт історії з result.json
  review_stats.py          метрики за проставленими вердиктами → Markdown
  simulate.py              прогін набору прикладів (dry-run / full / auto-approve)
reports/                   результати перевірок промта (html + json + вердикти)
tests/                     292 тести (schemas, prefilter, extractor, crud,
                           pipeline, moderation, import, ollama-status, db-types,
                           config, grounding, check-prompt, черга, воркер,
                           HTTP-ендпоінти, автентифікація, часовий пояс)
Dockerfile                 спільний образ для api / worker / bot
docker-compose.yml         db + api + worker; профілі: bot, ollama
docker-compose.dev.yml     накладення для розробки (--reload, код із хоста)
docker-entrypoint.sh       alembic upgrade head перед стартом
alembic.ini, .dockerignore, .env.example, .env.docker.example
```

## Встановлення

```bash
python -m venv .venv && source .venv/bin/activate   # Win: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                # потім відредагувати
```

Для dev достатньо ядра:
`pip install pydantic pydantic-settings sqlalchemy httpx fastapi uvicorn pytest`

## База даних: SQLite (dev) / PostgreSQL

За замовчуванням використовується SQLite-файл (`events.db`) — налаштовувати нічого не
треба. Рушій вибирається рядком `DATABASE_URL` у `.env`; код однаковий для обох
СКБД (`init_db()` створює схему при старті backend, тип `JSON` працює і там,
і там).

### PostgreSQL (локально, Windows)

Драйвер `psycopg[binary]` уже в `requirements.txt`. Нехай PostgreSQL встановлено,
наприклад, у `D:\Programs\PostgreSQL\18\` (в EDB-інсталятора `psql.exe` лежить у
`...\bin\` і зазвичай НЕ в PATH нового вікна).

```powershell
# psql у PATH поточної сесії (або викликайте за повним шляхом)
$env:Path += ';D:\Programs\PostgreSQL\18\bin'
$env:PGPASSWORD = 'ВАШ_ПАРОЛЬ'          # пароль ролі postgres, заданий при встановленні

# створити чисту БД (один раз)
psql -U postgres -c "CREATE DATABASE events ENCODING 'UTF8' TEMPLATE template0;"
```

У `.env` вкажіть рядок підключення (psycopg 3 → діалект `postgresql+psycopg`):

```
DATABASE_URL=postgresql+psycopg://postgres:ВАШ_ПАРОЛЬ@localhost:5432/events
# DATABASE_URL=sqlite:///./events.db     # відкат на SQLite — розкоментувати
```

Запустіть backend — таблиці створяться автоматично (`init_db()` →
`create_all`). Наповнити фід прикладами: `python -m scripts.simulate --auto-approve`.

Подивитися дані:

```powershell
psql -U postgres -d events -c "SELECT id, status, event_title, date, link FROM events ORDER BY date;"
```

Або через GUI **pgAdmin 4** (ставиться разом із PostgreSQL). Для читабельної
кирилиці в консолі psql: `chcp 65001` і `$env:PGCLIENTENCODING='UTF8'`.

### Міграції схеми (Alembic)

Схему накочує Alembic. `create_all` більше не використовується на PostgreSQL:
він **не робить ALTER для вже наявних таблиць** — саме на цьому проєкт
одного разу зламався, коли з'явилася колонка `meeting_code`.

```powershell
# порожня база — просто накотити все
alembic upgrade head

# база ВЖЕ існує (таблицю events створено колишнім create_all):
# позначити першу ревізію застосованою, потім накотити решту
alembic stamp 0001
alembic upgrade head
```

Ревізії: `0001` — таблиця `events` у тому вигляді, у якому її створював
`create_all` (включно з `meeting_code` і GIN-індексом); `0002` — черга
`ingest_jobs` і складений індекс `ix_events_status_date`.

Автостворення таблиць лишилося тільки для SQLite (`app/main.py::_prepare_schema`):
на PostgreSQL `create_all` створив би таблиці без запису в `alembic_version`, і
перша ж міграція впала б із «relation already exists».

У Docker міграції запускаються самі — `docker-entrypoint.sh` за
`RUN_MIGRATIONS=true` (це робить лише контейнер `api`, щоб кілька
контейнерів не побилися за одну DDL-транзакцію).

Індивідуальні консультації, збережені до появи фільтра, залишаться в
базі — фільтр не ретроактивний. Прибрати вручну:

```sql
UPDATE events SET status = 'rejected'
WHERE raw_text ILIKE '%за попереднім записом%'
   OR raw_text ILIKE '%індивідуальні консультації%'
   OR raw_text ILIKE '%години прийому%';
```

> Тести (`pytest`) завжди використовують in-memory SQLite і від PostgreSQL не залежать.
> На PostgreSQL `raw_json` зберігається як **JSONB** з **GIN-індексом** (запити за
> вмістом, напр. `WHERE raw_json @> '{"event_type":"seminar"}'`); на SQLite —
> generic JSON (портовність через `with_variant`).

## Запуск усієї системи (Windows / PowerShell)

Єдина інструкція «start here». Разова підготовка — у розділах «Встановлення» і
«База даних…» вище; деталі щодо Ollama/бота — у розділах нижче.

**0. Разова підготовка (один раз):**

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env            # вписати пароль БД; для бота — BOT_TOKEN і ADMIN_CHAT_IDS
ollama pull gemma3:4b            # ~3 ГБ (якщо 'ollama' не знайдено — див. розділ про PATH нижче)
# PostgreSQL — один раз створити БД (на SQLite не потрібно):
#   psql -U postgres -c "CREATE DATABASE events ENCODING 'UTF8' TEMPLATE template0;"
alembic upgrade head              # схема БД (див. розділ «Міграції схеми» вище)
```

**1. Фонові сервіси (зазвичай уже запущені):**

```powershell
Invoke-RestMethod http://localhost:11434/api/version   # Ollama живий?
Get-Service postgresql-x64-18                           # PostgreSQL: Running / Automatic
```

Ollama стартує значком у треї, PostgreSQL — службою з автозапуском. На SQLite
(`DATABASE_URL=sqlite:///./events.db`) піднімати БД не потрібно.

**2. Запуск (кожен компонент — свій термінал; на початку кожного активуйте venv):**

```powershell
.venv\Scripts\Activate.ps1

# Термінал 1 — backend (ОБОВ'ЯЗКОВИЙ). Приймає повідомлення в чергу.
python -m uvicorn app.main:app --port 8000      # Swagger UI: http://localhost:8000/docs

# Термінал 2 — ВОРКЕР (ОБОВ'ЯЗКОВИЙ). Без нього повідомлення лежать у черзі
# необробленими: видобуванням займається він, а не backend.
python -m app.worker

# Термінал 3 — бот (тільки для живого Telegram). При старті сам перевірить backend.
python -m app.ingestion.aiogram_bot
```

**3. Наповнити і перевірити:**

```powershell
python -m scripts.simulate --auto-approve                 # 12 прикладів → approved
Invoke-RestMethod "http://127.0.0.1:8000/events" | Format-Table event_title, date, status
```

**Що потрібно для кожного сценарію:**

| Сценарій | Ollama | БД + backend | Воркер | Бот (BOT_TOKEN) |
|---|:---:|:---:|:---:|:---:|
| `python -m pytest -q` (тести) | — | — (in-memory SQLite) | — | — |
| `demo_extract --ollama` (демо видобування) | ✅ | — | — | — |
| `check_prompt` (перевірка промта) | ✅ | — | — | — |
| Фід без Telegram (`simulate`, `GET /events`) | ✅ | ✅ | ✅ | — |
| Імпорт історії (`import_history`) | ✅ | ✅ | ✅ | — |
| Повний цикл із живим ботом | ✅ | ✅ | ✅ | ✅ |

> Порядок старту не критичний: компоненти переживуть запуск у будь-якому порядку
> (бот попередить, якщо backend ще не піднято; воркер зачекає задачі).
> Повідомлення, що прийшло при вимкненому воркері, не губиться — воно лежить у
> черзі і буде оброблене, коли воркер підніметься. А от при вимкненому
> backend бот прийняти допис не зможе.

## Розгортання в Docker

Піднімає PostgreSQL, API і воркера трьома контейнерами. Telegram-бот і Ollama —
за профілями (за замовчуванням Ollama лишається на хості, де налаштований GPU).

```powershell
copy .env.docker.example .env.docker
# заповнити POSTGRES_PASSWORD, INGEST_API_KEY, ADMIN_API_KEY:
#   python -c "import secrets;print(secrets.token_urlsafe(32))"

docker compose --env-file .env.docker up -d --build
docker compose --env-file .env.docker logs -f worker
docker compose --env-file .env.docker ps
docker compose --env-file .env.docker down        # додати -v, щоб знести і дані
```

> **Прапорець `--env-file` обов'язковий.** `env_file:` усередині сервісу віддає
> змінні запущеному контейнеру, а підстановки `${...}` у самому
> compose-файлі Compose бере з оточення оболонки або з `.env`. Без прапорця
> не підставиться пароль БД — Compose зупиниться зі зрозумілою помилкою, а не
> підніме базу з порожнім паролем.

Міграції накочуються автоматично при старті контейнера `api`
(`docker-entrypoint.sh`, тільки в нього `RUN_MIGRATIONS=true`).

**Профілі:**

```powershell
docker compose --env-file .env.docker --profile bot up -d      # + Telegram-бот
docker compose --env-file .env.docker --profile ollama up -d   # + Ollama в контейнері
```

Для переїзду Ollama в контейнер змініть у `.env.docker`
`OLLAMA_BASE_URL=http://ollama:11434` і один раз завантажте модель:
`docker compose exec ollama ollama pull gemma3:4b`. Без розкоментованого
блоку `deploy` у compose вона рахуватиме на CPU — це в рази повільніше;
для GPU потрібен NVIDIA Container Toolkit (на Windows працює через WSL2).

**Розробка з автоперезавантаженням** (код монтується з хоста):

```powershell
docker compose --env-file .env.docker -f docker-compose.yml -f docker-compose.dev.yml up -d
```

**Перевірка після старту:**

```powershell
curl http://localhost:8000/health/ready
curl -H "X-API-Key: <ADMIN_API_KEY>" http://localhost:8000/stats
```

Відомі граблі, уже враховані в конфігурації:

- В образах `postgres:18+` дані лежать у підкаталозі з номером версії, тому
  том монтується в `/var/lib/postgresql`, **не** в `/var/lib/postgresql/data`
  (зі старим шляхом контейнер відмовляється стартувати).
- У контейнерів `worker` і `bot` HEALTHCHECK вимкнено: вони не вебсервери і
  вічно вважалися б `unhealthy`.
- Образ збирається на Python 3.13 (`PYTHON_VERSION` у `.env.docker`), хоча на
  хості 3.14: у `grpcio` (залежність `firebase-admin`) може не бути готових
  wheel під найсвіжішу версію, і збірка пішла б компілювати C-розширення.

## Швидкий старт без Telegram

> Покроковий запуск під Windows/PowerShell — у розділі «Запуск усієї системи» вище.
> Нижче — стисла кросплатформна версія (bash).

```bash
# 1) Перевірити датасет — без сервера і Ollama (лише пре-фільтр):
python -m scripts.simulate --dry-run

# 2) Підняти Ollama і легку модель:
ollama pull gemma3:4b        # ~3-4 ГБ; якщо мало — gemma3:1b / qwen2.5:3b
#   Ollama зазвичай уже запущено після встановлення; інакше: ollama serve

# 3) Запустити backend:
uvicorn app.main:app --reload      # docs: http://localhost:8000/docs

# 4) Наповнити фід прикладами (одразу approved, без push):
python -m scripts.simulate --auto-approve

# 5) Подивитися результат:
curl "http://localhost:8000/events?upcoming=true"
```

## Ollama: холодний старт, PATH і вибір моделі

**Холодний старт — це не зависання.** Перший виклик після запуску Ollama
завантажує модель у пам'ять (на CPU це може зайняти до кількох хвилин); поки
триває завантаження і перша генерація, відповіді немає. Не переривайте Ctrl+C.
`demo_extract --ollama` друкує кожну фазу (сервер → модель → прогрів →
видобування) з таймінгами і «heartbeat», щоб прогрес було видно. Backend
прогріває модель у фоні при старті (`OLLAMA_WARMUP_ON_STARTUP=true`), тому
перший `/events/ingest` не платить повний cold start.

**CLI `ollama` не знайдено, хоча сервер працює.** Інсталятор додає шлях у
PATH, але вже відкриті вікна PowerShell його не бачать до перезапуску. Варіанти:

- відкрити **нове** вікно PowerShell (підхопить оновлений PATH);
- оновити PATH у поточному вікні:
  ```powershell
  $env:Path = [Environment]::GetEnvironmentVariable('Path','Machine') + ';' +
              [Environment]::GetEnvironmentVariable('Path','User')
  ```
- викликати за повним шляхом (уточніть свій — наприклад `D:\Programs\Ollama\ollama.exe`
  або `$env:LOCALAPPDATA\Programs\Ollama\ollama.exe`):
  `& "D:\Programs\Ollama\ollama.exe" pull gemma3:4b`.

Сам пайплайн CLI не використовує — він ходить по HTTP на `:11434`; перевірити сервер
без CLI: `Invoke-RestMethod http://localhost:11434/api/version`.

**Вибір моделі під залізо** (`OLLAMA_MODEL` у `.env`):

| Залізо | Модель | Очікування на повідомлення |
|---|---|---|
| NVIDIA GPU ≥ 6 ГБ VRAM | `gemma3:4b` (дефолт) | секунди |
| CPU + RAM ≥ 16 ГБ | `gemma3:4b` | ~30–90 с (для бота ок) |
| Слабкий CPU / RAM ≤ 8 ГБ | `gemma3:1b` | 5–15 с, слабша на датах |

Структуру JSON тримає constrained decoding (`format=`), тому навіть легка
модель не ламає формат — вона лише заповнює поля. Поріг: якщо тепле
видобування стабільно > ~90 с, перемкніться на `gemma3:1b` і порівняйте якість
дат на своєму датасеті.

## Моделювання даних

`scripts/simulate.py` проганяє `data/sample_messages.jsonl` через систему.
Корисно для розробки і демонстрації у статті без реального каналу.

```bash
python -m scripts.simulate --dry-run       # лише пре-фільтр (offline)
python -m scripts.simulate                 # повний прогін → pending (модерація)
python -m scripts.simulate --auto-approve  # одразу approved (наповнити фід)
python -m scripts.simulate my_data.jsonl   # свій набір
```

Формат рядка JSONL:
`{"text": "...", "date": "2026-04-02", "source_channel": "@demo", "source_message_id": 101}`

Доповнюйте датасет своїми оголошеннями — це ж основа для заміру точності.

## Імпорт історії каналу (без окремого акаунта)

Bot API не читає старі дописи. Разовий імпорт робиться з експорту:

1. Telegram Desktop → канал → меню (⋮) → **Export chat history** → формат
   **JSON** (медіа можна вимкнути) → отримаєте `result.json`.
2. За запущеного backend:

```bash
python -m scripts.import_history result.json               # → pending
python -m scripts.import_history result.json --auto-approve # одразу approved, без push
python -m scripts.import_history result.json --limit 200    # останні 200
```

Старі анонси не спамлять студентів: push іде тільки для майбутніх дат.

## Живий бот (нові дописи)

> ⚠️ **Backend має бути запущений в окремому терміналі** — бот лише
> пересилає повідомлення на `/events/ingest`, де працюють LLM-пайплайн і БД. Без
> backend картки не прийдуть (у лозі бота: `All connection attempts failed`).
> Бот перевіряє доступність backend при старті і попереджає, якщо його не піднято.

```bash
# Термінал 1: backend (потрібен боту!)
python -m uvicorn app.main:app

# Термінал 2: бот
python -m app.ingestion.aiogram_bot
```

Бота від @BotFather додайте **адміністратором** каналу. Нові дописи
автоматично йдуть у пайплайн; адміністраторам (`ADMIN_CHAT_IDS`) бот
надсилає картки з кнопками «Підтвердити / Редагувати / Відхилити» і
«📄 Оригінал» (після натискання бот надішле вихідний текст повідомлення). Можна й у
приватних повідомленнях боту: надішліть/перешліть йому текст оголошення — обробка та сама.

**Кілька адмінів:** перелічіть id через кому (без лапок) у
`ADMIN_CHAT_IDS`, напр. `ADMIN_CHAT_IDS=111, 222`. Кожен адмін має один раз
надіслати боту `/start` — інакше Telegram не дасть боту надіслати йому картку.

Повний порядок запуску всіх компонентів — у розділі «Запуск усієї системи» вище.

## Перевірка вручну (curl)

Якщо API-ключі задані, додавайте `-H "X-API-Key: <ключ>"` до всіх запитів,
крім `/health*` і `GET /events`.

```bash
# 1) Поставити повідомлення в чергу — відповідь 202 з номером задачі
curl -X POST http://localhost:8000/events/ingest \
  -H "Content-Type: application/json" \
  -d '{"text":"Завтра о 14:30 в 405 ауд. гостьова лекція про Kotlin","reference_date":"2026-04-02"}'
# → {"job_id":1,"status":"queued","duplicate":false}

# 2) Дочекатися воркера і подивитися, чим скінчилося
curl "http://localhost:8000/jobs/1"
# → status=done, event_ids=[...], skipped=[{event_title, scope, reason}, ...]

curl "http://localhost:8000/events?status=pending"     # черга модерації
curl -X POST "http://localhost:8000/events/1/approve"  # підтвердити
curl "http://localhost:8000/events?upcoming=true"      # публічний фід
curl "http://localhost:8000/stats"                     # лічильники + черга
```

Якщо задача лишилася в `queued` з `last_error` про `LLMUnavailableError` —
не запущено Ollama або не завантажено модель (`ollama pull gemma3:4b`). Повідомлення
при цьому не втрачене: воркер повторить його сам.

## Тести і демо

```bash
pytest -q                          # 292 тести, без зовнішніх сервісів
python -m scripts.demo_extract     # демо (реальний Ollama або вбудований фейк)
python -m scripts.demo_extract --ollama   # друкує фази і тайминги (див. вище)
```

## Перевірка промта: чи не вигадує модель

Проганяє пачку повідомлень через справжній екстрактор і показує, які
значення підтверджуються вихідним текстом, а які взяті з повітря.
**Backend і БД не потрібні** — лише Ollama.

```powershell
# 0. Основний датасет: 35 довгих повідомлень, 20 анонсів + 10 пасток + консультації
python -m scripts.check_prompt data\test_messages.json --sort flags --open

# 1. Швидкий smoke на коротких прикладах
python -m scripts.check_prompt --open

# 2. Свій файл: JSONL, JSON-масив або експорт Telegram Desktop — формат
#    визначається сам. Обов'язкове поле одне — "text".
python -m scripts.check_prompt my_messages.json --open

# 3. Порівняти моделі на одному датасеті
python -m scripts.check_prompt --model gemma3:1b --out reports\check_1b.html

# 4. Після розмітки: кнопка «Зберегти verdicts.jsonl» у звіті, потім
python -m scripts.review_stats reports\verdicts_<ts>.jsonl --run reports\prompt_check_<ts>.json
#    Кнопка «Завантажити вердикти з файлу» вносить збережений verdicts.jsonl
#    назад у звіт — так розмітку можна продовжити на іншій машині.
```

Формат вхідного файлу (мінімум — `text`; `date` потрібна для розв'язання
«завтра» і «наступної п'ятниці»). Приймається JSONL, JSON-масив і експорт
Telegram Desktop:

```json
{"text": "Завтра о 14:30 в 405 ауд. лекція", "date": "2026-04-02",
 "source_channel": "@demo", "source_message_id": 101,
 "_expect": "1 подія: лекція 2026-04-03, ауд. 405"}
```

`_expect` — необов'язковий ключ з очікуваним результатом. Пайплайн його
ігнорує, а перевірка промта друкує його в картці блоком «Очікується»,
щоб еталон було видно поряд із відповіддю моделі.

### Датасет пасток

`data/test_messages.json` — 35 повідомлень по 74–102 слова: 20 реальних анонсів
(конференції, лекції, дедлайни, іспити, хакатон), 10 пасток упереміш, плюс
3 індивідуальні консультації (їх зобов'язаний відсіяти фільтр охоплення) і
2 масові консультації (вони, навпаки, зобов'язані зберегтися — це перевірка
на пере-фільтрацію).
Пастки — це повідомлення, де подій бути НЕ повинно: подія в минулому,
скасована подія, питання студента, графік роботи деканату, методичні
вимоги, новина про чужий ЗВО, привітання, анонс збірника, голосування за
ще не призначену дату. Плюс числа-відволікачі в усіх повідомленнях (Zoom ID,
телефон, кегль шрифту, число учасників), які не мають потрапити в `date`,
`time` чи `location`.

Що у звіті:

- **ліворуч оригінал із підсвіткою** — підсвічено те, що підтверджує видобуте
  значення; значення праворуч без підсвітки ліворуч — кандидат у вигадки;
- **статус поля** — автоматика знає лише «чи є це в тексті», тому
  пише `з тексту` або `вигадана?` зі знаком питання. Підтип обирає
  рецензент чотирма кнопками: `✓` правильна (взята з тексту), `≈`
  вигадана, але вірна, `✗` вигадана і невірна, `○` пропущена (у тексті
  було, поле порожнє). Повторний клік повертає авто-статус;
- **оцінка події** — кнопки `✅ так` / `⛔ ні` в рядку «Відправляти далі?»:
  чи годиться подія, щоб піти в БД і на модерацію. Якщо система її
  відправить, а ви натиснули «ні», рядок червоніє — це і є обсяг сміття,
  який дійде до адміністратора;
- цифри (номер аудиторії, рік) звіряються **строго**, слова — за основою, щоб
  нормальний перефраз («405 аудиторії» → «Ауд. 405») не вважався помилкою;
  числівники словами зараховуються («третього корпусу» підтверджує «3»);
- **доля події**: чипи `→ БД (pending) → на модерацію` / `дубль` /
  `відсіяно пре-фільтром` / `індивідуальна консультація` / `подія вже минула`
  і `у фід: так/ні`.
  Це симуляція справжнього пайплайна (пре-фільтр + фільтр охоплення +
  `content_hash` із `crud.py` + правила фіду) — **у базу нічого не пишеться**.
  «Потрапить у фід» рахується від дати повідомлення, а не від сьогоднішньої;
- сира відповідь моделі, число спроб, latency, вердикт пре-фільтра;
- плитки зведення перераховуються одразу після кожної вашої позначки. Усе
  вивантажується в `verdicts.jsonl` кнопкою у звіті.

Корисні прапорці: `--limit N`, `--temperature`, `--no-warmup`,
`--sort flags` (найгірші картки нагору), `--respect-prefilter`
(не кликати LLM там, де пре-фільтр відсік би повідомлення, — як у проді).

## API

Ключ передається заголовком `X-API-Key`. Порожні `INGEST_API_KEY` і
`ADMIN_API_KEY` в `.env` = перевірка вимкнена (локальна розробка).

| Метод | Шлях | Ключ | Призначення |
|---|---|---|---|
| GET | `/health`, `/health/live` | — | процес живий (без звернень до БД) |
| GET | `/health/ready` | — | БД + Ollama; 503 лише якщо лежить БД |
| GET | `/events` | — | фід (`approved`; `?status=pending` — черга модерації), `limit`/`offset`, `X-Total-Count` |
| GET | `/events/sync` | — | інкрементальна синхронізація застосунку: `changed` + `removed` |
| GET | `/events/{id}` | admin | одна подія + оригінал (`raw_text`) |
| POST | `/events/ingest` | ingest | **202**: кладе в чергу, повертає `job_id` |
| GET | `/jobs/{id}` | ingest | результат: `event_ids`, `skipped` з причинами, помилки |
| GET | `/jobs` | admin | останні задачі черги |
| GET | `/stats` | admin | лічильники модерації і черги |
| POST | `/events/{id}/approve` | admin | підтвердити (+push для майбутніх дат) |
| POST | `/events/{id}/reject` | admin | відхилити (запис лишається заради дедупу) |
| PATCH | `/events/{id}` | admin | виправити поля (модерація) |
| POST | `/devices/register` | — | підписка FCM-токена на теми (кличе застосунок) |
| POST | `/devices/unregister` | — | відписка (користувач вимкнув сповіщення) |

> Повний контракт для того, хто пише мобільний клієнт, — у
> [`docs/mobile-api.md`](docs/mobile-api.md); машинозчитувана специфікація для
> генератора клієнтів — [`docs/openapi.json`](docs/openapi.json).

## Як це працює

1. **Джерело** (бот / імпорт / симуляція) шле сирий текст на `/events/ingest`.
2. **API кладе повідомлення в чергу** (таблиця `ingest_jobs`) і відповідає
   `202 {job_id}`. Тут HTTP-запит закінчується: тримати його на час
   інференсу не можна — під час імпорту історії робочі потоки FastAPI пішли б у
   модель цілком, і фід для застосунку перестав би відповідати.
3. **Воркер** (`python -m app.worker`) забирає задачу через
   `SELECT ... FOR UPDATE SKIP LOCKED` і проганяє її по пайплайну:
   1. **Пре-фільтр** дешево відсіює балачки за ключовими словами і патернами
      дат/часу — LLM не витрачається намарно.
   2. **Екстрактор** загортає текст у промпт із few-shot, викликає Ollama з
      `format=EventList.model_json_schema()` (граматика гарантує синтаксис
      JSON), валідує через Pydantic і при помилці робить retry з текстом
      помилки.
   3. **Фільтри** охоплення й актуальності відсікають індивідуальні консультації,
      звіти про минуле, скасовані події.
   4. **CRUD** робить upsert із подвійною дедуплікацією: точною (канал+message_id)
      і нечіткою (хеш нормалізованої назви+дати). Нове → `pending`.
4. **Підсумок задачі** записується в неї ж: id збережених подій, відсіяні з
   причинами, час обробки. Клієнт читає це за `GET /jobs/{id}`.
   Збій мережі або моделі повертає задачу в чергу з паузою; невалідний JSON
   закриває її (повтор за `temperature=0` дав би ту саму відповідь).
5. **Модерація**: адміністратор підтверджує/виправляє/відхиляє. При approve
   майбутньої події йде FCM-push.
6. **Застосунок** читає `GET /events` (тільки approved) і підписується на
   тему через `/devices/register`.

## Нотатки для статті

Каркас спроєктовано під метрики, яких чекають рецензенти. Рахує їх
`scripts/check_prompt.py` + `scripts/review_stats.py` (див. «Перевірка промта»):

- **JSON validity rate** — частка валідних відповідей із 1-ї спроби і після retry.
- **Точність за полями** — розмітьте вибірку в HTML-звіті (чекбокси за полями),
  `review_stats` зведе в таблицю.
- **Частка вигадок** — скільки повідомлень містять непідтверджені значення.
- **Latency** — медіана / p95 / максимум, прогрів окремо.
- **Порівняння моделей** — `check_prompt --model <name>` на одному датасеті.
- **Розбивка укр./рос.** — поле `language` у кожній події.
- **Ефект модерації** — частка правок адміністратора (approve vs edit).

Компонент LLM розробляється незалежно від Telegram: `check_prompt.py` дає
цифри без backend, БД і живого каналу.
