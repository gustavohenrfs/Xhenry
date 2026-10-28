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
import time
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


class SentAlerts:
    """Textos gravados em alerts_sent desde o último clear(), na ordem (antes era o Telegram)."""

    def __init__(self, world):
        self.world, self.mark = world, 0
        self.clear()

    def clear(self):
        self.mark = self.world.q("select coalesce(max(id), 0) from alerts_sent")[0][0]

    def _texts(self):
        return [row[0] or "" for row in self.world.q("select detail from alerts_sent where id > :m order by id", m=self.mark)]

    def __iter__(self):
        return iter(self._texts())

    def __len__(self):
        return len(self._texts())

    def __getitem__(self, index):
        return self._texts()[index]

    def __eq__(self, other):
        return self._texts() == list(other)


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
        # Modo paralelo: navegadores abertos agora, o máximo ao mesmo tempo e se um coletor abriu dois.
        import threading
        self.lock = threading.Lock()
        self.live: set[str] = set()
        self.max_live = 0
        self.overlap = False
        self.delay = 0.0          # segundos de "leitura" da timeline
        self.config_delay = 0.0   # config.json lento: alarga a janela da corrida entre leituras

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
            # Como o anyx: o Chrome lê o proxy do config.json na hora de abrir.
            world.events.append(("open", self.handle, config.load()["accounts"].get(self.handle, {}).get("proxy")))
            if world.behavior.get((self.handle, "*")) == "expired":
                raise SessionExpired("X showed the login wall")
            with world.lock:
                world.overlap = world.overlap or self.handle in world.live
                world.live.add(self.handle)
                world.max_live = max(world.max_live, len(world.live))
            return self

        def __exit__(self, *exc):
            with world.lock:
                world.live.discard(self.handle)
            world.events.append(("exit", self.handle))
            return False

        def user(self, username):
            world.events.append(("user", self.handle, username))
            return {"followers_count": 777}

        def timeline(self, username, limit=20):
            world.events.append(("timeline", self.handle, username))
            if world.delay:
                time.sleep(world.delay)
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

    def load_config():
        snapshot = json.loads(json.dumps(world.cfg))
        if world.config_delay:
            time.sleep(world.config_delay)
        return snapshot

    config.load = load_config
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
        folder = Folder(owner_id=services.ensure_admin(db).id, name="Dinheiro", color="#FF2EC8")
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

    import itertools

    ports = itertools.count(40001)

    class FakeRelay:
        """Proxy do burner1: sobe sem rede; probe falha com world.proxy_dead. Como o de verdade, cada
        leitura ganha uma porta nova (uma cópia velha do config.json aponta para porta errada)."""

        def __init__(self, upstream, timeout=20.0):
            self.upstream, self.failures, self.url = upstream, [], f"http://127.0.0.1:{next(ports)}"
            world.events.append(("relay", upstream))
            world.events.append(("relay_url", self.url))

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
        world.sent = SentAlerts(world)  # alertas só no banco (o painel manda por push)
        mp.setattr(robo, "OFFLINE_STAMP", state / "alerta_banco")
        mp.setattr(robo, "LOCK_FILE", state / "robo.lock")
        mp.setattr(robo, "READS_FILE", state / "leituras.json")
        # Os testes abaixo seguem a ordem exata de uma rodada em sequência e sem descanso; o modo
        # paralelo, o descanso e o limite do dia têm testes próprios no fim do arquivo.
        mp.setenv("ROBO_WORKERS", "1")
        mp.setenv("ROBO_COLLECTOR_REST_MIN", "0")
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
    assert any(m.startswith("❌ Erro na leitura") and f"@{first} — lida por @burner1" in m for m in w.sent)
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


# ---- leituras em paralelo e proteção dos coletores ----

def relay_urls(w):
    return [e[1] for e in w.events if e[0] == "relay_url"]


def due_round(w):
    """Coleta automática ligada, intervalo vencido e todos os coletores livres e descansados."""
    w.behavior.clear()
    w.proxy_dead = False
    w.sql("update collectors set is_active = true, paused_until = null, last_used_at = null")
    w.sql("update sync_runs set finished_at = now() - interval '30 minutes'")
    w.settings(robo_enabled=True, robo_interval_min=5, robo_run_requested_at=None)
    w.max_live, w.overlap = 0, False
    import robo
    robo.READS_FILE.unlink(missing_ok=True)


