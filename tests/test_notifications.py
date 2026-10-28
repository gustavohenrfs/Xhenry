"""Notificações: preferências por usuário, push filtrado, não perturbe, sininho e aparelhos."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app import push
from app.database import SessionLocal
from app.models import AlertSent, PushSubscription, User
from app.routers import auth
from app.services import ensure_admin, get_settings, in_quiet_hours, notify_prefs
from main import app


@pytest.fixture(autouse=True)
def secret(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "segredo-de-teste")


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def new_user(admin: bool = False) -> tuple[int, dict[str, str]]:
    with SessionLocal() as db:
        if admin:
            user = ensure_admin(db)
        else:
            user = User(email=f"n{uuid4().hex[:8]}@x.com", name="N", is_approved=True)
            db.add(user)
            db.commit()
        return user.id, {"Authorization": f"Bearer {auth.issue_token(user.id, user.token_version or 0)}"}


def alert(owner_id: int | None, kind: str, text: str = "x") -> int:
    with SessionLocal() as db:
        row = AlertSent(owner_id=owner_id, dedupe_key=f"{kind}:{uuid4().hex}", kind=kind, detail=text)
        db.add(row)
        db.commit()
        return row.id


def test_prefs_defaults_and_validation(client):
    _, h = new_user()
    body = client.get("/api/notifications", headers=h).json()
    prefs = body["prefs"]
    assert all(prefs["kinds"].values()) and prefs["post_step"] == 0 and prefs["day_step"] == 0
    assert prefs["quiet"] == {"on": False, "from": "00:00", "to": "08:00"}
    assert "acesso" not in {t["key"] for t in body["types"]}, "pedido de acesso é só do admin"
    assert 10_000 in body["post_steps"] and 50_000 in body["day_steps"]
    assert client.put("/api/notifications", json={"post_step": 123}, headers=h).status_code == 400
    assert client.put("/api/notifications", json={"kinds": {"inventado": True}}, headers=h).status_code == 400
    assert client.put("/api/notifications", json={"quiet": {"from": "25:00"}}, headers=h).status_code == 400
    saved = client.put("/api/notifications", headers=h, json={
        "kinds": {"queda": False}, "post_step": 10_000, "day_step": 50_000,
        "quiet": {"on": True, "from": "22:00", "to": "07:30"},
    }).json()["prefs"]
    assert saved["kinds"]["queda"] is False and saved["kinds"]["viral"] is True
    assert (saved["post_step"], saved["day_step"]) == (10_000, 50_000)
    assert saved["quiet"] == {"on": True, "from": "22:00", "to": "07:30"}
    _, admin = new_user(admin=True)
    assert "acesso" in {t["key"] for t in client.get("/api/notifications", headers=admin).json()["types"]}


@pytest.mark.parametrize(("start", "end", "hour", "quiet"), [
    ("00:00", "08:00", 3, True), ("00:00", "08:00", 9, False),
    ("22:00", "07:00", 23, True), ("22:00", "07:00", 6, True), ("22:00", "07:00", 12, False),
])
def test_quiet_hours_cross_midnight(start, end, hour, quiet):
    now = datetime(2026, 10, 28, (hour + 3) % 24, 30, tzinfo=timezone.utc)  # Brasília = UTC-3
    assert in_quiet_hours({"quiet": {"on": True, "from": start, "to": end}}, now) is quiet
    assert in_quiet_hours({"quiet": {"on": False, "from": start, "to": end}}, now) is False


def test_bell_and_history_show_every_kind_respecting_prefs(client):
    owner, h = new_user()
    viral = alert(owner, "viral", "🚀 Post viralizando!\n👤 Conta: Ana (@ana)\n👁 Views: 600")
    drop = alert(owner, "queda", "📉 Queda de views\n👤 Conta: Ana (@ana)\n👁 Hoje: 10")
    mark = alert(owner, "marco_post", "📈 Post passou de 10.000 views\n👤 Conta: Ana (@ana)\n👁 Views: 12.345")
    bell = {item["id"]: item for item in client.get("/api/viral", headers=h).json()}
    assert {viral, drop, mark} <= set(bell)
    assert bell[mark]["title"] == "Post passou de 10.000 views" and bell[mark]["views"] == 12345
    assert bell[drop]["emoji"] == "📉" and bell[drop]["username"] == "ana"
    client.put("/api/notifications", json={"kinds": {"queda": False}}, headers=h)
    assert drop not in {item["id"] for item in client.get("/api/viral", headers=h).json()}, "desligado some do sininho"
    history = client.get("/api/alerts?limit=50", headers=h).json()
    assert {"viral", "queda", "marco_post"} <= {row["kind"] for row in history}, "o histórico guarda tudo"
    views = client.get("/api/alerts?limit=50&group=views", headers=h).json()
    assert {row["kind"] for row in views} == {"viral", "marco_post"}
    assert client.get("/api/alerts?group=xyz", headers=h).status_code == 400


def test_push_respects_kinds_quiet_hours_and_sends_system_to_admin(client, monkeypatch):
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(push, "send_one", lambda db, sub, payload: sent.append((sub.endpoint, payload["title"])) or "ok")
    friend, h = new_user()
    admin_id, _ = new_user(admin=True)
    with SessionLocal() as db:
        db.query(PushSubscription).delete()
        get_settings(db).push_last_alert_id = db.query(AlertSent.id).order_by(AlertSent.id.desc()).limit(1).scalar() or 0
        db.add_all([PushSubscription(owner_id=friend, endpoint="https://p.example/f", p256dh="B" * 87, auth="a" * 22),
                    PushSubscription(owner_id=admin_id, endpoint="https://p.example/a", p256dh="B" * 87, auth="a" * 22)])
        db.commit()
    client.put("/api/notifications", json={"kinds": {"queda": False}}, headers=h)
    alert(friend, "queda", "📉 Queda de views")
    alert(friend, "meta", "🎯 Meta do dia batida\n👁 Views hoje: 60.000")
    alert(None, "silencio", "🔇 Robô parado\nNenhuma leitura há 40 min")
    with SessionLocal() as db:
        push.push_new_alerts(db)
    assert ("https://p.example/f", "🎯 Meta do dia batida") in sent
    assert not [s for s in sent if "Queda" in s[1]], "tipo desligado não apita"
    assert ("https://p.example/a", "🔇 Robô parado") in sent, "alerta do sistema vai para o admin"
    assert ("https://p.example/f", "🔇 Robô parado") not in sent
    sent.clear()
    client.put("/api/notifications", json={"quiet": {"on": True, "from": "00:00", "to": "23:59"}}, headers=h)
    alert(friend, "meta", "🎯 Meta do dia batida")
    with SessionLocal() as db:
        push.push_new_alerts(db)
        db.query(PushSubscription).delete()
        db.commit()
    assert not [s for s in sent if s[0].endswith("/f")], "não perturbe: fica só no histórico"


def test_devices_list_and_remove(client):
    owner, h = new_user()
    other, _ = new_user()
    with SessionLocal() as db:
        mine = PushSubscription(owner_id=owner, endpoint="https://p.example/" + uuid4().hex, p256dh="B" * 87,
                                auth="a" * 22, user_agent="Edge")
        theirs = PushSubscription(owner_id=other, endpoint="https://p.example/" + uuid4().hex, p256dh="B" * 87, auth="a" * 22)
        db.add_all([mine, theirs])
        db.commit()
        mine_id, theirs_id, tail = mine.id, theirs.id, mine.endpoint[-16:]
    devices = client.get("/api/push/devices", headers=h).json()
    assert [(d["id"], d["user_agent"], d["tail"]) for d in devices] == [(mine_id, "Edge", tail)]
    assert "endpoint" not in devices[0]
    assert client.delete(f"/api/push/devices/{theirs_id}", headers=h).status_code == 404
    assert client.delete(f"/api/push/devices/{mine_id}", headers=h).status_code == 204
    assert client.get("/api/push/devices", headers=h).json() == []
    with SessionLocal() as db:
        db.delete(db.get(PushSubscription, theirs_id))
        db.commit()


def test_access_request_is_an_admin_alert_not_telegram(client, monkeypatch):
    monkeypatch.setattr(auth, "send_telegram", lambda *_a: pytest.fail("pedido de acesso não vai ao Telegram"))
    admin_id, admin = new_user(admin=True)
    invite = client.post("/api/admin/invites", json={}, headers=admin).json()
    token = invite["link"].split("#invite=")[1]
    email = f"pedido{uuid4().hex[:6]}@x.com"
    body = {"nome": "Bia", "email": email, "senha": "12345678", "invite": token}
    assert client.post("/api/register", json=body).status_code == 201
    with SessionLocal() as db:
        row = db.query(AlertSent).filter(AlertSent.kind == "acesso").order_by(AlertSent.id.desc()).first()
        assert row.owner_id == admin_id and email in row.detail
    assert notify_prefs(None)["kinds"]["acesso"] is True
