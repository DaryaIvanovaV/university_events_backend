"""
Диагностика и прогрев Ollama.

Тонкие обёртки над HTTP-API Ollama, отделённые от экстрактора, чтобы:
  * demo_extract/бэкенд могли ДО генерации проверить, что сервер поднят и
    модель скачана, и дать понятную подсказку вместо немого таймаута;
  * прогреть модель (загрузить в память) заранее — тогда первый реальный
    запрос не платит cold start.

Все функции принимают опциональный httpx.Client — в тестах туда передаётся
client с MockTransport, поэтому проверка идёт без запущенного Ollama.
"""

from __future__ import annotations

import time
from contextlib import nullcontext

import httpx

from app.config import settings


def _client_cm(client: httpx.Client | None, timeout: float):
    """Отданный клиент используем как есть; иначе создаём временный."""
    if client is not None:
        return nullcontext(client)
    return httpx.Client(timeout=timeout)


def check_server(
    base_url: str | None = None, *, client: httpx.Client | None = None
) -> str:
    """
    Версия запущенного сервера Ollama (GET /api/version).

    Бросает httpx.ConnectError, если сервер не поднят — вызывающий код
    ловит и печатает понятную инструкцию.
    """
    base_url = base_url or settings.ollama_base_url
    with _client_cm(client, settings.ollama_connect_timeout_s) as c:
        resp = c.get(f"{base_url}/api/version")
        resp.raise_for_status()
        return resp.json().get("version", "unknown")


def list_models(
    base_url: str | None = None, *, client: httpx.Client | None = None
) -> list[str]:
    """Список скачанных моделей (имена с тегом, например 'gemma3:4b')."""
    base_url = base_url or settings.ollama_base_url
    with _client_cm(client, settings.ollama_connect_timeout_s) as c:
        resp = c.get(f"{base_url}/api/tags")
        resp.raise_for_status()
        return [m["name"] for m in resp.json().get("models", [])]


def model_available(
    model: str | None = None,
    base_url: str | None = None,
    *,
    client: httpx.Client | None = None,
) -> bool:
    """
    Скачана ли модель. Точное совпадение имени с тегом; если в model тег не
    указан — сверяем по базовому имени (Ollama трактует его как ':latest').
    """
    model = model or settings.ollama_model
    names = list_models(base_url, client=client)
    if model in names:
        return True
    if ":" not in model:
        return any(n.split(":", 1)[0] == model for n in names)
    return False


def warmup(
    model: str | None = None,
    base_url: str | None = None,
    keep_alive: str | None = None,
    num_ctx: int | None = None,
    *,
    client: httpx.Client | None = None,
) -> float:
    """
    Загружает модель в память без генерации (POST /api/generate с пустым
    prompt — штатный способ preload у Ollama). Возвращает время загрузки, с.

    num_ctx передаётся тот же, что и при извлечении. Иначе прогрев не имеет
    смысла: Ollama выгружает и заново загружает модель, когда меняется
    размер контекста, и первый же реальный запрос снова заплатит cold start.
    """
    model = model or settings.ollama_model
    base_url = base_url or settings.ollama_base_url
    keep_alive = keep_alive or settings.ollama_keep_alive
    num_ctx = settings.ollama_num_ctx if num_ctx is None else num_ctx
    payload: dict = {"model": model, "keep_alive": keep_alive, "stream": False}
    if num_ctx:
        payload["options"] = {"num_ctx": num_ctx}
    start = time.perf_counter()
    with _client_cm(client, settings.ollama_timeout_s) as c:
        resp = c.post(f"{base_url}/api/generate", json=payload)
        resp.raise_for_status()
    return time.perf_counter() - start
