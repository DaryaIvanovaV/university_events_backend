"""
Конфигурация приложения через pydantic-settings.

Все значения имеют дефолты, поэтому проект импортируется и тестируется
без .env. Для реального запуска создайте .env (см. .env.example).
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- База данных ---
    # dev: SQLite-файл; prod: postgresql+psycopg://user:pass@host/db
    database_url: str = "sqlite:///./events.db"
    # pool_pre_ping лечит «протухшие» соединения: контейнер с Postgres
    # перезапустился — соединения в пуле мертвы, и без пинга каждый запрос
    # падал бы 500-й, пока пул не обновится сам.
    db_pool_size: int = 5
    db_max_overflow: int = 10
    db_pool_recycle_s: int = 1800
    db_echo: bool = False

    # --- Часовой пояс приложения ---
    # Фильтр upcoming в фиде считает «сегодня» по этой зоне, а не по времени
    # процесса: в контейнере оно UTC (см. app/services/clock.py).
    app_timezone: str = "Europe/Kyiv"

    # --- Локальная LLM (Ollama) ---
    # Лёгкая модель по умолчанию. Альтернативы:
    #   gemma3:1b   — совсем лёгкая (идёт на CPU), но слабее на датах
    #   qwen2.5:3b  — если преобладает русский
    #   gemma3:12b  — максимум качества, если хватает железа
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "gemma3:4b"
    ollama_temperature: float = 0.0
    # Размер контекстного окна. 0 = не задавать, оставить дефолт Ollama.
    # Окно определяет размер KV-кэша, а тот занимает VRAM: если модель не
    # помещается в память видеокарты целиком, часть слоёв считает CPU и
    # инференс замедляется в разы.
    # ВНИМАНИЕ, нижняя граница измерена: промпт (системный + 3 few-shot +
    # сообщение) занимает ~2106 токенов, ответ — до ~200. Ставить меньше
    # 2400 нельзя: модель начнёт получать обрезанный промпт, причём молча.
    ollama_num_ctx: int = 0
    # Таймаут одной генерации. На холодном старте (первая загрузка модели в
    # память) первый ответ идёт заметно дольше — берём с запасом.
    ollama_timeout_s: float = 300.0
    # Таймаут ТОЛЬКО на установку соединения: если сервер Ollama не запущен,
    # незачем ждать полный ollama_timeout_s — падаем за секунды с ясной ошибкой.
    ollama_connect_timeout_s: float = 5.0
    # Сколько держать модель загруженной в памяти после запроса. Дефолт Ollama —
    # 5m; увеличиваем, чтобы между сообщениями не платить повторный cold start.
    ollama_keep_alive: str = "15m"
    # Прогревать модель при старте backend (в фоне): тогда первый /events/ingest
    # не ждёт загрузку модели. Выключить, если Ollama не используется.
    ollama_warmup_on_startup: bool = True
    llm_max_retries: int = 2

    # --- Telegram Bot API (aiogram) ---
    bot_token: str = ""
    # id администраторов через запятую — им приходят карточки модерации.
    # Узнать свой id: отправить боту команду /id
    admin_chat_ids: str = ""
    # Опциональный белый список каналов (@username или -100...id, через
    # запятую). Пусто = принимать посты из всех каналов, где бот админ.
    allowed_channels: str = ""

    # --- Очередь ingest-задач (отдельный воркер) ---
    # Извлечение занимает секунды, а таймаут генерации — до 300 с. Держать на
    # это время HTTP-запрос нельзя: пул потоков FastAPI забьётся, и фид для
    # приложения встанет. Поэтому /events/ingest только кладёт задачу в
    # таблицу ingest_jobs, а обрабатывает её процесс app/worker.py.
    worker_id: str = ""  # пусто → hostname:pid, чтобы различать воркеров в логе
    worker_poll_interval_s: float = 2.0
    worker_batch_size: int = 1  # Ollama всё равно считает по одному запросу
    worker_max_attempts: int = 3
    worker_retry_backoff_s: float = 30.0
    # Задача, «зависшая» в processing дольше этого срока, считается брошенной
    # (воркер убит) и возвращается в очередь. Берём с запасом над ollama_timeout_s.
    worker_lease_timeout_s: float = 900.0

    # --- Безопасность API ---
    # Пустой ключ = проверка выключена (dev на localhost). Как только порт
    # проброшен наружу — заполнить оба, иначе approve/reject/ingest открыты всем.
    ingest_api_key: str = ""  # для бота и импортёра истории
    admin_api_key: str = ""  # для модерации (approve/reject/patch)
    # Origins для браузерных клиентов через запятую; "*" — разрешить все.
    cors_origins: str = ""

    # --- Внутренний API (куда бот шлёт сообщения) ---
    # 127.0.0.1, а не localhost: на Windows localhost может резолвиться в IPv6
    # (::1) раньше IPv4, а uvicorn по умолчанию слушает IPv4 — лишние задержки/сбои.
    api_base_url: str = "http://127.0.0.1:8000"
    # Клиенты (бот, импортёр) получают на /events/ingest ответ 202 и дальше
    # опрашивают GET /jobs/{id}, пока воркер не отчитается.
    job_poll_interval_s: float = 1.5
    job_poll_timeout_s: float = 300.0

    # --- FCM push-уведомления ---
    fcm_credentials_path: str = ""  # путь к serviceAccount.json
    fcm_default_topic: str = "conferences"

    @property
    def admins(self) -> list[int]:
        out: list[int] = []
        for part in self.admin_chat_ids.split(","):
            part = part.strip(" '\"[]")  # терпим пробелы/кавычки/скобки вокруг id
            if part:
                try:
                    out.append(int(part))
                except ValueError:
                    pass
        return out

    @property
    def channels(self) -> list[str]:
        return [
            c.strip(" '\"[]")
            for c in self.allowed_channels.split(",")
            if c.strip(" '\"[]")
        ]

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def auth_enabled(self) -> bool:
        """Хотя бы один ключ задан → защита эндпоинтов включена."""
        return bool(self.ingest_api_key or self.admin_api_key)

    @property
    def client_api_key(self) -> str:
        """
        Ключ, которым наши же клиенты (бот, импортёр) ходят в API.

        Админский идёт первым: бот не только шлёт сообщения, но и подтверждает
        события кнопками, а это уже права модерации.
        """
        return self.admin_api_key or self.ingest_api_key

    @property
    def api_headers(self) -> dict[str, str]:
        """Заголовки для httpx-клиентов; пустой словарь, если ключей нет."""
        key = self.client_api_key
        return {"X-API-Key": key} if key else {}

    @property
    def resolved_worker_id(self) -> str:
        """Идентификатор процесса-воркера для колонки locked_by."""
        if self.worker_id:
            return self.worker_id[:64]
        import os
        import socket

        return f"{socket.gethostname()}:{os.getpid()}"[:64]


settings = Settings()
