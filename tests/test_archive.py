"""Arquivar esconde a conta do painel, mas guarda o histórico."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from conftest import admin_row
from app.database import SessionLocal
from app.models import DailyMetric, Folder, TweetSample, XAccount
from app.routers.auth import current_user
from app.services import accounts_for, ensure_admin
from main import app

BR = ZoneInfo("America/Sao_Paulo")


@pytest.fixture(scope="module")
def client():
    app.dependency_overrides[current_user] = admin_row
    try:
        with TestClient(app) as c:
            yield c
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def account(client):
    """Conta numa pasta própria, com um post de hoje e dois dias de daily_metrics."""
    now = datetime.now(BR)
    with SessionLocal() as db:
        folder = Folder(owner_id=ensure_admin(db).id, name=f"Arquivo {now.timestamp()}", color="#FF2EC8")
        db.add(folder)
        db.flush()
        row = XAccount(folder_id=folder.id, username="arquivar", display_name="<b>Arquivo</b>")
        db.add(row)
        db.flush()
        db.add(TweetSample(
            account_id=row.id, tweet_id="900", posted_at=now.astimezone(timezone.utc), timezone_used="America/Sao_Paulo",
            hour_local=now.hour, weekday=now.weekday(), like_count=7, retweet_count=2, reply_count=1, view_count=300,
        ))
        db.add_all([
            DailyMetric(account_id=row.id, metric_date=now.date() - timedelta(days=1), views=120, followers_count=50),
            DailyMetric(account_id=row.id, metric_date=now.date(), views=300, followers_count=55),
        ])
        db.commit()
        return {"id": row.id, "folder_id": folder.id}


def archive(client, account_id: int, flag: bool = True):
    return client.patch(f"/api/accounts/{account_id}", json={"is_archived": flag})


def dashboard_ids(client, folder_id: int | None = None) -> set[int]:
    query = f"&folder_id={folder_id}" if folder_id else ""
    return {item["id"] for item in client.get(f"/api/dashboard?period=today{query}").json()["accounts"]}


def test_archive_hides_from_dashboard_and_folders(client, account):
    assert account["id"] in dashboard_ids(client)
    resp = archive(client, account["id"])
    assert resp.status_code == 200 and resp.json()["is_archived"] is True

    assert account["id"] not in dashboard_ids(client)
    body = client.get(f"/api/dashboard?period=today&folder_id={account['folder_id']}").json()
    assert body["kpis"]["accounts"] == 0
    assert body["kpis"]["views"] == 0
    assert body["suggestions"]["items"] == []
    folder = next(f for f in body["folders"] if f["id"] == account["folder_id"])
    assert folder["accounts"] == 0
    listed = next(f for f in client.get("/api/folders").json() if f["id"] == account["folder_id"])
    assert listed["accounts"] == 0


def test_archive_keeps_history(client, account):
    archive(client, account["id"])
    with SessionLocal() as db:
        assert db.query(TweetSample).filter_by(account_id=account["id"]).count() == 1
        assert db.query(DailyMetric).filter_by(account_id=account["id"]).count() == 2
        assert db.get(XAccount, account["id"]).archived_at is not None


def test_archived_list_shows_history(client, account):
    archive(client, account["id"])
    items = client.get("/api/accounts/archived").json()
    item = next(i for i in items if i["id"] == account["id"])
    assert (item["views"], item["likes"], item["retweets"], item["replies"]) == (300, 7, 2, 1)
    assert item["followers"] == 55
    assert item["archived_at"] and item["first_post_at"] and item["last_post_at"]
    assert [day["views"] for day in item["history"]] == [120, 300]
    assert [day["followers"] for day in item["history"]] == [50, 55]


def test_unarchive_brings_it_back(client, account):
    archive(client, account["id"])
    resp = archive(client, account["id"], False)
    assert resp.json()["is_archived"] is False
    assert account["id"] in dashboard_ids(client)
    assert all(i["id"] != account["id"] for i in client.get("/api/accounts/archived").json())
    with SessionLocal() as db:
        assert db.get(XAccount, account["id"]).archived_at is None


def test_readding_archived_handle_explains(client, account):
    archive(client, account["id"])
    resp = client.post("/api/accounts", json={"folder_id": account["folder_id"], "username": "arquivar"})
    assert resp.status_code == 409
    assert "arquivada" in resp.json()["detail"]


def test_check_skips_archived(client, account):
    archive(client, account["id"])
    with SessionLocal() as db:
        assert all(a.id != account["id"] for a in accounts_for(db, ensure_admin(db).id, "arquivar", None))
