"""robo.py ponta a ponta: schema do painel num Postgres de verdade + Apify falso (apify_fetch trocado).

O robô usa SQL de Postgres (NULLS FIRST, make_interval, ::json), então este teste só roda com
ROBO_TEST_DATABASE_URL apontando para um Postgres DESCARTÁVEL: as tabelas são apagadas e recriadas.

    ROBO_TEST_DATABASE_URL=postgresql://postgres@localhost:55432/postgres python -m pytest tests/test_robo_e2e.py

Os testes rodam em ordem e cada um parte do estado deixado pelo anterior (rodadas seguidas do cron).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import pytest

PG_URL = os.getenv("ROBO_TEST_DATABASE_URL", "").strip()
pytestmark = pytest.mark.skipif(not PG_URL, reason="defina ROBO_TEST_DATABASE_URL (Postgres descartável)")


class SentAlerts:
    """Textos gravados em alerts_sent desde o último clear(), na ordem."""

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
    """Banco e relógio que o robô enxerga."""

    def __init__(self):
        self.sent: list[str] = []
        self.ids: dict[str, int] = {}
        self.clock = "12:00"       # hora de Brasília que o robô enxerga (pausa e resumo)
        self.summary_day = None    # dia do resumo das 23:59 (None = fora do horário)

    def run(self):
        import robo

        assert robo.main() == 0

    def q(self, sql, **params):
        from sqlalchemy import text

        with self.engine.connect() as conn:
            return conn.execute(text(sql), params).all()

    def sql(self, sql):
        from sqlalchemy import text

        with self.engine.begin() as conn:
            conn.execute(text(sql))

    def heartbeats(self):
        return self.q("select count(*) from sync_runs where kind='tweets' and account_id is null")[0][0]

    def last_beat(self):
        return self.q("select status, stats, error from sync_runs order by id desc limit 1")[0]

    def settings(self, **values):
        from sqlalchemy.orm import Session

        from app import services

        with Session(self.engine) as db:
            row = services.get_settings(db)
            for key, value in values.items():
                setattr(row, key, value)
            db.commit()


@pytest.fixture(scope="module")
def w(tmp_path_factory):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    import app.models  # noqa: F401
    import robo
    from app import services
    from app.database import Base
    from app.models import Folder, XAccount

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
        db.commit()

    state = tmp_path_factory.mktemp("robo")
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("DATABASE_URL", PG_URL)
        mp.setenv("ROBO_ENV_FILE", str(state / "sem.env"))  # não lê o .env local
        world.sent = SentAlerts(world)  # alertas só no banco (o painel manda por push)
        mp.setattr(robo, "OFFLINE_STAMP", state / "alerta_banco")
        mp.setattr(robo, "LOCK_FILE", state / "robo.lock")
        # Pausa da madrugada e resumo dependem do relógio: de dia, salvo nos testes deles.
        mp.setattr(robo, "br_clock", lambda now=None: world.clock)
        mp.setattr(robo, "summary_day", lambda now=None: world.summary_day)
        # Nenhum teste chama o Apify de verdade: quem precisa troca apify_fetch.
        mp.setattr(robo, "apify_fetch", lambda token, handles: pytest.fail("Apify chamado sem resposta falsa"))
        yield world
    world.engine.dispose()


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


def due_round(w):
    """Leitura automática do admin ligada, sem pedido e com o intervalo do Apify vencido."""
    w.settings(robo_enabled=True, apify_interval_min=30, robo_run_requested_at=None)
    w.sql(f"update user_settings set apify_checked_at = now() - interval '2 hours' where owner_id={admin_id(w)}")


# ---- contas do admin pelo Apify ----

def test_admin_without_token_reads_nothing_and_says_so(w):
    admin_token(w, None)
    w.settings(robo_enabled=True, robo_run_requested_at=None)
    w.sent.clear()
    w.run()  # apify_fetch falha o teste se for chamado
    assert w.q("select apify_status from user_settings where owner_id=:o", o=admin_id(w)) == [("sem_token",)]
    assert [m for m in w.sent if "Token do Apify em falta" in m], "aviso no painel do admin"
    assert w.heartbeats() == 0 and w.q("select count(*) from tweet_samples")[0][0] == 0
    w.run()
    assert w.q("select count(*) from alerts_sent where kind='apify'")[0][0] == 1, "dentro do intervalo: nem repete"


def test_admin_reads_by_apify(w, monkeypatch):
    import robo

    admin_token(w, "tok-admin")
    due_round(w)
    calls = []

    def fetch(token, handles):
        calls.append((token, sorted(handles)))
        return admin_items(w) if token == "tok-admin" else []

    monkeypatch.setattr(robo, "apify_fetch", fetch)
    w.sent.clear()
    w.run()
    assert ("tok-admin", ["conta1", "conta2", "conta3", "conta4"]) in calls
    status, stats, _ = w.last_beat()
    assert status == "ok" and stats["via"] == "apify" and stats["contas"] == 4 and stats["manual"] is False
    row = w.q("select view_count, like_count, retweet_count, reply_count, quote_count, bookmark_count "
              "from tweet_samples where account_id=:a and tweet_id='770'", a=w.ids["conta1"])
    assert row == [(700, 3, 1, 2, 1, 4)], "todas as métricas"
    assert w.q("select followers_count from daily_metrics where account_id=:a and metric_date=current_date",
               a=w.ids["conta1"]) == [(5000,)]
    assert [m for m in w.sent if "viralizando" in m.lower()], "alerta de views do admin"
    calls.clear()
    w.run()
    assert not [c for c in calls if c[0] == "tok-admin"], "dentro dos 30 min: não lê de novo"


def test_disabled_never_reads_alone(w, monkeypatch):
    import robo

    calls = []
    monkeypatch.setattr(robo, "apify_fetch", lambda token, handles: calls.append(token) or [])
    due_round(w)
    w.settings(robo_enabled=False)
    before = w.heartbeats()
    w.run()
    assert "tok-admin" not in calls and w.heartbeats() == before


def test_refresh_button_reads_right_away(w, monkeypatch):
    import robo

    calls = []
    monkeypatch.setattr(robo, "apify_fetch", lambda token, handles: calls.append(token) or admin_items(w, views=10))
    w.settings(robo_run_requested_at=datetime.now(timezone.utc))
    w.run()
    assert "tok-admin" in calls, "com a leitura desligada, o botão lê"
    assert w.last_beat()[1]["manual"] is True and w.last_beat()[1]["via"] == "apify"
    assert w.q("select robo_run_requested_at from app_settings where id=1")[0][0] is None
    calls.clear()
    w.run()
    assert "tok-admin" not in calls, "sem pedido, desligado fica quieto"
    w.settings(robo_enabled=True)


def test_zero_posts_is_not_a_failure(w, monkeypatch):
    import robo

    monkeypatch.setattr(robo, "apify_fetch", lambda token, handles: [{"noResults": True}])
    w.settings(robo_run_requested_at=datetime.now(timezone.utc))
    w.run()
    status, stats, _ = w.last_beat()
    assert status == "ok" and stats["via"] == "apify"


def test_apify_failure_records_a_failed_beat_and_alerts(w, monkeypatch):
    import robo

    def refuse(token, handles):
        raise robo.ApifyError("a conta do Apify está sem crédito (402).")

    monkeypatch.setattr(robo, "apify_fetch", refuse)
    due_round(w)
    w.sent.clear()
    w.run()
    status, stats, error = w.last_beat()
    assert status == "failed" and stats["via"] == "apify" and "402" in error
    assert w.q("select apify_status from user_settings where owner_id=:o", o=admin_id(w)) == [("erro",)]
    assert [m for m in w.sent if m.startswith("⚠️ Apify falhou") and "402" in m]
    w.run()
    assert len([m for m in w.sent if m.startswith("⚠️ Apify falhou")]) == 1, "só tenta de novo no próximo intervalo"


def test_admin_read_now_failure_leaves_no_request(w, monkeypatch):
    import robo

    def refuse(token, handles):
        raise robo.ApifyError("sem resposta do Apify (TimeoutError).")

    monkeypatch.setattr(robo, "apify_fetch", refuse)
    w.settings(robo_run_requested_at=None)
    assert robo.read_now(admin_id(w)) == "erro"
    assert w.q("select robo_run_requested_at from app_settings where id=1") == [(None,)]
    monkeypatch.setattr(robo, "apify_fetch", lambda token, handles: admin_items(w, views=20))
    assert robo.read_now(admin_id(w)) == "ok"
    assert w.last_beat()[0] == "ok"


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
    alerts = w.q("select owner_id, kind from alerts_sent where kind='apify' and owner_id <> :a", a=admin_id(w))
    assert alerts == [(owner_id, "apify")]
    assert [m for m in w.sent if "Apify" in m], "o aviso fica no histórico do amigo (owner_id acima)"
    w.run()
    assert w.q("select count(*) from alerts_sent where kind='apify' and owner_id=:o", o=owner_id)[0][0] == 1, "um aviso por dia"


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


# ---- Pausa da madrugada e resumo do fim do dia ----

def test_night_pause_waits_but_the_button_reads(w, monkeypatch):
    import robo

    friend = w.q("select owner_id from user_settings where apify_token='tok-amiga'")[0][0]
    calls = []
    monkeypatch.setattr(robo, "apify_fetch", lambda token, handles: calls.append(token) or [])
    admin_token(w, "tok-admin")
    due_round(w)
    w.sql(f"update x_accounts set collected_at = now() - interval '5 hours' where owner_id={friend}")
    w.sql(f"update user_settings set apify_auto=true, apify_run_requested_at=NULL, pause_on=true, "
          f"pause_from='00:00', pause_to='06:00' where owner_id={friend}")
    w.sql(f"update user_settings set pause_on=true, pause_from='00:00', pause_to='06:00' where owner_id={admin_id(w)}")
    w.clock = "03:00"
    w.run()
    assert not calls, "madrugada: ninguém lê sozinho"
    w.sql(f"update user_settings set apify_run_requested_at=now() where owner_id={friend}")
    w.settings(robo_run_requested_at=datetime.now(timezone.utc))
    w.run()
    assert "tok-amiga" in calls and "tok-admin" in calls, "o Atualizar dados lê mesmo na pausa"
    calls.clear()
    w.clock = "06:00"
    due_round(w)
    w.run()
    assert "tok-admin" in calls, "acabou a pausa: a leitura automática volta sozinha"
    w.clock = "12:00"


def test_summary_reads_at_2355_and_congratulates_at_2359(w, monkeypatch):
    import robo

    friend = w.q("select owner_id from user_settings where apify_token='tok-amiga'")[0][0]
    friend_handle, friend_acc = w.q("select username, id from x_accounts where owner_id=:o", o=friend)[0]
    posted = datetime.now(timezone.utc).strftime("%a %b %d %H:%M:%S +0000 %Y")
    calls = []

    def fetch(token, handles):
        calls.append(token)
        return [{"id": "7001", "createdAt": posted, "viewCount": 4321,
                 "author": {"userName": friend_handle, "followers": 1}}] if token == "tok-amiga" else []

    monkeypatch.setattr(robo, "apify_fetch", fetch)
    due_round(w)
    w.sql(f"update x_accounts set collected_at = now() where owner_id={friend}")  # dentro do intervalo
    w.sql("update user_settings set summary_read_on = NULL, notify_prefs = NULL")
    w.sql(f"update user_settings set apify_run_requested_at = NULL where owner_id={friend}")
    w.clock = "23:56"
    w.run()
    assert "tok-amiga" in calls, "23:55: lê tudo para o resumo, mesmo dentro do intervalo"
    assert "tok-admin" in calls and w.last_beat()[1]["manual"] is True, "o admin também lê"
    calls.clear()
    w.clock = "23:57"
    w.run()
    assert "tok-amiga" not in calls, "a leitura do resumo é uma vez por dia"
    w.clock = "23:59"
    w.summary_day = datetime.now(timezone.utc).astimezone(robo.BR_TZ).date()
    w.sent.clear()
    w.run()
    total = w.q("select coalesce(sum(d.views),0) from daily_metrics d join x_accounts a on a.id=d.account_id "
                "where a.owner_id=:o and d.metric_date=:d", o=friend, d=w.summary_day)[0][0]
    rows = dict(w.q("select owner_id, detail from alerts_sent where kind='resumo'"))
    assert rows[friend].startswith(f"🎉 Parabéns! Hoje você fechou o dia com {robo.fmt_int(total)} views")
    assert admin_id(w) in rows, "o admin também recebe o resumo"
    w.run()
    assert w.q("select count(*) from alerts_sent where kind='resumo'")[0][0] == len(rows), "uma vez por dia"
    w.sql(f"update user_settings set notify_prefs='{{\"kinds\": {{\"resumo\": false}}}}' where owner_id={friend}")
    w.sql("delete from alerts_sent where kind='resumo'")
    w.run()
    assert friend not in dict(w.q("select owner_id, detail from alerts_sent where kind='resumo'")), "desligado: sem resumo"
    w.clock, w.summary_day = "12:00", None
    w.sql("update user_settings set notify_prefs = NULL")
    admin_token(w, None)
