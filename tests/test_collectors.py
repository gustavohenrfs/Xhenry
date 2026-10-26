"""Aba Coletores: validação, máscara, segredos fora das respostas e trava contra conta monitorada."""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text

from app import services
from app.collectors import mask_cookies, mask_proxy, parse_cookies, parse_proxy
from app.database import SessionLocal
from app.models import Collector, Folder, XAccount
from app.routers.auth import current_user
from app.services import ensure_owner
from main import app

TOKEN = "a" * 36 + "b1c2"
SECRET = "senha-super-secreta"


@pytest.mark.parametrize(("raw", "expected"), [
    ("http://user:pass@1.2.3.4:8000", "http://user:pass@1.2.3.4:8000"),
    ("1.2.3.4:8000", "http://1.2.3.4:8000"),
    ("  SOCKS5://u:p@proxy.exemplo.com:1080 ", "socks5://u:p@proxy.exemplo.com:1080"),
    ("http://us%40er:p%3Ass@host:1", "http://us%40er:p%3Ass@host:1"),
    ("https://host:443", "https://host:443"),
    # Formato dos vendedores: ip:porta:usuário:senha.
    ("203.0.113.7:5619:usuario:senha123", "http://usuario:senha123@203.0.113.7:5619"),
    ("socks5://1.2.3.4:1080:u:se:nha", "socks5://u:se%3Anha@1.2.3.4:1080"),
])
def test_parse_proxy(raw, expected):
    assert parse_proxy(raw) == expected


@pytest.mark.parametrize("raw", ["ftp://h:1", "http://host", "http://host:abc", "http://h:1/caminho", "http://:8000"])
def test_parse_proxy_rejects(raw):
    with pytest.raises(ValueError):
        parse_proxy(raw)


def test_mask_proxy_hides_password():
    assert mask_proxy("http://user:pass@1.2.3.4:8000") == "http://user:***@1.2.3.4:8000"
    assert mask_proxy("socks5://1.2.3.4:1080") == "socks5://1.2.3.4:1080"
    assert mask_proxy(None) is None


def test_parse_cookies_formats():
    assert parse_cookies(TOKEN) == {"auth_token": TOKEN}
    assert parse_cookies(f"Cookie: auth_token={TOKEN}; ct0=abc; lang=pt") == {"auth_token": TOKEN, "ct0": "abc", "lang": "pt"}
    exported = json.dumps([
        {"name": "auth_token", "value": TOKEN, "domain": ".x.com"},
        {"name": "ct0", "value": "zzz", "domain": ".x.com"},
        {"name": "outro", "value": "1", "domain": ".google.com"},
    ])
    assert parse_cookies(exported) == {"auth_token": TOKEN, "ct0": "zzz"}
    assert parse_cookies(json.dumps({"auth_token": TOKEN, "twid": "u%3D1"})) == {"auth_token": TOKEN, "twid": "u%3D1"}


@pytest.mark.parametrize("raw", ["", "ct0=abc", "auth_token=curto", "[{]", '"texto"'])
def test_parse_cookies_rejects(raw):
    with pytest.raises(ValueError):
        parse_cookies(raw)


def test_mask_cookies_shows_only_last_chars():
    masked = mask_cookies(json.dumps({"auth_token": TOKEN, "ct0": "x", "twid": "y"}))
    assert masked == "auth_token …b1c2 + ct0, twid (3 cookies)"
    assert TOKEN not in masked


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def logged(client):
    app.dependency_overrides[current_user] = lambda: object()
    yield client
    app.dependency_overrides.clear()


def handle() -> str:
    return "b" + uuid4().hex[:10]


def test_list_requires_login(client):
    assert client.get("/api/collectors").status_code == 401


