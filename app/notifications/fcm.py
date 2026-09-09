"""
Push-уведомления через Firebase Cloud Messaging.

firebase-admin импортируется ЛЕНИВО внутри функций, поэтому модуль
импортируется даже без установленного пакета (нужно для тестов/dev).
Если креды не заданы — notify() становится no-op с предупреждением.

Используется topic-рассылка: устройства подписываются на тему (например,
"conferences"), и одно сообщение уходит всем подписчикам — не нужно вести
реестр отдельных токенов для рассылки.
"""

from __future__ import annotations

import datetime as dt
import logging

from app.config import settings
from app.models.db import Event

logger = logging.getLogger(__name__)

_initialized = False


def _ensure_app() -> bool:
    """Инициализирует Firebase один раз. Возвращает False, если нет кредов."""
    global _initialized
    if _initialized:
        return True
    if not settings.fcm_credentials_path:
        logger.warning("FCM не сконфигурирован (fcm_credentials_path пуст) — пропуск")
        return False
    import firebase_admin
    from firebase_admin import credentials

    cred = credentials.Certificate(settings.fcm_credentials_path)
    firebase_admin.initialize_app(cred)
    _initialized = True
    return True


def notify_event(event: Event, topic: str | None = None) -> str | None:
    """Отправляет push о событии в topic. Возвращает message_id или None."""
    if not _ensure_app():
        return None
    from firebase_admin import messaging

    topic = topic or settings.fcm_default_topic
    when = event.date.isoformat() if event.date else "дата уточнюється"
    body = when + (f", {event.time}" if event.time else "")
    if event.location:
        body += f", {event.location}"

    message = messaging.Message(
        notification=messaging.Notification(title=event.event_title, body=body),
        data={
            "event_id": str(event.id),
            "event_type": event.event_type,
            "date": event.date.isoformat() if event.date else "",
        },
        topic=topic,
    )
    message_id = messaging.send(message)
    logger.info("FCM push отправлен: %s", message_id)
    return message_id


def subscribe_token_to_topics(token: str, topics: list[str]) -> None:
    """Подписывает токен устройства на список тем."""
    if not _ensure_app():
        return
    from firebase_admin import messaging

    for topic in topics:
        messaging.subscribe_to_topic([token], topic)
        logger.info("Токен подписан на тему %s", topic)


def unsubscribe_token_from_topics(token: str, topics: list[str]) -> None:
    """
    Отписывает токен от тем — пользователь выключил уведомления.

    Парная операция к subscribe: без неё отказаться от push было
    невозможно, токен оставался подписанным навсегда.
    """
    if not _ensure_app():
        return
    from firebase_admin import messaging

    for topic in topics:
        messaging.unsubscribe_from_topic([token], topic)
        logger.info("Токен отписан от темы %s", topic)
