"""Dashboard: períodos, filtro por pasta, série diária e soma dos KPIs."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.models import DailyMetric, Folder, HealthStatus, TweetSample, XAccount
from app.services import ensure_owner, period_window
from main import app

BR = ZoneInfo("America/Sao_Paulo")
DAY = date(2026, 10, 25)


@pytest.mark.parametrize(("period", "start", "end"), [
    ("today", DAY, DAY),
    ("hoje", DAY, DAY),
    ("yesterday", DAY - timedelta(days=1), DAY - timedelta(days=1)),
    ("7d", DAY - timedelta(days=6), DAY),
    ("30d", DAY - timedelta(days=29), DAY),
    ("3m", DAY - timedelta(days=89), DAY),
    ("1y", DAY - timedelta(days=364), DAY),
    (" TOTAL ", DAY - timedelta(days=3649), DAY),
])
def test_period_window(period, start, end):
    assert period_window(period, today=DAY) == (start, end)


def test_invalid_period():
    with pytest.raises(HTTPException) as exc:
        period_window("semana", today=DAY)
    assert exc.value.status_code == 400


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def folder(client):
    """Pasta com duas contas: uma saudável com 2 posts hoje, outra em search ban com 1 post ontem."""
    now = datetime.now(BR).replace(microsecond=0)
    yesterday = now - timedelta(days=1)
    with SessionLocal() as db:
        row = Folder(owner_id=ensure_owner(db).id, name=f"Dash {uuid4().hex[:8]}", color="#FF2EC8")
        db.add(row)
        db.flush()
        a = XAccount(folder_id=row.id, username="dash_a", last_health_status=HealthStatus.HEALTHY)
        b = XAccount(folder_id=row.id, username="dash_b", last_health_status=HealthStatus.SEARCH_BAN)
        db.add_all([a, b])
        db.flush()

        def post(account, tweet_id, when, views, likes):
            db.add(TweetSample(
                account_id=account.id, tweet_id=tweet_id, posted_at=when.astimezone(timezone.utc),
                timezone_used="America/Sao_Paulo", hour_local=when.hour, weekday=when.weekday(),
                like_count=likes, retweet_count=1, reply_count=2, view_count=views,
            ))

        post(a, "a1", now, 100, 10)
        post(a, "a2", now, 50, 5)
        post(b, "b1", yesterday, 1000, 20)
        db.add_all([
            DailyMetric(account_id=a.id, metric_date=yesterday.date(), followers_count=90),
            DailyMetric(account_id=a.id, metric_date=now.date(), followers_count=100),
            DailyMetric(account_id=b.id, metric_date=now.date(), followers_count=40),
        ])
        db.commit()
        return {"id": row.id, "a": a.id, "b": b.id}


def get(client, period, folder_id):
    resp = client.get(f"/api/dashboard?period={period}&folder_id={folder_id}")
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_kpis_sum_accounts_in_folder(client, folder):
    body = get(client, "7d", folder["id"])
    kpis = body["kpis"]
    assert (kpis["accounts"], kpis["healthy"], kpis["search_ban"]) == (2, 1, 1)
    assert (kpis["views"], kpis["likes"], kpis["retweets"], kpis["replies"]) == (1150, 35, 3, 6)
    assert kpis["followers"] == 140, "seguidores vêm do daily_metrics mais recente de cada conta"


def test_today_only_counts_todays_posts(client, folder):
    kpis = get(client, "today", folder["id"])["kpis"]
    assert kpis["views"] == 150 and kpis["likes"] == 15


def test_series_has_one_bucket_per_day(client, folder):
    series = get(client, "7d", folder["id"])["series"]
    assert len(series) == 7
    assert [row["date"] for row in series] == sorted(row["date"] for row in series)
    assert series[-1]["views"] == 150 and series[-1]["posts"] == 2
    assert series[-2]["views"] == 1000 and series[-2]["posts"] == 1


def test_folder_card_counts_statuses(client, folder):
    card = next(f for f in get(client, "7d", folder["id"])["folders"] if f["id"] == folder["id"])
    assert (card["accounts"], card["healthy"], card["search_ban"], card["ghost_ban"]) == (2, 1, 1, 0)


def test_by_account_ranks_views(client, folder):
    rows = {row["username"]: row["views"] for row in get(client, "7d", folder["id"])["by_account"]}
    assert rows == {"dash_a": 150, "dash_b": 1000}


def test_unknown_folder_is_404(client):
    assert client.get("/api/dashboard?period=today&folder_id=999999").status_code == 404


def test_invalid_period_route_is_400(client):
    assert client.get("/api/dashboard?period=semana").status_code == 400


def test_debug_route_is_gone(client):
    assert client.get("/debug-metrics").status_code == 404