def test_create_never_returns_secrets(logged):
    name = handle()
    resp = logged.post("/api/collectors", json={
        "handle": "@" + name.upper(), "proxy": f"http://user:{SECRET}@9.9.9.9:8000", "cookies": f"auth_token={TOKEN}; ct0=abc",
    })
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["handle"] == name
    assert body["proxy"] == "http://user:***@9.9.9.9:8000"
    assert body["cookies"] == "auth_token …b1c2 + ct0 (2 cookies)"
    assert body["state"] == "ready"

    listing = logged.get("/api/collectors").text
    for leaked in (SECRET, TOKEN):
        assert leaked not in resp.text and leaked not in listing
    assert SECRET not in logged.get("/api/dashboard?period=today").text

    with SessionLocal() as db:  # o robô lê o valor inteiro no banco
        row = db.get(Collector, body["id"])
        assert row.proxy == f"http://user:{SECRET}@9.9.9.9:8000"
        assert json.loads(row.cookies) == {"auth_token": TOKEN, "ct0": "abc"}


def test_duplicate_and_invalid(logged):
    name = handle()
    assert logged.post("/api/collectors", json={"handle": name}).status_code == 201
    assert logged.post("/api/collectors", json={"handle": name}).status_code == 409
    bad = logged.post("/api/collectors", json={"handle": handle(), "proxy": "ftp://x:1"})
    assert bad.status_code == 400 and "http://" in bad.json()["detail"]
    bad = logged.post("/api/collectors", json={"handle": handle(), "cookies": "ct0=1"})
    assert bad.status_code == 400 and "auth_token" in bad.json()["detail"]


def test_monitored_account_cannot_be_collector_and_vice_versa(logged):
    money, burner = handle(), handle()
    with SessionLocal() as db:
        folder = Folder(owner_id=ensure_owner(db).id, name=f"Col {uuid4().hex[:8]}", color="#FF2EC8")
        db.add(folder)
        db.flush()
        db.add(XAccount(folder_id=folder.id, username=money))
        db.commit()
        folder_id = folder.id
    resp = logged.post("/api/collectors", json={"handle": money})
    assert resp.status_code == 400 and "conta monitorada" in resp.json()["detail"]

    assert logged.post("/api/collectors", json={"handle": burner}).status_code == 201
    resp = logged.post("/api/accounts", json={"folder_id": folder_id, "username": burner})
    assert resp.status_code == 400 and "coletor" in resp.json()["detail"]


def test_patch_keeps_clears_and_unpauses(logged):
    created = logged.post("/api/collectors", json={"handle": handle(), "proxy": "http://u:p@1.1.1.1:1", "cookies": TOKEN}).json()
    with SessionLocal() as db:
        row = db.get(Collector, created["id"])
        row.paused_until = services.now_utc().replace(year=2099)
        row.last_status, row.last_error = "sessao", "deslogado"
        db.commit()
    assert logged.get("/api/collectors").json() and next(
        c for c in logged.get("/api/collectors").json() if c["id"] == created["id"])["state"] == "paused"

    # Desligar não mexe no castigo nem nos segredos.
    body = logged.patch(f"/api/collectors/{created['id']}", json={"is_active": False}).json()
    assert body["state"] == "off" and body["proxy"] == "http://u:***@1.1.1.1:1" and body["last_status"] == "sessao"

    # Cookies novos: limpa o castigo e o erro; o proxy (None) fica.
    body = logged.patch(f"/api/collectors/{created['id']}", json={"cookies": TOKEN.replace("a", "c"), "is_active": True}).json()
    assert body["state"] == "ready" and body["last_status"] is None and body["proxy"] == "http://u:***@1.1.1.1:1"

    body = logged.patch(f"/api/collectors/{created['id']}", json={"proxy": ""}).json()
    assert body["proxy"] is None
    assert logged.delete(f"/api/collectors/{created['id']}").status_code == 204
    assert logged.patch(f"/api/collectors/{created['id']}", json={"proxy": ""}).status_code == 404


def test_startup_adds_rotation_columns(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'antigo.db'}")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE x_accounts (id INTEGER PRIMARY KEY, username VARCHAR(15), is_archived BOOLEAN)"))
    monkeypatch.setattr(services, "engine", engine)
    services.ensure_user_auth_columns()
    cols = {col["name"] for col in inspect(engine).get_columns("x_accounts")}
    assert {"archived_at", "collected_at", "followers_at"} <= cols
