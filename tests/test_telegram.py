"""Envio para o Telegram, dedupe dos alertas e as rotas de configuração/teste."""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from conftest import admin_row
from app import services
from app.database import SessionLocal
from app.models import AlertSent
from app.routers import checks
from app.routers.auth import current_user
from main import app

REAL_CLIENT = httpx.Client


@pytest.fixture
def telegram_http(monkeypatch):
    """Troca a rede do send_telegram por um handler; devolve a lista de requisições."""
    requests: list[httpx.Request] = []

    def install(handler):
        def recording(request):
            requests.append(request)
            return handler(request)

        monkeypatch.setattr(
            services.httpx, "Client", lambda **kw: REAL_CLIENT(transport=httpx.MockTransport(recording), **kw)
        )
        return requests

    return install


def test_send_posts_chat_and_text(telegram_http):
    requests = telegram_http(lambda _r: httpx.Response(200, json={"ok": True}))
    services.send_telegram("123:abc", "-100", "oi")
    assert str(requests[0].url) == "https://api.telegram.org/bot123:abc/sendMessage"
    assert json.loads(requests[0].content) == {"chat_id": "-100", "text": "oi"}


def test_send_without_creds_is_503():
    with pytest.raises(HTTPException) as exc:
        services.send_telegram("", "-100", "oi")
    assert exc.value.status_code == 503


def test_send_refused_shows_telegram_reason(telegram_http):
    telegram_http(lambda _r: httpx.Response(400, json={"ok": False, "description": "Bad Request: chat not found"}))
    with pytest.raises(HTTPException) as exc:
        services.send_telegram("123:abc", "-100", "oi")
    assert exc.value.status_code == 502 and "chat not found" in exc.value.detail


def test_send_network_error_is_502(telegram_http):
    def boom(request):
        raise httpx.ConnectError("sem rede", request=request)

    telegram_http(boom)
    with pytest.raises(HTTPException) as exc:
        services.send_telegram("123:abc", "-100", "oi")
    assert exc.value.status_code == 502


@pytest.fixture
def db():
    with TestClient(app):
        pass
    with SessionLocal() as session:
        session.query(AlertSent).delete()
        session.commit()
        yield session


def test_try_send_dedupes_by_key(db, monkeypatch):
    sent: list[str] = []
    monkeypatch.setattr(services, "telegram_creds", lambda _db: ("bot", "chat"))
    monkeypatch.setattr(services, "send_telegram", lambda _t, _c, text: sent.append(text))
    assert services.try_send(db, "teste:1", "teste", "primeira") is True
    assert services.try_send(db, "teste:1", "teste", "repetida") is False
    assert services.try_send(db, "teste:2", "teste", "outra chave") is True
    assert sent == ["primeira", "outra chave"]


def test_try_send_without_creds_does_nothing(db, monkeypatch):
    monkeypatch.setattr(services, "telegram_creds", lambda _db: ("", ""))
    assert services.try_send(db, "teste:3", "teste", "x") is False
    assert db.query(AlertSent).count() == 0


def test_failed_send_is_not_recorded_so_it_retries(db, monkeypatch):
    def refuse(*_args):
        raise HTTPException(502, "fora do ar")

    monkeypatch.setattr(services, "telegram_creds", lambda _db: ("bot", "chat"))
    monkeypatch.setattr(services, "send_telegram", refuse)
    assert services.try_send(db, "teste:4", "teste", "x") is False
    assert db.query(AlertSent).count() == 0
    monkeypatch.setattr(services, "send_telegram", lambda *_args: None)
    assert services.try_send(db, "teste:4", "teste", "x") is True


@pytest.fixture
def logged_in():
    app.dependency_overrides[current_user] = admin_row
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.clear()


def test_telegram_comes_only_from_env(logged_in, monkeypatch):
    """Um bot só, o do .env (o mesmo do robo.py): o que ficou salvo no banco é ignorado."""
    with SessionLocal() as session:
        row = services.get_settings(session)
        row.telegram_token, row.telegram_chat_id = "999:antigo", "-1"
        session.commit()
    try:
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        monkeypatch.delenv("TELEGRAM_TOKEN", raising=False)
        monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
        assert services.telegram_creds(None) == ("", "")
        assert logged_in.get("/api/settings").json()["telegram_ready"] is False
        monkeypatch.setenv("TELEGRAM_TOKEN", "123:segredo")  # nome que o robo.py usa
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "-100")
        assert services.telegram_creds(None) == ("123:segredo", "-100")
        body = logged_in.get("/api/settings").json()
        assert body["telegram_ready"] is True and "segredo" not in json.dumps(body)
        # O painel não aceita mais token pelo formulário.
        logged_in.put("/api/settings", json={"telegram_token": "777:novo", "impression_goal": 600})
        with SessionLocal() as session:
            assert services.get_settings(session).telegram_token == "999:antigo"
    finally:
        with SessionLocal() as session:
            row = services.get_settings(session)
            row.telegram_token = row.telegram_chat_id = None
            row.impression_goal = 500
            session.commit()


