"""/api/config e recuperação de senha pelo Telegram."""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.models import User
from app.routers import auth
from main import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def telegram(monkeypatch):
    """Bot configurado; guarda as mensagens em vez de chamar o Telegram."""
    sent: list[str] = []
    monkeypatch.setenv("SECRET_KEY", "segredo-de-teste")
    monkeypatch.setattr(auth, "telegram_creds", lambda _db: ("bot-token", "chat-id"))
    monkeypatch.setattr(auth, "send_telegram", lambda _t, _c, text: sent.append(text))
    auth._reset_sent.clear()
    return sent


def make_user(email: str, senha: str = "senha-antiga") -> int:
    with SessionLocal() as db:
        user = User(email=email, name="Teste", nome_exibicao="Teste", senha_hash=auth.hash_password(senha))
        db.add(user)
        db.commit()
        return user.id


def link_token(message: str) -> str:
    match = re.search(r"#reset=([\w\-.]+)", message)
    assert match, message
    return match.group(1)


@pytest.mark.parametrize(("env", "expected"), [(None, False), ("false", False), ("true", True)])
def test_config_reflects_registration(client, monkeypatch, env, expected):
    if env is None:
        monkeypatch.delenv("ALLOW_REGISTRATION", raising=False)
    else:
        monkeypatch.setenv("ALLOW_REGISTRATION", env)
    body = client.get("/api/config").json()
    assert body["allow_registration"] is expected


def test_config_password_reset_follows_telegram(client, monkeypatch):
    monkeypatch.setattr(auth, "telegram_creds", lambda _db: ("", ""))
    assert client.get("/api/config").json()["password_reset"] is False
    monkeypatch.setattr(auth, "telegram_creds", lambda _db: ("t", "c"))
    assert client.get("/api/config").json()["password_reset"] is True


def test_request_without_telegram_is_unavailable(client, monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "segredo-de-teste")
    monkeypatch.setattr(auth, "telegram_creds", lambda _db: ("", ""))
    resp = client.post("/api/password-reset/request", json={"email": "x@x.com"})
    assert resp.status_code == 503


def test_unknown_email_gets_same_answer_and_no_message(client, telegram):
    make_user("existe@x.com")
    known = client.post("/api/password-reset/request", json={"email": "existe@x.com"})
    unknown = client.post("/api/password-reset/request", json={"email": "ninguem@x.com"})
    assert known.status_code == unknown.status_code == 200
    assert known.json() == unknown.json()
    assert len(telegram) == 1 and "existe@x.com" in telegram[0]


def test_cooldown_blocks_repeated_messages(client, telegram):
    make_user("repete@x.com")
    for _ in range(3):
        assert client.post("/api/password-reset/request", json={"email": "repete@x.com"}).status_code == 200
    assert len(telegram) == 1


def test_link_uses_public_url(client, telegram, monkeypatch):
    monkeypatch.setenv("PUBLIC_URL", "https://painel.exemplo.com/")
    make_user("url@x.com")
    client.post("/api/password-reset/request", json={"email": "url@x.com"})
    assert "https://painel.exemplo.com/#reset=" in telegram[0]


def test_reset_changes_password_and_link_is_single_use(client, telegram):
    make_user("troca@x.com")
    client.post("/api/password-reset/request", json={"email": "troca@x.com"})
    token = link_token(telegram[0])

    resp = client.post("/api/password-reset/confirm", json={"token": token, "nova_senha": "senha-nova-123"})
    assert resp.status_code == 200, resp.text
    assert client.post("/api/login", json={"email": "troca@x.com", "senha": "senha-antiga"}).status_code == 401
    assert client.post("/api/login", json={"email": "troca@x.com", "senha": "senha-nova-123"}).status_code == 200

    again = client.post("/api/password-reset/confirm", json={"token": token, "nova_senha": "outra-senha-456"})
    assert again.status_code == 400
    assert again.json()["detail"] == auth.RESET_INVALID


def test_expired_reset_token_is_rejected(client, telegram):
    user_id = make_user("expira@x.com")
    with SessionLocal() as db:
        token = auth.issue_reset_token(db.get(User, user_id), ttl=-1)
    resp = client.post("/api/password-reset/confirm", json={"token": token, "nova_senha": "senha-nova-123"})
    assert resp.status_code == 400


def test_login_and_reset_tokens_are_not_interchangeable(client, telegram):
    user_id = make_user("cruza@x.com")
    login_token = auth.issue_token(user_id)
    resp = client.post("/api/password-reset/confirm", json={"token": login_token, "nova_senha": "senha-nova-123"})
    assert resp.status_code == 400

    with SessionLocal() as db:
        reset_token = auth.issue_reset_token(db.get(User, user_id))
    me = client.get("/api/me", headers={"Authorization": f"Bearer {reset_token}"})
    assert me.status_code == 401


def test_short_new_password_is_rejected(client, telegram):
    user_id = make_user("curta@x.com")
    with SessionLocal() as db:
        token = auth.issue_reset_token(db.get(User, user_id))
    resp = client.post("/api/password-reset/confirm", json={"token": token, "nova_senha": "123"})
    assert resp.status_code == 422
