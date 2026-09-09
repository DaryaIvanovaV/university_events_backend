"""
Тесты экстрактора с ЗАГЛУШКОЙ chat_fn (без запущенного Ollama).

Проверяем именно логику валидации/retry — самую важную для надёжности.
"""

import json

import httpx
import pytest

from app.llm.extractor import (
    EventExtractor,
    ExtractionError,
    LLMUnavailableError,
    make_ollama_chat_fn,
)

GOOD = '{"events":[{"event_title":"Конференція AI","date":"2026-09-01"}]}'
BAD = '{"events":[{"event_title":"Конференція AI","date":"NOT-A-DATE"}]}'
PROSE_THEN_GOOD = GOOD  # после retry модель «исправляется»


def make_chat_fn(responses):
    """Возвращает chat_fn, отдающий заготовленные ответы по очереди."""
    it = iter(responses)

    def chat_fn(messages, schema):
        return next(it)

    return chat_fn


def test_extract_success_first_try():
    extractor = EventExtractor(make_chat_fn([GOOD]), max_retries=2)
    result = extractor.extract("Анонс конференції AI 1 вересня")
    assert len(result.events) == 1
    assert result.events[0].event_title == "Конференція AI"


def test_extract_retries_then_succeeds():
    # Первый ответ невалиден, второй — корректен. Должен сработать retry.
    extractor = EventExtractor(make_chat_fn([BAD, PROSE_THEN_GOOD]), max_retries=2)
    result = extractor.extract("текст")
    assert result.events[0].event_title == "Конференція AI"


def test_extract_gives_up_after_retries():
    # Всегда невалидно → ExtractionError после исчерпания попыток.
    extractor = EventExtractor(make_chat_fn([BAD, BAD, BAD]), max_retries=2)
    with pytest.raises(ExtractionError) as exc:
        extractor.extract("текст")
    # Именно ExtractionError, а не подтип «недоступна»: модель ответила,
    # просто мусором — повторять такую задачу незачем.
    assert not isinstance(exc.value, LLMUnavailableError)


def test_http_error_raises_unavailable_not_extraction_error():
    """
    404 «модель не скачана» — недоступность сервиса, а не плохой ответ модели.

    Разница определяет судьбу сообщения: LLMUnavailableError вернёт задачу
    в очередь, обычный ExtractionError закрыл бы её как обработанную.
    """
    response = httpx.Response(404, request=httpx.Request("POST", "http://o/api/chat"))

    def chat_fn(messages, schema):
        raise httpx.HTTPStatusError("404", request=response.request, response=response)

    extractor = EventExtractor(chat_fn, max_retries=1)
    with pytest.raises(LLMUnavailableError):
        extractor.extract("текст")


def test_timeout_raises_unavailable():
    def chat_fn(messages, schema):
        raise httpx.ReadTimeout("too slow")

    with pytest.raises(LLMUnavailableError):
        EventExtractor(chat_fn, max_retries=0).extract("текст")


def test_recovery_after_transport_error_is_not_unavailable():
    # Первая попытка — сетевая ошибка, вторая вернула валидный JSON.
    # Итог успешный, никаких исключений.
    calls = {"n": 0}

    def chat_fn(messages, schema):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ReadTimeout("blip")
        return GOOD

    result = EventExtractor(chat_fn, max_retries=2).extract("текст")
    assert result.events[0].event_title == "Конференція AI"


def test_schema_passed_to_chat_fn():
    captured = {}

    def chat_fn(messages, schema):
        captured["schema"] = schema
        captured["messages"] = messages
        return GOOD

    EventExtractor(chat_fn, max_retries=0).extract("текст")
    assert "events" in captured["schema"]["properties"]
    # системный промпт + few-shot + сообщение пользователя
    assert captured["messages"][0]["role"] == "system"


# --- Размер контекстного окна (num_ctx) ---


def _captured_request(num_ctx):
    """Возвращает тело запроса, которое chat_fn отправил бы в Ollama."""
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"message": {"content": GOOD}})

    transport = httpx.MockTransport(handler)
    original = httpx.post

    def fake_post(url, **kwargs):
        with httpx.Client(transport=transport) as client:
            return client.post(url, **kwargs)

    httpx.post = fake_post
    try:
        chat_fn = make_ollama_chat_fn(num_ctx=num_ctx)
        chat_fn([{"role": "user", "content": "текст"}], {})
    finally:
        httpx.post = original
    return captured


def test_num_ctx_is_passed_to_ollama():
    options = _captured_request(3072)["options"]
    assert options["num_ctx"] == 3072


def test_zero_num_ctx_leaves_ollama_default():
    # Ноль означает «не вмешиваться»: параметр не должен уезжать вовсе,
    # иначе мы навязали бы своё окно там, где хотели дефолт сервера.
    assert "num_ctx" not in _captured_request(0)["options"]
