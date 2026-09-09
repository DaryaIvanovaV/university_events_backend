"""
Тесты хелперов диагностики/прогрева Ollama на httpx.MockTransport.

Конвенция репозитория: тесты не требуют внешних сервисов. Здесь вместо
реального сервера Ollama подставляется MockTransport, который отвечает
заготовленными JSON — проверяем разбор ответов и обработку недоступности.
"""

import json

import httpx
import pytest

from app.llm import ollama_status


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_check_server_returns_version():
    def handler(request):
        assert request.url.path == "/api/version"
        return httpx.Response(200, json={"version": "0.31.2"})

    with _client(handler) as c:
        assert ollama_status.check_server(client=c) == "0.31.2"


def test_check_server_raises_when_down():
    def handler(request):
        raise httpx.ConnectError("connection refused", request=request)

    with _client(handler) as c:
        with pytest.raises(httpx.ConnectError):
            ollama_status.check_server(client=c)


def test_model_available_exact_and_basename():
    def handler(request):
        assert request.url.path == "/api/tags"
        return httpx.Response(
            200,
            json={"models": [{"name": "gemma3:4b"}, {"name": "qwen2.5:3b"}]},
        )

    with _client(handler) as c:
        assert ollama_status.model_available("gemma3:4b", client=c) is True
        # имя без тега → сверка по базовому имени (Ollama трактует как :latest)
        assert ollama_status.model_available("gemma3", client=c) is True
        assert ollama_status.model_available("llama3:8b", client=c) is False


def test_warmup_posts_generate_and_returns_time():
    seen = {}

    def handler(request):
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"done": True, "done_reason": "load"})

    with _client(handler) as c:
        elapsed = ollama_status.warmup("gemma3:4b", client=c)

    assert seen["path"] == "/api/generate"
    assert seen["body"]["model"] == "gemma3:4b"
    assert "keep_alive" in seen["body"]  # держим модель в памяти
    assert elapsed >= 0.0
