"""
Тесты контракта для мобильного приложения: синхронизация и подписки.

Проверяется то, без чего клиент нельзя написать корректно: узнать об
изменениях в уже загруженных событиях и узнать, что событие сняли с
публикации.

Отдельно проверяется постраничный обход: записи, созданные в одну секунду,
получают близкие `updated_at`, и курсор по одному лишь времени застревал бы
на первой странице. Поэтому курсор составной — (updated_at, id).
"""

from __future__ import annotations

import datetime as dt

from app.db import crud
from app.models.schemas import EventType, ExtractedEvent
from app.services.clock import utc_now


def _event(title="Конференція AI", date=dt.date(2099, 9, 1)):
    return ExtractedEvent(
        event_title=title, date=date, event_type=EventType.conference
    )


def _approved(session, title="Конференція AI", message_id=1):
    ev, _ = crud.upsert_event(
        session, _event(title=title), source_channel="@cs",
        source_message_id=message_id,
    )
    crud.set_event_status(session, ev.id, "approved")
    return ev


# --- Первая синхронизация ---


def test_first_sync_returns_whole_feed(client, db_session):
    _approved(db_session, "Подія A", 1)
    _approved(db_session, "Подія B", 2)

    body = client.get("/events/sync").json()
    assert len(body["changed"]) == 2
    assert body["removed"] == []
    assert body["cursor"]
    assert body["has_more"] is False


def _cursor(moment: dt.datetime, last_id: int = 0) -> str:
    """Собирает курсор так же, как это делает сервер."""
    return f"{moment.isoformat()}|{last_id}"


def test_sync_route_not_shadowed_by_event_id(client):
    # /events/sync объявлен до /events/{event_id}. Если порядок нарушить,
    # FastAPI попробует разобрать «sync» как int и ответит 422.
    resp = client.get("/events/sync")
    assert resp.status_code == 200
    assert "changed" in resp.json()


def test_sync_is_public(client, db_session):
    # Приложение ходит без ключа — синхронизация обязана быть открытой.
    _approved(db_session)
    assert client.get("/events/sync").status_code == 200


def test_pending_events_never_reach_the_client(client, db_session):
    crud.upsert_event(
        db_session, _event(), source_channel="@cs", source_message_id=1
    )  # остаётся pending
    body = client.get("/events/sync").json()
    assert body["changed"] == []


# --- Инкремент ---


def test_nothing_changed_after_catching_up(client, db_session):
    _approved(db_session)
    cursor = client.get("/events/sync").json()["cursor"]
    body = client.get("/events/sync", params={"cursor": cursor}).json()
    assert body["changed"] == []
    assert body["removed"] == []


def test_everything_returned_from_past_cursor(client, db_session):
    _approved(db_session)
    past = _cursor(utc_now() - dt.timedelta(hours=1))
    body = client.get("/events/sync", params={"cursor": past}).json()
    assert len(body["changed"]) == 1


def test_malformed_cursor_is_422(client):
    # Молча начать с нуля нельзя: клиент решил бы, что его кэш актуален.
    assert client.get("/events/sync", params={"cursor": "мусор"}).status_code == 422
    assert client.get("/events/sync", params={"cursor": "|5"}).status_code == 422


def test_moderator_edit_shows_up_in_sync(client, db_session):
    """
    Правка уже опубликованного события должна долетать до клиента.

    Ровно ради этого добавлена колонка updated_at: по created_at перенос
    даты неотличим от отсутствия изменений, и приложение продолжало бы
    показывать студентам старую аудиторию.
    """
    ev = _approved(db_session)
    cursor = client.get("/events/sync").json()["cursor"]
    assert client.get("/events/sync", params={"cursor": cursor}).json()["changed"] == []

    client.patch(f"/events/{ev.id}", json={"location": "Ауд. 305"})

    # Курсор из далёкого прошлого — правка обязана попасть в выдачу.
    past = _cursor(utc_now() - dt.timedelta(hours=1))
    changed = client.get("/events/sync", params={"cursor": past}).json()["changed"]
    assert [e["location"] for e in changed] == ["Ауд. 305"]


def test_edit_moves_updated_at_but_not_created_at(client, db_session):
    ev = _approved(db_session)
    before = client.get("/events").json()[0]
    client.patch(f"/events/{ev.id}", json={"location": "Ауд. 305"})
    after = client.get("/events").json()[0]

    assert after["updated_at"] >= before["updated_at"]
    # created_at обязан остаться прежним: на нём держится смысл «когда
    # событие появилось», и правка модератора не должна его сдвигать.
    assert after["created_at"] == before["created_at"]


