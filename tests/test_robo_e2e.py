"""robo.py ponta a ponta: schema do painel num Postgres de verdade + anyx falso.

O robô usa SQL de Postgres (NULLS FIRST, make_interval, ::json), então este teste só roda com
ROBO_TEST_DATABASE_URL apontando para um Postgres DESCARTÁVEL: as tabelas são apagadas e recriadas.

    ROBO_TEST_DATABASE_URL=postgresql://postgres@localhost:55432/postgres python -m pytest tests/test_robo_e2e.py

Os testes rodam em ordem e cada um parte do estado deixado pelo anterior (rodadas seguidas do cron).
"""

from __future__ import annotations

import json
import os
import sys
import types
from datetime import datetime, timedelta, timezone

import pytest

PG_URL = os.getenv("ROBO_TEST_DATABASE_URL", "").strip()
pytestmark = pytest.mark.skipif(not PG_URL, reason="defina ROBO_TEST_DATABASE_URL (Postgres descartável)")

TOKEN = "f" * 40
NOW = datetime.now(timezone.utc)


class RateLimited(Exception):
    def __init__(self, msg, reset=None):
        super().__init__(msg)
        self.reset = reset


class NotFound(Exception):
    pass


class SessionExpired(Exception):
    pass


class World:
    """Banco, anyx falso e o que o robô fez na última rodada."""

    def __init__(self):
        self.events: list[tuple] = []
        self.vault: dict[str, object] = {"burner3": [{"name": "auth_token", "value": TOKEN}]}
        self.cfg = {"default_browser": "chromium", "accounts": {}}
        self.behavior: dict[tuple[str, str], str] = {}  # (coletor, conta ou "*") -> falha
        self.sent: list[str] = []
        self.pauses: list[int] = []
        self.proxy_dead = False
        self.ids: dict[str, int] = {}

    def run(self):
        import robo

        self.events.clear()
        assert robo.main() == 0

    def q(self, sql, **params):
        from sqlalchemy import text

        with self.engine.connect() as conn:
            return conn.execute(text(sql), params).all()

    def sql(self, sql):
        from sqlalchemy import text

        with self.engine.begin() as conn:
            conn.execute(text(sql))

    def collector(self, handle):
        return self.q("select last_status, paused_until, last_error from collectors where handle=:h", h=handle)[0]

    def opens(self):
        return [e for e in self.events if e[0] == "open"]

    def timelines(self):
        return [(e[1], e[2]) for e in self.events if e[0] == "timeline"]

    def heartbeats(self):
        return self.q("select count(*) from sync_runs where kind='tweets' and account_id is null")[0][0]

    def last_beat(self):
        return self.q("select status, stats from sync_runs order by id desc limit 1")[0]

    def settings(self, **values):
        from sqlalchemy.orm import Session

        from app import services

        with Session(self.engine) as db:
            row = services.get_settings(db)
            for key, value in values.items():
                setattr(row, key, value)
            db.commit()


def fake_anyx(world: World) -> dict[str, types.ModuleType]:
    class Session:
        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            world.events.append(("open", self.handle, world.cfg["accounts"].get(self.handle, {}).get("proxy")))
            if world.behavior.get((self.handle, "*")) == "expired":
                raise SessionExpired("X showed the login wall")
            return self

        def __exit__(self, *exc):
            world.events.append(("exit", self.handle))
            return False

        def user(self, username):
            world.events.append(("user", self.handle, username))
            return {"followers_count": 777}

        def timeline(self, username, limit=20):
            world.events.append(("timeline", self.handle, username))
            what = world.behavior.get((self.handle, username))
            if what == "429":
                raise RateLimited("429 on UserTweets", reset=int((NOW + timedelta(minutes=20)).timestamp()))
            if what == "blocked":
                raise NotFound(f"nothing loaded at https://x.com/{username} (no UserTweets)")
            if what == "gone":
                raise NotFound(f"@{username} is unavailable (Suspended)")
            return [{"id": f"{username}-{self.handle}", "created_at": NOW.isoformat(), "view_count": 10, "like_count": 1}]

    class Chromium:
        @staticmethod
        def stop(handle):
            world.events.append(("stop", handle))

    anyx = types.ModuleType("anyx")
    anyx.Session = Session
    anyx.__path__ = []
    vault = types.ModuleType("anyx.vault")
    vault.save = lambda handle, cookies: world.vault.__setitem__(handle, cookies)
    vault.load = lambda handle: world.vault.get(handle)
    config = types.ModuleType("anyx.config")
    config.load = lambda: json.loads(json.dumps(world.cfg))
    config.save = lambda cfg: (world.cfg.clear(), world.cfg.update(cfg))
    browsers = types.ModuleType("anyx.browsers")
    browsers.__path__ = []
    chromium = types.ModuleType("anyx.browsers.chromium")
    chromium.Chromium = Chromium
    anyx.vault, anyx.config, anyx.browsers, browsers.chromium = vault, config, browsers, chromium
    return {"anyx": anyx, "anyx.vault": vault, "anyx.config": config, "anyx.browsers": browsers,
            "anyx.browsers.chromium": chromium}


