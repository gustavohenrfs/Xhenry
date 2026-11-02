"""Gerador por palavras-chave: lê o papel de cada palavra, monta todos os formatos com elas, nota viral, páginas
sem repetir, mistura nova, e as regras fixas (sem link, pessoa real nunca sexualizada, assunto sensível bloqueado)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import radar_ideas, radar_keywords as kw
from app.radar_hot import has_cta, unsafe
from app.radar_sources import norm
from main import app
from test_radar_hot import user

STORY = "VAZOU, Pitbull amigo do Fejuca, Elisa Sanches, Live"
WED = datetime(2026, 11, 4, 15, 0, tzinfo=timezone(timedelta(hours=-3)))  # quarta-feira


@pytest.fixture(autouse=True)
def secret(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "segredo-de-teste")


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def every(raw, heat="quente", mix=0):
    return [line for _, line in kw.bank(raw, heat, mix, WED)]


def test_reads_the_role_of_each_word():
    roles = {r["text"]: r["role"] for r in kw.parse(STORY + ", academia")["roles"]}
    assert roles == {"VAZOU": "gancho", "Pitbull amigo do Fejuca": "nome", "Elisa Sanches": "nome", "Live": "fato",
                     "academia": "tema"}
    fixed = kw.parse("Academia=tema, legging")["roles"][0]
    assert fixed == {"text": "Academia", "role": "tema", "forced": True}, "a tela corrige o papel"


def test_builds_the_owner_headline_and_every_format():
    lines = every(STORY)
    assert "VAZOU!!\n\nPitbull amigo do Fejuca faz live com Elisa Sanches" in lines, "o exemplo do dono sai igual"
    assert any("da live do Pitbull amigo do Fejuca com a Elisa Sanches" in line for line in lines), "artigo certo (do/da)"
    page = kw.generate(STORY, "quente", now=WED)
    assert {i["fmt"] for i in page["items"]} >= {"manchete", "pergunta", "polemica", "enquete", "desafio", "promessa",
                                                   "confissao", "urgencia"}, "a primeira página traz todos os formatos"
    assert len(lines) >= 300, "centenas de frases com 4 palavras"


def test_same_words_same_lines_new_mix_new_lines():
    assert every(STORY, mix=0) == every(STORY, mix=0)
    assert set(every(STORY, mix=1)) - set(every(STORY, mix=0)), "outra mistura traz frases novas"


def test_pages_never_repeat_and_end():
    seen, page = set(), 0
    while True:
        data = kw.generate("Neymar, treta", "leve", page=page, now=WED)
        texts = [norm(i["text"]) for i in data["items"]]
        assert not seen & set(texts), "página nova, frase nova"
        seen |= set(texts)
        if data["done"]:
            break
        page += 1
    assert len(seen) == data["total"]


def test_ordered_by_viral_score_with_reasons():
    items = kw.generate(STORY, "quente", now=WED)["items"]
    assert all(1 <= i["score"] <= 99 and i["tier_label"] for i in items)
    assert items == sorted(items, key=lambda i: -i["score"])
    asks = kw.viral_score("Vocês viram a live do Pitbull com a Elisa Sanches? Sejam sinceros", "pergunta", ["Pitbull", "live"])
    flat = kw.viral_score("Live", "urgencia", ["Pitbull", "live"])
    assert asks > flat, "pergunta com as palavras vale mais que frase solta"
    mine = {"enquete": {"score": 90}, "pergunta": {"score": 10}}
    poll = "Academia ou legging? Defende o seu nos comentários"
    assert kw.viral_score(poll, "enquete", ["academia"], mine) > kw.viral_score(poll, "enquete", ["academia"], None)


def test_format_filter():
    data = kw.generate(STORY, "quente", fmt="enquete", now=WED)
    assert data["items"] and all(i["fmt"] == "enquete" for i in data["items"])


def test_never_a_link_or_forbidden_content():
    for raw in (STORY, "academia, legging, cama", "Flamengo, Corinthians, jogo"):
        for heat in ("leve", "quente"):
            for line in every(raw, heat):
                assert not has_cta(line) and not unsafe(line) and len(line) <= kw.MAX_LEN, line


def test_real_person_never_sexualized():
    for line in every(STORY, "quente") + every(STORY, "leve"):
        if kw.SEX_RE.search(norm(line)):
            assert line.startswith("Se ") or line.startswith("Já que") or "post bater" in line, line
            assert not kw.HOOK_START.match(line), "VAZOU!! nunca junto de prêmio sexual com nome real: " + line
    assert not any(kw.SEX_RE.search(norm(line)) for line in every(STORY, "leve")), "leve sem nada sexual"
    with pytest.raises(kw.KeywordError, match="pessoa real"):
        kw.parse("Elisa Sanches, nude")


def test_hot_tone_only_with_themes():
    hot = every("academia, legging", "quente")
    assert any(kw.SEX_RE.search(norm(line)) for line in hot), "tema no quente pode ser quente"
    assert not any(kw.SEX_RE.search(norm(line)) for line in every("academia, legging", "leve"))


@pytest.mark.parametrize("raw, hint", [("acidente, Neymar", "sensível"), ("link na bio", "link"), ("novinha", "proibida"),
                                       ("", "pelo menos"), ("vazou, urgente", "assunto")])
def test_refuses(raw, hint):
    with pytest.raises(kw.KeywordError, match=hint):
        kw.parse(raw)


def test_weekday_words_follow_the_day():
    assert all(radar_ideas.fits_day(line, WED) for line in every("academia, cama", "quente"))


def test_route(client):
    _, headers = user()
    data = client.get("/api/radar/keywords", params={"terms": STORY}, headers=headers).json()
    assert len(data["items"]) == kw.PAGE and data["terms"][0]["role"] == "gancho" and data["total"] > kw.PAGE
    second = client.get("/api/radar/keywords", params={"terms": STORY, "page": 1}, headers=headers).json()
    assert not {i["text"] for i in data["items"]} & {i["text"] for i in second["items"]}
    bad = client.get("/api/radar/keywords", params={"terms": "acidente, Neymar"}, headers=headers)
    assert bad.status_code == 400 and "sensível" in bad.json()["detail"]


def test_grok_route(client, monkeypatch):
    _, headers = user()
    assert client.post("/api/radar/keywords/grok", json={"terms": STORY}, headers=headers).status_code == 400, "sem chave"
    _, headers = user(xai="xai-teste")
    prompts = []
    reply = ("VAZOU!! / Pitbull amigo do Fejuca faz live com Elisa Sanches\nVocês viram a live? 👀\n"
             "Elisa Sanches mandou nude na live\nlink na bio pra ver a live")
    monkeypatch.setattr(radar_ideas, "ask_grok_lines", lambda key, prompt: prompts.append(prompt) or reply)
    data = client.post("/api/radar/keywords/grok", json={"terms": STORY}, headers=headers).json()
    texts = [i["text"] for i in data["items"]]
    assert "VAZOU!!\n\nPitbull amigo do Fejuca faz live com Elisa Sanches" in texts
    assert not any("nude" in t or "link" in t for t in texts), "nada sexual sobre o nome, nunca link"
    assert "Elisa Sanches (nome)" in prompts[0] and "pessoas reais" in prompts[0]


# ---- Frase pronta ----

OWNER = "VAZOU!! Pitbull amigo do Fejuca faz live com Elisa Sanches"


def phrase_lines(text, heat="quente", mix=0):
    return [line for _, line in kw.phrase_bank(text, heat, mix, WED)]


def test_phrase_is_split_by_itself():
    info = kw.read_phrase(OWNER)
    assert info["hook"] == "VAZOU" and info["sentence"] == "Pitbull amigo do Fejuca faz live com Elisa Sanches"
    assert info["names"] == ["Pitbull amigo do Fejuca", "Elisa Sanches"] and info["verb"] == "faz live"
    assert info["event"] == "a live do Pitbull amigo do Fejuca com a Elisa Sanches" and info["kind"] == "noticia"
    other = kw.read_phrase("EITA: Neymar arma uma treta com Vini Jr no treino 👀")
    assert other["hook"] == "EITA" and other["names"] == ["Neymar", "Vini Jr"] and other["event"] == "a treta do Neymar com o Vini Jr"
    assert kw.read_phrase("Hoje acordei querendo atenção de vocês")["kind"] == "pessoal"
    assert kw.read_phrase("Vocês preferem morena ou loira?")["kind"] == "pergunta"
    assert kw.read_phrase("Se o Flamengo ganhar hoje eu mando nude")["kind"] == "promessa"


def test_phrase_lines_stay_on_the_same_story():
    lines = phrase_lines(OWNER)
    assert len(lines) >= 200
    for line in lines:
        key = norm(line)
        assert "pitbull" in key and ("elisa" in key or "fejuca" in key), "toda frase conta a mesma notícia: " + line
    first = kw.generate_phrase(OWNER, "quente", now=WED)
    shapes = [norm(kw.HOOK_START.sub("", i["text"])) for i in first["items"]]
    assert len(shapes) == len(set(shapes)), "a mesma frase só com outro gancho não se repete na página"
    assert first["phrase"]["kind"] == "noticia" and first["terms"][0] == {"text": "VAZOU", "role": "gancho", "forced": False}


def test_personal_phrase_gets_no_news_frames():
    lines = phrase_lines("Hoje acordei querendo atenção de vocês")
    assert lines and all("acordei querendo atencao" in norm(line) for line in lines)
    assert not any("ja viram" in norm(line) or "dividiu a internet" in norm(line) for line in lines)


def test_promise_with_real_name_keeps_its_own_promise():
    lines = phrase_lines("Se o Lula ganhar eu mando nude pra vocês")
    assert len(lines) >= 30 and all("se o lula ganhar eu mando nude" in norm(line) for line in lines)
    assert not any(kw.HOOK_START.match(line) for line in lines), "nada de VAZOU!! junto"
    with pytest.raises(kw.KeywordError, match="pessoa real"):
        kw.read_phrase("Elisa Sanches mandou nude na live")


def test_phrase_route_and_grok(client, monkeypatch):
    _, headers = user(xai="xai-teste")
    data = client.get("/api/radar/keywords", params={"phrase": OWNER}, headers=headers).json()
    assert len(data["items"]) == kw.PAGE and data["phrase"]["event"].startswith("a live do Pitbull")
    prompts = []
    monkeypatch.setattr(radar_ideas, "ask_grok_lines", lambda key, prompt: prompts.append(prompt) or "Vocês viram a live? 👀")
    assert client.post("/api/radar/keywords/grok", json={"phrase": OWNER}, headers=headers).status_code == 200
    assert OWNER in prompts[0] and "mesma notícia" in prompts[0]
    bad = client.get("/api/radar/keywords", params={"phrase": "link na bio"}, headers=headers)
    assert bad.status_code == 400


def test_phrase_edges():
    low = kw.read_phrase("vazou! a live do pitbull com a elisa sanches")
    assert low["hook"] == "VAZOU" and low["sentence"].startswith("A live") and low["kind"] == "noticia"
    start = kw.read_phrase("Se a live do Pitbull bater 1 milhão eu posto uma foto nova")
    assert "Se" not in start["names"] and start["kind"] == "promessa", "palavra comum no começo não é nome"
