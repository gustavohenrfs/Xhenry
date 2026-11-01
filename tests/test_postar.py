"""Aba Postar (textos guardados por conta) e "Responder agora" das Métricas (posts do lote com comentário)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app import postar
from app.algo_metrics import reply_watch
from app.database import SessionLocal
from app.models import Folder, PostDraft, TweetSample, User, XAccount
from app.routers import auth
from main import app


@pytest.fixture(autouse=True)
def secret(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "segredo-de-teste")


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def user() -> tuple[int, dict[str, str]]:
    with SessionLocal() as db:
        row = User(email=f"p{uuid4().hex[:8]}@x.com", name="P", is_approved=True)
        db.add(row)
        db.commit()
        return row.id, {"Authorization": f"Bearer {auth.issue_token(row.id, 0)}"}


def account(owner_id: int, name: str | None = None, archived: bool = False) -> int:
    tag = uuid4().hex[:6]
    with SessionLocal() as db:
        folder = Folder(owner_id=owner_id, name=f"Pasta {tag}")
        db.add(folder)
        db.flush()
        row = XAccount(folder_id=folder.id, username=name or f"c{tag}", is_archived=archived)
        db.add(row)
        db.commit()
        return row.id


def post(account_id: int, minutes_ago: int, replies: int, text: str = "Quem tá vendo comigo?") -> None:
    with SessionLocal() as db:
        db.add(TweetSample(account_id=account_id, tweet_id=uuid4().hex[:12], text_preview=text,
                           posted_at=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
                           timezone_used="America/Sao_Paulo", hour_local=10, weekday=0, reply_count=replies,
                           view_count=50))
        db.commit()


# ---------- textos guardados ----------

def test_save_queue_post_and_undo(client):
    _, h = user()
    acc = account(_)
    first = client.post("/api/drafts", json={"account_id": acc, "text": "  Homens, sejam sinceros  "}, headers=h)
    assert first.status_code == 201 and first.json()["text"] == "Homens, sejam sinceros"
    client.post("/api/drafts", json={"account_id": acc, "text": "Bom dia pra quem?"}, headers=h)
    again = client.post("/api/drafts", json={"account_id": acc, "text": "homens, sejam sinceros"}, headers=h)
    assert again.status_code == 409, "o mesmo texto não entra duas vezes na fila da conta"
    assert client.post("/api/drafts", json={"account_id": acc, "text": "   "}, headers=h).status_code in (400, 422)

    card = next(a for a in client.get("/api/drafts", headers=h).json()["accounts"] if a["id"] == acc)
    assert [d["text"] for d in card["queue"]] == ["Homens, sejam sinceros", "Bom dia pra quem?"], "fila na ordem"
    assert card["posted"] is None

    draft_id = card["queue"][0]["id"]
    assert client.patch(f"/api/drafts/{draft_id}", json={"posted": True}, headers=h).status_code == 200
    board = client.get("/api/drafts", headers=h).json()
    card = next(a for a in board["accounts"] if a["id"] == acc)
    assert [d["text"] for d in card["queue"]] == ["Bom dia pra quem?"], "o postado sai da fila"
    assert card["posted"]["text"] == "Homens, sejam sinceros"
    assert board["batch"]["done"] == 1 and board["batch"]["total"] == 1

    client.patch(f"/api/drafts/{draft_id}", json={"posted": False}, headers=h)
    card = next(a for a in client.get("/api/drafts", headers=h).json()["accounts"] if a["id"] == acc)
    assert card["posted"] is None and len(card["queue"]) == 2, "desfazer devolve o texto para a fila"


def test_board_lists_every_active_account_and_warns_repeats(client):
    owner, h = user()
    a, b = account(owner), account(owner)
    archived = account(owner, archived=True)
    ids = [x["id"] for x in client.get("/api/drafts", headers=h).json()["accounts"]]
    assert a in ids and b in ids and archived not in ids, "contas sem texto aparecem; arquivadas não"
    client.post("/api/drafts", json={"account_id": a, "text": "Enquete: praia ou piscina?"}, headers=h)
    saved = client.post("/api/drafts", json={"account_id": b, "text": "Enquete: praia ou piscina?"}, headers=h)
    assert saved.status_code == 201 and len(saved.json()["elsewhere"]) == 1, "avisa que o texto já está em outra conta"


def test_drafts_are_private(client):
    a_id, a = user()
    _, b = user()
    acc = account(a_id)
    assert client.post("/api/drafts", json={"account_id": acc, "text": "oi"}, headers=b).status_code == 404
    draft = client.post("/api/drafts", json={"account_id": acc, "text": "oi"}, headers=a).json()
    assert client.patch(f"/api/drafts/{draft['id']}", json={"posted": True}, headers=b).status_code == 404
    assert client.delete(f"/api/drafts/{draft['id']}", headers=b).status_code == 404
    assert all(x["id"] != acc for x in client.get("/api/drafts", headers=b).json()["accounts"])
    assert client.delete(f"/api/drafts/{draft['id']}", headers=a).status_code == 204
    with SessionLocal() as db:
        assert db.get(PostDraft, draft["id"]) is None


def test_queue_limit(client):
    owner, h = user()
    acc = account(owner)
    for n in range(postar.MAX_QUEUE):
        assert client.post("/api/drafts", json={"account_id": acc, "text": f"texto {n}"}, headers=h).status_code == 201
    assert client.post("/api/drafts", json={"account_id": acc, "text": "um a mais"}, headers=h).status_code == 400


def test_old_batch_is_not_counted(client):
    owner, h = user()
    acc = account(owner)
    draft = client.post("/api/drafts", json={"account_id": acc, "text": "antigo"}, headers=h).json()
    with SessionLocal() as db:
        row = db.get(PostDraft, draft["id"])
        row.posted_at = datetime.now(timezone.utc) - postar.BATCH_WINDOW - timedelta(minutes=5)
        db.commit()
    board = client.get("/api/drafts", headers=h).json()
    card = next(a for a in board["accounts"] if a["id"] == acc)
    assert card["posted"] is None and board["batch"]["done"] == 0, "lote de horas atrás não conta"


# ---------- responder agora ----------

def test_reply_watch_lists_fresh_posts_with_comments(client):
    owner, h = user()
    hot = account(owner, name=f"q{uuid4().hex[:6]}")
    late = account(owner)
    quiet = account(owner)
    post(hot, minutes_ago=8, replies=3)
    post(hot, minutes_ago=2, replies=1)
    post(late, minutes_ago=40, replies=5)
    post(late, minutes_ago=200, replies=9)            # fora da janela
    post(quiet, minutes_ago=5, replies=0)             # sem comentário
    post(quiet, minutes_ago=5, replies=4, text="@fulano valeu")  # reply da própria conta
    other_id, _ = user()
    post(account(other_id), minutes_ago=3, replies=7)  # de outro dono

    data = client.get("/api/metrics/replies", headers=h).json()
    rows = [(r["account_id"], r["replies"]) for r in data["items"]]
    assert rows == [(hot, 3), (hot, 1), (late, 5)], "abertos primeiro, quem fecha antes no topo; depois os atrasados"
    first = data["items"][0]
    assert first["username"].startswith("q") and 21 <= first["left_min"] <= 22 and first["open"] is True
    assert data["items"][2]["open"] is False
    assert data["posts"] == 4 and data["with_replies"] == 3, "posts da janela, sem replies próprios"


def test_reply_watch_respects_folder(client):
    owner, _ = user()
    a, b = account(owner), account(owner)
    post(a, 5, 2)
    post(b, 5, 2)
    with SessionLocal() as db:
        folder_id = db.get(XAccount, a).folder_id
        data = reply_watch(db, owner, folder_id)
    assert [r["account_id"] for r in data["items"]] == [a]


def test_postar_markup():
    from app.config import ROOT

    html = (ROOT / "templates/index.html").read_text(encoding="utf-8")
    js = (ROOT / "static/js/app.js").read_text(encoding="utf-8")
    assert '<section id="pane-postar"' in html and html.count('data-tab="postar"') == 2, "barra lateral e Mais"
    assert 'id="pick"' in html and 'id="mx-reply"' in html
    actions = js.split("function ideaActions")[1].split("\n}\n")[0]
    assert "intent/post" not in actions and "openPick" in actions, "no Radar o Postar virou Guardar"
    assert "x.com" not in js.split("// ---------- Postar:")[1].split("// ---------- Métricas → Responder agora")[0], \
        "a aba Postar não abre o X (o dono usa multilogin)"
