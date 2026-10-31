"""Radar hot: textos quentes do nicho (Apify falso), formatos, filtros, Grok (falso) e ideias nível quente."""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app import radar, radar_hot, radar_ideas
from app.database import SessionLocal
from app.models import HotText, RadarItem, RadarPostCache, RadarSnapshot, User
from app.routers import auth
from app.services import user_settings
from main import app


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
        for model in (HotText, RadarPostCache, RadarItem, RadarSnapshot):
            db.query(model).delete()
        db.commit()


def user(apify: str | None = None, xai: str | None = None) -> tuple[int, dict[str, str]]:
    with SessionLocal() as db:
        row = User(email=f"h{uuid4().hex[:8]}@x.com", name="H", is_approved=True)
        db.add(row)
        db.commit()
        settings = user_settings(db, row.id)
        settings.apify_token = apify
        settings.xai_api_key = xai
        db.commit()
        return row.id, {"Authorization": f"Bearer {auth.issue_token(row.id, 0)}"}


def tweet(tid, text, likes=500, views=40_000, replies=120, retweets=30, author="modelo", **extra):
    return {"id": tid, "text": text, "likeCount": likes, "viewCount": views, "replyCount": replies,
            "retweetCount": retweets, "createdAt": "Fri Oct 30 18:00:00 +0000 2026",
            "author": {"userName": author, "name": author}} | extra


ITEMS = [
    tweet("1", "Os homens gostam de buceta peluda ou lisa? Tem alguém que diz que não faz diferença? Estou curiosa"),
    tweet("2", "Homens, eu prometo um nude dos seios aqui se lula ganhar", likes=900, replies=700),
    tweet("3", "Fantasias sexuais que toda mulher quer experimentar pelo menos uma vez\n\nA primeira é"),
    tweet("4", "Por que os homens acham difícil continuar transando depois que gozam, eles são fracos?"),
    tweet("5", "RT @outra: Se o Flamengo ganhar eu mando foto", likes=9000),           # repost
    tweet("6", "Quer ver mais? Link na bio 🔥", likes=9000),                         # chama para link
    tweet("7", "Quem curte uma novinha? 👀", likes=9000),                              # menor de idade: nunca
    tweet("8", "oi gente", likes=9000),                                                # curto demais
    tweet("9", "Vocês homens gozam rápido mesmo? Sinceramente", likes=5),             # pouca curtida
    tweet("10", "@fulano vocês homens são todos iguais né", likes=9000),               # resposta
    tweet("11", "Me chama no telegram que eu te mostro tudo", likes=9000),             # chama para fora
]


def test_formats_explain_why_the_owner_examples_work():
    fmt = radar_hot.format_of
    assert fmt("Homens, eu prometo um nude dos seios aqui se lula ganhar") == "promessa"
    assert fmt("Se o Lula ganhar eu mando a foto") == "promessa"
    assert fmt("Fantasias sexuais que toda mulher quer experimentar pelo menos uma vez. A primeira é") == "lista"
    assert fmt("Os homens gostam de peluda ou lisa? Estou curiosa") == "enquete"
    assert fmt("Duvido um homem aguentar 10 minutos comigo") == "desafio"
    assert fmt("Confesso: eu gosto mais de mandar do que de receber") == "confissao"
    assert fmt("Por que os homens acham difícil continuar, eles são fracos?") == "pergunta"
    assert fmt("Hoje eu tô com vontade de sentar rs") == "provocacao"
    assert set(radar_hot.LABELS) == {"promessa", "lista", "enquete", "desafio", "confissao", "pergunta", "provocacao"}


def test_hot_rows_keep_only_clean_text_posts():
    rows = radar_hot.hot_rows(ITEMS + [tweet("1", "duplicado do 1 com texto longo o bastante")])
    assert [r["tweet_id"] for r in rows] == ["1", "2", "3", "4"], "sem repost, link, menor, curto, fraco, resposta, duplicado"
    two = rows[1]
    assert two["fmt"] == "promessa" and two["url"] == "https://x.com/modelo/status/2"
    assert two["score"] == 900 + 2 * 30 + 3 * 700 + 40_000 // 200, "resposta pesa mais"
    assert radar_hot.unsafe("ninfeta") and radar_hot.unsafe("sem consentimento") and not radar_hot.unsafe("peluda ou lisa")
    assert radar_hot.has_cta("vem no meu privado") and radar_hot.has_cta("https://t.co/x") and not radar_hot.has_cta("se o lula ganhar")