def test_parallel_round_reads_at_once_with_distinct_collectors(w, monkeypatch):
    monkeypatch.setenv("ROBO_WORKERS", "4")
    due_round(w)
    w.delay = 0.3
    w.run()
    w.delay = 0.0
    tl = w.timelines()
    assert len(tl) == 3 and {h for h, _ in tl} == {"burner1", "burner2", "burner3"}
    assert len({u for _, u in tl}) == 3, "cada conta uma vez"
    assert w.max_live >= 2, "as leituras correram juntas"
    assert not w.overlap, "nenhum coletor com dois navegadores ao mesmo tempo"
    assert [e[2] for e in w.opens() if e[1] == "burner1"] == relay_urls(w), "burner1 sempre pelo proxy da leitura"
    status, stats = w.last_beat()
    assert status == "ok" and stats["contas"] == 3 and stats["em_paralelo"] == 3


def test_proxy_never_lost_when_opening_in_parallel(w, monkeypatch):
    """O config.json do anyx é um só: sem a trava, a gravação de um coletor apagava o proxy de outro
    e aquele Chrome abria pelo IP do VPS. Com o arquivo lento, a corrida apareceria aqui."""
    monkeypatch.setenv("ROBO_WORKERS", "4")
    for _ in range(3):
        due_round(w)
        w.config_delay = 0.05
        w.run()
        w.config_delay = 0.0
        burner1 = [e[2] for e in w.opens() if e[1] == "burner1"]
        assert burner1 == relay_urls(w) and len(burner1) == 1, (burner1, relay_urls(w))


def test_rest_keeps_recent_collectors_out_without_alert(w, monkeypatch):
    monkeypatch.setenv("ROBO_WORKERS", "4")
    monkeypatch.setenv("ROBO_COLLECTOR_REST_MIN", "10")
    due_round(w)
    w.sql("update collectors set last_used_at = now() - interval '3 minutes' where handle in ('burner1', 'burner2')")
    w.sql("update collectors set last_used_at = now() - interval '30 minutes' where handle = 'burner3'")
    w.run()
    assert [e[1] for e in w.opens()] == ["burner3"], "só quem descansou 10 min"
    w.sql("update sync_runs set finished_at = now() - interval '30 minutes'")
    w.sent.clear()
    w.run()
    assert not w.opens() and not w.sent, "todos descansando: espera quieto, sem alerta"
    assert w.last_beat()[1]["descansando"] == 3


def test_daily_limit_stops_collector_and_alerts_once(w, monkeypatch):
    import robo

    monkeypatch.setenv("ROBO_WORKERS", "4")
    monkeypatch.setenv("ROBO_COLLECTOR_DAILY_MAX", "2")
    due_round(w)
    w.sent.clear()
    w.run()
    w.sql("update collectors set last_used_at = null")
    w.sql("update sync_runs set finished_at = now() - interval '30 minutes'")
    w.run()
    reads = robo.reads_today()
    assert sorted(reads.values()) == [2, 2, 2]
    w.sql("update sync_runs set finished_at = now() - interval '30 minutes'")
    w.run()
    assert not w.opens(), "limite do dia: nenhum coletor abre"
    assert [m for m in w.sent if m.startswith("🧯")], "avisa que todos bateram o limite"
    w.sql("update sync_runs set finished_at = now() - interval '30 minutes'")
    before = len(w.sent)
    w.run()
    assert len(w.sent) == before, "o aviso do limite sai uma vez por dia"


def test_manual_round_in_parallel_reads_everything(w, monkeypatch):
    monkeypatch.setenv("ROBO_WORKERS", "4")
    due_round(w)
    w.settings(robo_run_requested_at=datetime.now(timezone.utc))
    w.delay = 0.2
    w.run()
    w.delay = 0.0
    tl = w.timelines()
    assert sorted(u for _, u in tl) == ["conta1", "conta2", "conta3", "conta4"]
    assert not w.overlap and w.max_live >= 2
    assert w.last_beat()[1]["manual"] is True


# ---- Multiusuário: contas dos outros usuários pelo Apify ----

