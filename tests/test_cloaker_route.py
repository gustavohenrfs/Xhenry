"""Rota POST /api/cloaker."""

from __future__ import annotations

import base64
import io
import json
import zipfile

import pytest
from conftest import image_bytes
from fastapi.testclient import TestClient

from conftest import admin_row
from app.config import ROOT
from app.routers.auth import current_user
from main import app


@pytest.fixture
def client():
    app.dependency_overrides[current_user] = admin_row
    yield TestClient(app)
    app.dependency_overrides.clear()


def _post(c, data: bytes, ctype: str, name: str = "foto.jpg", **form):
    fields = {"copies": "3", "intensity": "leve"}
    fields.update({k: str(v) for k, v in form.items()})
    return c.post("/api/cloaker", files={"media": (name, data, ctype)}, data=fields)


def test_requires_auth():
    resp = _post(TestClient(app), image_bytes(), "image/jpeg")
    assert resp.status_code == 401


def test_returns_zip_with_manifest(client):
    resp = _post(client, image_bytes(), "image/jpeg")
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "application/zip"
    header = json.loads(base64.b64decode(resp.headers["x-cloaker-manifest"]))
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        names = zf.namelist()
        assert json.loads(zf.read("manifest.json")) == header
    assert sorted(names) == ["foto_01.jpg", "foto_02.jpg", "foto_03.jpg", "manifest.json"]
    assert len({item["sha256"] for item in header}) == 3


def test_invalid_type_400(client):
    resp = _post(client, b"ola", "text/plain", name="nota.txt")
    assert resp.status_code == 400
    assert "Formato não suportado" in resp.json()["detail"]


def test_invalid_copies_400(client):
    resp = _post(client, image_bytes(), "image/jpeg", copies=50)
    assert resp.status_code == 400
    assert "cópias" in resp.json()["detail"]


def test_filename_sanitized(client):
    resp = _post(client, image_bytes("PNG"), "image/png", name="../fóto final.png", copies=1)
    assert resp.status_code == 200, resp.text
    assert 'filename="cloaker-foto-final.zip"' in resp.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        assert "foto-final_01.png" in zf.namelist()


def test_busy_returns_429(client, make_video):
    from app import cloaker

    assert cloaker._VIDEO_SLOTS.acquire(blocking=False)
    try:
        resp = _post(client, make_video(), "video/mp4", name="v.mp4", copies=1)
    finally:
        cloaker._VIDEO_SLOTS.release()
    assert resp.status_code == 429
    assert "Outro vídeo" in resp.json()["detail"]


JOB = "0123456789abcdef0123456789abcdef"


def test_progress_tracked_during_job_and_cleared_after(client, monkeypatch):
    from app.routers import cloaker as route
    from app.cloaker import build_variants as real_build

    seen = {}

    def fake_build(*args, on_progress=None, **kwargs):
        on_progress(0, 2)
        seen["start"] = client.get(f"/api/cloaker/progress/{JOB}").json()
        on_progress(1, 2)
        seen["middle"] = client.get(f"/api/cloaker/progress/{JOB}").json()
        return real_build(*args, on_progress=on_progress, **kwargs)

    monkeypatch.setattr(route, "build_variants", fake_build)
    resp = _post(client, image_bytes(), "image/jpeg", copies=2, job_id=JOB)
    assert resp.status_code == 200, resp.text
    assert seen["start"] == {"stage": "processing", "done": 0, "total": 2}
    assert seen["middle"] == {"stage": "processing", "done": 1, "total": 2}
    assert client.get(f"/api/cloaker/progress/{JOB}").status_code == 404


def test_progress_unknown_job_404(client):
    resp = client.get(f"/api/cloaker/progress/{'f' * 32}")
    assert resp.status_code == 404


def test_invalid_job_id_400(client):
    resp = _post(client, image_bytes(), "image/jpeg", copies=1, job_id="../nao-hex")
    assert resp.status_code == 400


def test_tmp_cleaned(client, make_video):
    tmp = ROOT / "data" / "tmp"
    before = set(tmp.iterdir()) if tmp.exists() else set()
    resp = _post(client, make_video(), "video/mp4", name="v.mp4", copies=1)
    assert resp.status_code == 200, resp.text
    after = set(tmp.iterdir()) if tmp.exists() else set()
    assert after == before