def test_test_notification_sends_viral_sample(logged_in, monkeypatch):
    sent: list[tuple[str, str, str]] = []
    monkeypatch.setattr(checks, "telegram_creds", lambda _db: ("bot", "chat"))
    monkeypatch.setattr(checks, "send_telegram", lambda *args: sent.append(args))
    resp = logged_in.post("/api/telegram/test")
    assert resp.status_code == 200 and resp.json() == {"ok": True}
    token, chat, text = sent[0]
    assert (token, chat) == ("bot", "chat")
    assert "Post viralizando" in text and "Curtidas" in text and "Retweets" in text


def test_recent_alerts_newest_first():
    from datetime import timedelta
    from uuid import uuid4

    from fastapi.testclient import TestClient

    from app.database import SessionLocal
    from app.models import AlertSent
    from app.routers.auth import current_user
    from app.services import now_utc
    from main import app

    with TestClient(app):
        pass  # o lifespan cria as tabelas antes de gravar
    tag = uuid4().hex[:8]
    with SessionLocal() as db:
        db.add_all([
            AlertSent(dedupe_key=f"velho:{tag}", kind="queda", detail="velho", sent_at=now_utc() + timedelta(days=1)),
            AlertSent(dedupe_key=f"novo:{tag}", kind="viral", detail="🚀 Post viralizando!", sent_at=now_utc() + timedelta(days=2)),
        ])
        db.commit()
    app.dependency_overrides[current_user] = admin_row
    try:
        with TestClient(app) as client:
            rows = client.get("/api/alerts?limit=2").json()
    finally:
        app.dependency_overrides.clear()
    assert [row["kind"] for row in rows] == ["viral", "queda"]
    assert rows[0]["text"] == "🚀 Post viralizando!" and rows[0]["sent_at"]


def test_viral_feed_has_account_link_and_live_views():
    from datetime import datetime, timezone
    from uuid import uuid4

    from fastapi.testclient import TestClient

    from app.database import SessionLocal
    from app.models import AlertSent, Folder, TweetSample, XAccount
    from app.routers.auth import current_user
    from app.services import ensure_admin
    from main import app

    with TestClient(app):
        pass  # o lifespan cria as tabelas antes de gravar
    tag = uuid4().hex[:6]
    with SessionLocal() as db:
        folder = Folder(owner_id=ensure_admin(db).id, name=f"Viral {tag}")
        db.add(folder)
        db.flush()
        account = XAccount(folder_id=folder.id, username=f"vir_{tag}", display_name="Ana Vibes")
        db.add(account)
        db.flush()
        db.add(TweetSample(
            account_id=account.id, tweet_id="777", posted_at=datetime.now(timezone.utc),
            timezone_used="America/Sao_Paulo", hour_local=10, weekday=0,
            text_preview="Hoje tem live", view_count=9000, like_count=300,
        ))
        db.add_all([
            AlertSent(owner_id=folder.owner_id, dedupe_key=f"viral:{account.id}:777", kind="viral", detail="🚀 Post viralizando!\n👁 Views: 600"),
            AlertSent(dedupe_key=f"queda:{tag}", kind="queda", detail="📉 Queda"),
            # Conta apagada: sai do texto do alerta.
            AlertSent(owner_id=folder.owner_id, dedupe_key="viral:9999999:888", kind="viral",
                      detail=f"🚀 Post viralizando!\n👤 Conta: Bia (@bia_{tag})\n👁 Views: 1.234\n🔗 Link do Post: https://x.com/bia/status/888"),
        ])
        db.commit()
    app.dependency_overrides[current_user] = admin_row
    try:
        with TestClient(app) as client:
            items = client.get("/api/viral?limit=50").json()
    finally:
        app.dependency_overrides.clear()
    mine = [item for item in items if tag in (item["username"] or "")]
    assert all(item["username"] for item in mine) and len(mine) == 2, "só alertas viral"
    gone, live = mine
    assert (live["url"], live["views"], live["likes"], live["text"]) == (f"https://x.com/vir_{tag}/status/777", 9000, 300, "Hoje tem live")
    assert live["display_name"] == "Ana Vibes"
    assert (gone["username"], gone["display_name"], gone["views"]) == (f"bia_{tag}", "Bia", 1234)
    assert gone["url"] == f"https://x.com/bia_{tag}/status/888"
