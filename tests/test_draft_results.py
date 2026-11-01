"""Aba Postar → como os textos se saíram: cada "Postei" ligado ao post real que o robô leu, frase a frase, e as
campeãs alimentando o "Com base nas contas"."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app import postar, radar_hot, radar_ideas
from app.database import SessionLocal
from app.models import Folder, PostDraft, TweetSample, User, XAccount
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
        row = User(email=f"r{uuid4().hex[:8]}@x.com", name="R", is_approved=True)
        db.add(row)
        db.commit()
        return row.id, {"Authorization": f"Bearer {auth.issue_token(row.id, 0)}"}


def account(owner_id: int, folder_id: int | None = None) -> tuple[int, int]:
    with SessionLocal() as db:
        if folder_id is None:
            folder = Folder(owner_id=owner_id, name=f"P{uuid4().hex[:4]}")
            db.add(folder)
            db.flush()
            folder_id = folder.id
        row = XAccount(folder_id=folder_id, username=f"r{uuid4().hex[:6]}")
        db.add(row)
        db.commit()
        return row.id, folder_id


def posted(owner_id: int, account_id: int, text: str, minutes_ago: int = 60) -> None:
    with SessionLocal() as db:
        db.add(PostDraft(owner_id=owner_id, account_id=account_id, text=text, posted_at=NOW - timedelta(minutes=minutes_ago)))
        db.commit()


def tweet(account_id: int, text: str, minutes_ago: int, views: int, replies: int = 0) -> None:
    with SessionLocal() as db:
        db.add(TweetSample(account_id=account_id, tweet_id=uuid4().hex[:12], text_preview=text,
                           posted_at=NOW - timedelta(minutes=minutes_ago), timezone_used="America/Sao_Paulo",
                           hour_local=12, weekday=0, view_count=views, reply_count=replies, like_count=views // 100))
        db.commit()


def test_posted_texts_find_their_real_post_and_add_up_by_phrase(client):
    owner_id, h = owner()
    a, folder = account(owner_id)
    b, _ = account(owner_id, folder)
    c, _ = account(owner_id)
    phrase = "Peito ou bunda? Só pode escolher um 😏"
    posted(owner_id, a, phrase, 60)
    posted(owner_id, b, phrase, 50)
    posted(owner_id, c, "Duvido um homem aguentar 10 minutos comigo", 40)
    posted(owner_id, c, "Texto que ainda não foi lido pelo robô", 5)
    tweet(a, "peito ou bunda? so pode escolher um", 70, views=30_000, replies=120)   # sem emoji/acento: o mesmo
    tweet(b, "Peito ou bunda? Só pode escolher um 😏", 52, views=10_000, replies=40)
    tweet(c, "Duvido um homem aguentar 10 minutos comigo 🔥", 45, views=2_000, replies=5)
    tweet(c, "Peito ou bunda? Só pode escolher um 😏", 2000, views=99_999)            # fora da janela: não conta
    tweet(c, "Bom dia amores", 41, views=50_000)                                       # outro texto: não conta

    data = client.get("/api/drafts/results", headers=h).json()
    rows = {r["text"]: r for r in data["phrases"]}
    poll = rows[phrase]
    assert poll["posts"] == 2 and poll["matched"] == 2 and poll["avg_views"] == 20_000 and poll["avg_replies"] == 80
    assert poll["best"]["views"] == 30_000 and poll["best"]["url"].startswith("https://x.com/")
    assert rows["Duvido um homem aguentar 10 minutos comigo"]["avg_views"] == 2_000
    assert data["pending"] == 1, "o que o robô ainda não leu fica esperando"
    assert data["phrases"][0]["text"] == phrase and poll["champion"] is True
    assert rows["Duvido um homem aguentar 10 minutos comigo"]["champion"] is False

    only = client.get("/api/drafts/results", params={"folder_id": folder}, headers=h).json()
    assert [r["text"] for r in only["phrases"]] == [phrase], "filtro de pasta"
    _, other = owner()
    assert client.get("/api/drafts/results", headers=other).json()["phrases"] == [], "cada um vê só os seus"


def test_champions_feed_the_accounts_switch(client):
    owner_id, h = owner()
    a, _ = account(owner_id)
    b, _ = account(owner_id)
    champ = "Vocês preferem morena ou loira? Sinceramente"
    posted(owner_id, a, champ, 60)
    tweet(a, champ, 61, views=900, replies=30)
    for n in range(6):  # as views normais das contas são baixas: 900 é campeã aqui
        tweet(b, f"post comum número {n} sem nada", 3000 + n, views=200)
    with SessionLocal() as db:
        items = radar_hot.proven(db, owner_id)
    lote = [i for i in items if i["source"] == "lote"]
    assert [i["text"] for i in lote] == [champ]
    bank = client.get("/api/radar/hot/questions", params={"accounts": 1}, headers=h).json()
    assert any(i.get("source") == "lote" and i["text"] == champ for i in bank["items"]), "entra igual, para repostar"
    assert bank["accounts_info"]["lote"] == 1
    with SessionLocal() as db:
        refs = radar_hot.proven_refs(db, owner_id, 10)
    assert any("seu lote" in r for r in refs)


def test_results_route_needs_login(client):
    assert client.get("/api/drafts/results").status_code == 401


def test_results_card_markup():
    from app.config import ROOT

    html = (ROOT / "templates/index.html").read_text(encoding="utf-8")
    js = (ROOT / "static/js/app.js").read_text(encoding="utf-8")
    assert 'id="pz-results"' in html and 'data-pz-sort="replies"' in html
    assert 'if (tab === "postar") { loadDrafts(); loadDraftResults(); }' in js
    assert "loadDraftResults();" in js.split("function setFolder(id)")[1].split("\n}\n")[0], "o filtro de pasta vale aqui também"
    assert "Deu certo no seu lote" in js
