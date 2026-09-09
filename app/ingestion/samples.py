"""
Парсер набора симулированных сообщений (JSONL).

Каждая строка — объект вида:
  {"text": "...", "date": "2026-04-02",
   "source_channel": "@demo", "source_message_id": 101}

Возвращает те же ExportMessage, что и парсер Telegram-экспорта, — поэтому
импорт истории и моделирование данных идут по одному коду пайплайна.
Используется для разработки и демонстрации без реального Telegram-канала.

load_messages() дополнительно распознаёт формат файла (JSONL / JSON-массив /
экспорт Telegram Desktop) — чтобы скрипты принимали то, что дал пользователь,
без ручной конвертации.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Iterable, Iterator
from pathlib import Path

from app.ingestion.tg_export import ExportMessage, iter_export_messages
from app.services.clock import today


def _message_from_obj(obj: dict, index: int) -> tuple[ExportMessage, str | None]:
    """
    Один объект датасета → ExportMessage (+ предупреждение, если оно есть).

    Дата — не косметика: относительные даты («завтра») модель разрешает
    ОТНОСИТЕЛЬНО неё, поэтому подмена на сегодняшнюю тихо ломает проверку.
    Историческое поведение (подставить today) сохранено, но теперь о нём
    можно узнать.
    """
    warning: str | None = None
    raw_date = str(obj.get("date", ""))[:10]
    try:
        date = dt.date.fromisoformat(raw_date)
    except ValueError:
        date = today()
        warning = (
            f"запись #{index}: поле date отсутствует или нечитаемо "
            f"({obj.get('date')!r}) — подставлена сегодняшняя {date.isoformat()}; "
            "относительные даты («завтра») будут разрешены неверно"
        )
    return (
        ExportMessage(
            message_id=int(obj.get("source_message_id", obj.get("id", index))),
            date=date,
            text=str(obj["text"]).strip(),
            channel=str(obj.get("source_channel", obj.get("channel", "@sample"))),
            # _expect — ожидаемый результат из тестового датасета; пайплайн его
            # игнорирует, проверка промта показывает рядом с ответом модели.
            note=str(obj.get("_expect", "")).strip(),
        ),
        warning,
    )


def iter_sample_messages(lines: Iterable[str]) -> Iterator[ExportMessage]:
    """Построчный JSONL. Пустые строки и комментарии (#) пропускаются."""
    for i, line in enumerate(lines, 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        message, _ = _message_from_obj(json.loads(line), i)
        yield message


def load_messages(path: str | Path) -> tuple[list[ExportMessage], list[str]]:
    """
    Загружает сообщения из файла, определяя формат по содержимому.

    Поддерживается:
      * JSONL — по объекту в строке (data/sample_messages.jsonl);
      * JSON-массив — [{...}, {...}];
      * экспорт Telegram Desktop — {"name": ..., "messages": [...]}.

    Возвращает (сообщения, предупреждения). Предупреждения показываются
    пользователю, а не глотаются: чаще всего это подменённая дата.
    """
    path = Path(path)
    raw = path.read_text(encoding="utf-8-sig").strip()
    if not raw:
        return [], [f"файл {path} пуст"]

    warnings: list[str] = []

    if raw[0] == "[":
        objects = json.loads(raw)
    elif raw[0] == "{":
        first = json.loads(raw.splitlines()[0]) if "\n" in raw else json.loads(raw)
        if isinstance(first, dict) and "messages" in first:
            export = json.loads(raw)
            messages = list(iter_export_messages(export))
            if not messages:
                warnings.append("в экспорте Telegram не нашлось текстовых сообщений")
            return messages, warnings
        objects = [
            json.loads(line)
            for line in raw.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
    else:
        objects = [
            json.loads(line)
            for line in raw.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]

    messages: list[ExportMessage] = []
    for i, obj in enumerate(objects, 1):
        if "text" not in obj:
            warnings.append(f"запись #{i}: нет обязательного поля 'text' — пропущена")
            continue
        message, warning = _message_from_obj(obj, i)
        if not message.text:
            warnings.append(f"запись #{i}: пустой text — пропущена")
            continue
        if warning:
            warnings.append(warning)
        messages.append(message)
    return messages, warnings
