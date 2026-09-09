"""
FastAPI-приложение — HTTP-слой системы.

Эндпоинты:
  GET   /health               — жив ли процесс (алиас /health/live)
  GET   /health/live          — проба живости для Docker HEALTHCHECK
  GET   /health/ready         — готовность: БД обязательна, Ollama желательна
  GET   /events               — фид (по умолчанию только approved)
  GET   /events/sync          — инкрементальная синхронизация приложения
  GET   /events/{id}          — событие + оригинал сообщения (модерация)
  POST  /events/ingest        — принять сообщение в очередь (202 + job_id)
  GET   /jobs/{id}            — что стало с задачей: события, отсев, ошибки
  GET   /jobs                 — последние задачи очереди
  GET   /stats                — счётчики модерации и очереди
  POST  /events/{id}/approve  — подтвердить (+push для будущих дат)
  POST  /events/{id}/reject   — отклонить
  PATCH /events/{id}          — исправить поля (модерация)
  POST  /devices/register     — подписка FCM-токена на темы
  POST  /devices/unregister   — отписка (пользователь выключил уведомления)

ВАЖНО про /events/ingest: он больше не выполняет извлечение сам, а только
кладёт сообщение в таблицу `ingest_jobs` и отвечает 202. Обработку ведёт
отдельный процесс `python -m app.worker`. Причина — время: одно сообщение
занимает у модели секунды, а таймаут генерации выставлен в 300 с. Раньше это
время держался HTTP-запрос, и при импорте истории рабочие потоки FastAPI
уходили в инференс целиком — фид для мобильного приложения переставал
отвечать. Теперь API остаётся быстрым независимо от нагрузки на LLM.

Поток: бот шлёт пост на /events/ingest → задача в очереди → воркер извлекает
события и сохраняет их как pending → бот показывает админам карточки с
кнопками → approve публикует и шлёт push.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text as sql_text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.api import cursor as cursor_codec
from app.api.security import require_admin_key, require_ingest_key
from app.config import settings
from app.db import crud
from app.db import jobs as jobs_repo
from app.db.database import engine, get_session, init_db
from app.llm import ollama_status
from app.models.schemas import (
    DeviceRegistration,
    EventDetail,
    EventOut,
    EventUpdate,
    IngestAccepted,
    IngestRequest,
    JobOut,
    QueueStats,
    Stats,
    SyncResponse,
)
from app.notifications.fcm import (
    notify_event,
    subscribe_token_to_topics,
    unsubscribe_token_from_topics,
)
from app.services import moderation
from app.services.clock import utc_now

logger = logging.getLogger(__name__)


def _prepare_schema() -> None:
    """
    Создать таблицы автоматически — ТОЛЬКО на SQLite.

    На PostgreSQL схему накатывает Alembic (`alembic upgrade head`, его
    вызывает docker-entrypoint.sh). Делать там ещё и `create_all` нельзя:
    таблицы появились бы без записи в alembic_version, и первая же миграция
    упала бы с «relation already exists». Плюс `create_all` не умеет ALTER —
    ровно на этом система однажды уже сломалась при добавлении meeting_code.
    """
    if settings.database_url.startswith("sqlite"):
        init_db()
        return
    logger.info(
        "Схему БД накатывает Alembic. Если таблиц ещё нет — выполните "
        "`alembic upgrade head`."
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    _prepare_schema()
    if not settings.auth_enabled:
        logger.warning(
            "API-ключи не заданы: /events/ingest, approve, reject и PATCH "
            "открыты без аутентификации. Для локальной разработки это норма, "
            "но перед публикацией порта задайте INGEST_API_KEY и ADMIN_API_KEY."
        )
    # Прогрев Ollama здесь больше не нужен: инференс переехал в воркер,
    # он же и греет модель при старте (app/worker.py::_warmup).
    yield


app = FastAPI(title="University Events Extractor", lifespan=lifespan)

if settings.cors_origin_list:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH", "OPTIONS"],
        allow_headers=["*"],
        expose_headers=["X-Total-Count", "X-Request-ID"],
    )


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    """
    Сквозной идентификатор запроса в логах и в заголовке ответа.

    В контейнере логи трёх сервисов (api, worker, bot) сливаются в один поток;
    без такого маркера невозможно связать «бот получил ошибку» с конкретной
    строчкой в логе API.
    """
    request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:12]
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    return response


# --- Обработчики ошибок инфраструктуры ---


@app.exception_handler(OperationalError)
async def _database_unavailable(request: Request, exc: OperationalError):
    """
    Недоступная БД — это 503 «повтори позже», а не 500 «в коде баг».

    Различие не косметическое: контейнер `db` перезапускается, и клиент
    (бот, импортёр истории) должен понять, что запрос имеет смысл повторить.
    По 500 он бы просто записал сообщение в потери.

    Ошибки самой LLM сюда не попадают: инференс выполняет воркер, и его сбои
    видны в поле `last_error` задачи (GET /jobs/{id}), а не в ответе API.
    """
    logger.warning("БД недоступна: %s", exc)
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"detail": "База данных временно недоступна, повторите запрос"},
    )


# --- Пробы ---


@app.get("/health/live")
def health_live() -> dict[str, str]:
    """Процесс жив. Без обращений к БД — иначе Docker убивал бы контейнер
    API из-за недоступной базы, хотя чинить надо не его."""
    return {"status": "ok"}


@app.get("/health")
def health() -> dict[str, str]:
    """Алиас /health/live — его пингует бот при старте."""
    return health_live()


@app.get("/health/ready")
def health_ready(response: Response) -> dict:
    """
    Готовность обслуживать запросы.

    БД обязательна: без неё не отдать ни фида, ни очереди — 503.
    Ollama желательна: фид и модерация работают и без неё, задачи просто
    полежат в очереди, пока воркер не достучится. Поэтому её недоступность
    отражается как `degraded`, но не роняет пробу — иначе перезапуск Ollama
    выводил бы из ротации весь API.
    """
    checks: dict[str, str] = {}

    try:
        with engine.connect() as conn:
            conn.execute(sql_text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:
        checks["database"] = f"error: {type(exc).__name__}"
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "unavailable", "checks": checks}

    try:
        ollama_status.check_server()
        checks["ollama"] = "ok"
    except Exception as exc:
        checks["ollama"] = f"unreachable: {type(exc).__name__}"

    overall = "ok" if checks["ollama"] == "ok" else "degraded"
    return {"status": overall, "checks": checks}


# --- Публичный фид ---


@app.get("/events", response_model=list[EventOut])
def get_events(
    response: Response,
    session: Session = Depends(get_session),
    status_: str = Query(
        "approved", alias="status", pattern="^(approved|pending|rejected|all)$",
        description="Публичный фид приложения = approved",
    ),
    upcoming: bool = Query(False, description="Только будущие события"),
    date_from: dt.date | None = None,
    date_to: dt.date | None = None,
    event_type: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0, description="Смещение для постраничной выдачи"),
):
    filters = dict(
        status=None if status_ == "all" else status_,
        upcoming_only=upcoming,
        date_from=date_from,
        date_to=date_to,
        event_type=event_type,
    )
    # Общее число под теми же фильтрами — клиенту нужно знать, есть ли ещё
    # страницы, а по длине текущей выдачи это не определить.
    response.headers["X-Total-Count"] = str(crud.count_events(session, **filters))
    return crud.list_events(session, limit=limit, offset=offset, **filters)


@app.get("/events/sync", response_model=SyncResponse)
def sync_events(
    session: Session = Depends(get_session),
    cursor: str | None = Query(
        None,
        description="Значение поля cursor из предыдущего ответа. "
        "Пусто — первая синхронизация, вернётся весь опубликованный фид.",
    ),
    limit: int = Query(500, ge=1, le=1000),
):
    """
    Инкрементальная синхронизация для мобильного приложения.

    Отдаёт два списка: `changed` — опубликованные события, изменившиеся с
    момента `since`, и `removed` — id тех, что модератор отклонил. Второй
    список принципиален: без него приложение с локальным кэшем продолжало бы
    показывать отменённое мероприятие, а в календаре вуза это худший вид
    ошибки — студент придёт к закрытой аудитории.

    Курсор берётся с сервера, а не с устройства: часы телефона могут
    отставать или спешить, и события в этом промежутке потерялись бы.
    Пока `has_more` истинно, запрос нужно повторять — выдача упёрлась в
    `limit`, и изменения кончились не все.

    ВАЖНО: этот маршрут объявлен ДО `/events/{event_id}`. Иначе FastAPI
    сопоставил бы «sync» с параметром event_id и вернул 422.
    """
    try:
        after = cursor_codec.decode(cursor) if cursor else None
    except ValueError as exc:
        # Не начинаем молча с нуля: клиент решил бы, что его кэш актуален.
        raise HTTPException(422, f"Некорректный курсор: {exc}") from exc

    # Момент фиксируем ДО выборки: всё, что изменится во время запроса,
    # попадёт в следующую синхронизацию, а не потеряется между ними.
    server_time = utc_now()
    # Оба статуса — одним упорядоченным потоком, чтобы курсор был один и
    # продвигался согласованно (см. crud.changed_since).
    rows = crud.changed_since(
        session, after, statuses=("approved", "rejected"), limit=limit
    )
    has_more = len(rows) == limit
    next_cursor = (
        cursor_codec.encode(rows[-1].updated_at, rows[-1].id)
        if has_more
        # Догнали хвост: следующий запрос начнётся с «сейчас». id=0 меньше
        # любого настоящего, поэтому запись, изменённая ровно в этот момент,
        # в следующую выдачу попадёт, а не потеряется.
        else cursor_codec.encode(server_time, 0)
    )
    return SyncResponse(
        cursor=next_cursor,
        has_more=has_more,
        changed=[ev for ev in rows if ev.status == "approved"],
        # Отклонённые отдаём только идентификаторами: содержимое клиенту
        # не нужно, ему достаточно знать, что запись пора убрать.
        removed=[ev.id for ev in rows if ev.status == "rejected"],
    )


@app.get(
    "/events/{event_id}",
    response_model=EventDetail,
    dependencies=[Depends(require_admin_key)],
)
def get_event_detail(event_id: int, session: Session = Depends(get_session)):
    """
    Одно событие + оригинал источника (raw_text) — для карточек модерации.

    Под админским ключом: raw_text — это неотредактированный текст объявления,
    в нём бывают телефоны и фамилии, поэтому в публичный фид он не идёт.
    """
    ev = crud.get_event(session, event_id)
    if ev is None:
        raise HTTPException(404, "Event not found")
    return ev


# --- Приём сообщений (очередь) ---


@app.post(
    "/events/ingest",
    response_model=IngestAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_ingest_key)],
)
def ingest(req: IngestRequest, session: Session = Depends(get_session)):
    """
    Ставит сообщение в очередь на обработку и сразу отвечает 202.

    Событий в ответе нет — на момент ответа их ещё не существует. Результат
    забирается по `GET /jobs/{job_id}`: там окажутся id сохранённых событий и
    список отсеянных с причинами.
    """
    job, is_new = jobs_repo.enqueue(
        session,
        req.text,
        source_channel=req.source_channel,
        source_message_id=req.source_message_id,
        reference_date=req.reference_date,
    )
    if is_new:
        logger.info(
            "Задача %s поставлена в очередь (%s/%s)",
            job.id, req.source_channel, req.source_message_id,
        )
    return IngestAccepted(job_id=job.id, status=job.status, duplicate=not is_new)


@app.get(
    "/jobs/{job_id}",
    response_model=JobOut,
    dependencies=[Depends(require_ingest_key)],
)
def get_job(job_id: int, session: Session = Depends(get_session)):
    """Судьба задачи: статус, id сохранённых событий, отсеянные с причиной."""
    job = jobs_repo.get_job(session, job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    return job


@app.get(
    "/jobs",
    response_model=list[JobOut],
    dependencies=[Depends(require_admin_key)],
)
def list_jobs(
    session: Session = Depends(get_session),
    status_: str | None = Query(
        None, alias="status", pattern="^(queued|processing|done|failed)$"
    ),
    limit: int = Query(50, ge=1, le=500),
):
    return jobs_repo.list_jobs(session, status=status_, limit=limit)


@app.get("/stats", response_model=Stats, dependencies=[Depends(require_admin_key)])
def get_stats(session: Session = Depends(get_session)):
    """
    Счётчики модерации и очереди.

    Двойное назначение: смоук после развёртывания («события идут?») и цифры
    для таблицы результатов в статье — сколько извлечено, сколько подтверждено,
    какова медленная граница обработки одного сообщения.
    """
    return Stats(
        events=crud.status_counts(session),
        queue=QueueStats(**jobs_repo.queue_stats(session)),
    )


# --- Модерация ---


@app.post(
    "/events/{event_id}/approve",
    response_model=EventOut,
    dependencies=[Depends(require_admin_key)],
)
def approve(
    event_id: int,
    notify: bool = Query(
        True, description="Слать push (уходит только для будущих дат)"
    ),
    session: Session = Depends(get_session),
):
    ev = moderation.approve_event(
        session, event_id, notifier=notify_event, notify=notify
    )
    if ev is None:
        raise HTTPException(404, "Event not found")
    return ev


@app.post(
    "/events/{event_id}/reject",
    response_model=EventOut,
    dependencies=[Depends(require_admin_key)],
)
def reject(event_id: int, session: Session = Depends(get_session)):
    ev = moderation.reject_event(session, event_id)
    if ev is None:
        raise HTTPException(404, "Event not found")
    return ev


@app.patch(
    "/events/{event_id}",
    response_model=EventOut,
    dependencies=[Depends(require_admin_key)],
)
def update_event(
    event_id: int, update: EventUpdate, session: Session = Depends(get_session)
):
    ev = moderation.edit_event(session, event_id, update)
    if ev is None:
        raise HTTPException(404, "Event not found")
    return ev


# --- Мобильное приложение ---


@app.post("/devices/register")
def register_device(reg: DeviceRegistration) -> dict[str, str]:
    """Публичный: вызывается приложением, API-ключа у него нет."""
    subscribe_token_to_topics(reg.fcm_token, reg.topics)
    return {"status": "subscribed", "topics": ",".join(reg.topics)}


@app.post("/devices/unregister")
def unregister_device(reg: DeviceRegistration) -> dict[str, str]:
    """
    Отписка от тем: пользователь выключил уведомления в настройках.

    Без этого эндпоинта отписаться было нельзя вовсе — токен оставался
    подписанным навсегда, и человек продолжал получать push после того,
    как явно от них отказался.
    """
    unsubscribe_token_from_topics(reg.fcm_token, reg.topics)
    return {"status": "unsubscribed", "topics": ",".join(reg.topics)}
