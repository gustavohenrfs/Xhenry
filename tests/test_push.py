"""Web Push do sininho: inscrição, botão Testar e o envio dos alertas de views pelo vigia."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from conftest import admin_row
from app import push
from app.database import SessionLocal
from app.models import AlertSent, Folder, PushSubscription, TweetSample, XAccount
from app.routers.auth import current_user
from app.services import ensure_admin, get_settings
from main import app

SUB = {"endpoint": "https://push.example/abc", "keys": {"p256dh": "B" * 87, "auth": "a" * 22}}


@pytest.fixture
def client():
    app.dependency_overrides[current_user] = admin_row
    try:
        with TestClient(app) as c:
            with SessionLocal() as db:
                db.query(PushSubscription).delete()
                # Começa sem alertas pendentes de outros testes.
                newest = db.query(AlertSent.id).order_by(AlertSent.id.desc()).limit(1).scalar()
                get_settings(db).push_last_alert_id = newest or 0
                db.commit()
            yield c
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def sent(monkeypatch):
    """Troca o envio de verdade (pywebpush) por uma lista; devolve as mensagens enviadas."""
    calls: list[tuple[str, dict]] = []

    def fake(db, sub, payload):
        calls.append((sub.endpoint, payload))
        return "gone" if sub.endpoint.endswith("/morta") else "ok"

    monkeypatch.setattr(push, "send_one", fake)
    monkeypatch.setattr("app.routers.push.send_one", fake)
    return calls


def test_public_key_is_stable_and_uncompressed(client):
    first = client.get("/api/push/key").json()["public_key"]
    assert first == client.get("/api/push/key").json()["public_key"]
    assert len(first) == 87  # 65 bytes (0x04 + X + Y) em base64url sem "="


def test_subscribe_test_and_unsubscribe(client, sent):
    assert client.post("/api/push/subscribe", json=SUB).json() == {"ok": True}
    assert client.post("/api/push/subscribe", json=SUB).status_code == 200  # de novo: atualiza, não duplica
    with SessionLocal() as db:
        assert db.query(PushSubscription).count() == 1
    assert client.post("/api/push/test", json={"endpoint": SUB["endpoint"]}).json() == {"ok": True}
    assert sent[0][0] == SUB["endpoint"] and "Teste" in sent[0][1]["title"]
    client.post("/api/push/unsubscribe", json={"endpoint": SUB["endpoint"]})
    resp = client.post("/api/push/test", json={"endpoint": SUB["endpoint"]})
    assert resp.status_code == 404


def test_subscribe_refuses_plain_http(client):
    bad = dict(SUB, endpoint="http://push.example/abc")
    assert client.post("/api/push/subscribe", json=bad).status_code == 400


def make_alert(db, suffix: str = "") -> AlertSent:
    tag = uuid4().hex[:6]
    folder = Folder(owner_id=ensure_admin(db).id, name=f"Push {tag}")
    db.add(folder)
    db.flush()
    account = XAccount(folder_id=folder.id, username=f"push_{tag}", display_name="Ana")
    db.add(account)
    db.flush()
    db.add(TweetSample(
        account_id=account.id, tweet_id="42", posted_at=datetime.now(timezone.utc),
        timezone_used="America/Sao_Paulo", hour_local=10, weekday=0,
        text_preview="Bom dia", view_count=123456, like_count=10,
    ))
    row = AlertSent(owner_id=account.owner_id, dedupe_key=f"viral:{account.id}:42" + (f":{suffix}" if suffix else ""), kind="viral", detail="x")
    db.add(row)
    db.commit()
    return row


def test_watchdog_pushes_only_new_alerts_with_level(client, sent):
    with SessionLocal() as db:
        make_alert(db)
        get_settings(db).push_last_alert_id = None
        db.commit()
        # Primeira rodada: só marca onde está, o histórico não sai.
        assert push.push_new_alerts(db) == 0
        db.add_all([PushSubscription(owner_id=admin_row().id, endpoint=SUB["endpoint"], p256dh="B" * 87, auth="a" * 22),
                    PushSubscription(owner_id=admin_row().id, endpoint="https://push.example/morta", p256dh="B" * 87, auth="a" * 22)])
        db.commit()
        assert push.push_new_alerts(db) == 0
        row_id = make_alert(db, "100k").id
        assert push.push_new_alerts(db) == 1, "a inscrição morta não conta"
        assert push.push_new_alerts(db) == 0, "o mesmo alerta não sai duas vezes"
    assert sorted(endpoint for endpoint, _ in sent) == ["https://push.example/abc", "https://push.example/morta"]
    good = [p for e, p in sent if e == SUB["endpoint"]][0]
    assert good["title"] == "💯 Passou de 100K views"
    assert "123.456 views" in good["body"] and good["url"].endswith("/status/42")
    assert good["tag"] == f"viral-{row_id}" and good["icon"].endswith(".png")


def test_backlog_goes_out_next_minute_and_stale_is_skipped(client, sent):
    subscribed = PushSubscription(owner_id=admin_row().id, endpoint=SUB["endpoint"], p256dh="B" * 87, auth="a" * 22)
    with SessionLocal() as db:
        db.add(subscribed)
        db.commit()
        old = make_alert(db)
        old.sent_at = datetime.now(timezone.utc) - timedelta(hours=7)
        get_settings(db).push_last_alert_id = old.id - 1
        db.commit()
        ids = [make_alert(db).id for _ in range(7)]
        assert push.push_new_alerts(db) == 5, "velho fica de fora, 5 por rodada"
        assert get_settings(db).push_last_alert_id == ids[4]
        assert push.push_new_alerts(db) == 2, "o resto sai no minuto seguinte"
        assert get_settings(db).push_last_alert_id == ids[-1]
        assert push.push_new_alerts(db) == 0
    assert [p["tag"] for _, p in sent] == [f"viral-{i}" for i in ids]


def test_renew_swaps_subscription_without_login(client):
    app.dependency_overrides.clear()  # o service worker chama sem token
    new = {"endpoint": "https://push.example/nova", "keys": {"p256dh": "C" * 87, "auth": "c" * 22}}
    assert client.post("/api/push/renew", json=dict(new, old_endpoint=SUB["endpoint"])).status_code == 404
    with SessionLocal() as db:
        db.add(PushSubscription(owner_id=admin_row().id, endpoint=SUB["endpoint"], p256dh="B" * 87, auth="a" * 22))
        db.commit()
    assert client.post("/api/push/renew", json=dict(new, old_endpoint=SUB["endpoint"])).json() == {"ok": True}
    with SessionLocal() as db:
        rows = db.query(PushSubscription).all()
        assert [(r.endpoint, r.auth) for r in rows] == [(new["endpoint"], "c" * 22)]
        # A página já tinha reinscrito: a antiga some e não duplica.
        db.add(PushSubscription(owner_id=admin_row().id, endpoint=SUB["endpoint"], p256dh="B" * 87, auth="a" * 22))
        db.commit()
    assert client.post("/api/push/renew", json=dict(new, old_endpoint=SUB["endpoint"])).status_code == 200
    with SessionLocal() as db:
        assert [r.endpoint for r in db.query(PushSubscription).all()] == [new["endpoint"]]


def test_viral_feed_reports_level(client):
    with SessionLocal() as db:
        row = make_alert(db, "bombando")
        row_id = row.id
    items = {item["id"]: item for item in client.get("/api/viral?limit=50").json()}
    assert items[row_id]["level"] == "bombando" and items[row_id]["title"] == "POST BOMBANDO"
    assert items[row_id]["emoji"] == "🔥"


def test_service_worker_is_served_from_root(client):
    resp = client.get("/sw.js")
    assert resp.status_code == 200 and resp.headers["service-worker-allowed"] == "/"
    assert "showNotification" in resp.text
    manifest = client.get("/manifest.webmanifest")
    assert manifest.headers["content-type"].startswith("application/manifest+json")
    assert manifest.json()["display"] == "standalone"