@pytest.fixture(scope="module")
def w(tmp_path_factory):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    import app.models  # noqa: F401
    import robo
    from app import services
    from app.database import Base
    from app.models import Collector, Folder, XAccount

    world = World()
    world.engine = create_engine(PG_URL.replace("postgresql://", "postgresql+psycopg://", 1))
    Base.metadata.drop_all(world.engine)
    Base.metadata.create_all(world.engine)
    with Session(world.engine) as db:
        folder = Folder(owner_id=services.ensure_owner(db).id, name="Dinheiro", color="#FF2EC8")
        db.add(folder)
        db.flush()
        for name in ("conta1", "conta2", "conta3", "conta4"):
            acc = XAccount(folder_id=folder.id, username=name)
            db.add(acc)
            db.flush()
            world.ids[name] = acc.id
        db.add_all([
            Collector(handle="burner1", proxy="http://u:p@127.0.0.1:9", cookies=json.dumps({"auth_token": TOKEN})),
            Collector(handle="burner2", cookies=json.dumps({"auth_token": TOKEN, "ct0": "x"})),
            Collector(handle="burner3"),  # sem cookies: usa o login do anyx (o cofre falso tem)
            Collector(handle="conta4", cookies=json.dumps({"auth_token": TOKEN})),  # @ de uma monitorada
        ])
        db.commit()

    class FakeRelay:
        """Proxy do burner1: sobe sem rede; probe falha com world.proxy_dead."""

        def __init__(self, upstream, timeout=20.0):
            self.upstream, self.failures, self.url = upstream, [], "http://127.0.0.1:40001"
            world.events.append(("relay", upstream))

        def probe(self):
            if world.proxy_dead:
                raise robo.ProxyError("não conectou em 127.0.0.1:9 (recusada)")

        def close(self):
            world.events.append(("relay_close",))

    state = tmp_path_factory.mktemp("robo")
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("DATABASE_URL", PG_URL)
        mp.setenv("ROBO_ENV_FILE", str(state / "sem.env"))  # não lê o .env local
        for name, module in fake_anyx(world).items():
            mp.setitem(sys.modules, name, module)
        mp.setattr(robo, "send_telegram", lambda message: world.sent.append(message) or True)
        mp.setattr(robo, "OFFLINE_STAMP", state / "alerta_banco")
        mp.setattr(robo, "LOCK_FILE", state / "robo.lock")
        mp.setattr(robo, "TIMELINE_RETRY_PAUSE", 0)
        mp.setattr(robo, "pause", lambda: world.pauses.append(1))
        mp.setattr(robo, "ProxyRelay", FakeRelay)
        yield world
    world.engine.dispose()


def test_round_one_collector_per_account(w):
    w.run()
    assert [e[1] for e in w.opens()] == ["burner1", "burner2", "burner3"], "nunca usa o @conta4"
    assert dict((u, h) for h, u in w.timelines()) == {"conta1": "burner1", "conta2": "burner2", "conta3": "burner3"}
    opens = w.opens()
    assert opens[0][2] == "http://127.0.0.1:40001" and ("relay", "http://u:p@127.0.0.1:9") in w.events
    assert opens[1][2] is None
    assert w.vault["burner1"] == {"auth_token": TOKEN}
    assert [e[1] for e in w.events if e[0] == "stop"] == ["burner1", "burner1", "burner2", "burner2", "burner3", "burner3"]
    assert len([e for e in w.events if e[0] == "user"]) == 3
    assert len(w.pauses) == 5  # 3 entre perfil e timeline + 2 entre pares
    assert w.q("select count(*) from tweet_samples")[0][0] == 3
    assert w.q("select followers_count from daily_metrics where account_id=:a", a=w.ids["conta1"]) == [(777,)]
    assert w.q("select collected_at from x_accounts where id=:a", a=w.ids["conta4"])[0][0] is None
    status, stats = w.last_beat()
    assert status == "ok" and stats["contas"] == 3


def test_round_two_rotates_and_followers_hourly(w):
    w.pauses.clear()
    w.run()
    assert [u for _, u in w.timelines()] == ["conta4", "conta1", "conta2"]
    assert [e[2] for e in w.events if e[0] == "user"] == ["conta4"]


