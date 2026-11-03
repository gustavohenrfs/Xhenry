"""Aba Métricas: sinais do algoritmo (salvamentos, replies, citações, pontos) por período, conta e post."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from app import algo_metrics
from app.database import SessionLocal
from app.models import DailyMetric, Folder, HealthStatus, TweetSample, User, XAccount
from app.routers import auth
from main import app

BR = ZoneInfo("America/Sao_Paulo")


@pytest.fixture(autouse=True)
def secret(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "segredo-de-teste")


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def owner() -> tuple[int, dict[str, str]]:
    with SessionLocal() as db:
        row = User(email=f"m{uuid4().hex[:8]}@x.com", name="M", is_approved=True)
        db.add(row)
        db.commit()
        return row.id, {"Authorization": f"Bearer {auth.issue_token(row.id, 0)}"}


def seed(owner_id: int) -> dict[str, int]:
    """Duas contas: "forte" (muito salvamento e conversa) e "fraca" (só curtida); posts hoje, há 3 dias e
    há 10 dias (período anterior de 7d), mais uma reply que a própria conta mandou."""
    today = datetime.now(BR).replace(hour=12, minute=0, second=0, microsecond=0)
    with SessionLocal() as db:
        folder = Folder(owner_id=owner_id, name="Teste", color="#FF2EC8")
        db.add(folder)
        db.flush()
        strong = XAccount(folder_id=folder.id, username=f"forte{uuid4().hex[:4]}", last_health_status=HealthStatus.HEALTHY)
        weak = XAccount(folder_id=folder.id, username=f"fraca{uuid4().hex[:4]}", last_health_status=HealthStatus.GHOST_BAN)
        db.add_all([strong, weak])
        db.flush()
        posts = [
            (strong, "s1", 0, "Peito ou bunda? Só pode escolher um", 10_000, 200, 10, 300, 20, 150),
            (strong, "s2", 3, "Vocês homens gozam rápido mesmo?", 5_000, 100, 5, 100, 5, 30),
            (strong, "s3", 0, "@fulano verdade rs", 50, 1, 0, 0, 0, 0),  # reply que a conta mandou
            (strong, "s0", 10, "post do período anterior", 1_000, 10, 1, 5, 0, 2),
            (weak, "w1", 1, "bom dia", 2_000, 400, 2, 1, 0, 0),
        ]
        for account, tid, ago, text, views, likes, rts, replies, quotes, bookmarks in posts:
            posted = today - timedelta(days=ago)
            db.add(TweetSample(
                account_id=account.id, tweet_id=tid, posted_at=posted.astimezone(timezone.utc), timezone_used="America/Sao_Paulo",
                hour_local=posted.hour, weekday=posted.weekday(), text_preview=text, view_count=views,
                like_count=likes, retweet_count=rts, reply_count=replies, quote_count=quotes, bookmark_count=bookmarks,
            ))
        db.add(DailyMetric(account_id=strong.id, metric_date=today.date(), followers_count=4_500))
        db.add(DailyMetric(account_id=strong.id, metric_date=today.date() - timedelta(days=20), followers_count=4_000))
        db.commit()
        return {"strong": strong.id, "weak": weak.id, "folder": folder.id}


def test_metrics_need_login(client):
    assert client.get("/api/metrics?period=7d").status_code == 401


def test_signals_points_and_comparison(client):
    owner_id, h = owner()
    ids = seed(owner_id)
    body = client.get("/api/metrics", params={"period": "7d"}, headers=h).json()
    t = body["totals"]
    assert t["posts"] == 3 and t["own_replies"] == 1, "a reply que a conta mandou não conta como post"
    assert (t["bookmarks"], t["replies"], t["quotes"]) == (180, 401, 25)
    assert t["bookmarks_per_post"] == 60.0 and t["replies_per_post"] == round(401 / 3, 2)
    points = 700 * 0.5 + 17 * 1 + 25 * 5 + 401 * 5  # pesos de 2026: salvar não soma
    assert t["points"] == round(points, 1) and t["points_per_k"] == round(points * 1000 / 17_000, 2)
    assert body["previous"]["posts"] == 1 and body["previous"]["bookmarks"] == 2, "período anterior do mesmo tamanho"
    assert body["available"] == {"dwell": False, "own_replies": True}, "tempo de leitura não existe para quem é de fora"
    mix = {m["key"]: m["points"] for m in body["mix"]}
    assert mix["replies"] == round(401 * 5, 1) and mix["quotes"] == 125 and "bookmarks" not in mix
    assert len(body["series"]) >= 14 and sum(d["bookmarks"] for d in body["series"]) == 180 + 2
    assert sum(slot["posts"] for slot in body["hours"]) == 3 and body["hours"][12]["posts"] == 3


def test_accounts_ranked_with_phase_and_status(client):
    owner_id, h = owner()
    ids = seed(owner_id)
    rows = client.get("/api/metrics", params={"period": "7d"}, headers=h).json()["accounts"]
    assert [r["id"] for r in rows] == [ids["strong"], ids["weak"]], "mais pontos primeiro"
    strong, weak = rows
    assert strong["phase"]["label"] == "1 mil a 10 mil" and strong["phase"]["kpi"] == "replies"
    assert weak["phase"] is None and weak["status"] == "ghost_ban" and weak["status_label"]
    assert strong["bookmarks_per_post"] == 90.0 and weak["bookmarks_per_post"] == 0.0
    assert len(strong["spark"]) == 14 and strong["spark"][-1] > 0
    assert strong["points_per_k_prev"] is not None


def test_top_posts_and_isolation(client):
    owner_id, h = owner()
    seed(owner_id)
    top = client.get("/api/metrics", params={"period": "7d"}, headers=h).json()["top"]
    assert top["bookmarks"][0]["text"].startswith("Peito ou bunda") and top["bookmarks"][0]["url"].startswith("https://x.com/")
    assert all(not p["text"].startswith("@") for p in top["replies"])
    _, other = owner()
    empty = client.get("/api/metrics", params={"period": "7d"}, headers=other).json()
    assert empty["accounts"] == [] and empty["totals"]["posts"] == 0 and empty["totals"]["bookmarks_per_post"] is None
    assert client.get("/api/metrics", params={"period": "7d", "folder_id": 999_999}, headers=other).status_code == 404


def test_phase_limits():
    assert algo_metrics.phase_of(None) is None
    assert algo_metrics.phase_of(999)["kpi"] == "replies" and algo_metrics.phase_of(25_000)["kpi"] == "quotes"
    assert algo_metrics.phase_of(2_000_000)["kpi"] == "points"


def test_followers_growth_vanity_and_pace(client):
    owner_id, h = owner()
    ids = seed(owner_id)
    body = client.get("/api/metrics", params={"period": "7d"}, headers=h).json()
    f = body["followers"]
    assert f["gained"] == 500, "última leitura do período menos a última antes dele"
    assert f["series"][-1]["total"] == 4_500 and f["series"][0]["total"] == 4_000, "dia sem leitura repete a última"
    strong = next(r for r in body["accounts"] if r["id"] == ids["strong"])
    assert strong["followers_gained"] == 500
    vanity = {v["key"]: v for v in body["vanity"]}
    assert set(vanity) == {"likes", "silent", "followers", "retweets"}
    assert vanity["silent"]["value"] == 0 and vanity["followers"]["value"] == 4_500 and "+500" in vanity["followers"]["fact"]
    assert "%" in vanity["likes"]["fact"] and "reposts puros" in vanity["retweets"]["fact"]
    assert body["totals"]["posts_per_day"] == round(3 / 7 / 2, 2) and body["previous"]["posts_per_day"] == round(1 / 7 / 2, 2)
