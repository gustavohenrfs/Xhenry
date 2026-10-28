"""Foto do perfil: guardada pequena (WebP 256 px) e servida com cache, nunca no JSON."""

from __future__ import annotations

import base64
import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.database import SessionLocal
from app.models import User
from app.routers import auth
from app.services import ensure_admin
from main import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def png(size=(1080, 1080)) -> bytes:
    image = Image.new("RGB", size)
    image.putdata([((x * 7) % 256, (x * 3) % 256, 120) for x in range(size[0] * size[1])])
    out = io.BytesIO()
    image.save(out, "PNG")
    return out.getvalue()


def login(monkeypatch) -> tuple[int, dict[str, str]]:
    monkeypatch.setenv("SECRET_KEY", "segredo-de-teste")
    with SessionLocal() as db:
        user = ensure_admin(db)
        return user.id, {"Authorization": f"Bearer {auth.issue_token(user.id, user.token_version or 0)}"}


def test_upload_becomes_small_webp_served_with_cache(client, monkeypatch):
    user_id, h = login(monkeypatch)
    raw = png()
    resp = client.patch("/api/me", data={"nome_exibicao": "Henrique"}, files={"avatar": ("f.png", raw, "image/png")}, headers=h)
    assert resp.status_code == 200, resp.text
    link = resp.json()["avatar_url"]
    assert link.startswith(f"/api/avatar/{user_id}?v=") and "base64" not in resp.text
    assert client.get("/api/me", headers=h).json()["avatar_url"] == link
    photo = client.get(link)
    assert photo.status_code == 200 and photo.headers["content-type"] == "image/webp"
    assert "immutable" in photo.headers["cache-control"]
    assert len(photo.content) < 60_000
    assert Image.open(io.BytesIO(photo.content)).size == (256, 256)
    assert client.get(f"/api/avatar/{user_id}?v=errado").status_code == 404, "sem o v certo, nada"
    assert client.get(f"/api/avatar/{user_id}").status_code == 404


def test_old_big_photo_is_shrunk_on_first_request(client, monkeypatch):
    user_id, h = login(monkeypatch)
    big = io.BytesIO()
    Image.open(io.BytesIO(png((900, 900)))).save(big, "JPEG", quality=98)
    with SessionLocal() as db:
        db.get(User, user_id).avatar_url = "data:image/jpeg;base64," + base64.b64encode(big.getvalue()).decode()
        db.commit()
    link = client.get("/api/me", headers=h).json()["avatar_url"]
    photo = client.get(link)
    assert photo.status_code == 200 and photo.headers["content-type"] == "image/webp"
    with SessionLocal() as db:
        stored = db.get(User, user_id).avatar_url
    assert stored.startswith("data:image/webp;base64,") and len(stored) < 80_000, "gravou a versão pequena"
    assert client.get("/api/me", headers=h).json()["avatar_url"] != link, "foto nova, link novo"


def test_bad_image_is_refused(client, monkeypatch):
    _, h = login(monkeypatch)
    resp = client.patch("/api/me", data={"nome_exibicao": "X"}, files={"avatar": ("f.png", b"nao-e-imagem", "image/png")}, headers=h)
    assert resp.status_code == 400
