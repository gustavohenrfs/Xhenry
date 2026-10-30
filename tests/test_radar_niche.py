"""Radar, parte 3: Brasil/Internacional, nota "bom pra engajar" (com bloqueios) e ideias de post."""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app import radar, radar_ideas
from app import radar_sources as src
from app.database import SessionLocal
from app.models import RadarItem, RadarSnapshot, User
from app.routers import auth
from app.services import user_settings
from main import app
from test_radar import GOOGLE_XML, fake_fetch

WORLD_X = """<html><body>
<div class=list-container><h3 class=title data-timestamp=1793390644.004>x</h3><ol>
<li><a href="#" class=trend-link>Halloween</a></li><li><a href="#" class=trend-link>#ภาษาไทยเท่านั้น</a></li>
</ol></div></body></html>"""
WORLD_GOOGLE = GOOGLE_XML.replace("flamengo", "houston weather").replace("anitta", "jack antonoff")
BR_X = """<html><body>
<div class=list-container><h3 class=title data-timestamp=1793390644.004>x</h3><ol>
<li><a href="#" class=trend-link>#Flamengo</a></li><li><a href="#" class=trend-link>#PlsLoveรักได้ไหม</a></li>
<li><a href="#" class=trend-link>Morre ator famoso</a></li><li><a href="#" class=trend-link>BBB 27</a></li>
</ol></div></body></html>"""


def fetch_all(url):
    if url == src.URLS["x_world"]:
        return WORLD_X
    if url == src.URLS["google_world"]:
        return WORLD_GOOGLE
    if "trends24" in url:
        return BR_X
    return fake_fetch(url)


@pytest.fixture(autouse=True)
def secret(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "segredo-de-teste")


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def db(client):
    with SessionLocal() as session:
        session.query(RadarItem).delete()
        session.query(RadarSnapshot).delete()
        session.commit()
        radar.refresh(session, fetch=fetch_all)
        yield session


def headers():
    with SessionLocal() as session:
        row = User(email=f"n{uuid4().hex[:8]}@x.com", name="N", is_approved=True)
        session.add(row)
        session.commit()
        return row, {"Authorization": f"Bearer {auth.issue_token(row.id, 0)}"}


def test_world_sources_exist():
    assert src.URLS["x_world"] == "https://trends24.in/"
    assert src.URLS["google_world"].endswith("geo=US")
    assert set(radar.REGIONS) == {"br", "world"}


def test_brasil_drops_foreign_scripts_and_world_is_separate(db):
    br = {t["norm"] for t in radar.board(db, region="br")["topics"]}
    assert "flamengo" in br and "bbb 27" in br
    assert not any("pls" in n for n in br), "hashtag tailandesa some do Brasil"
    world = radar.board(db, region="world")
    names = {t["norm"] for t in world["topics"]}
    assert {"halloween", "houston weather"} <= names and "flamengo" not in names
    assert set(world["sources"]) == {"x", "google"}
    assert world["upcoming"] == [] or all(i["kind"] == "previsao" for i in world["upcoming"]), "datas brasileiras só no Brasil"


def test_engage_score_and_blocks(db):
    topics = {t["norm"]: t for t in radar.board(db, region="br")["topics"]}
    assert topics["flamengo"]["engage"] >= 70 and topics["flamengo"]["blocked"] is False
    assert topics["bbb 27"]["engage"] >= 60
    assert topics["morre ator famoso"]["blocked"] is True and topics["morre ator famoso"]["engage"] == 0
    assert topics["flamengo"]["ideas"] and topics["morre ator famoso"]["ideas"] == [], "ideias prontas no board"
    assert radar.topic_detail(db, "flamengo")["ideas"]
    assert radar.blocked_text("menor de idade desaparecida") and radar.blocked_text("acidente na BR-116")
    assert not radar.blocked_text("final do brasileirao")