def test_hunt_needs_a_token_then_caches_for_everyone(client, monkeypatch):
    _, nobody = user()
    resp = client.post("/api/radar/hot/hunt", headers=nobody)
    assert resp.status_code == 400 and "Apify" in resp.json()["detail"]
    a_id, a = user(apify="tok-a")
    _, b = user()
    calls = []

    def run(token, body):
        calls.append((token, body))
        return ITEMS

    monkeypatch.setattr(radar, "apify_run", run)
    first = client.post("/api/radar/hot/hunt", headers=a).json()
    assert first["cached"] is False and first["total"] == 4 and first["last"]["new"] == 4
    token, body = calls[0]
    assert token == "tok-a" and body["searchTerms"] == radar_hot.HOT_QUERIES and body["sort"] == "Top"
    assert all("lang:pt" in q and "-filter:links" in q for q in body["searchTerms"])
    assert first["items"][0]["id"] and first["items"][0]["fmt_label"] == "Promessa", "maior nota primeiro"
    assert first["next_hunt_at"], "diz até quando a caçada não gasta"
    again = client.post("/api/radar/hot/hunt", headers=b).json()
    assert again["cached"] is True and len(calls) == 1, "2 h de cache para todos, sem token de quem clicou"
    lib = client.get("/api/radar/hot", params={"fmt": "lista"}, headers=b).json()
    assert [i["fmt"] for i in lib["items"]] == ["lista"] and {f["key"]: f["count"] for f in lib["formats"]}["promessa"] == 1
    with SessionLocal() as db:
        assert db.query(HotText).filter(HotText.owner_id == a_id).count() == 4


def test_new_hunt_updates_numbers_and_prunes(client, monkeypatch):
    _, a = user(apify="tok")
    monkeypatch.setattr(radar, "apify_run", lambda token, body: ITEMS[:4])
    client.post("/api/radar/hot/hunt", headers=a)
    with SessionLocal() as db:
        hunt = db.query(RadarPostCache).filter(RadarPostCache.norm == radar_hot.HUNT_KEY).one()
        hunt.fetched_at = hunt.fetched_at - timedelta(hours=3)
        db.commit()
    monkeypatch.setattr(radar, "apify_run", lambda token, body: [tweet("1", ITEMS[0]["text"], likes=99_999)])
    monkeypatch.setattr(radar_hot, "KEEP_MAX", 2)
    data = client.post("/api/radar/hot/hunt", headers=a).json()
    assert data["cached"] is False and data["last"]["new"] == 0, "o mesmo post só atualiza os números"
    assert data["total"] == 2 and data["items"][0]["likes"] == 99_999, "a biblioteca guarda só os de nota maior"


def test_hot_grok_remix_and_new_texts(client, monkeypatch):
    _, nokey = user()
    _, a = user(xai="xai-key")
    with SessionLocal() as db:
        radar_hot.store(db, radar_hot.hot_rows(ITEMS[:4]), None, radar_hot._now())
        db.commit()
        source = db.query(HotText).filter(HotText.tweet_id == "2").one().id
    assert client.post("/api/radar/hot/grok", json={}, headers=nokey).status_code == 400
    assert client.post("/api/radar/hot/grok", json={"id": 999_999}, headers=a).status_code == 404
    prompts = []

    def fake(key, prompt):
        prompts.append(prompt)
        return ("1. Homens, se o Flamengo ganhar eu prometo um nude aqui\n"
                "Homens, eu prometo um nude dos seios aqui se lula ganhar\n"
                "- Se o Corinthians perder eu mando foto, link na bio\n"
                "Quem curte novinha?\n"
                "Se esse post bater 1000 respostas eu posto sem censura")

    monkeypatch.setattr(radar_ideas, "ask_grok_lines", fake)
    body = client.post("/api/radar/hot/grok", json={"id": source, "heat": "quente"}, headers=a).json()
    assert body["ideas"] == ["Homens, se o Flamengo ganhar eu prometo um nude aqui",
                             "Se esse post bater 1000 respostas eu posto sem censura"], "sem cópia, sem link, sem menor"
    assert "prometo um nude dos seios" in prompts[0] and "Promessa" in prompts[0] and "Nunca repita" in prompts[0]
    fresh = client.post("/api/radar/hot/grok", json={}, headers=a).json()
    assert fresh["source"] is None and fresh["ideas"]
    assert "8 posts novos" in prompts[1] and "Nível QUENTE" in prompts[1] and "nunca sexualizar uma pessoa real" in prompts[1]
    assert "Nunca mencione link" in prompts[1]


