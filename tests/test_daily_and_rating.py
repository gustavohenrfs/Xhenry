"""Frases por conta sem limite prático, zeradas a cada dia (o que não foi postado), e a nota viral (régua de
milhões): real para o que já foi postado, estimativa pelo formato para o resto."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app import postar, radar_hot
from app.database import SessionLocal
from app.models import Folder, HotText, PostDraft, TweetSample, User, XAccount
from app.routers import auth
from main import app

NOW = datetime.now(timezone.utc)


@pytest.fixture(autouse=True)
def secret(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "segredo-de-teste")


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def owner() -> tuple[int, dict[str, str]]:
    with SessionLocal() as db:
        row = User(email=f"d{uuid4().hex[:8]}@x.com", name="D", is_approved=True)
        db.add(row)
        db.commit()
        return row.id, {"Authorization": f"Bearer {auth.issue_token(row.id, 0)}"}


def account(owner_id: int) -> int:
    with SessionLocal() as db:
        folder = Folder(owner_id=owner_id, name=f"P{uuid4().hex[:4]}")
        db.add(folder)
        db.flush()
        row = XAccount(folder_id=folder.id, username=f"d{uuid4().hex[:6]}")
        db.add(row)
        db.commit()
        return row.id


def test_rating_scale_goes_to_millions():
    rate = postar.viral_rating
    assert rate(None) is None
    assert rate(1_000_000)["tier"] == "viral" and rate(1_000_000)["score"] == 100
    assert rate(3_000_000)["score"] == 100 and rate(3_000_000)["label"] == "Viral (milhões)"
    assert rate(600_000)["tier"] == "quase" and rate(150_000)["tier"] == "forte"
    assert rate(20_000)["tier"] == "boa" and rate(900)["tier"] == "fraca"
    assert rate(10_000)["score"] < rate(100_000)["score"] < rate(1_000_000)["score"]


def test_many_texts_per_day_and_the_queue_resets(client):
    owner_id, h = owner()
    acc = account(owner_id)
    assert postar.MAX_QUEUE >= 200
    for n in range(30):
        assert client.post("/api/drafts", json={"account_id": acc, "text": f"frase do dia {n}"}, headers=h).status_code == 201
    with SessionLocal() as db:  # duas sobras de ontem: uma não postada (sai), uma postada (fica para o ranking)
        db.add(PostDraft(owner_id=owner_id, account_id=acc, text="sobra de ontem", created_at=NOW - timedelta(days=1, hours=2)))
        db.add(PostDraft(owner_id=owner_id, account_id=acc, text="postada ontem", created_at=NOW - timedelta(days=1, hours=2),
                         posted_at=NOW - timedelta(days=1)))
        db.commit()
    card = next(a for a in client.get("/api/drafts", headers=h).json()["accounts"] if a["id"] == acc)
    texts = [d["text"] for d in card["queue"]]
    assert len(texts) == 30 and "sobra de ontem" not in texts, "a fila zera na virada do dia"
    assert card["today_posted"] == 0
    with SessionLocal() as db:
        assert db.query(PostDraft).filter(PostDraft.text == "sobra de ontem").count() == 0
        assert db.query(PostDraft).filter(PostDraft.text == "postada ontem").count() == 1
    client.patch(f"/api/drafts/{card['queue'][0]['id']}", json={"posted": True}, headers=h)
    card = next(a for a in client.get("/api/drafts", headers=h).json()["accounts"] if a["id"] == acc)
    assert card["today_posted"] == 1


def test_results_carry_the_real_rating(client):
    owner_id, h = owner()
    acc = account(owner_id)
    text = "Peito ou bunda? Só pode escolher um 😏"
    with SessionLocal() as db:
        db.add(PostDraft(owner_id=owner_id, account_id=acc, text=text, posted_at=NOW - timedelta(hours=1)))
        db.add(TweetSample(account_id=acc, tweet_id=uuid4().hex[:12], text_preview=text, posted_at=NOW - timedelta(hours=1),
                           timezone_used="America/Sao_Paulo", hour_local=1, weekday=0, view_count=1_400_000, reply_count=900))
        db.commit()
    row = client.get("/api/drafts/results", headers=h).json()["phrases"][0]
    assert row["rating"]["tier"] == "viral" and row["rating"]["score"] == 100 and row["rating"]["real"] is True


def test_format_estimate_for_texts_not_posted_yet(client):
    owner_id, h = owner()
    acc = account(owner_id)
    with SessionLocal() as db:
        for n, views in enumerate((200_000, 400_000, 800_000, 300_000)):
            db.add(TweetSample(account_id=acc, tweet_id=uuid4().hex[:12], text_preview=f"Morena ou loira número {n}? Escolhe",
                               posted_at=NOW - timedelta(days=2), timezone_used="America/Sao_Paulo", hour_local=1, weekday=0,
                               view_count=views))
        db.commit()
        potential = postar.format_potential(db, owner_id)
    poll = potential["enquete"]
    assert poll["real"] is False and poll["n"] >= 4 and poll["tier"] in ("forte", "quase")
    client.post("/api/drafts", json={"account_id": acc, "text": "Peito ou bunda? Só pode escolher um"}, headers=h)
    board = client.get("/api/drafts", headers=h).json()
    assert board["potential"]["enquete"]["tier"] == poll["tier"]
    bank = client.get("/api/radar/hot/questions", headers=h).json()
    assert bank["potential"]["enquete"]["tier"] == poll["tier"], "as perguntas quentes mostram a estimativa"