def test_ideas_follow_the_tone_and_rules():
    fla = radar_ideas.ideas({"title": "#Flamengo", "norm": "flamengo", "category": "Futebol"})
    assert 3 <= len(fla) <= 5 and any("flamenguistas" in line for line in fla) and any("Flamengo" in line for line in fla)
    lula = radar_ideas.ideas({"title": "Lula", "norm": "lula", "category": "Política"})
    assert any(line.startswith("Se o Lula") or line.startswith("Se Lula") for line in lula)
    bbb = radar_ideas.ideas({"title": "BBB 27", "norm": "bbb 27", "category": "Famosos/TV"})
    assert any("BBB 27" in line for line in bbb)
    assert radar_ideas.ideas({"title": "Morre ator", "norm": "morre ator", "category": "Famosos/TV", "blocked": True}) == []
    assert len(set(fla)) == len(fla), "sem repetição"


def test_routes_region_and_ideas(client, db):
    _, h = headers()
    world = client.get("/api/radar?region=world", headers=h).json()
    assert world["region"] == "world" and any(t["norm"] == "halloween" for t in world["topics"])
    assert client.get("/api/radar?region=marte", headers=h).status_code == 422
    ideas = client.get("/api/radar/ideas", params={"norm": "flamengo"}, headers=h).json()
    assert ideas["blocked"] is False and any("flamenguistas" in line for line in ideas["ideas"])
    assert client.get("/api/radar/ideas", params={"norm": "morre ator famoso"}, headers=h).json()["ideas"] == []


def test_grok_ideas_need_a_key_and_parse_lines(client, db, monkeypatch):
    user, h = headers()
    resp = client.post("/api/radar/ideas/grok", json={"norm": "flamengo"}, headers=h)
    assert resp.status_code == 400 and "xAI" in resp.json()["detail"]
    with SessionLocal() as session:
        user_settings(session, user.id).xai_api_key = "xai-teste"
        session.commit()
    seen = {}

    def fake_grok(key, prompt):
        seen["key"], seen["prompt"] = key, prompt
        return "1. Se o Flamengo ganhar hoje eu fico boazinha rs\n2) Quem tá vendo o jogo comigo? 👀\n\n- Flamenguista é tudo igual né"

    monkeypatch.setattr(radar_ideas, "ask_grok_lines", fake_grok)
    body = client.post("/api/radar/ideas/grok", json={"norm": "flamengo"}, headers=h).json()
    assert body["ideas"] == ["Se o Flamengo ganhar hoje eu fico boazinha rs", "Quem tá vendo o jogo comigo? 👀", "Flamenguista é tudo igual né"]
    assert seen["key"] == "xai-teste" and "nunca explícit" in seen["prompt"] and "menor" in seen["prompt"]
    assert client.post("/api/radar/ideas/grok", json={"norm": "morre ator famoso"}, headers=h).status_code == 400


def test_people_get_buzz_lines_and_titles_are_tidy():
    person = radar_ideas.ideas({"title": "mariana ximenes", "norm": "mariana ximenes", "category": "Famosos/TV"})
    assert person and all("Mariana Ximenes" in line for line in person[:2])
    assert not any("assistindo" in line or "votavam" in line for line in person), "frase de programa não serve para pessoa"
    show = radar_ideas.ideas({"title": "bbb 27", "norm": "bbb 27", "category": "Famosos/TV"})
    assert any("assistindo BBB 27" in line for line in show)
    loud = radar_ideas.ideas({"title": "CONGRESSO INIMIGO DO POVO", "norm": "congresso inimigo do povo", "category": "Política"})
    assert all("CONGRESSO" not in line for line in loud) and not any(line.startswith("Se Congresso") for line in loud)
    assert radar_ideas.clean_title("copa do brasil sub 20") == "Copa do Brasil Sub 20"
    assert radar_ideas.clean_title("CONGRESSO INIMIGO DO POVO") == "Congresso Inimigo do Povo"
    assert radar_ideas.clean_title("bbb 27") == "BBB 27"
    assert radar.engage_of({"blocked": False, "norm": "mariana ximenes", "news": [], "category": "Famosos/TV", "heat": 30}) < 55
    assert radar_ideas.clean_title("NÃO ENGAJE A EXTREMA-DIREITA") == "Não Engaje a Extrema-direita"
    wiki_person = radar_ideas.ideas({"title": "Patrick Stewart", "norm": "patrick stewart", "category": "Futebol", "sources": ["wiki"]})
    assert all("time" not in line for line in wiki_person) and "Patrick Stewart" in wiki_person[0]