def friend_with_account(w, approved=True):
    """Usuário comum (não admin) com uma conta ativa. Devolve (id do usuário, id da conta)."""
    from sqlalchemy.orm import Session

    from app.models import Folder, User, XAccount

    with Session(w.engine) as db:
        user = User(email=f"amiga{len(w.ids)}@x.com", name="Amiga", is_approved=approved)
        db.add(user)
        db.flush()
        folder = Folder(owner_id=user.id, name="Da amiga")
        db.add(folder)
        db.flush()
        account = XAccount(folder_id=folder.id, username=f"amiga{len(w.ids)}")
        db.add(account)
        db.commit()
        w.ids[account.username] = account.id
        return user.id, account.id


def apify_state(w, owner_id):
    rows = w.q("select apify_status, apify_detail, apify_run_requested_at from user_settings where owner_id=:o", o=owner_id)
    return rows[0] if rows else None


def test_apify_without_token_says_it_is_missing(w, monkeypatch):
    import robo

    monkeypatch.setattr(robo, "apify_fetch", lambda *_a: pytest.fail("sem token não chama o Apify"))
    pending_id, pending_account = friend_with_account(w, approved=False)
    owner_id, account_id = friend_with_account(w)
    w.sent.clear()
    w.run()
    status, detail, _ = apify_state(w, owner_id)
    assert status == "sem_token" and "Token do Apify em falta" in detail
    assert apify_state(w, pending_id) is None, "pendente não é lido"
    assert all(user != f"amiga{n}" for _, user in w.timelines() for n in range(20)), "coletor nunca lê conta de amigo"
    alerts = w.q("select owner_id, kind from alerts_sent where kind='apify'")
    assert alerts == [(owner_id, "apify")]
    assert [m for m in w.sent if "Apify" in m], "o aviso fica no histórico do amigo (owner_id acima)"
    w.run()
    assert w.q("select count(*) from alerts_sent where kind='apify'")[0][0] == 1, "um aviso por dia"


def test_apify_reads_friend_accounts_with_their_token(w, monkeypatch):
    import robo

    owner_id = w.q("select owner_id from user_settings where apify_status='sem_token'")[0][0]
    username, account_id = w.q("select username, id from x_accounts where owner_id=:o", o=owner_id)[0]
    w.sql(f"update user_settings set apify_token='tok-amiga', apify_run_requested_at=now() where owner_id={owner_id}")
    calls = []
    posted = datetime.now(timezone.utc).strftime("%a %b %d %H:%M:%S +0000 %Y")

    def fetch(token, handles):
        calls.append((token, handles))
        return [
            {"id": "5551", "createdAt": posted, "text": "bombou", "viewCount": 12000, "likeCount": 30,
             "author": {"userName": username.upper(), "followers": 4321}},
            {"id": "5552", "createdAt": "Mon Jan 01 10:00:00 +0000 2024", "viewCount": 9,
             "author": {"userName": username, "followers": 4321}},  # de outro dia: não grava
            {"noResults": True},
        ]

    monkeypatch.setattr(robo, "apify_fetch", fetch)
    w.sent.clear()
    w.run()
    assert calls == [("tok-amiga", [username])]
    status, detail, requested = apify_state(w, owner_id)
    assert status == "ok" and requested is None and "1 posts de hoje" in detail
    assert w.q("select tweet_id, view_count from tweet_samples where account_id=:a", a=account_id) == [("5551", 12000)]
    assert w.q("select followers_count from daily_metrics where account_id=:a", a=account_id) == [(4321,)]
    viral = w.q("select owner_id, dedupe_key from alerts_sent where kind='viral' and dedupe_key like :k", k=f"viral:{account_id}:%")
    assert viral == [(owner_id, f"viral:{account_id}:5551:bombando")]
    assert w.q("select count(*) from alerts_sent where detail like :u and owner_id <> :o", u=f"%@{username}%", o=owner_id)[0][0] == 0,         "alerta do amigo é só dele"
    calls.clear()
    w.run()
    assert calls == [], "dentro do intervalo, sem pedido: não chama de novo"


