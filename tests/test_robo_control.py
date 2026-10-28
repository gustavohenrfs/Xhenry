"""Controle do robô pela aba Configurações: liga/desliga, intervalo e "Atualizar Dados"."""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from conftest import AlertLog, admin_row
from app import services, watchdog
from app.database import SessionLocal
from app.models import AlertSent, SyncKind, SyncRun, SyncStatus
from app.routers.auth import current_user
from app.services import get_settings, now_utc
from main import app


@pytest.fixture
def client():
    app.dependency_overrides[current_user] = admin_row
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
    return AlertLog()


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


def test_health_counts_late_accounts_and_collectors(client):
    from uuid import uuid4

    from app.models import Collector, Folder, XAccount
    from app.services import ensure_admin

    now = now_utc()
    tag = uuid4().hex[:6]
    with SessionLocal() as db:
        folder = Folder(owner_id=ensure_admin(db).id, name=f"Saude {tag}")
        db.add(folder)
        db.flush()
        db.add_all([
            XAccount(folder_id=folder.id, username=f"em_dia_{tag}", collected_at=now - timedelta(minutes=10)),
            XAccount(folder_id=folder.id, username=f"atras_{tag}", collected_at=now - timedelta(days=2)),
            XAccount(folder_id=folder.id, username=f"nunca_{tag}"),
            Collector(handle=f"livre_{tag}", is_active=True),
            Collector(handle=f"cast_{tag}", is_active=True, paused_until=now + timedelta(minutes=30)),
            Collector(handle=f"off_{tag}", is_active=False),
        ])
        db.commit()
    beat(3, contas=1)
    try:
        body = client.get("/api/robo/health").json()
        late = {item["username"] for item in body["stale"]}
        assert {f"atras_{tag}", f"nunca_{tag}"} <= late and f"em_dia_{tag}" not in late
        assert body["stale"][0]["collected_at"] is None, "nunca coletada vem primeiro"
        assert body["fresh"] == body["accounts"] - len(body["stale"])
        c = body["collectors"]
        assert (c["free"], c["benched"], c["off"]) >= (1, 1, 1)
        assert c["next_free_at"] is not None
        assert body["late_after_min"] >= 60 and body["last_run"] is not None
        assert "proxy" not in str(body) and "cookies" not in str(body)
    finally:
        with SessionLocal() as db:
            db.query(XAccount).filter(XAccount.username.like(f"%_{tag}")).delete(synchronize_session=False)
            db.query(Collector).filter(Collector.handle.like(f"%_{tag}")).delete(synchronize_session=False)
            db.query(Folder).filter(Folder.name == f"Saude {tag}").delete(synchronize_session=False)
            db.commit()