def test_block_and_429(w):
    w.behavior[("burner1", "conta3")] = "blocked"
    w.behavior[("burner2", "conta4")] = "429"
    w.sent.clear()
    w.run()
    tl = w.timelines()
    assert tl.count(("burner1", "conta3")) == 2, "bloqueio: 2 tentativas"
    assert tl.count(("burner2", "conta4")) == 1 and not any(h == "burner3" for h, _ in tl), "429 para a rodada"
    st, until, _err = w.collector("burner1")
    assert st == "bloqueio" and timedelta(minutes=25) < until - datetime.now(timezone.utc) <= timedelta(minutes=31)
    st, until, _err = w.collector("burner2")
    assert st == "limite" and abs((until - NOW).total_seconds() - (20 * 60 + 60)) < 5
    front = w.q("select username from x_accounts order by collected_at nulls first, id limit 2")
    assert [r[0] for r in front] == ["conta3", "conta4"]
    assert any(m.startswith("🚫 X bloqueou o coletor @burner1 ao ler @conta3") for m in w.sent)
    assert any(m.startswith("⛔ X limitou o coletor @burner2 (429) ao ler @conta4.\nRodada parada.") for m in w.sent)
    status, stats = w.last_beat()
    assert status == "partial" and stats["parada_429"] is True


def test_paused_collectors_wait(w):
    w.behavior.clear()
    w.run()
    assert [e[1] for e in w.opens()] == ["burner3"]
    assert [u for _, u in w.timelines()] == ["conta3"]


def test_session_and_proxy_down(w):
    w.sql("update collectors set paused_until = null")
    w.behavior[("burner2", "*")] = "expired"
    w.proxy_dead = True
    w.sent.clear()
    w.run()
    assert not any(e[1] == "burner1" for e in w.opens()), "proxy fora: nem abre o navegador"
    assert w.collector("burner1")[0] == "proxy" and any(m.startswith("🔌 Proxy do coletor @burner1") for m in w.sent)
    assert w.collector("burner2")[0] == "sessao" and any(m.startswith("🔑 Coletor @burner2") for m in w.sent)
    assert w.collector("burner3")[0] == "ok"


def test_suspended_account_goes_to_the_back(w):
    w.behavior.clear()
    w.proxy_dead = False
    w.sql("update collectors set paused_until = null")
    first = w.q("select username from x_accounts order by collected_at nulls first, id limit 1")[0][0]
    w.behavior[("burner1", first)] = "gone"
    w.sent.clear()
    w.run()
    assert w.collector("burner1")[0] == "ok"
    assert any(m.startswith("❌ Erro na coleta") and f"@{first} — lida por @burner1" in m for m in w.sent)
    last = w.q("select username from x_accounts order by collected_at desc nulls last limit 3")
    assert last.count((first,)) == 1
    w.behavior.clear()


def test_no_free_collector(w):
    w.sql("update collectors set is_active = false")
    w.sent.clear()
    w.run()
    assert not w.opens()
    assert w.sent == ["⚠️ Robô sem coletor: todos desligados na aba Coletores.\nNenhuma timeline foi lida."]
    w.run()
    assert len(w.sent) == 1, "não repete o alerta"
    assert w.last_beat()[0] == "partial"


# ---- controle pela aba Configurações ----

def test_within_interval_does_nothing(w):
    w.sql("update collectors set is_active = true, paused_until = null")
    w.sql("update sync_runs set finished_at = now() where kind='tweets' and account_id is null")
    w.settings(robo_enabled=True, robo_interval_min=5, robo_run_requested_at=None)
    before = w.heartbeats()
    w.run()
    assert not w.events and w.heartbeats() == before


def test_interval_due_runs_automatic_round(w):
    w.sql("update sync_runs set finished_at = now() - interval '10 minutes'")
    before = w.heartbeats()
    w.run()
    assert len(w.opens()) == 3 and w.heartbeats() == before + 1
    assert w.last_beat()[1]["manual"] is False


def test_disabled_never_runs_alone(w):
    w.sql("update sync_runs set finished_at = now() - interval '10 minutes'")
    w.settings(robo_enabled=False)
    before = w.heartbeats()
    w.run()
    assert not w.events and w.heartbeats() == before


def test_refresh_button_reads_every_account(w):
    w.settings(robo_run_requested_at=datetime.now(timezone.utc))
    w.run()
    tl = w.timelines()
    assert sorted(u for _, u in tl) == ["conta1", "conta2", "conta3", "conta4"]
    assert [h for h, _ in tl] == ["burner1", "burner2", "burner3", "burner1"], "reveza os coletores"
    assert w.q("select robo_run_requested_at from app_settings where id=1")[0][0] is None
    assert w.last_beat()[1]["manual"] is True
    w.run()
    assert not w.events, "sem pedido, desligado fica quieto"


def test_manual_round_skips_benched_collector(w):
    for name in ("conta1", "conta2", "conta3", "conta4"):
        w.behavior[("burner1", name)] = "blocked"
    w.settings(robo_run_requested_at=datetime.now(timezone.utc))
    w.run()
    tl = w.timelines()
    b1 = [u for h, u in tl if h == "burner1"]
    assert len(b1) == 2 and len(set(b1)) == 1, "depois do castigo, o 2º par do burner1 é pulado"
    assert {h for h, _ in tl} == {"burner1", "burner2", "burner3"}
    w.behavior.clear()