def test_apify_error_shows_on_the_panel(w, monkeypatch):
    import robo

    owner_id = w.q("select owner_id from user_settings where apify_token='tok-amiga'")[0][0]
    w.sql(f"update user_settings set apify_run_requested_at=now() where owner_id={owner_id}")

    def refuse(_token, _handles):
        raise robo.ApifyError("o Apify recusou o token (401). Confira o token em Configurações.")

    monkeypatch.setattr(robo, "apify_fetch", refuse)
    w.run()
    status, detail, requested = apify_state(w, owner_id)
    assert status == "erro" and "401" in detail and requested is None


# ---- Fase 2: o admin lê pelo Apify primeiro; coletores só de reserva ----

def admin_id(w):
    return w.q("select id from users where is_admin is true order by id limit 1")[0][0]


def admin_token(w, token):
    owner = admin_id(w)
    if w.q("select count(*) from user_settings where owner_id=:o", o=owner)[0][0] == 0:
        w.sql(f"insert into user_settings (owner_id, impression_goal) values ({owner}, 500)")
    value = "NULL" if token is None else f"'{token}'"
    w.sql(f"update user_settings set apify_token={value}, apify_checked_at=NULL, apify_status=NULL where owner_id={owner}")


def admin_items(w, views=700):
    """Resposta do Apify para as contas do admin: um post de hoje por conta, com seguidores."""
    posted = datetime.now(timezone.utc).strftime("%a %b %d %H:%M:%S +0000 %Y")
    return [{"id": f"77{n}", "createdAt": posted, "text": "via apify", "viewCount": views, "likeCount": 3,
             "retweetCount": 1, "replyCount": 2, "quoteCount": 1, "bookmarkCount": 4,
             "author": {"userName": name, "followers": 5000 + n}}
            for n, name in enumerate(("conta1", "conta2", "conta3", "conta4"))]


def test_admin_reads_by_apify_and_collectors_wait(w, monkeypatch):
    import robo

    due_round(w)
    admin_token(w, "tok-admin")
    calls = []

    def fetch(token, handles):
        calls.append((token, sorted(handles)))
        return admin_items(w) if token == "tok-admin" else []

    monkeypatch.setattr(robo, "apify_fetch", fetch)
    w.sent.clear()
    w.run()
    assert ("tok-admin", ["conta1", "conta2", "conta3", "conta4"]) in calls
    assert not w.opens(), "com o Apify funcionando nenhum coletor abre"
    status, stats = w.last_beat()
    assert status == "ok" and stats["via"] == "apify" and stats["contas"] == 4
    row = w.q("select view_count, like_count, retweet_count, reply_count, quote_count, bookmark_count "
              "from tweet_samples where account_id=:a and tweet_id='770'", a=w.ids["conta1"])
    assert row == [(700, 3, 1, 2, 1, 4)], "todas as métricas, como os coletores"
    assert w.q("select followers_count from daily_metrics where account_id=:a and metric_date=current_date",
               a=w.ids["conta1"]) == [(5000,)]
    assert [m for m in w.sent if "via apify" not in m and "Viralizando" in m or "viralizando" in m.lower()], \
        "alerta de views do admin vai para o Telegram"
    calls.clear()
    w.run()
    assert not [c for c in calls if c[0] == "tok-admin"] and not w.opens(), "dentro dos 30 min: nem Apify nem coletores"


def test_refresh_button_uses_apify_right_away(w, monkeypatch):
    import robo

    calls = []
    monkeypatch.setattr(robo, "apify_fetch", lambda token, handles: calls.append(token) or admin_items(w, views=10))
    w.settings(robo_run_requested_at=datetime.now(timezone.utc))
    w.run()
    assert "tok-admin" in calls and not w.opens()
    assert w.last_beat()[1]["manual"] is True and w.last_beat()[1]["via"] == "apify"
    assert w.q("select robo_run_requested_at from app_settings where id=1")[0][0] is None


def test_zero_posts_is_not_a_failure(w, monkeypatch):
    import robo

    monkeypatch.setattr(robo, "apify_fetch", lambda token, handles: [{"noResults": True}])
    w.settings(robo_run_requested_at=datetime.now(timezone.utc))
    w.run()
    assert not w.opens(), "conta sem post hoje não chama os coletores"
    assert w.last_beat()[1]["via"] == "apify"


