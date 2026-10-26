"""Redefinir ou trocar a senha derruba as sessões abertas (users.token_version)."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text

from app import services
from app.database import SessionLocal
from app.models import User
from app.routers import auth
from main import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def secret(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "segredo-de-teste")
    monkeypatch.setattr(auth, "telegram_creds", lambda _db: ("bot-token", "chat-id"))
    monkeypatch.setattr(auth, "send_telegram", lambda *_args: None)


def make_user(email: str) -> int:
    with SessionLocal() as db:
        user = User(email=email, name="Teste", nome_exibicao="Teste", senha_hash=auth.hash_password("senha-antiga"))
        db.add(user)
        db.commit()
        return user.id


def login(client, email: str, senha: str = "senha-antiga") -> dict[str, str]:
    resp = client.post("/api/login", json={"email": email, "senha": senha})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def test_reset_logs_out_open_sessions(client):
    user_id = make_user("sessao-reset@x.com")
    celular = login(client, "sessao-reset@x.com")
    assert client.get("/api/me", headers=celular).status_code == 200

    with SessionLocal() as db:
        token = auth.issue_reset_token(db.get(User, user_id))
    assert client.post("/api/password-reset/confirm", json={"token": token, "nova_senha": "senha-nova-123"}).status_code == 200

    resp = client.get("/api/me", headers=celular)
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Sessão encerrada. Entre novamente."
    assert client.get("/api/me", headers=login(client, "sessao-reset@x.com", "senha-nova-123")).status_code == 200


def test_change_password_keeps_only_the_current_session(client):
    make_user("sessao-troca@x.com")
    notebook = login(client, "sessao-troca@x.com")
    celular = login(client, "sessao-troca@x.com")

    resp = client.post("/api/change-password", json={"nova_senha": "senha-nova-123"}, headers=notebook)
    assert resp.status_code == 200
    novo = {"Authorization": f"Bearer {resp.json()['access_token']}"}

    assert client.get("/api/me", headers=celular).status_code == 401
    assert client.get("/api/me", headers=notebook).status_code == 401, "o token antigo do próprio aparelho também cai"
    assert client.get("/api/me", headers=novo).status_code == 200


def test_token_from_before_versioning_still_works(client):
    user_id = make_user("sessao-antiga@x.com")
    # Formato emitido antes desta mudança: sem "ver".
    old = auth._sign({"sub": str(user_id), "exp": int(time.time()) + 60}, auth.secret_key().encode())
    headers = {"Authorization": f"Bearer {old}"}
    assert client.get("/api/me", headers=headers).status_code == 200

    with SessionLocal() as db:
        token = auth.issue_reset_token(db.get(User, user_id))
    client.post("/api/password-reset/confirm", json={"token": token, "nova_senha": "senha-nova-123"})
    assert client.get("/api/me", headers=headers).status_code == 401


def test_startup_adds_token_version_to_old_table(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'antigo.db'}")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE users (id INTEGER PRIMARY KEY, email VARCHAR(320), name VARCHAR(120))"))
        conn.execute(text("INSERT INTO users (id, email, name) VALUES (1, 'velho@x.com', 'Velho')"))
    monkeypatch.setattr(services, "engine", engine)
    services.ensure_user_auth_columns()

    assert "token_version" in {col["name"] for col in inspect(engine).get_columns("users")}
    with engine.connect() as conn:
        assert conn.execute(text("SELECT token_version FROM users WHERE id = 1")).scalar() == 0
