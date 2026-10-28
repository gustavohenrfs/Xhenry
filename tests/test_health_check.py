"""Health check: como as respostas da API do X viram status, e o que fica gravado."""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from conftest import admin_row
from app import services
from app.database import SessionLocal
from app.models import DailyMetric, Folder, HealthCheck, HealthStatus, SyncKind, SyncRun, XAccount
from app.routers.auth import current_user
from app.services import derive_status, ensure_admin
from main import app

PROFILE = {"data": {"id": "42", "name": "Fulano", "username": "saude", "protected": False,
                    "public_metrics": {"followers_count": 321, "following_count": 10, "tweet_count": 99}}}
TWEETS = {"data": [{"id": "1", "author_id": "42", "created_at": "2026-10-25T12:00:00Z"}]}


def status_of(**overrides) -> HealthStatus:
    args = dict(
        had_known_user_id=False, profile_status=200, profile_body=PROFILE, protected=False,
        timeline_status=200, timeline_ids=["1"], newest_tweet=None, search_status=200, search_ids={"1"},
    )
    args.update(overrides)
    return derive_status(**args)[0]


@pytest.mark.parametrize(("overrides", "expected"), [
    ({}, HealthStatus.HEALTHY),
    ({"timeline_ids": [], "search_ids": set()}, HealthStatus.HEALTHY),
    ({"profile_status": 401}, HealthStatus.API_ERROR),
    ({"profile_status": 429}, HealthStatus.RATE_LIMITED),
    ({"search_status": 429}, HealthStatus.RATE_LIMITED),
    ({"profile_status": 0, "profile_body": {"detail": "rede"}}, HealthStatus.API_ERROR),
    ({"profile_status": 404, "profile_body": {}}, HealthStatus.NOT_FOUND),
    ({"profile_status": 404, "profile_body": {}, "had_known_user_id": True}, HealthStatus.GHOST_BAN),
    ({"profile_status": 200, "profile_body": {"errors": [{"detail": "User has been suspended"}]}}, HealthStatus.SUSPENDED),
    ({"protected": True}, HealthStatus.PROTECTED),
    ({"timeline_status": 403}, HealthStatus.SEARCH_BAN),
    ({"search_status": 404}, HealthStatus.SEARCH_BAN),
    ({"timeline_status": 500}, HealthStatus.API_ERROR),
])
def test_derive_status(overrides, expected):
    assert status_of(**overrides) == expected


class FakeX:
    """Responde /users/by, /users/{id}/tweets e /tweets/search/recent conforme o cenário."""

    def __init__(self, profile=(200, PROFILE), timeline=(200, TWEETS), search=(200, TWEETS)):
        self.routes = {"profile": profile, "timeline": timeline, "search": search}
        self.calls: list[str] = []

    def __call__(self, path, _params, token=""):
        self.calls.append(path)
        if path.startswith("/users/by/username/"):
            return self.routes["profile"]
        if path.endswith("/tweets"):
            return self.routes["timeline"]
        return self.routes["search"]


@pytest.fixture
def x_api(monkeypatch):
    def install(**routes) -> FakeX:
        fake = FakeX(**routes)
        monkeypatch.setattr(services, "x_get", fake)
        return fake
    return install


@pytest.fixture
def telegram(monkeypatch):
    sent: list[str] = []
    monkeypatch.setattr(services, "telegram_creds", lambda _db: ("bot", "chat"))
    monkeypatch.setattr(services, "send_telegram", lambda _t, _c, text: sent.append(text))
    return sent


@pytest.fixture
def account_id():
    with TestClient(app):
        pass
    with SessionLocal() as db:
        folder = Folder(owner_id=ensure_admin(db).id, name=f"Saúde {uuid4().hex[:8]}", color="#FF2EC8")
        db.add(folder)
        db.flush()
        account = XAccount(folder_id=folder.id, username="saude")
        db.add(account)
        db.commit()
        yield account.id
        db.delete(db.get(Folder, folder.id))
        db.commit()


def check(account_id: int) -> dict:
    with SessionLocal() as db:
        folder_id = db.get(XAccount, account_id).folder_id
        return services.check_handle(db, admin_row(), "saude", folder_id)


def test_healthy_check_persists_everything(x_api, telegram, account_id):
    fake = x_api()
    result = check(account_id)
    assert result["status"] == "healthy" and result["persisted"] is True
    assert len(fake.calls) == 3
    with SessionLocal() as db:
        account = db.get(XAccount, account_id)
        assert account.last_health_status == HealthStatus.HEALTHY
        assert account.x_user_id == "42" and account.display_name == "Fulano"
        run = db.query(HealthCheck).filter_by(account_id=account_id).one()
        assert (run.profile_http_status, run.timeline_tweet_count, run.matched_recent_in_search) == (200, 1, 1)
        assert db.query(DailyMetric).filter_by(account_id=account_id).one().followers_count == 321
        assert db.query(SyncRun).filter_by(account_id=account_id, kind=SyncKind.HEALTH).count() == 1
    assert telegram == []


def test_protected_account_skips_tweet_calls(x_api, telegram, account_id):
    fake = x_api(profile=(200, {"data": {**PROFILE["data"], "protected": True}}))
    assert check(account_id)["status"] == "protected"
    assert len(fake.calls) == 1


def test_ban_alert_only_on_transition(x_api, telegram, account_id):
    x_api(search=(403, {"detail": "Forbidden"}))
    assert check(account_id)["status"] == "search_ban"
    assert len(telegram) == 1 and "Search Ban" in telegram[0] and "@saude" in telegram[0]
    check(account_id)
    assert len(telegram) == 1, "mesmo status de novo não repete o alerta"


def test_ghost_ban_after_known_id(x_api, telegram, account_id):
    x_api()
    check(account_id)
    x_api(profile=(404, {"errors": [{"detail": "Could not find user"}]}))
    assert check(account_id)["status"] == "ghost_ban"
    assert len(telegram) == 1 and "Ghost Ban" in telegram[0]


def test_rejected_token_is_502(x_api, account_id):
    x_api(profile=(401, {"detail": "Unauthorized"}))
    with pytest.raises(HTTPException) as exc:
        check(account_id)
    assert exc.value.status_code == 502


def test_missing_token_is_503(monkeypatch):
    monkeypatch.delenv("X_BEARER_TOKEN", raising=False)
    app.dependency_overrides[current_user] = admin_row
    try:
        with TestClient(app) as client:
            resp = client.get("/api/check/qualquer")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 503
