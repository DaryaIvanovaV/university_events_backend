"""
Тесты аутентификации по API-ключу.

Мотив: после публикации порта наружу (Docker) approve/reject/PATCH и приём
сообщений обязаны быть закрыты, а публичный фид — остаться открытым, иначе
мобильное приложение не сможет его читать.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import main
from app.config import settings
from app.db.database import get_session

INGEST_KEY = "ingest-secret"
ADMIN_KEY = "admin-secret"


@pytest.fixture()
def secured_client(engine, db_session, monkeypatch):
    """Клиент с включённой проверкой ключей."""
    monkeypatch.setattr(main, "init_db", lambda: None)
    monkeypatch.setattr(main, "engine", engine)
    monkeypatch.setattr(settings, "ingest_api_key", INGEST_KEY, raising=False)
    monkeypatch.setattr(settings, "admin_api_key", ADMIN_KEY, raising=False)

    main.app.dependency_overrides[get_session] = lambda: db_session
    with TestClient(main.app) as test_client:
        yield test_client
    main.app.dependency_overrides.clear()


# --- Что остаётся открытым ---


@pytest.mark.parametrize("path", ["/health", "/health/live", "/events"])
def test_public_endpoints_need_no_key(secured_client, path):
    assert secured_client.get(path).status_code == 200


def test_device_registration_is_public(secured_client):
    # Приложение регистрирует FCM-токен без ключа: ключа у него нет и быть
    # не должно — он утёк бы вместе с APK.
    resp = secured_client.post(
        "/devices/register", json={"fcm_token": "t0k3n", "topics": ["conferences"]}
    )
    assert resp.status_code == 200


# --- Что закрывается ---


def test_ingest_without_key_is_401(secured_client):
    resp = secured_client.post("/events/ingest", json={"text": "текст"})
    assert resp.status_code == 401


def test_ingest_with_wrong_key_is_403(secured_client):
    resp = secured_client.post(
        "/events/ingest", json={"text": "текст"}, headers={"X-API-Key": "nope"}
    )
    assert resp.status_code == 403


def test_ingest_accepts_ingest_key(secured_client):
    resp = secured_client.post(
        "/events/ingest",
        json={"text": "Конференція 1 вересня о 10:00"},
        headers={"X-API-Key": INGEST_KEY},
    )
    assert resp.status_code == 202


def test_admin_key_also_works_for_ingest(secured_client):
    # Админский ключ — надмножество прав: боту, который и принимает посты,
    # и подтверждает события, хватает одного заголовка.
    resp = secured_client.post(
        "/events/ingest",
        json={"text": "Конференція 1 вересня о 10:00"},
        headers={"X-API-Key": ADMIN_KEY},
    )
    assert resp.status_code == 202


@pytest.mark.parametrize(
    "method,path",
    [
        ("post", "/events/1/approve"),
        ("post", "/events/1/reject"),
        ("patch", "/events/1"),
        ("get", "/events/1"),
        ("get", "/jobs"),
        ("get", "/stats"),
    ],
)
def test_moderation_endpoints_require_key(secured_client, method, path):
    # У GET в TestClient нет параметра json — тело шлём только там, где оно есть.
    kwargs = {} if method == "get" else {"json": {}}
    resp = getattr(secured_client, method)(path, **kwargs)
    assert resp.status_code == 401


def test_ingest_key_is_not_enough_for_moderation(secured_client):
    resp = secured_client.get("/stats", headers={"X-API-Key": INGEST_KEY})
    assert resp.status_code == 403


def test_moderation_accepts_admin_key(secured_client):
    # 404, а не 403: ключ принят, просто события с таким id нет.
    resp = secured_client.get("/events/1", headers={"X-API-Key": ADMIN_KEY})
    assert resp.status_code == 404


def test_single_key_protects_everything(engine, db_session, monkeypatch):
    """
    Если заполнен только INGEST_API_KEY, админские эндпоинты закрываются им же.

    Иначе забытая вторая переменная окружения оставляла бы approve/reject
    полностью открытыми — самый неприятный вид ошибки конфигурации: тихий.
    """
    monkeypatch.setattr(main, "init_db", lambda: None)
    monkeypatch.setattr(main, "engine", engine)
    monkeypatch.setattr(settings, "ingest_api_key", INGEST_KEY, raising=False)
    monkeypatch.setattr(settings, "admin_api_key", "", raising=False)

    main.app.dependency_overrides[get_session] = lambda: db_session
    with TestClient(main.app) as c:
        assert c.get("/stats").status_code == 401
        assert c.get("/stats", headers={"X-API-Key": INGEST_KEY}).status_code == 200
    main.app.dependency_overrides.clear()


def test_auth_disabled_when_no_keys_configured(client):
    # Локальная разработка: без ключей всё работает как раньше.
    assert client.get("/stats").status_code == 200
