"""Controle do robô pela aba Configurações: liga/desliga, intervalo e "Atualizar Dados"."""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app import services, watchdog
from app.database import SessionLocal
from app.models import AlertSent, SyncKind, SyncRun, SyncStatus
from app.routers.auth import current_user
from app.services import get_settings, now_utc
from main import app


@pytest.fixture
def client():
    app.dependency_overrides[current_user] = lambda: object()
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def clean(client):
    def reset():
        with SessionLocal() as db:
            db.query(SyncRun).delete()
            db.query(AlertSent).delete()
            row = get_settings(db)
            row.robo_enabled, row.robo_interval_min, row.robo_run_requested_at = True, 5, None
            db.commit()
    reset()
    yield
    reset()


@pytest.fixture
def sent(monkeypatch):
    messages: list[str] = []
    monkeypatch.setattr(services, "telegram_creds", lambda _db: ("bot", "chat"))
    monkeypatch.setattr(services, "send_telegram", lambda _t, _c, text: messages.append(text))
    return messages


def beat(minutes_ago: int, **stats) -> None:
    when = now_utc() - timedelta(minutes=minutes_ago)
    with SessionLocal() as db:
        db.add(SyncRun(kind=SyncKind.TWEETS, status=SyncStatus.OK, started_at=when, finished_at=when, stats=stats))
        db.commit()


def silence() -> bool:
    with SessionLocal() as db:
        return watchdog.check_silence(db)


def test_defaults(client):
    body = client.get("/api/robo").json()
    assert body["enabled"] is True and body["interval_min"] == 5
    assert body["requested_at"] is None and body["last_run"] is None and 5 in body["intervals"]


def test_last_run_and_next(client):
    beat(2, contas=5, tweets=40)
    body = client.get("/api/robo").json()
    assert body["last_run"]["contas"] == 5 and body["last_run"]["tweets"] == 40
    assert body["next_run_at"] > body["last_run"]["finished_at"]


def test_change_interval_and_reject_odd_values(client):
    assert client.put("/api/robo", json={"interval_min": 30}).json()["interval_min"] == 30
    assert client.put("/api/robo", json={"interval_min": 7}).status_code == 400


def test_run_now_records_request(client):
    body = client.post("/api/robo/run").json()
    assert body["requested_at"] is not None


def test_turning_back_on_asks_for_a_run(client):
    body = client.put("/api/robo", json={"enabled": False}).json()
    assert body["enabled"] is False and body["requested_at"] is None and body["next_run_at"] is None
    body = client.put("/api/robo", json={"enabled": True}).json()
    assert body["enabled"] is True and body["requested_at"] is not None


def test_watchdog_quiet_when_off(client, sent):
    beat(120)
    client.put("/api/robo", json={"enabled": False})
    assert silence() is False and sent == []


def test_watchdog_follows_interval(client, sent):
    client.put("/api/robo", json={"interval_min": 60})
    beat(100)
    assert silence() is False  # limite: 2 x 60 + 5 = 125 min
    beat(130)
    with SessionLocal() as db:
        db.query(SyncRun).filter(SyncRun.finished_at > now_utc() - timedelta(minutes=110)).delete()
        db.commit()
    assert silence() is True and "Robô parado" in sent[0]


def test_watchdog_waits_for_pending_request(client, sent):
    beat(30)
    client.post("/api/robo/run")
    assert silence() is False
