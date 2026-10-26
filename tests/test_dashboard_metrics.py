"""O dashboard só fala de Views, Curtidas, Retweets, Replies e Seguidores."""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.models import Folder, TweetSample, XAccount
from app.services import ensure_owner, viral_message
from main import app

BR = ZoneInfo("America/Sao_Paulo")


def keys_of(value) -> set[str]:
    """Todas as chaves de dicts, em qualquer nível do JSON."""
    if isinstance(value, dict):
        return set(value) | set().union(*(keys_of(item) for item in value.values()))
    if isinstance(value, list):
        return set().union(*(keys_of(item) for item in value)) if value else set()
    return set()


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def account_id(client):
    """Conta com dois posts de hoje: às 9h com poucas views e muito like, às 20h com muitas views."""
    today = datetime.now(BR).replace(minute=0, second=0, microsecond=0)
    with SessionLocal() as db:
        folder = Folder(owner_id=ensure_owner(db).id, name="Métricas", color="#FF2EC8")
        db.add(folder)
        db.flush()
        account = XAccount(folder_id=folder.id, username="metricas")
        db.add(account)
        db.flush()
        for tweet_id, hour, views, likes in [("1", 9, 10, 500), ("2", 20, 9000, 1)]:
            posted = today.replace(hour=hour)
            db.add(TweetSample(
                account_id=account.id, tweet_id=tweet_id, posted_at=posted.astimezone(timezone.utc),
                timezone_used="America/Sao_Paulo", hour_local=hour, weekday=posted.weekday(),
                like_count=likes, retweet_count=2, reply_count=3, view_count=views, impression_count=views,
            ))
        db.commit()
        return account.id


def test_dashboard_has_no_engagement(client, account_id):
    body = client.get("/api/dashboard?period=today").json()
    assert not any("engag" in key for key in keys_of(body)), sorted(k for k in keys_of(body) if "engag" in k)
    card = next(item for item in body["accounts"] if item["id"] == account_id)
    assert (card["views"], card["likes"], card["retweets"], card["replies"]) == (9010, 501, 4, 6)
    assert set(body["kpis"]) >= {"views", "likes", "retweets", "replies", "followers"}


def test_series_and_by_account_carry_views(client, account_id):
    body = client.get("/api/dashboard?period=today").json()
    assert body["series"][-1]["views"] >= 9010
    row = next(item for item in body["by_account"] if item["username"] == "metricas")
    assert row["views"] == 9010


def test_suggestions_rank_by_views(client, account_id):
    folder_id = client.get("/api/dashboard?period=today").json()["accounts"]
    folder_id = next(item["folder_id"] for item in folder_id if item["id"] == account_id)
    body = client.get(f"/api/suggestions?period=total&folder_id={folder_id}").json()
    assert body["items"][0]["hour"] == 20
    assert body["items"][0]["avg_views"] == 9000
    assert not any("engag" in key for key in keys_of(body))
    assert "engajamento" not in body["note"].lower()


def test_viral_message_lists_likes_and_retweets():
    text = viral_message("Nome", "user", 500, "01/01/2026 10:00", 7, 3, "https://x.com/user/status/1")
    assert "Engajamento" not in text
    assert "Curtidas: 7" in text and "Retweets: 3" in text
