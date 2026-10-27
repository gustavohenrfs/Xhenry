"""Painel ao vivo: os carimbos mudam quando o robô grava, quando chega alerta e quando um coletor muda."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app import live
from app.database import SessionLocal
from app.models import AlertSent, Collector, Folder, TweetSample, XAccount
from app.services import ensure_owner
from main import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def stamp(client):
    return client.get("/api/live?once=1").json()


def test_stamps_change_only_for_what_changed(client):
    first = stamp(client)
    assert set(first) == {"data", "alerts", "collectors"}
    assert stamp(client) == first, "nada mudou, carimbo igual"
    tag = uuid4().hex[:6]
    with SessionLocal() as db:
        folder = Folder(owner_id=ensure_owner(db).id, name=f"Live {tag}")
        db.add(folder)
        db.flush()
        account = XAccount(folder_id=folder.id, username=f"live_{tag}")
        db.add(account)
        db.flush()
        now = datetime.now(timezone.utc)
        db.add(TweetSample(account_id=account.id, tweet_id="1", posted_at=now, timezone_used="America/Sao_Paulo",
                           hour_local=10, weekday=0, view_count=5, collected_at=now + timedelta(seconds=30)))
        db.commit()
    second = stamp(client)
    assert second["data"] != first["data"] and second["alerts"] == first["alerts"]
    with SessionLocal() as db:
        db.add(AlertSent(dedupe_key=f"live:{tag}", kind="viral", detail="x"))
        db.commit()
    third = stamp(client)
    assert third["alerts"] != second["alerts"] and third["data"] == second["data"]
    with SessionLocal() as db:
        db.add(Collector(handle=f"lv{tag}", paused_until=datetime.now(timezone.utc) + timedelta(hours=1)))
        db.commit()
    assert stamp(client)["collectors"] != third["collectors"]


def test_stream_sends_first_stamp_then_stops_on_disconnect(monkeypatch):
    monkeypatch.setattr(live, "CHECK_SECONDS", 0.05)
    async def run():
        calls = {"n": 0}

        async def disconnected():
            calls["n"] += 1
            return calls["n"] > 1  # cliente sai depois do primeiro evento

        events = [chunk async for chunk in live.hub.stream(disconnected)]
        return events

    events = asyncio.run(run())
    assert events and events[0].startswith("data: {") and '"data"' in events[0]
    assert live.hub.listeners == 0
