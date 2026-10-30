"""Pausa da madrugada e resumo do fim do dia: regras de horário, rotas e o vigia."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import robo
from app import services, watchdog
from app.database import SessionLocal
from app.models import AlertSent, SyncKind, SyncRun, SyncStatus
from app.routers import auth
from app.services import ensure_admin, get_settings, user_settings
from main import app


def utc_at(hour: int, minute: int = 0, day: int = 28) -> datetime:
    """Hora de Brasília (UTC-3) em UTC."""
    return datetime(2026, 10, day, hour, minute, tzinfo=timezone(timedelta(hours=-3))).astimezone(timezone.utc)


@pytest.mark.parametrize(("start", "end", "clock", "inside"), [
    ("00:00", "06:00", "03:10", True), ("00:00", "06:00", "06:00", False), ("00:00", "06:00", "23:59", False),
    ("23:00", "05:00", "23:30", True), ("23:00", "05:00", "04:59", True), ("23:00", "05:00", "12:00", False),
    ("06:00", "06:00", "06:00", False),
])
def test_window(start, end, clock, inside):
    assert robo.in_window(start, end, clock) is inside
    assert services.in_window(start, end, clock) is inside


def test_robot_pause_and_summary_rules():
    night, day = utc_at(3), utc_at(14)
    assert robo.paused({"pause_on": True, "pause_from": "00:00", "pause_to": "06:00"}, night) is True
    assert robo.paused({"pause_on": None}, night) is True, "padrão: pausa ligada de 00:00 às 06:00"
    assert robo.paused({"pause_on": False}, night) is False
    assert robo.paused({"pause_on": True}, day) is False
    row = {"notify_prefs": None, "summary_read_on": None}
    assert robo.summary_read_due(row, utc_at(23, 55)) is True
    assert robo.summary_read_due(row, utc_at(23, 50)) is False
    assert robo.summary_read_due(row, utc_at(23, 59)) is False, "às 23:59 já é o aviso, não a leitura"
    assert robo.summary_read_due(dict(row, summary_read_on=date(2026, 10, 28)), utc_at(23, 56)) is False
    assert robo.summary_read_due(dict(row, notify_prefs='{"kinds": {"resumo": false}}'), utc_at(23, 56)) is False
    assert robo.summary_day(utc_at(23, 59)) == date(2026, 10, 28)
    assert robo.summary_day(utc_at(0, 3, day=29)) == date(2026, 10, 28), "cron perdeu 23:59: sai até 00:04"
    assert robo.summary_day(utc_at(0, 6, day=29)) is None and robo.summary_day(utc_at(15)) is None
    assert robo.summary_text(12345, {"username": "ana", "display_name": "Ana", "views": 9000}) == (
        "🎉 Parabéns! Hoje você fechou o dia com 12.345 views\n🏆 Melhor conta: Ana (@ana) com 9.000 views")
    assert robo.summary_text(0, None).startswith("🌙 Dia encerrado")


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def headers(user_id: int) -> dict[str, str]:
    with SessionLocal() as db:
        from app.models import User

        user = db.get(User, user_id)
        return {"Authorization": f"Bearer {auth.issue_token(user.id, user.token_version or 0)}"}


def test_pause_routes_for_friend_and_admin(client, monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "segredo-de-teste")
    from app.models import User

    with SessionLocal() as db:
        friend = User(email="pausa@x.com", name="P", is_approved=True)
        db.add(friend)
        db.commit()
        friend_id, admin_id = friend.id, ensure_admin(db).id
    h, admin = headers(friend_id), headers(admin_id)
    assert client.get("/api/apify", headers=h).json()["pause"] == {"on": True, "from": "00:00", "to": "06:00"}
    assert client.put("/api/apify", json={"pause": {"from": "7h"}}, headers=h).status_code == 400
    body = client.put("/api/apify", json={"pause": {"on": True, "from": "01:00", "to": "07:30"}}, headers=h).json()
    assert body["pause"] == {"on": True, "from": "01:00", "to": "07:30"}
    robo_body = client.put("/api/robo", json={"pause": {"on": False}}, headers=admin).json()
    assert robo_body["pause"]["on"] is False
    assert client.put("/api/robo", json={"pause": {"on": True, "to": "05:00"}}, headers=admin).json()["pause"] == {
        "on": True, "from": "00:00", "to": "05:00"}
    client.put("/api/robo", json={"pause": {"on": True, "from": "00:00", "to": "06:00"}}, headers=admin)
    types = {t["key"] for t in client.get("/api/notifications", headers=h).json()["types"]}
    assert "resumo" in types


def test_watchdog_quiet_during_pause_and_after_it_ends(monkeypatch):
    # Esta função testa a pausa de verdade: tira a troca do conftest (que finge ser de dia).
    monkeypatch.setattr(watchdog, "paused_now", services.paused_now)
    monkeypatch.setattr(watchdog, "last_pause_end", services.last_pause_end)
    with SessionLocal() as db:
        db.query(SyncRun).delete()
        db.query(AlertSent).filter(AlertSent.kind == "silencio").delete()
        settings = get_settings(db)
        settings.robo_enabled, settings.apify_interval_min, settings.robo_run_requested_at = True, 5, None
        row = user_settings(db, ensure_admin(db).id)
        row.pause_on, row.pause_from, row.pause_to, row.apify_token = True, "00:00", "06:00", None
        last = utc_at(23, 50, day=27)
        db.add(SyncRun(kind=SyncKind.TWEETS, status=SyncStatus.OK, started_at=last, finished_at=last))
        db.commit()
        assert watchdog.check_silence(db, utc_at(3)) is False, "madrugada sem leitura é a pausa"
        assert watchdog.check_silence(db, utc_at(6, 10)) is False, "pausa acabou há 10 min: ainda dentro do limite"
        assert watchdog.check_silence(db, utc_at(6, 30)) is True, "30 min depois da pausa sem ler: robô parado"
        row.pause_on, settings.apify_interval_min = False, 30
        db.query(SyncRun).delete()
        db.query(AlertSent).filter(AlertSent.kind == "silencio").delete()
        db.commit()