# --- Снятие с публикации ---


def test_rejected_event_lands_in_removed(client, db_session):
    """
    Отклонённое событие должно приходить клиенту как removed.

    Без этого списка приложение с локальным кэшем продолжало бы показывать
    отменённое мероприятие — студент пришёл бы к закрытой аудитории.
    """
    ev = _approved(db_session)
    assert len(client.get("/events/sync").json()["changed"]) == 1

    client.post(f"/events/{ev.id}/reject")

    body = client.get("/events/sync").json()
    assert body["changed"] == []
    assert body["removed"] == [ev.id]


def test_removed_carries_only_ids(client, db_session):
    ev = _approved(db_session)
    client.post(f"/events/{ev.id}/reject")
    removed = client.get("/events/sync").json()["removed"]
    assert removed == [ev.id]
    assert all(isinstance(i, int) for i in removed)


def test_sync_limit_is_bounded(client, db_session):
    for i in range(5):
        _approved(db_session, f"Подія {i}", i)
    body = client.get("/events/sync", params={"limit": 2}).json()
    assert len(body["changed"]) == 2


def test_sync_rejects_absurd_limit(client):
    assert client.get("/events/sync", params={"limit": 5000}).status_code == 422


def test_truncated_page_reports_has_more(client, db_session):
    for i in range(5):
        _approved(db_session, f"Подія {i}", i)

    body = client.get("/events/sync", params={"limit": 2}).json()
    assert body["has_more"] is True
    assert len(body["changed"]) == 2


def test_paging_through_sync_yields_every_event(client, db_session):
    """
    Цикл «пока has_more» обязан обойти весь фид без потерь и без зацикливания.

    Все семь записей создаются в одну секунду, поэтому на SQLite у них
    совпадает updated_at. Курсор только по времени здесь не сдвигался бы за
    границу страницы — клиент вечно получал бы одни и те же две записи.
    Ровно поэтому курсор составной: (updated_at, id).
    """
    for i in range(7):
        _approved(db_session, f"Подія {i}", i)

    seen: set[int] = set()
    params: dict = {"limit": 2}
    for _ in range(10):  # страховка от зацикливания
        body = client.get("/events/sync", params=params).json()
        seen.update(e["id"] for e in body["changed"])
        if not body["has_more"]:
            break
        params = {"limit": 2, "cursor": body["cursor"]}
    else:
        raise AssertionError("синхронизация не сошлась за 10 страниц")

    assert len(seen) == 7


def test_paging_mixes_changed_and_removed_in_one_stream(client, db_session):
    # Оба списка идут одним потоком под одним курсором — иначе продвигать
    # два независимых курсора согласованно было бы нечем.
    keep = [_approved(db_session, f"Лишається {i}", i) for i in range(3)]
    drop = [_approved(db_session, f"Скасовано {i}", 10 + i) for i in range(3)]
    for ev in drop:
        client.post(f"/events/{ev.id}/reject")

    changed: set[int] = set()
    removed: set[int] = set()
    params: dict = {"limit": 2}
    for _ in range(10):
        body = client.get("/events/sync", params=params).json()
        changed.update(e["id"] for e in body["changed"])
        removed.update(body["removed"])
        if not body["has_more"]:
            break
        params = {"limit": 2, "cursor": body["cursor"]}
    else:
        raise AssertionError("синхронизация не сошлась")

    assert changed == {e.id for e in keep}
    assert removed == {e.id for e in drop}


# --- Подписки на push ---


def test_register_device(client):
    resp = client.post(
        "/devices/register",
        json={"fcm_token": "t0k3n", "topics": ["conferences", "deadlines"]},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "subscribed"


def test_unregister_device(client):
    # Пользователь выключил уведомления — до появления этого эндпоинта
    # отписаться было нельзя вовсе.
    resp = client.post(
        "/devices/unregister", json={"fcm_token": "t0k3n", "topics": ["conferences"]}
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "unsubscribed"


def test_invalid_topic_name_is_422_not_firebase_error(client):
    # Мусорное имя темы отсекается схемой, а не падает внутри firebase-admin.
    resp = client.post(
        "/devices/register", json={"fcm_token": "t0k3n", "topics": ["не тема!"]}
    )
    assert resp.status_code == 422


def test_empty_topic_list_rejected(client):
    resp = client.post(
        "/devices/register", json={"fcm_token": "t0k3n", "topics": []}
    )
    assert resp.status_code == 422


def test_empty_token_rejected(client):
    resp = client.post(
        "/devices/register", json={"fcm_token": "", "topics": ["conferences"]}
    )
    assert resp.status_code == 422
