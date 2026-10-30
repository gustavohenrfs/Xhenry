"""Multiusuário: convite, aprovação, admin e isolamento dos dados entre usuários."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app import push, services
from app.config import LOCAL_EMAIL
from app.database import SessionLocal
from app.models import AlertSent, Folder, Invite, PushSubscription, TweetSample, User, UserSetting, XAccount
from app.routers import auth
from app.services import ensure_admin, get_settings
from main import app


@pytest.fixture(autouse=True)
def secret(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "segredo-de-teste")
    monkeypatch.delenv("ALLOW_REGISTRATION", raising=False)
    monkeypatch.setattr(auth, "telegram_creds", lambda _db: ("", ""))


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def bearer(user_id: int) -> dict[str, str]:
    with SessionLocal() as db:
        user = db.get(User, user_id)
        return {"Authorization": f"Bearer {auth.issue_token(user.id, user.token_version or 0)}"}


def admin_headers() -> dict[str, str]:
    with SessionLocal() as db:
        return bearer(ensure_admin(db).id)


def make_user(approved: bool = True) -> int:
    with SessionLocal() as db:
        user = User(email=f"amigo{uuid4().hex[:8]}@x.com", name="Amigo", senha_hash=auth.hash_password("12345678"),
                    is_approved=approved)
        db.add(user)
        db.commit()
        return user.id


def make_account(owner_id: int, views: int = 100) -> tuple[int, int]:
    """Pasta + conta + um post de hoje + um alerta viral, tudo do mesmo dono. Devolve (pasta, conta)."""
    tag = uuid4().hex[:6]
    with SessionLocal() as db:
        folder = Folder(owner_id=owner_id, name=f"Pasta {tag}")
        db.add(folder)
        db.flush()
        account = XAccount(folder_id=folder.id, username=f"u{tag}")
        db.add(account)
        db.flush()
        db.add(TweetSample(account_id=account.id, tweet_id=f"9{tag}"[:7].replace("a", "1"), posted_at=datetime.now(timezone.utc),
                           timezone_used="America/Sao_Paulo", hour_local=10, weekday=0, view_count=views))
        db.add(AlertSent(owner_id=owner_id, dedupe_key=f"viral:{account.id}:1", kind="viral", detail="🚀"))
        db.commit()
        assert account.owner_id == owner_id, "a conta herda o dono da pasta"
        return folder.id, account.id


def test_invite_register_wait_approve_and_revoke(client):
    admin = admin_headers()
    invite = client.post("/api/admin/invites", json={"note": "João"}, headers=admin)
    assert invite.status_code == 201, invite.text
    link = invite.json()["link"]
    token = link.split("#invite=")[1]
    assert client.get(f"/api/config?invite={token}").json()["invite_valid"] is True

    email = f"joao{uuid4().hex[:6]}@x.com"
    body = {"nome": "João", "email": email, "senha": "12345678", "invite": token}
    created = client.post("/api/register", json=body)
    assert created.status_code == 201, created.text
    assert created.json()["is_approved"] is False and created.json()["is_admin"] is False
    again = client.post("/api/register", json=dict(body, email="outro" + email))
    assert again.status_code == 403, "convite é de uso único"
    assert client.get(f"/api/config?invite={token}").json()["invite_valid"] is False

    login = client.post("/api/login", json={"email": email, "senha": "12345678"})
    assert login.status_code == 200, "pendente entra, mas só vê a tela de espera"
    friend = {"Authorization": f"Bearer {login.json()['access_token']}"}
    assert client.get("/api/me", headers=friend).json()["is_approved"] is False
    waiting = client.get("/api/dashboard?period=today", headers=friend)
    assert waiting.status_code == 403 and waiting.headers["x-access"] == "pending"

    users = client.get("/api/admin/users", headers=admin).json()
    pending = [row for row in users["pending"] if row["email"] == email]
    assert pending, "o pedido aparece para o admin"
    user_id = pending[0]["id"]
    assert client.post(f"/api/admin/users/{user_id}/approve", headers=admin).status_code == 200
    assert client.get("/api/dashboard?period=today", headers=friend).status_code == 200

    assert client.post(f"/api/admin/users/{user_id}/revoke", headers=admin).status_code == 200
    assert client.get("/api/dashboard?period=today", headers=friend).status_code == 401, "revogar derruba a sessão"
    used = [row for row in client.get("/api/admin/invites", headers=admin).json() if row["id"] == invite.json()["id"]]
    assert used[0]["status"] == "usado" and used[0]["used_by"] == email and used[0]["link"] is None


def test_expired_or_cancelled_invite_is_refused(client):
    with SessionLocal() as db:
        db.add(Invite(token="vencido-" + uuid4().hex, expires_at=datetime.now(timezone.utc) - timedelta(minutes=1)))
        db.commit()
        expired = db.query(Invite).order_by(Invite.id.desc()).first().token
    body = {"nome": "X", "email": f"x{uuid4().hex[:6]}@x.com", "senha": "12345678"}
    assert client.post("/api/register", json=dict(body, invite=expired)).status_code == 403
    assert client.post("/api/register", json=body).status_code == 403, "sem convite continua fechado"
    admin = admin_headers()
    invite = client.post("/api/admin/invites", json={}, headers=admin).json()
    assert client.delete(f"/api/admin/invites/{invite['id']}", headers=admin).status_code == 204
    token = invite["link"].split("#invite=")[1]
    assert client.post("/api/register", json=dict(body, invite=token)).status_code == 403


def test_users_never_see_each_other(client):
    a, b = make_user(), make_user()
    a_folder, a_account = make_account(a, views=111)
    b_folder, _ = make_account(b, views=222)
    ha, hb = bearer(a), bearer(b)

    folders_b = {row["id"] for row in client.get("/api/folders", headers=hb).json()}
    assert folders_b == {b_folder}
    dash_a = client.get("/api/dashboard?period=today", headers=ha).json()
    dash_b = client.get("/api/dashboard?period=today", headers=hb).json()
    assert dash_a["kpis"]["views"] == 111 and dash_b["kpis"]["views"] == 222, "cache separado por dono"
    assert [card["id"] for card in dash_a["accounts"]] == [a_account]
    assert client.get(f"/api/dashboard?period=today&folder_id={a_folder}", headers=hb).status_code == 404
    assert len(client.get("/api/viral", headers=hb).json()) == 1
    assert all("viral" == row["kind"] for row in client.get("/api/alerts", headers=hb).json())
    assert len(client.get("/api/alerts", headers=hb).json()) == 1, "nem os do sistema, nem os do outro"

    # Por id, o que é do outro não existe.
    assert client.get(f"/api/accounts/{a_account}/detail", headers=hb).status_code == 404
    assert client.patch(f"/api/accounts/{a_account}", json={"display_name": "invadido"}, headers=hb).status_code == 404
    assert client.patch(f"/api/accounts/{a_account}", json={"is_archived": True}, headers=hb).status_code == 404
    assert client.delete(f"/api/accounts/{a_account}", headers=hb).status_code == 404
    assert client.post("/api/accounts", json={"folder_id": a_folder, "username": "intruso"}, headers=hb).status_code == 404
    assert client.patch(f"/api/folders/{a_folder}", json={"name": "minha"}, headers=hb).status_code == 404
    assert client.delete(f"/api/folders/{a_folder}", headers=hb).status_code == 404
    assert client.get(f"/api/check/qualquer?folder_id={a_folder}", headers=hb).status_code == 404
    with SessionLocal() as db:
        assert db.get(XAccount, a_account).display_name is None and db.get(Folder, a_folder) is not None

    # Arquivadas também são por dono.
    assert client.patch(f"/api/accounts/{a_account}", json={"is_archived": True}, headers=ha).status_code == 200
    assert [row["id"] for row in client.get("/api/accounts/archived", headers=ha).json()] == [a_account]
    assert client.get("/api/accounts/archived", headers=hb).json() == []


def test_admin_only_routes_refuse_friends(client):
    friend = bearer(make_user())
    for method, path in [("get", "/api/robo"), ("post", "/api/robo/run"),
                         ("get", "/api/robo/health"), ("get", "/api/admin/users"), ("post", "/api/admin/invites"),
                         ]:
        resp = client.request(method, path, headers=friend, json={} if method == "post" else None)
        assert resp.status_code == 403, (path, resp.text)


def test_admin_cannot_remove_itself(client):
    admin = admin_headers()
    with SessionLocal() as db:
        admin_id = ensure_admin(db).id
    assert client.post(f"/api/admin/users/{admin_id}/revoke", headers=admin).status_code == 400
    assert client.delete(f"/api/admin/users/{admin_id}", headers=admin).status_code == 400
    assert client.delete("/api/me", headers=admin).status_code == 400
    approved = make_user()
    assert client.delete(f"/api/admin/users/{approved}", headers=admin).status_code == 400, "revogar antes"
    pending = make_user(approved=False)
    assert client.delete(f"/api/admin/users/{pending}", headers=admin).status_code == 204


def test_settings_and_keys_are_per_user(client, monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "chave-do-admin")
    monkeypatch.setenv("X_BEARER_TOKEN", "bearer-do-admin")
    a, b = bearer(make_user()), bearer(make_user())
    saved = client.put("/api/settings", json={"impression_goal": 777, "apify_token": "apify_api_xyz"}, headers=a).json()
    assert saved["impression_goal"] == 777 and saved["apify_token_set"] is True
    assert "apify_api_xyz" not in str(saved), "chave nunca volta"
    assert saved["apify"]["requested_at"], "token novo pede leitura na hora"
    other = client.get("/api/settings", headers=b).json()
    assert other["impression_goal"] == 500 and other["apify_token_set"] is False
    # A chave do .env é do admin: amigo sem chave própria não usa.
    assert other["xai_key_set"] is False and other["x_token_set"] is False and other["telegram_ready"] is False
    assert client.post("/api/posts/review", data={"caption": "oi"}, headers=b).status_code == 503
    assert client.post("/api/check-all", headers=b).status_code == 503
    assert client.get("/api/settings", headers=admin_headers()).json()["xai_key_set"] is True
    cleared = client.put("/api/settings", json={"apify_token": ""}, headers=a).json()
    assert cleared["apify_token_set"] is False and cleared["impression_goal"] == 777


def test_apify_refresh_needs_token(client):
    friend = bearer(make_user())
    assert client.get("/api/apify", headers=friend).json()["token_set"] is False
    assert client.post("/api/apify/run", headers=friend).status_code == 400
    client.put("/api/settings", json={"x_bearer_token": "meu"}, headers=friend)
    client.put("/api/settings", json={"apify_token": "tok"}, headers=friend)
    with SessionLocal() as db:
        row = db.query(UserSetting).order_by(UserSetting.id.desc()).first()
        row.apify_run_requested_at = None
        db.commit()
    state = client.post("/api/apify/run", headers=friend).json()
    assert state["token_set"] is True and state["requested_at"]


def test_push_goes_only_to_the_owner(client, monkeypatch):
    sent: list[str] = []
    monkeypatch.setattr(push, "send_one", lambda db, sub, payload: sent.append(sub.endpoint) or "ok")
    a, b = make_user(), make_user()
    with SessionLocal() as db:
        db.query(PushSubscription).delete()
        newest = db.query(AlertSent.id).order_by(AlertSent.id.desc()).limit(1).scalar()
        get_settings(db).push_last_alert_id = newest
        db.add_all([
            PushSubscription(owner_id=a, endpoint="https://push.example/a", p256dh="B" * 87, auth="a" * 22),
            PushSubscription(owner_id=b, endpoint="https://push.example/b", p256dh="B" * 87, auth="a" * 22),
        ])
        db.commit()
    make_account(a)
    with SessionLocal() as db:
        assert push.push_new_alerts(db) == 1
        db.query(PushSubscription).delete()
        db.commit()
    assert sent == ["https://push.example/a"]


def test_alerts_never_go_to_telegram(monkeypatch):
    monkeypatch.setattr(services, "send_telegram", lambda *_a: pytest.fail("alerta não vai mais ao Telegram"))
    friend = make_user()
    with SessionLocal() as db:
        admin_id = ensure_admin(db).id
        tag = uuid4().hex
        assert services.record_alert(db, f"t:{tag}:amigo", "ban", "do amigo", friend) is True
        assert services.record_alert(db, f"t:{tag}:admin", "ban", "do admin", admin_id) is True
        assert services.record_alert(db, f"t:{tag}:sistema", "silencio", "do sistema") is True
        assert services.record_alert(db, f"t:{tag}:sistema", "silencio", "repetido") is False, "uma vez por chave"
        owners = dict(db.query(AlertSent.dedupe_key, AlertSent.owner_id).filter(AlertSent.dedupe_key.like(f"t:{tag}:%")).all())
    assert owners == {f"t:{tag}:amigo": friend, f"t:{tag}:admin": admin_id, f"t:{tag}:sistema": None}


def test_live_stamps_are_per_user(client):
    a, b = make_user(), make_user()
    first_a = client.get("/api/live?once=1", headers=bearer(a)).json()
    first_b = client.get("/api/live?once=1", headers=bearer(b)).json()
    make_account(a)
    assert client.get("/api/live?once=1", headers=bearer(a)).json()["alerts"] != first_a["alerts"]
    assert client.get("/api/live?once=1", headers=bearer(b)).json()["alerts"] == first_b["alerts"]
    # O EventSource não manda cabeçalho: o token vai na URL.
    token = bearer(a)["Authorization"].split()[1]
    assert client.get(f"/api/live?once=1&token={token}").status_code == 200
    pending = bearer(make_user(approved=False))
    assert client.get("/api/live?once=1", headers=pending).status_code == 403


def test_admin_takes_over_the_legacy_owner():
    """Banco do tempo de um usuário só: tudo do local@suzana vai para o admin, sem mexer duas vezes."""
    with SessionLocal() as db:
        admin = ensure_admin(db)
        legacy = db.query(User).filter(User.email == LOCAL_EMAIL).first() or User(email=LOCAL_EMAIL, name="XHenry")
        db.add(legacy)
        db.flush()
        name = f"Antiga {uuid4().hex[:6]}"
        db.add_all([Folder(owner_id=admin.id, name=name), Folder(owner_id=legacy.id, name=name)])
        db.flush()
        moved = Folder(owner_id=legacy.id, name=f"Só dela {uuid4().hex[:6]}")
        db.add(moved)
        db.flush()
        account = XAccount(folder_id=moved.id, username=f"leg{uuid4().hex[:6]}")
        db.add(account)
        db.flush()
        alert = AlertSent(dedupe_key=f"viral:{account.id}:5", kind="viral", detail="x")
        sub = PushSubscription(endpoint=f"https://push.example/{uuid4().hex}", p256dh="B" * 87, auth="a" * 22)
        db.add_all([alert, sub])
        db.commit()
        ids = (legacy.id, moved.id, account.id, alert.id, sub.id)
    for _ in range(2):
        with SessionLocal() as db:
            ensure_admin(db)
    with SessionLocal() as db:
        admin = ensure_admin(db)
        legacy_id, folder_id, account_id, alert_id, sub_id = ids
        assert db.query(Folder).filter(Folder.owner_id == legacy_id).count() == 0
        names = [row.name for row in db.query(Folder).filter(Folder.owner_id == admin.id)]
        assert f"{name} (antiga)" in names and name in names, "nome repetido ganha sufixo"
        assert db.get(XAccount, account_id).owner_id == admin.id
        assert db.get(AlertSent, alert_id).owner_id == admin.id
        assert db.get(PushSubscription, sub_id).owner_id == admin.id
        assert admin.is_admin and admin.is_approved
        db.delete(db.get(PushSubscription, sub_id))
        db.commit()


def test_admin_apify_interval_and_token_request_a_read(client):
    admin = admin_headers()
    try:
        robo = client.get("/api/robo", headers=admin).json()
        assert robo["apify"]["interval_min"] == 30 and 15 in robo["apify"]["intervals"]
        assert client.put("/api/robo", json={"apify_interval_min": 5}, headers=admin).status_code == 400, "abaixo de 15 min não"
        assert client.put("/api/robo", json={"apify_interval_min": 60}, headers=admin).json()["apify"]["interval_min"] == 60
        with SessionLocal() as db:
            get_settings(db).robo_run_requested_at = None
            db.commit()
        saved = client.put("/api/settings", json={"apify_token": "tok-admin"}, headers=admin).json()
        assert saved["apify_token_set"] is True and saved["apify"]["requested_at"] is None
        robo = client.get("/api/robo", headers=admin).json()
        assert robo["apify"]["active"] is True and robo["requested_at"], "token novo do admin: o robô lê já"
        health = client.get("/api/robo/health", headers=admin).json()
        assert health["late_after_min"] == 120, "pelo Apify a volta é um intervalo só (2 x 60)"
    finally:
        client.put("/api/settings", json={"apify_token": ""}, headers=admin)
        client.put("/api/robo", json={"apify_interval_min": 30}, headers=admin)


def test_watchdog_waits_the_apify_interval(monkeypatch):
    from app import watchdog

    sent: list[str] = []
    monkeypatch.setattr(watchdog, "record_alert", lambda db, key, kind, text: sent.append(text) or True)
    with SessionLocal() as db:
        admin = ensure_admin(db)
        settings = get_settings(db)
        settings.robo_enabled, settings.apify_interval_min = True, 60
        settings.robo_run_requested_at = None
        row = services.user_settings(db, admin.id)
        row.apify_token = "tok"
        row.pause_on = False  # o teste passa a hora real (now): sem a pausa da madrugada no meio
        db.commit()
        now = datetime.now(timezone.utc)
        monkeypatch.setattr(watchdog, "last_robot_beat", lambda _db: now - timedelta(minutes=40))
        assert watchdog.check_silence(db, now) is False, "40 min sem batimento é normal com Apify a cada 60"
        settings.apify_interval_min = 15
        db.commit()
        assert watchdog.check_silence(db, now) is True, "Apify a cada 15 min: 40 min é robô parado"
        settings.apify_interval_min = 30
        db.commit()


def test_friend_reading_schedule_and_instant_read(client, monkeypatch):
    from app import reader

    friend = bearer(make_user())
    state = client.get("/api/apify", headers=friend).json()
    assert state["auto"] is True and state["interval_min"] == 60 and 15 in state["intervals"] and state["running"] is False
    assert client.put("/api/apify", json={"interval_min": 7}, headers=friend).status_code == 400
    saved = client.put("/api/apify", json={"auto": False, "interval_min": 120}, headers=friend).json()
    assert saved["auto"] is False and saved["interval_min"] == 120
    client.put("/api/settings", json={"apify_token": "tok"}, headers=friend)
    with SessionLocal() as db:
        db.query(UserSetting).filter(UserSetting.apify_token == "tok").update({"apify_run_requested_at": None})
        db.commit()
    started: list[tuple[int, bool]] = []
    monkeypatch.setattr(reader, "start", lambda owner_id, is_admin: started.append((owner_id, is_admin)) or True)
    monkeypatch.setattr(reader, "running", lambda owner_id: True)
    state = client.post("/api/apify/run", headers=friend).json()
    assert started and started[0][1] is False, "lê na hora pelo painel"
    assert state["requested_at"] is None and state["running"] is True, "sem pedido para o cron (não lê duas vezes)"


def test_admin_refresh_reads_now_and_needs_a_token(client, monkeypatch):
    from app import reader

    admin = admin_headers()
    started: list[tuple[int, bool]] = []
    monkeypatch.setattr(reader, "start", lambda owner_id, is_admin: started.append((owner_id, is_admin)) or True)
    try:
        with SessionLocal() as db:
            get_settings(db).robo_run_requested_at = None
            db.commit()
        client.put("/api/settings", json={"apify_token": "tok-admin"}, headers=admin)
        with SessionLocal() as db:
            get_settings(db).robo_run_requested_at = None
            db.commit()
        robo = client.post("/api/robo/run", headers=admin).json()
        assert started and started[0][1] is True and robo["requested_at"] is None and robo["asked_at"]
        client.put("/api/settings", json={"apify_token": ""}, headers=admin)
        started.clear()
        resp = client.post("/api/robo/run", headers=admin)
        assert resp.status_code == 400 and "Apify" in resp.json()["detail"], "sem token não há por onde ler"
        assert not started and client.get("/api/robo", headers=admin).json()["requested_at"] is None
    finally:
        client.put("/api/settings", json={"apify_token": ""}, headers=admin)
        with SessionLocal() as db:
            get_settings(db).robo_run_requested_at = None
            db.commit()


def test_reader_without_postgres_leaves_it_to_the_cron():
    from app import reader

    assert reader.available() is False and reader.start(1, False) is False and reader.running(1) is False
