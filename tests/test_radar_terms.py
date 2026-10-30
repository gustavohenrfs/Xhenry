"""Radar de trend, Fase 3: Meus termos (alerta) e posts que estão engajando (Apify do usuário, falso aqui)."""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app import radar
from app.database import SessionLocal
from app.models import AlertSent, RadarItem, RadarPostCache, RadarSnapshot, RadarTerm, User
from app.routers import auth
from app.services import NOTIFY_TYPES, user_settings
from main import app
from test_radar import fake_fetch


@pytest.fixture(autouse=True)
def secret(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "segredo-de-teste")


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def clean(client):
    with SessionLocal() as db:
        for model in (RadarItem, RadarSnapshot, RadarTerm, RadarPostCache):
            db.query(model).delete()
        db.query(AlertSent).filter(AlertSent.kind == "radar").delete()
        db.commit()


def user() -> tuple[int, dict[str, str]]:
    with SessionLocal() as db:
        row = User(email=f"r{uuid4().hex[:8]}@x.com", name="R", is_approved=True)
        db.add(row)
        db.commit()
        return row.id, {"Authorization": f"Bearer {auth.issue_token(row.id, 0)}"}


def test_terms_crud_and_isolation(client):
    a_id, a = user()
    _, b = user()
    created = client.post("/api/radar/terms", json={"term": "  Flamengo "}, headers=a)
    assert created.status_code == 201 and created.json()["term"] == "Flamengo"
    assert client.post("/api/radar/terms", json={"term": "#flamengo"}, headers=a).status_code == 409, "mesmo termo"
    assert client.post("/api/radar/terms", json={"term": "#"}, headers=a).status_code == 400, "sem letra"
    assert [t["term"] for t in client.get("/api/radar/terms", headers=a).json()] == ["Flamengo"]
    assert client.get("/api/radar/terms", headers=b).json() == []
    term_id = created.json()["id"]
    assert client.delete(f"/api/radar/terms/{term_id}", headers=b).status_code == 404, "termo de outro"
    assert client.delete(f"/api/radar/terms/{term_id}", headers=a).status_code == 204
    for n in range(radar.MAX_TERMS):
        client.post("/api/radar/terms", json={"term": f"termo{n}"}, headers=a)
    assert client.post("/api/radar/terms", json={"term": "mais um"}, headers=a).status_code == 400, "limite"


def test_board_marks_my_terms_in_trend(client):
    _, a = user()
    client.post("/api/radar/terms", json={"term": "Flamengo"}, headers=a)
    client.post("/api/radar/terms", json={"term": "Corinthians"}, headers=a)
    with SessionLocal() as db:
        radar.refresh(db, fetch=fake_fetch)
    board = client.get("/api/radar", headers=a).json()
    mine = {t["term"]: t["hot"] for t in board["my_terms"]}
    assert mine == {"Flamengo": True, "Corinthians": False}


def test_term_in_trend_alerts_once_per_window(client):
    a_id, a = user()
    b_id, b = user()
    client.post("/api/radar/terms", json={"term": "Lula"}, headers=a)
    client.post("/api/radar/terms", json={"term": "anitta"}, headers=b)
    client.post("/api/radar/terms", json={"term": "palmeiras"}, headers=b)
    with SessionLocal() as db:
        radar.refresh(db, fetch=fake_fetch)
        radar.refresh(db, fetch=fake_fetch)  # mesma janela de 6 h: não repete
        rows = db.query(AlertSent).filter(AlertSent.kind == "radar").order_by(AlertSent.owner_id).all()
    assert [(r.owner_id, r.dedupe_key.split(":")[2]) for r in rows] == [(a_id, "lula"), (b_id, "anitta")]
    lula = rows[0].detail
    assert lula.startswith("📡 Lula em alta") and "X Brasil #" in lula and "https://x.com/search?q=Lula" in lula


def test_radar_is_a_notification_type(client):
    assert ("radar", "Radar de trend (meus termos)", "📡", False, "Radar") in NOTIFY_TYPES
    _, a = user()
    assert client.get("/api/alerts?group=radar", headers=a).status_code == 200


def test_engaging_posts_need_a_token(client):
    _, a = user()
    resp = client.post("/api/radar/posts", json={"term": "Flamengo"}, headers=a)
    assert resp.status_code == 400 and "Apify" in resp.json()["detail"]


def test_engaging_posts_sorted_and_cached(client, monkeypatch):
    a_id, a = user()
    _, b = user()
    with SessionLocal() as db:
        user_settings(db, a_id).apify_token = "tok-a"
        db.commit()
    calls = []

    def search(token, term):
        calls.append((token, term))
        return [
            {"id": "1", "text": "gol do flamengo", "viewCount": 900, "likeCount": 5, "url": "https://x.com/a/status/1",
             "createdAt": "Fri Oct 30 18:00:00 +0000 2026", "author": {"userName": "fulano", "name": "Fulano"}},
            {"id": "2", "text": "RT @x: repost", "viewCount": 99999, "author": {"userName": "outro"}},
            {"id": "3", "text": "que jogo", "viewCount": 5000, "likeCount": 50, "author": {"userName": "beltrano"}},
        ]

    monkeypatch.setattr(radar, "apify_search", search)
    first = client.post("/api/radar/posts", json={"term": "Flamengo"}, headers=a).json()
    assert [p["id"] for p in first["posts"]] == ["3", "1"], "mais views primeiro, sem repost"
    assert first["posts"][0]["url"] == "https://x.com/beltrano/status/3" and first["cached"] is False
    again = client.post("/api/radar/posts", json={"term": "#flamengo"}, headers=b).json()
    assert again["cached"] is True and calls == [("tok-a", "Flamengo")], "30 min de cache, sem gastar de novo"


def test_engaging_posts_apify_error_is_readable(client, monkeypatch):
    a_id, a = user()
    with SessionLocal() as db:
        user_settings(db, a_id).apify_token = "tok-a"
        db.commit()

    def refuse(token, term):
        raise radar.RadarError("a conta do Apify está sem crédito (402).")

    monkeypatch.setattr(radar, "apify_search", refuse)
    resp = client.post("/api/radar/posts", json={"term": "Palmeiras"}, headers=a)
    assert resp.status_code == 502 and "402" in resp.json()["detail"]
