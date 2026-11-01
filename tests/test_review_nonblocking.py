"""O Verificar post espera o Grok (dezenas de segundos com foto). Essa espera não pode acontecer no laço
principal do servidor: com um processo só, ela parava o painel inteiro para todo mundo."""

from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from conftest import admin_row
from app.routers import auth, checks
from main import app


def test_grok_review_runs_off_the_event_loop(monkeypatch):
    where = {}

    def fake_grok(key, caption, image_uri):
        try:
            asyncio.get_running_loop()
            where["on_loop"] = True
        except RuntimeError:
            where["on_loop"] = False
        return "Liberado."

    monkeypatch.setattr(checks, "ask_grok", fake_grok)
    monkeypatch.setattr(checks, "xai_key", lambda db, user: "k")
    app.dependency_overrides[auth.approved_user] = admin_row
    try:
        with TestClient(app) as client:
            response = client.post("/api/posts/review", data={"caption": "oi"})
    finally:
        app.dependency_overrides.pop(auth.approved_user, None)
    assert response.status_code == 200 and response.json()["verdict"] == "Liberado."
    assert where["on_loop"] is False, "o Grok rodou no laço principal e travou o painel"


def test_radar_and_post_tools_do_not_reset_the_dashboard(monkeypatch):
    """Entrar no Radar já atualiza (POST): isso não muda número nenhum do dashboard, então não pode zerar o
    cache nem mandar todas as abas abertas recarregarem pelo ao vivo."""
    from app import cache, radar

    monkeypatch.setattr(radar, "refresh_manual", lambda db: False)  # 429, sem ir à rede
    app.dependency_overrides[auth.approved_user] = admin_row
    try:
        with TestClient(app) as client:
            before = cache._generation
            client.post("/api/radar/refresh")
            client.post("/api/posts/ideas", json={"caption": "Peito ou bunda?"})
            assert cache._generation == before, "Radar/Verificar post zeraram o cache do dashboard"
            client.post("/api/folders", json={"name": "Nova pasta"})
            assert cache._generation == before + 1, "escrita de verdade continua zerando"
    finally:
        app.dependency_overrides.pop(auth.approved_user, None)
