"""Quais rotas exigem login e quais continuam livres para leitura."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.routers.auth import current_user
from main import app

PROTECTED = [
    ("get", "/api/settings", None),
    ("put", "/api/settings", {"impression_goal": 500}),
    ("post", "/api/telegram/test", None),
    ("post", "/api/folders", {"name": "Pasta"}),
    ("patch", "/api/folders/1", {"name": "Outra"}),
    ("delete", "/api/folders/1", None),
    ("post", "/api/accounts", {"folder_id": 1, "username": "fulano"}),
    ("patch", "/api/accounts/1", {"display_name": "Fulano"}),
    ("delete", "/api/accounts/1", None),
    ("get", "/api/check/fulano", None),
    ("post", "/api/check-all", None),
    ("get", "/api/cloaker/progress/0123456789abcdef0123456789abcdef", None),
    ("get", "/api/collectors", None),
    ("post", "/api/collectors", {"handle": "burner1"}),
    ("patch", "/api/collectors/1", {"is_active": False}),
    ("delete", "/api/collectors/1", None),
    ("post", "/api/collectors/1/test", None),
    ("get", "/api/accounts/1/detail?period=7d", None),
    ("get", "/api/robo", None),
    ("put", "/api/robo", {"enabled": False}),
    ("post", "/api/robo/run", None),
    ("get", "/api/robo/health", None),
    ("get", "/api/alerts", None),
    ("get", "/api/viral", None),
    ("get", "/api/push/key", None),
    ("post", "/api/push/subscribe", {"endpoint": "https://push.example/abc", "keys": {"p256dh": "x" * 30, "auth": "y" * 10}}),
    ("post", "/api/push/unsubscribe", {"endpoint": "https://push.example/abc"}),
    ("post", "/api/push/test", {"endpoint": "https://push.example/abc"}),
]

PUBLIC = ["/", "/health", "/sw.js", "/manifest.webmanifest", "/api/live?once=1", "/api/config", "/api/accounts/archived", "/api/dashboard?period=today", "/api/suggestions", "/api/folders"]


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:  # roda o lifespan: cria as tabelas no SQLite de teste
        yield c


@pytest.mark.parametrize(("method", "path", "body"), PROTECTED)
def test_protected_route_requires_login(client, method, path, body):
    resp = client.request(method, path, json=body)
    assert resp.status_code == 401, resp.text
    assert resp.json()["detail"] == "Login necessário."


def test_post_review_requires_login(client):
    resp = client.post("/api/posts/review", data={"caption": "oi"})
    assert resp.status_code == 401


@pytest.mark.parametrize("path", PUBLIC)
def test_public_read_routes_stay_open(client, path):
    assert client.get(path).status_code == 200


@pytest.mark.parametrize("value", [None, "false", "0", "nao"])
def test_register_closed_unless_allowed(client, monkeypatch, value):
    if value is None:
        monkeypatch.delenv("ALLOW_REGISTRATION", raising=False)
    else:
        monkeypatch.setenv("ALLOW_REGISTRATION", value)
    resp = client.post("/api/register", json={"nome": "Intruso", "email": "intruso@x.com", "senha": "12345678"})
    assert resp.status_code == 403
    assert resp.json()["detail"] == "Cadastro desativado. Peça acesso ao administrador."


def test_register_open_when_allowed(client, monkeypatch):
    monkeypatch.setenv("ALLOW_REGISTRATION", "true")
    resp = client.post("/api/register", json={"nome": "Nova", "email": "nova@x.com", "senha": "12345678"})
    assert resp.status_code == 201, resp.text
    assert resp.json()["email"] == "nova@x.com"


def test_logged_in_can_create_and_delete_folder(client):
    app.dependency_overrides[current_user] = lambda: object()
    try:
        created = client.post("/api/folders", json={"name": "Teste auth", "color": "#FF2EC8"})
        assert created.status_code == 201, created.text
        assert client.delete(f"/api/folders/{created.json()['id']}").status_code == 204
    finally:
        app.dependency_overrides.clear()
