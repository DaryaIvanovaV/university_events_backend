"""
Аутентификация по API-ключу в заголовке `X-API-Key`.

Два ключа с разными правами:

  INGEST_API_KEY — приём сообщений и чтение статуса задачи. Его знают
                   Telegram-бот и scripts/import_history.py.
  ADMIN_API_KEY  — модерация (approve/reject/PATCH), оригиналы сообщений,
                   очередь и статистика. Админский ключ подходит везде, где
                   годится ingest-ключ: это надмножество прав.

Публичными остаются только фид `GET /events`, пробы живости и регистрация
устройства для push — их вызывает мобильное приложение, ключа у него нет.

Если оба ключа пусты, проверка выключена. Это сделано ради обратной
совместимости с локальной разработкой (и 200+ существующих тестов), но при
старте с проброшенным наружу портом выводится предупреждение: без ключей
любой, кто дотянется до порта, может подтверждать и править события.

Сравнение через `secrets.compare_digest` — обычное `==` на строках сравнивает
побайтово с ранним выходом, и по времени ответа ключ можно подбирать посимвольно.
"""

from __future__ import annotations

import secrets

from fastapi import Header, HTTPException, status

from app.config import settings

_HEADER = "X-API-Key"


def _allowed_ingest_keys() -> list[str]:
    return [k for k in (settings.ingest_api_key, settings.admin_api_key) if k]


def _allowed_admin_keys() -> list[str]:
    # Если задан только ingest-ключ, он же защищает и админские эндпоинты:
    # это лучше, чем оставить их полностью открытыми из-за незаполненной
    # второй переменной окружения.
    keys = [k for k in (settings.admin_api_key,) if k]
    return keys or [k for k in (settings.ingest_api_key,) if k]


def _verify(provided: str | None, allowed: list[str]) -> None:
    if not settings.auth_enabled:
        return  # ключи не настроены — режим локальной разработки
    if provided is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            f"Требуется заголовок {_HEADER}",
            headers={"WWW-Authenticate": _HEADER},
        )
    if not any(secrets.compare_digest(provided, key) for key in allowed):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Неверный API-ключ")


def require_ingest_key(x_api_key: str | None = Header(default=None)) -> None:
    """Зависимость для приёма сообщений и чтения статуса задачи."""
    _verify(x_api_key, _allowed_ingest_keys())


def require_admin_key(x_api_key: str | None = Header(default=None)) -> None:
    """Зависимость для модерации и служебных эндпоинтов."""
    _verify(x_api_key, _allowed_admin_keys())