def test_quente_ideas_are_varied_and_keep_the_rules():
    pools = [getattr(radar_ideas, name) for name in dir(radar_ideas)
             if name.isupper() and isinstance(getattr(radar_ideas, name), list) and name != "OWNER_EXAMPLES_QUENTE"]
    for lines in pools:
        for line in lines:
            assert not radar_hot.has_cta(line) and not radar_hot.unsafe(line), line
    topics = [
        {"title": "#Flamengo", "norm": "flamengo", "category": "Futebol"},
        {"title": "Lula", "norm": "lula", "category": "Política"},
        {"title": "UFC 320", "norm": "ufc 320", "category": "Lutas"},
        {"title": "GTA 6", "norm": "gta 6", "category": "Games"},
        {"title": "Corinthians x Santos", "norm": "corinthians x santos", "category": "Futebol"},
    ]
    for topic in topics:
        lines = radar_ideas.ideas(topic, heat="quente")
        assert 3 <= len(lines) <= 5 and len(set(lines)) == len(lines), lines
        assert lines == radar_ideas.ideas(topic, heat="quente"), "estável: a tela não pula a cada leitura"
        formats = {radar_hot.format_of(line) for line in lines}
        assert len(formats) >= 3, ("formatos variados", lines)
    lula = radar_ideas.ideas(topics[1], heat="quente")
    assert any("se Lula ganhar" in line or line.startswith("Se Lula ganhar") for line in lula)
    match = radar_ideas.ideas(topics[4], heat="quente") + radar_ideas.ideas(topics[4], heat="quente", seed=3)
    assert any("Corinthians ou Santos?" in line for line in match), "jogo de grande também ganha enquete"
    # Mesmo assunto em dois jogos diferentes não abre com a mesma frase.
    firsts = {radar_ideas.ideas({"title": t, "norm": t.lower(), "category": "Futebol"}, heat="quente")[0]
              for t in ("Libertadores", "Brasileirão", "Copa do Brasil", "Sul-Americana")}
    assert len(firsts) >= 2, firsts
    seeds = {tuple(radar_ideas.ideas(topics[0], heat="quente", seed=s)) for s in range(6)}
    assert len(seeds) >= 3, "Mais ideias sorteia outra combinação"


def test_quente_never_sexualizes_a_named_person_and_skips_long_politics():
    person = radar_ideas.ideas({"title": "mariana ximenes", "norm": "mariana ximenes", "category": "Famosos/TV"}, heat="quente")
    assert person and all(line in [l.format(topic="Mariana Ximenes") for l in radar_ideas.PERSON_Q] for line in person)
    loud = radar_ideas.ideas({"title": "CONGRESSO INIMIGO DO POVO", "norm": "congresso inimigo do povo", "category": "Política"}, heat="quente")
    assert loud and not any("ganhar" in line and "Congresso" in line for line in loud)
    assert radar_ideas.ideas({"title": "Morre ator", "norm": "morre ator", "category": "Famosos/TV", "blocked": True}, heat="quente") == []
    leve = radar_ideas.ideas({"title": "#Flamengo", "norm": "flamengo", "category": "Futebol"})
    assert leve[0].startswith("Se o Flamengo ganhar hoje"), "leve sem semente continua igual a antes"


def test_hot_questions_route_rotates(client):
    _, a = user()
    one = client.get("/api/radar/hot/questions", params={"seed": 1}, headers=a).json()["ideas"]
    two = client.get("/api/radar/hot/questions", params={"seed": 2}, headers=a).json()["ideas"]
    assert len(one) == 5 and len(set(one)) == 5 and one != two
    leve = client.get("/api/radar/hot/questions", params={"heat": "leve"}, headers=a).json()["ideas"]
    assert set(leve) <= set(radar_ideas.EVERGREEN)
    assert client.get("/api/radar/hot/questions", params={"heat": "fogo"}, headers=a).status_code == 422


def test_angle_and_polemic_bonus():
    def topic(norm, category):
        return {"blocked": False, "norm": norm, "news": [], "category": category, "heat": 40}

    assert radar.angle_of(topic("flamengo x vasco", "Futebol")) == "enquete"
    assert radar.angle_of(topic("lula", "Política")) == "promessa"
    assert radar.angle_of(topic("final do bbb", "Famosos/TV")) == "promessa"
    assert radar.angle_of(topic("anitta", "Música")) == "pergunta"
    assert radar.engage_of(topic("treta no programa", "Outros")) > radar.engage_of(topic("programa novo", "Outros"))


def test_board_carries_heat_and_angle(client):
    _, a = user()
    from test_radar import fake_fetch

    with SessionLocal() as db:
        radar.refresh(db, ["x", "google", "news"], fetch=fake_fetch)
    leve = client.get("/api/radar", headers=a).json()
    quente = client.get("/api/radar", params={"heat": "quente"}, headers=a).json()
    assert leve["heat"] == "leve" and quente["heat"] == "quente"
    assert all("angle" in t for t in quente["topics"])
    pairs = [(l["ideas"], q["ideas"]) for l, q in zip(leve["topics"], quente["topics"]) if l["ideas"]]
    assert pairs and any(l != q for l, q in pairs), "o nível muda as ideias"
    norm = quente["topics"][0]["norm"]
    detail = client.get("/api/radar/topic", params={"norm": norm, "heat": "quente"}, headers=a).json()
    assert detail["ideas"] and detail["angle"]
    more = client.get("/api/radar/ideas", params={"norm": norm, "heat": "quente", "seed": 2}, headers=a)
    assert more.status_code == 200
