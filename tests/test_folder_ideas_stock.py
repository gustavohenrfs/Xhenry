"""Filtro de pasta em todo o painel, ideias do Verificar post (grátis e Grok), estoque de 1000+ frases e o
Grok que não pode morrer em 60 s."""

from __future__ import annotations

from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from app import radar_hot, radar_ideas
from app.config import ROOT
from main import app
from test_radar_hot import ITEMS, user
from app.database import SessionLocal


@pytest.fixture(autouse=True)
def secret(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "segredo-de-teste")


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


JS = (ROOT / "static/js/app.js").read_text(encoding="utf-8")
HTML = (ROOT / "templates/index.html").read_text(encoding="utf-8")


def test_folder_picker_filters_every_tab():
    assert 'id="folder-pick"' in HTML, "seletor de pasta no topo"
    assert 'localStorage.setItem("folder_id"' in JS, "a pasta escolhida fica lembrada"
    picker = JS.split("function setFolder")[1].split("\n}\n")[0]
    for loader in ("load(", "loadMetrics(", "paintDraftGrid(", "paintRede("):
        assert loader in picker, loader
    assert "state.folderId" in JS.split("function paintDraftGrid")[1].split("\n}\n")[0], "Postar respeita a pasta"


def test_stock_has_more_than_a_thousand_ready_lines():
    made = radar_ideas.compose("quente", seed=5)
    assert len(made) >= 1000 and len({radar_ideas.src.norm(x) for x in made}) == len(made)
    assert all(not radar_hot.unsafe(x) and not radar_hot.has_cta(x) for x in made)
    bank = radar_ideas.question_bank("quente", mix=5)
    assert len(bank["items"]) >= 1100
    assert JS.count("HOT_PAGE") >= 2, "a lista grande é mostrada aos poucos"


def test_grok_waits_long_enough_and_says_when_it_timed_out(monkeypatch):
    seen = {}

    class Slow:
        def __init__(self, timeout=None, **_):
            seen["timeout"] = timeout

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def post(self, *a, **k):
            raise httpx.ReadTimeout("lento")

    monkeypatch.setattr(radar_ideas.httpx, "Client", Slow)
    with pytest.raises(ValueError) as error:
        radar_ideas.ask_grok_lines("k", "oi")
    assert seen["timeout"] >= 150, "10 textos de um modelo que pensa passam de 60 s"
    assert "demorou" in str(error.value)


def test_accounts_switch_reports_what_came_in(client):
    _, h = user()
    off = client.get("/api/radar/hot/questions", headers=h).json()
    assert off["accounts_info"] is None
    with SessionLocal() as db:
        radar_hot.store(db, radar_hot.hot_rows(ITEMS[:4]), None, radar_hot._now())
        db.commit()
    on = client.get("/api/radar/hot/questions", params={"accounts": 1}, headers=h).json()
    info = on["accounts_info"]
    assert info["nicho"] >= 1 and info["sua"] == 0 and "views" in info["rule"]


def test_post_ideas_free_and_grok(client, monkeypatch):
    _, h = user(xai="xai-key")
    caption = "Peito ou bunda? Só pode escolher um 😏"
    free = client.post("/api/posts/ideas", json={"caption": caption, "verdict": "Risco alto: nudez"}, headers=h).json()
    texts = [i["text"] for i in free["ideas"]]
    assert len(texts) >= 6 and caption not in texts and free["mode"] == "free"
    assert all(not radar_hot.has_cta(t) and not radar_hot.unsafe(t) for t in texts)
    link = client.post("/api/posts/ideas", json={"caption": "Foto nova, link na bio 🔥"}, headers=h).json()
    assert link["ideas"] and all(not radar_hot.has_cta(i["text"]) for i in link["ideas"]), "tira o link e ainda sugere"

    prompts = []
    monkeypatch.setattr(radar_ideas, "ask_grok_lines", lambda key, prompt: prompts.append(prompt) or "Bunda ou peito? Escolhe 😈\nlink na bio")
    paid = client.post("/api/posts/ideas", json={"caption": caption, "verdict": "Risco alto: nudez", "mode": "grok"}, headers=h).json()
    assert [i["text"] for i in paid["ideas"]] == ["Bunda ou peito? Escolhe 😈"] and paid["mode"] == "grok"
    assert "Risco alto: nudez" in prompts[-1] and caption in prompts[-1]
    _, nokey = user()
    assert client.post("/api/posts/ideas", json={"caption": caption, "mode": "grok"}, headers=nokey).status_code == 400
    assert client.post("/api/posts/ideas", json={"caption": "quem curte novinha"}, headers=h).status_code == 400
