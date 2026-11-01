"""Perguntas quentes: dia da semana certo, gerador novo (combina peças), contas só quando o usuário pede e só
o que viralizou de verdade."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
import pytest

from app import radar_hot, radar_ideas
from app.database import SessionLocal
from app.models import Folder, TweetSample, XAccount
from main import app
from test_radar_hot import ITEMS, tweet, user


@pytest.fixture(autouse=True)
def secret(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "segredo-de-teste")


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def sp(day: int, hour: int = 15) -> datetime:
    """Novembro de 2026 em São Paulo (UTC-3): dia 1 é domingo, 6 é sexta, 7 é sábado."""
    return datetime(2026, 11, day, hour, 0, tzinfo=timezone(timedelta(hours=-3)))


def test_weekday_words_only_on_their_day():
    fits = radar_ideas.fits_day
    assert fits("Sextou! Vão sair hoje?", sp(6)) and not fits("Sextou! Vão sair hoje?", sp(1))
    assert fits("Domingou e eu na cama 👀", sp(1)) and not fits("Domingo é dia de ficar na cama", sp(6))
    assert fits("Sábado à noite: balada ou motel?", sp(7)) and not fits("Sábado à noite: balada ou motel?", sp(2))
    assert fits("Fim de semana chegou", sp(6)) and fits("Fim de semana chegou", sp(1)) and not fits("Fim de semana chegou", sp(3))
    assert fits("Se esse post bater 500 curtidas até domingo eu posto", sp(7)) and not fits("…até domingo eu posto", sp(2))
    assert fits("Homem que não beija bem merece segunda chance?", sp(4)), "segunda chance não é segunda-feira"
    assert fits("Bom dia segunda-feira, segundou", sp(2)) and not fits("Segundou! Bom dia", sp(1))


def test_bank_and_draw_follow_the_day():
    sunday = radar_ideas.question_bank("quente", sp(1))
    texts = [i["text"] for i in sunday["items"]]
    assert not any("sext" in t.lower() for t in texts), "domingo não fala de sexta"
    friday = [i["text"] for i in radar_ideas.question_bank("quente", sp(6, 19))["items"]]
    assert any("Sextou" in t for t in friday) and not any(t.lower().startswith("domingo") for t in friday)
    for seed in range(20):
        assert all(radar_ideas.fits_day(t, sp(1)) for t in radar_ideas.hot_questions("quente", seed, now=sp(1)))


def test_generator_composes_new_lines_every_mix():
    one = radar_ideas.compose("quente", seed=1)
    two = radar_ideas.compose("quente", seed=2)
    assert len(one) >= 40 and len(set(one)) == len(one) and set(one) != set(two), "frases novas a cada mistura"
    formats = {radar_hot.format_of(line) for line in one}
    assert {"enquete", "pergunta", "desafio", "promessa", "lista", "confissao"} <= formats
    assert all(not radar_hot.unsafe(line) and not radar_hot.has_cta(line) and len(line) <= 220 for line in one)
    bank = radar_ideas.question_bank("quente", sp(4), mix=3)
    assert len(bank["items"]) >= 150, "banco fixo + frases montadas"
    assert sum(1 for i in bank["items"] if i.get("made")) >= 40


def test_accounts_only_when_asked_and_only_viral(client):
    owner_id, h = user()
    with SessionLocal() as db:
        weak = [tweet("w1", "Boa noite meus amores, dormir agora", likes=40, views=900, replies=2)]
        radar_hot.store(db, radar_hot.hot_rows(ITEMS[:4] + weak), None, radar_hot._now())
        folder = Folder(owner_id=owner_id, name="Minhas")
        db.add(folder)
        db.flush()
        acc = XAccount(folder_id=folder.id, username="minhagen")
        db.add(acc)
        db.flush()
        for n, (views, text) in enumerate([(120_000, "Vocês homens aguentam quanto tempo? Sinceramente"),
                                           (300, "Peito ou bunda? Só pode escolher um 😏")]):
            db.add(TweetSample(account_id=acc.id, tweet_id=f"g{n}", posted_at=radar_hot._now() - timedelta(days=1),
                               timezone_used="America/Sao_Paulo", hour_local=22, weekday=1, view_count=views,
                               reply_count=900 if views > 1000 else 1, text_preview=text))
        db.commit()
        proven = radar_hot.proven(db, owner_id)
    texts = [p["text"] for p in proven]
    assert "Vocês homens aguentam quanto tempo? Sinceramente" in texts
    assert "Peito ou bunda? Só pode escolher um 😏" not in texts, "post seu que não viralizou fica de fora"
    assert not any("Boa noite meus amores" in t for t in texts), "post fraco do nicho fica de fora"

    off = client.get("/api/radar/hot/questions", headers=h).json()
    assert not any(i.get("source") for i in off["items"]), "sem o botão, nada das contas"
    on = client.get("/api/radar/hot/questions", params={"accounts": 1}, headers=h).json()
    assert any(i.get("source") == "sua" for i in on["items"])


def test_grok_prompt_knows_the_day_and_skips_accounts(client, monkeypatch):
    _, h = user(xai="xai-key")
    prompts = []

    def fake(key, prompt):
        prompts.append(prompt)
        return "Sextou! Quem vem?\nDuvido um homem aguentar 10 minutos comigo sem gozar 😏"

    monkeypatch.setattr(radar_ideas, "ask_grok_lines", fake)
    monkeypatch.setattr(radar_ideas, "now_sp", lambda: sp(1))
    body = client.post("/api/radar/hot/grok", json={}, headers=h).json()
    assert "domingo" in prompts[-1].lower() and "já engajaram" not in prompts[-1], "sem contas por padrão"
    assert [i["text"] for i in body["ideas"]] == ["Duvido um homem aguentar 10 minutos comigo sem gozar 😏"], \
        "frase de sexta no domingo é descartada"
    with SessionLocal() as db:
        radar_hot.store(db, radar_hot.hot_rows(ITEMS[:4]), None, radar_hot._now())
        db.commit()
    client.post("/api/radar/hot/grok", json={"accounts": True}, headers=h)
    assert "já engajaram" in prompts[-1]


def test_tab_markup_for_radar_refresh_and_accounts_switch():
    from app.config import ROOT

    js = (ROOT / "static/js/app.js").read_text(encoding="utf-8")
    html = (ROOT / "templates/index.html").read_text(encoding="utf-8")
    assert 'if (tab === "radar") { loadRadar(); refreshRadar({ auto: true }); }' in js, "entrar no Radar já atualiza"
    assert 'id="hot-accounts"' in html and 'localStorage.getItem("hot_accounts") === "1"' in js, "contas: desligado por padrão"
    assert "body.accounts = hotAccounts" in js and "&mix=" in js
