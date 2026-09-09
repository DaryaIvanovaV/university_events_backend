"""
Сервис модерации — этап «Перевірка та підтвердження» из статьи.

Новые события сохраняются пайплайном в статусе pending. Администратор
через Telegram-бота (или напрямую через API) подтверждает, правит или
отклоняет их:

  approve → status=approved; push уходит ТОЛЬКО если дата события не в
            прошлом (импорт старой истории не спамит студентов)
  reject  → status=rejected; запись остаётся ради дедупликации —
            переотправленный тот же анонс не появится снова
  edit    → точечное обновление полей (PATCH)
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from sqlalchemy.orm import Session

from app.db import crud
from app.models.db import Event
from app.models.schemas import EventUpdate
from app.services.clock import today

logger = logging.getLogger(__name__)

Notifier = Callable[[Event], None]


def approve_event(
    session: Session,
    event_id: int,
    *,
    notifier: Notifier | None = None,
    notify: bool = True,
) -> Event | None:
    ev = crud.set_event_status(session, event_id, "approved")
    if ev is None:
        return None
    is_future = ev.date is not None and ev.date >= today()
    if notify and notifier is not None and is_future:
        try:
            notifier(ev)
        except Exception:  # push не должен ронять подтверждение
            logger.exception("Ошибка отправки уведомления")
    return ev


def reject_event(session: Session, event_id: int) -> Event | None:
    return crud.set_event_status(session, event_id, "rejected")


def edit_event(
    session: Session, event_id: int, update: EventUpdate
) -> Event | None:
    fields = update.model_dump(exclude_unset=True)
    if not fields:
        return crud.get_event(session, event_id)
    return crud.update_event_fields(session, event_id, fields)
