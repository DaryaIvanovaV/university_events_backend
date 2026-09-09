"""
LLM-экстрактор событий.

Конвейер на одно сообщение:
  build_messages → chat_fn(messages, schema) → JSON-строка
  → EventList.model_validate_json → при ValidationError retry с ошибкой.

Гарантии:
  * Ollama `format=` (constrained decoding) гарантирует СИНТАКСИС JSON.
  * Pydantic гарантирует СЕМАНТИКУ (типы, обязательные поля, корректные даты).
  * Retry добавляет текст ошибки в контекст — это меняет вывод даже при
    temperature=0 и вытягивает редкие невалидные ответы.

chat_fn вынесен как зависимость: в проде это вызов Ollama, в тестах — заглушка.
Так логика валидации/retry проверяется без запущенного Ollama.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable

import httpx
from pydantic import ValidationError

from app.config import settings
from app.llm.prompts import build_messages
from app.models.schemas import EventList
from app.services.clock import today

logger = logging.getLogger(__name__)

# chat_fn(messages, json_schema) -> сырая строка ответа модели
ChatFn = Callable[[list[dict], dict], str]


class ExtractionError(RuntimeError):
    """Модель не вернула валидный по схеме JSON даже после всех retry."""


class LLMUnavailableError(ExtractionError):
    """
    До модели не удалось достучаться: сервер не отвечает, модель не скачана
    (Ollama отдаёт 404), сеть отвалилась, вышел таймаут генерации.

    Отделено от ExtractionError намеренно, и различие не формальное.
    Невалидный JSON — свойство самой модели: повтор той же задачи при
    temperature=0 даст тот же ответ, сообщение можно закрывать. А
    недоступность временная, и сообщение обязано вернуться в очередь.
    Без этого различия забытый перед запуском `ollama pull` тихо съедал бы
    весь входящий поток: каждая задача помечалась бы «выполнена, событий 0».
    """


def make_ollama_chat_fn(
    base_url: str | None = None,
    model: str | None = None,
    temperature: float | None = None,
    timeout_s: float | None = None,
    connect_timeout_s: float | None = None,
    keep_alive: str | None = None,
    num_ctx: int | None = None,
    use_schema: bool = True,
    think: bool | None = None,
) -> ChatFn:
    """
    Створює chat_fn, що звертається до локального Ollama (/api/chat).

    `use_schema=False` шле `format="json"` замість повної схеми: JSON лишається
    синтаксично гарантованим, але поля перевіряє тільки Pydantic. Потрібно
    ЛИШЕ для порівняння моделей: `qwen3:4b-instruct-2507` під граматикою з
    нашої схеми зривається в нескінченні повтори (виміряно 2026-09-06), і без
    цього прапорця його неможливо навіть заміряти. У пайплайні не вмикати.

    `think=False` вимикає блок міркувань у моделей із capability `thinking`
    (`qwen3:8b`). Теж для порівняння: з увімкненими міркуваннями така модель
    витрачає сотні токенів ДО відповіді — на цьому залізі 60–125 с на
    повідомлення, і довгі анонси не встигають у таймаут. `None` — не чіпати
    налаштування моделі (моделі без міркувань від явного `think` падають 400).
    """
    base_url = base_url or settings.ollama_base_url
    model = model or settings.ollama_model
    temperature = settings.ollama_temperature if temperature is None else temperature
    timeout_s = timeout_s or settings.ollama_timeout_s
    connect_timeout_s = connect_timeout_s or settings.ollama_connect_timeout_s
    keep_alive = keep_alive or settings.ollama_keep_alive
    num_ctx = settings.ollama_num_ctx if num_ctx is None else num_ctx
    # Долгий таймаут на саму генерацию (терпим cold start), но короткий — на
    # установку соединения: неподнятый сервер обнаруживаем за секунды.
    timeout = httpx.Timeout(timeout_s, connect=connect_timeout_s)

    options: dict = {"temperature": temperature}
    if num_ctx:
        # Ноль означает «не вмешиваться»: пусть Ollama берёт свой дефолт.
        options["num_ctx"] = num_ctx

    payload_extra: dict = {} if think is None else {"think": think}

    def chat_fn(messages: list[dict], json_schema: dict) -> str:
        resp = httpx.post(
            f"{base_url}/api/chat",
            json={
                "model": model,
                "messages": messages,
                # <-- constrained decoding по схеме
                "format": json_schema if use_schema else "json",
                "stream": False,
                "keep_alive": keep_alive,  # держим модель в памяти между вызовами
                "options": options,
                **payload_extra,
            },
            timeout=timeout,
        )
        resp.raise_for_status()
        return resp.json()["message"]["content"]

    return chat_fn


class EventExtractor:
    def __init__(self, chat_fn: ChatFn, max_retries: int | None = None) -> None:
        self.chat_fn = chat_fn
        self.max_retries = (
            settings.llm_max_retries if max_retries is None else max_retries
        )

    def extract(
        self, text: str, reference_date: dt.date | None = None
    ) -> EventList:
        reference_date = reference_date or today()
        schema = EventList.model_json_schema()
        messages = build_messages(text, reference_date)

        last_error: Exception | None = None
        # Чем закончилась ПОСЛЕДНЯЯ попытка: недоступностью сервиса или
        # невалидным ответом модели. От этого зависит, повторять ли задачу.
        unavailable = False
        for attempt in range(self.max_retries + 1):
            try:
                content = self.chat_fn(messages, schema)
            except httpx.ConnectError:
                # Сервер Ollama не запущен — повторять бессмысленно, падаем сразу
                # (не молчим max_retries таймаутов, как раньше).
                raise
            except Exception as exc:  # прочие сетевые ошибки/таймаут генерации
                last_error = exc
                unavailable = True
                logger.warning(
                    "Ошибка обращения к LLM (попытка %d/%d): %s: %s",
                    attempt + 1, self.max_retries + 1, type(exc).__name__, exc,
                )
                continue
            try:
                return EventList.model_validate_json(content)
            except ValidationError as exc:
                last_error = exc
                unavailable = False
                # Подкладываем модели её же ответ + текст ошибки.
                messages = [
                    *messages,
                    {"role": "assistant", "content": content},
                    {
                        "role": "user",
                        "content": (
                            "Your JSON failed validation with this error:\n"
                            f"{exc}\nReturn corrected JSON only, matching the "
                            "schema. No prose."
                        ),
                    },
                ]
        attempts = self.max_retries + 1
        if unavailable:
            raise LLMUnavailableError(
                f"LLM недоступна: {attempts} попыток завершились ошибкой "
                f"обращения ({type(last_error).__name__}: {last_error})"
            ) from last_error
        raise ExtractionError(
            f"Не удалось получить валидный JSON за {attempts} попыток"
        ) from last_error
