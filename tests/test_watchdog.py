"""Vigia: robô parado e verificação automática de saúde."""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app import services, watchdog
from app.database import SessionLocal
from app.models import AlertSent, Folder, SyncKind, SyncRun, SyncStatus, XAccount
from app.services import ensure_admin, now_utc
from main import app


@pytest.fixture(scope="module", autouse=True)
def tables():
    with TestClient(app):
        yield


@pytest.fixture
def sent(monkeypatch):
    """Telegram configurado; guarda as mensagens. Começa sem batimentos nem alertas."""
    messages: list[str] = []
    monkeypatch.setattr(services, "telegram_creds", lambda _db: ("bot", "chat"))
    monkeypatch.setattr(services, "send_telegram", lambda _t, _c, text: messages.append(text))
    with SessionLocal() as db:
        db.query(SyncRun).delete()
        db.query(AlertSent).delete()
        db.commit()
    return messages


def beat(minutes_ago: int, status: SyncStatus = SyncStatus.OK) -> None:
    when = now_utc() - timedelta(minutes=minutes_ago)
    with SessionLocal() as db:
        db.add(SyncRun(kind=SyncKind.TWEETS, status=status, started_at=when, finished_at=when))
        db.commit()


def silence() -> bool:
    with SessionLocal() as db:
        return watchdog.check_silence(db)


def test_no_beat_yet_stays_quiet(sent):
    assert silence() is False
    assert sent == []


def test_recent_beat_is_fine(sent):
    beat(5)
    assert silence() is False


def test_old_beat_alerts_once(sent):
    beat(20)
    assert silence() is True
    assert silence() is False
    assert len(sent) == 1 and "Robô parado" in sent[0] and "20 min" in sent[0]


def test_failed_run_does_not_count_as_collection(sent):
    beat(30)
    beat(1, SyncStatus.FAILED)
    assert silence() is True


def test_new_outage_after_recovery_alerts_again(sent):
    beat(40)
    assert silence() is True
    beat(18)
    assert silence() is True
    assert len(sent) == 2


def test_health_beats_from_app_are_ignored(sent):
    with SessionLocal() as db:
        db.add(SyncRun(kind=SyncKind.HEALTH, status=SyncStatus.OK, finished_at=now_utc() - timedelta(minutes=60)))
        db.commit()
    assert silence() is False


def test_silence_limit_from_env(sent, monkeypatch):
    monkeypatch.setenv("ROBO_SILENCE_MIN", "30")
    beat(20)
    assert silence() is False


def test_auto_check_off_by_default(monkeypatch):
    monkeypatch.delenv("AUTO_CHECK_MINUTES", raising=False)
    monkeypatch.setattr(watchdog, "token_configured", lambda: True)
    monkeypatch.setattr(watchdog, "check_handle", lambda *_a: pytest.fail("não devia verificar"))
    with SessionLocal() as db:
        assert watchdog.run_auto_check(db) == 0


def test_auto_check_only_stale_accounts(monkeypatch):
    with SessionLocal() as db:
        folder = Folder(owner_id=ensure_admin(db).id, name="Vigia", color="#FF2EC8")
        db.add(folder)
        db.flush()
        db.add_all([
            XAccount(folder_id=folder.id, username="velha", last_health_checked_at=now_utc() - timedelta(hours=2)),
            XAccount(folder_id=folder.id, username="nunca"),
            XAccount(folder_id=folder.id, username="fresca", last_health_checked_at=now_utc() - timedelta(minutes=5)),
            XAccount(folder_id=folder.id, username="arquivada", is_archived=True),
        ])
        db.commit()
    checked: list[str] = []
    monkeypatch.setenv("AUTO_CHECK_MINUTES", "60")
    monkeypatch.setattr(watchdog, "token_configured", lambda: True)
    monkeypatch.setattr(watchdog, "check_handle", lambda _db, _owner, handle, _folder: checked.append(handle))
    with SessionLocal() as db:
        watchdog.run_auto_check(db)
    assert "velha" in checked and "nunca" in checked
    assert "fresca" not in checked and "arquivada" not in checked