def test_collectors_take_over_when_apify_fails(w, monkeypatch):
    import robo

    def refuse(token, handles):
        raise robo.ApifyError("a conta do Apify está sem crédito (402).")

    monkeypatch.setattr(robo, "apify_fetch", refuse)
    due_round(w)
    w.sql(f"update user_settings set apify_checked_at = now() - interval '2 hours' where owner_id={admin_id(w)}")
    w.sent.clear()
    w.run()
    read = sorted(u for _, u in w.timelines())
    assert read == ["conta1", "conta2", "conta3", "conta4"], "na falha, os coletores leem tudo na mesma passada"
    status, stats = w.last_beat()
    assert stats["via"] == "coletores" and "402" in stats["apify_erro"]
    assert w.q("select apify_status from user_settings where owner_id=:o", o=admin_id(w)) == [("erro",)]
    assert [m for m in w.sent if m.startswith("⚠️ Apify falhou")], "avisa no Telegram do admin"


def test_without_admin_token_collectors_work_as_before(w, monkeypatch):
    import robo

    monkeypatch.setattr(robo, "apify_fetch", lambda token, handles: pytest.fail("sem token do admin") if token is None else [])
    admin_token(w, None)
    due_round(w)
    w.run()
    assert w.opens(), "sem Apify, os coletores seguem o próprio intervalo"
    assert w.last_beat()[1]["via"] == "coletores"


def test_apify_skips_reposts(w, monkeypatch):
    """Repost feito hoje de um post de ontem: o Apify manda com a data de hoje, mas não é post da conta."""
    import robo

    admin_token(w, "tok-admin")
    today = datetime.now(timezone.utc).strftime("%a %b %d %H:%M:%S +0000 %Y")
    author = {"userName": "conta2", "followers": 9100}
    items = [
        {"id": "880", "createdAt": today, "text": "post de hoje", "viewCount": 50, "author": author},
        {"id": "881", "createdAt": today, "text": "RT @outra: post de ontem", "viewCount": 90000, "author": author},
        {"id": "882", "createdAt": today, "text": "sem prefixo", "isRetweet": True, "viewCount": 70000,
         "retweetedTweet": {"id": "5", "createdAt": "Mon Oct 27 10:00:00 +0000 2026"}, "author": author},
    ]
    monkeypatch.setattr(robo, "apify_fetch", lambda token, handles: items if token == "tok-admin" else [])
    w.settings(robo_run_requested_at=datetime.now(timezone.utc))
    w.sent.clear()
    w.run()
    saved = w.q("select tweet_id from tweet_samples where account_id=:a and tweet_id like '88%' order by tweet_id",
                a=w.ids["conta2"])
    assert saved == [("880",)], "só o post de hoje entra"
    assert w.q("select followers_count from daily_metrics where account_id=:a and metric_date=current_date",
               a=w.ids["conta2"]) == [(9100,)]
    assert not [m for m in w.sent if "881" in m or "882" in m], "repost não dispara alerta de views"
    admin_token(w, None)


# ---- Atualizar dados na hora (read_now) e leitura automática por usuário ----

def test_read_now_for_a_friend_reads_everything_right_away(w, monkeypatch):
    import robo

    owner = w.q("select owner_id from user_settings where apify_token='tok-amiga'")[0][0]
    username, account_id = w.q("select username, id from x_accounts where owner_id=:o", o=owner)[0]
    posted = datetime.now(timezone.utc).strftime("%a %b %d %H:%M:%S +0000 %Y")
    calls = []
    monkeypatch.setattr(robo, "apify_fetch", lambda token, handles: calls.append((token, handles)) or [
        {"id": "9901", "createdAt": posted, "viewCount": 33, "author": {"userName": username, "followers": 1}}])
    w.sql(f"update user_settings set apify_run_requested_at=NULL where owner_id={owner}")
    assert robo.read_now(owner) == "ok"
    assert calls == [("tok-amiga", [username])], "lê mesmo dentro do intervalo"
    assert w.q("select count(*) from tweet_samples where account_id=:a and tweet_id='9901'", a=account_id)[0][0] == 1
    assert w.q("select apify_status, apify_run_requested_at from user_settings where owner_id=:o", o=owner) == [("ok", None)]


