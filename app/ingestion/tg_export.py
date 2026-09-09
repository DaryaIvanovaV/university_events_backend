"""
Парсер JSON-экспорта Telegram Desktop (result.json).

Как получить файл: Telegram Desktop → открыть канал → меню (⋮) →
«Export chat history» → формат JSON (медиа можно отключить) → result.json.

Структура экспорта: {"name": ..., "id": ..., "messages": [{"id", "type",
"date", "text": str | list[str | {"type","text"}], ...}, ...]}
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from dataclasses import dataclass


@dataclass
class ExportMessage:
    message_id: int
    date: dt.date
    text: str
    channel: str
    # Необязательная пометка из тестового датасета (поле _expect: что модель
    # ДОЛЖНА извлечь). В пайплайне не используется — нужна только проверке
    # промта, чтобы показать ожидаемый результат рядом с фактическим.
    note: str = ""


def flatten_text(text_field: object) -> str:
    """Поле text бывает строкой или списком строк и {'type','text'}-частей."""
    if isinstance(text_field, str):
        return text_field
    if isinstance(text_field, list):
        parts: list[str] = []
        for part in text_field:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                parts.append(str(part.get("text", "")))
        return "".join(parts)
    return ""


def iter_export_messages(export: dict) -> Iterator[ExportMessage]:
    """Итерирует содержательные сообщения экспорта (пропуская service)."""
    channel = str(export.get("name") or export.get("id") or "export")
    for msg in export.get("messages", []):
        if msg.get("type") != "message":
            continue  # service-сообщения: создание канала, закрепы и т.п.
        text = flatten_text(msg.get("text", "")).strip()
        if not text:
            continue
        raw_date = str(msg.get("date", ""))[:10]
        try:
            date = dt.date.fromisoformat(raw_date)
        except ValueError:
            continue
        yield ExportMessage(
            message_id=int(msg["id"]), date=date, text=text, channel=channel
        )