# ---- público masculino, só Brasil, sem Wikipédia, chamada para o grupo ----

def male(norm, category=None, heat=50):
    cat = category or radar.category_of(norm)
    return radar.engage_of({"blocked": False, "norm": norm, "news": [], "category": cat, "heat": heat, "sources": ["x"]}), cat


def test_male_audience_ranking():
    assert male("flamengo x palmeiras")[0] >= 85
    assert male("ufc 320") == (male("ufc 320")[0], "Lutas") and male("ufc 320")[0] >= 80
    assert male("gp de sao paulo f1")[1] == "Esportes" and male("gp de sao paulo f1")[0] >= 70
    assert male("gta 6")[1] == "Games" and male("gta 6")[0] >= 65
    assert male("churrasco de domingo")[1] == "Resenha"
    assert male("bbb 27")[0] >= 60, "reality com votação ainda rende"
    for weak in ("novela das nove", "#bts", "skincare coreano", "horoscopo de hoje", "look do dia"):
        assert male(weak)[0] < 55, weak


def test_wikipedia_is_gone():
    assert "wiki" not in radar.SOURCES and "wiki" not in radar.REGIONS["br"]
    assert "wiki" not in src.URLS and not hasattr(src, "parse_wiki")


def test_ideas_never_call_for_a_link():
    """Só engajamento: nenhuma frase chama para link, bio, grupo ou Telegram, nem nos modelos nem no pedido ao Grok."""
    banned = ("link", "bio", "grupo", "telegram", "http", "canal", "privado", "dm")
    lists = [getattr(radar_ideas, name) for name in dir(radar_ideas) if name.isupper() and isinstance(getattr(radar_ideas, name), list)]
    for lines in lists:
        for line in lines:
            assert not any(word in line.lower().split() or word in line.lower() for word in banned[:5]), line
    for topic in (
        {"title": "#Flamengo", "norm": "flamengo", "category": "Futebol"},
        {"title": "UFC 320", "norm": "ufc 320", "category": "Lutas"},
        {"title": "GTA 6", "norm": "gta 6", "category": "Games"},
        {"title": "Lula", "norm": "lula", "category": "Política"},
    ):
        lines = radar_ideas.ideas(topic)
        assert 3 <= len(lines) <= 5 and not any("link" in l.lower() or "grupo" in l.lower() for l in lines)
    prompt = radar_ideas.grok_prompt({"title": "Flamengo", "category": "Futebol"})
    assert "homens" in prompt and "nunca mencione link" in prompt.lower()


def test_brazilian_football_first_and_google_prefixes_go():
    br, _ = male("criciuma x ponte preta", "Futebol")
    foreign, _ = male("elversberg x mainz 05", "Futebol")
    assert br >= 85 and foreign < 55, (br, foreign)
    assert male("libertadores final", "Futebol")[0] >= 85
    assert radar_ideas.clean_title("Classificações de Elversberg x Mainz 05") == "Elversberg x Mainz 05"
    assert radar_ideas.clean_title("escalações de flamengo x vasco") == "Flamengo x Vasco"
    assert radar_ideas.clean_title("onde assistir corinthians x santos") == "Corinthians x Santos"


def test_match_people_and_hangout_lines():
    player = radar_ideas.ideas({"title": "Andrey Santos", "norm": "andrey santos", "category": "Futebol"})
    assert not any("Se o Santos" in line for line in player), "jogador não é o time"
    assert radar_ideas.team_of("santos x vasco") and radar_ideas.team_of("flamengo campeao") and not radar_ideas.team_of("andrey santos")
    match = radar_ideas.ideas({"title": "Criciúma x Ponte Preta", "norm": "criciuma x ponte preta", "category": "Futebol"})
    assert match[0].startswith("Criciúma ou Ponte Preta?")
    sextou = radar_ideas.ideas({"title": "Sextou", "norm": "sextou", "category": "Resenha"})
    assert not any("Sextou com Sextou" in line or line.count("Sextou") > 1 for line in sextou)
    volei = radar_ideas.ideas({"title": "Superliga Feminina", "norm": "superliga feminina", "category": "Esportes"})
    assert not any("pódio" in line for line in volei)