def test_friend_auto_off_and_own_interval(w, monkeypatch):
    import robo

    owner = w.q("select owner_id from user_settings where apify_token='tok-amiga'")[0][0]
    calls = []
    monkeypatch.setattr(robo, "apify_fetch", lambda token, handles: calls.append(token) or [])
    w.sql(f"update x_accounts set collected_at = now() - interval '20 minutes' where owner_id={owner}")
    w.sql(f"update user_settings set apify_auto=true, apify_interval_min=15, apify_run_requested_at=NULL where owner_id={owner}")
    w.run()
    assert "tok-amiga" in calls, "intervalo do usuário (15 min) venceu"
    calls.clear()
    w.sql(f"update x_accounts set collected_at = now() - interval '5 hours' where owner_id={owner}")
    w.sql(f"update user_settings set apify_auto=false where owner_id={owner}")
    w.run()
    assert "tok-amiga" not in calls, "leitura automática desligada: só pelo botão"
    w.sql(f"update user_settings set apify_run_requested_at=now() where owner_id={owner}")
    w.run()
    assert "tok-amiga" in calls, "o botão lê mesmo desligada"
    w.sql(f"update user_settings set apify_auto=true, apify_interval_min=60 where owner_id={owner}")


def test_admin_read_now_failure_goes_to_collectors_without_retrying_apify(w, monkeypatch):
    import robo

    due_round(w)
    admin_token(w, "tok-admin")
    tokens = []

    def refuse(token, handles):
        tokens.append(token)
        raise robo.ApifyError("sem resposta do Apify (TimeoutError).")

    monkeypatch.setattr(robo, "apify_fetch", refuse)
    assert robo.read_now(admin_id(w)) == "erro"
    assert w.q("select robo_run_requested_at is not null from app_settings where id=1") == [(True,)]
    tokens.clear()
    w.run()
    assert "tok-admin" not in tokens, "o cron não repete o Apify que acabou de falhar"
    assert w.opens() and w.last_beat()[1]["via"] == "coletores"
    admin_token(w, None)


# ---- Notificações: marcos de views (por post e do dia) e meta do dia ----

def test_milestones_and_daily_goal(w, monkeypatch):
    import robo

    owner = admin_id(w)
    admin_token(w, "tok-admin")
    prefs = json.dumps({"post_step": 10000, "day_step": 50000})
    w.sql(f"update user_settings set notify_prefs='{prefs}', impression_goal=60000 where owner_id={owner}")
    posted = datetime.now(timezone.utc).strftime("%a %b %d %H:%M:%S +0000 %Y")
    items = [{"id": "9701", "createdAt": posted, "viewCount": 35000, "author": {"userName": "conta1", "followers": 1}},
             {"id": "9702", "createdAt": posted, "viewCount": 31000, "author": {"userName": "conta2", "followers": 1}}]
    monkeypatch.setattr(robo, "apify_fetch", lambda token, handles: items if token == "tok-admin" else [])
    w.settings(robo_run_requested_at=datetime.now(timezone.utc))
    w.run()
    rows = dict(w.q("select dedupe_key, owner_id from alerts_sent where kind in ('marco_post','marco_dia','meta')"))
    post = [k for k in rows if k.startswith(f"marco_post:{w.ids['conta1']}:9701:")]
    assert post == [f"marco_post:{w.ids['conta1']}:9701:10000:3"], "só o maior marco (30 mil) do post"
    day = [k for k in rows if k.startswith("marco_dia:")]
    assert day and all(k.startswith(f"marco_dia:{owner}:") for k in day)
    assert any(k.startswith(f"meta:{owner}:") for k in rows), "total do dia passou da meta"
    mine = [k for k in rows if f":{w.ids['conta1']}:" in k or f":{w.ids['conta2']}:" in k or k.split(":")[1] == str(owner)]
    assert {rows[k] for k in mine} == {owner}, "os alertas das contas do admin são do admin"
    before = len(rows)
    w.settings(robo_run_requested_at=datetime.now(timezone.utc))
    w.run()
    assert w.q("select count(*) from alerts_sent where kind in ('marco_post','marco_dia','meta')")[0][0] == before,         "mesmos números: nada repete"
    w.sql(f"update user_settings set notify_prefs=NULL where owner_id={owner}")
    admin_token(w, None)
