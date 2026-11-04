"""robo.py sem banco: regras puras, a chamada ao Apify (urlopen falso) e a decisão da rodada do admin.

Nada sai para a internet.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import robo


class FakeCursor:
    def __init__(self, log):
        self.log = log

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.log.append((" ".join(sql.split()), params))


class FakeConn:
    def __init__(self):
        self.log = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self):
        return FakeCursor(self.log)

    def commit(self):
        pass

    def rollback(self):
        pass


@pytest.fixture
def admin_round(monkeypatch):
    """admin_round com as leituras do banco trocadas: devolve (rodar, lidas, avisos)."""

    def run(*, manual=False, token="tok", due=False, paused=False, summary=False):
        reads, notes = [], []
        monkeypatch.setattr(robo, "take_request", lambda conn: manual)
        monkeypatch.setattr(robo, "admin_apify", lambda conn: {"id": 1, "token": token, "due": due})
        monkeypatch.setattr(robo, "admin_schedule", lambda conn: {"id": 1, "paused": paused, "summary_due": summary})
        monkeypatch.setattr(robo, "mark_summary_read", lambda cur, owner_id: notes.append("resumo"))
        monkeypatch.setattr(robo, "set_apify_status", lambda cur, owner_id, status, detail: notes.append(status))
        monkeypatch.setattr(robo, "alert_once", lambda cur, key, kind, text, owner_id=None: notes.append(key) or True)
        monkeypatch.setattr(robo, "apify_admin_read",
                            lambda conn, admin, started, midnight, manual_: reads.append(manual_) or None)
        code = robo.admin_round(FakeConn(), datetime.now(timezone.utc), robo.today_start_br())
        return code, reads, notes

    return run


def test_admin_round_reads_only_when_due_or_asked(admin_round):
    assert admin_round(due=False) == (0, [], []), "dentro do intervalo: nada"
    assert admin_round(due=True)[1] == [False], "venceu o intervalo: lê (automática)"
    assert admin_round(due=True, paused=True)[1] == [], "pausa da madrugada: espera"
    assert admin_round(manual=True, paused=True)[1] == [True], "o botão lê mesmo na pausa"
    code, reads, notes = admin_round(summary=True, paused=True)
    assert reads == [True] and notes == ["resumo"], "23:55: lê para o resumo"


def test_admin_round_without_token_warns_and_reads_nothing(admin_round):
    code, reads, notes = admin_round(manual=True, token="")
    assert code == 0 and reads == []
    assert notes[0] == "sem_token" and notes[1].startswith("apify:1:sem_token:")


def test_views_drop():
    assert robo.views_drop(60, 100) == 40
    assert robo.views_drop(80, 100) is None   # caiu menos que QUEDA_LIMITE
    assert robo.views_drop(0, 50) is None     # ontem pequeno demais


def test_tweet_metrics_reads_bookmarks():
    tweet = {"legacy": {"favorite_count": 5, "bookmark_count": 7, "quote_count": 2}, "views": {"count": "900"}}
    metrics = robo.tweet_metrics(tweet)
    assert (metrics["bookmark_count"], metrics["quote_count"], metrics["view_count"]) == (7, 2, 900)
    assert robo.tweet_metrics({"id": "1"})["bookmark_count"] == 0


@pytest.mark.parametrize(
    ("views", "suffix"),
    [(500, None), (501, ""), (10_000, ""), (10_001, "bombando"), (150_000, "100k"), (1_000_001, "1m")],
)
def test_view_level_picks_the_highest_reached(views, suffix):
    level = robo.view_level(views)
    assert (level[0] if level else None) == suffix


def test_view_alert_key_keeps_the_old_key_for_the_first_level():
    assert robo.view_alert_key(7, "123", "") == "viral:7:123"
    assert robo.view_alert_key(7, "123", "1m") == "viral:7:123:1m"
    assert robo.fmt_int(1234567) == "1.234.567"


# ---- Apify ----

APIFY_ITEM = {
    "id": "1861",
    "createdAt": "Fri Nov 24 17:49:36 +0000 2023",
    "text": "bom dia",
    "likeCount": 10, "retweetCount": 2, "replyCount": 3, "quoteCount": 1, "bookmarkCount": 4, "viewCount": 5000,
    "author": {"userName": "Fulana", "followers": 1234},
}


def test_apify_item_parses():
    assert robo.parse_created_at(APIFY_ITEM["createdAt"]) == datetime(2023, 11, 24, 17, 49, 36, tzinfo=timezone.utc)
    assert robo.tweet_metrics(APIFY_ITEM) == {
        "like_count": 10, "retweet_count": 2, "reply_count": 3, "quote_count": 1, "bookmark_count": 4, "view_count": 5000,
    }
    assert robo.tweet_id_of(APIFY_ITEM) == "1861"
    assert robo.apify_author(APIFY_ITEM) == ("fulana", 1234)
    assert robo.apify_author({"noResults": True}) == ("", None)


def test_apify_input_and_actor(monkeypatch):
    monkeypatch.delenv("APIFY_ACTOR", raising=False)
    assert robo.apify_actor() == "apidojo~tweet-scraper"
    monkeypatch.setenv("APIFY_ACTOR", "outro/ator")
    assert robo.apify_actor() == "outro~ator"
    # Regras do Tweet Scraper V2: até 5 buscas por execução e cada busca precisa trazer bastante coisa. As contas
    # vão juntas numa busca ("from:a OR from:b"), só com os posts desde hoje (since:, o formato da documentação).
    midnight = datetime(2026, 11, 2, 0, 0, tzinfo=robo.BR_TZ)
    assert robo.apify_input(["a", "b"], midnight) == {
        "searchTerms": ["(from:a OR from:b) since:2026-11-02"],
        "maxItems": max(robo.APIFY_MIN_PER_QUERY, 2 * robo.APIFY_TODAY_PER_ACCOUNT), "sort": "Latest"}
    assert robo.apify_input(["a"], midnight)["searchTerms"] == ["from:a since:2026-11-02"]
    many = [f"c{n}" for n in range(robo.APIFY_QUERY_ACCOUNTS + 1)]
    terms = robo.apify_input(many, midnight)["searchTerms"]
    assert len(terms) == 2 and terms[1] == f"from:c{robo.APIFY_QUERY_ACCOUNTS} since:2026-11-02"
    assert robo.APIFY_MAX_ACCOUNTS <= robo.APIFY_MAX_QUERIES * robo.APIFY_QUERY_ACCOUNTS and robo.APIFY_MAX_QUERIES <= 5
    assert robo.APIFY_PARALLEL == 1, "o ator só aceita uma execução por vez"


@pytest.mark.parametrize(("code", "text"), [(401, "recusou o token"), (402, "sem crédito"), (500, "respondeu 500")])
def test_apify_errors_become_readable(monkeypatch, code, text):
    import urllib.error

    def refuse(req, timeout):
        assert req.headers["Authorization"] == "Bearer tok" and "run-sync-get-dataset-items" in req.full_url
        raise urllib.error.HTTPError(req.full_url, code, "x", {}, None)

    monkeypatch.setattr(robo.urllib.request, "urlopen", refuse)
    with pytest.raises(robo.ApifyError, match=text):
        robo.apify_fetch("tok", ["fulana"])


def test_apify_fetch_returns_items(monkeypatch):
    import io
    import json

    class Answer(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    sent = {}

    def answer(req, timeout):
        sent["body"] = json.loads(req.data)
        return Answer(json.dumps([APIFY_ITEM, "lixo"]).encode())

    monkeypatch.setattr(robo.urllib.request, "urlopen", answer)
    assert robo.apify_fetch("tok", ["fulana"]) == [APIFY_ITEM]
    day = robo.today_start_br().date().isoformat()
    assert sent["body"]["searchTerms"] == [f"from:fulana since:{day}"], "só os posts de hoje da conta"


@pytest.mark.parametrize(("item", "repost"), [
    ({"text": "RT @outra: post de ontem"}, True),
    ({"text": "  RT @outra: com espaço"}, True),
    ({"isRetweet": True, "text": "sem prefixo"}, True),
    ({"retweetedTweet": {"id": "1", "createdAt": "Mon Oct 27 10:00:00 +0000 2026"}, "text": "x"}, True),
    ({"retweeted_status": {"id": "1"}, "text": "x"}, True),
    ({"text": "post normal de hoje", "isRetweet": False, "retweetedTweet": None}, False),
    ({"text": "citando", "isQuote": True, "quotedTweet": {"id": "9"}}, False),
    ({"text": "falando de RT @fulano no meio"}, False),
])
def test_is_repost(item, repost):
    assert robo.is_repost(item) is repost


def test_apify_read_reports_accounts_apify_brought_nothing_for(monkeypatch):
    """Uma leitura em que o Apify não traz nada de uma conta: anota a conta (o que já estava guardado fica)."""
    monkeypatch.setattr(robo, "apify_profile", lambda token, handles: [])
    saved = []
    monkeypatch.setattr(robo, "save_followers", lambda cur, account, day, n: None)
    monkeypatch.setattr(robo, "save_tweets", lambda cur, account, tweets, midnight: saved.append((account["username"], len(tweets))) or len(tweets))
    posted = datetime.now(timezone.utc).strftime("%a %b %d %H:%M:%S +0000 %Y")
    monkeypatch.setattr(robo, "apify_fetch", lambda token, handles: [
        {"id": "1", "createdAt": posted, "author": {"userName": "Cheia", "followers": 9}}])
    report = {}
    accounts = [{"id": 1, "username": "cheia"}, {"id": 2, "username": "vazia"}]
    assert robo.apify_read(FakeConn(), "tok", accounts, robo.today_start_br(), report) == 1
    assert report == {"sem_post": ["vazia"]} and saved == [("cheia", 1), ("vazia", 0)]


def test_read_detail_says_stored_total_folder_and_empty_accounts():
    assert robo.read_detail(4, 7, {}) == "4 contas lidas, 7 posts de hoje"
    text = robo.read_detail(4, 7, {"sem_post": ["a"]}, "Dinheiro")
    assert text.startswith("Pasta Dinheiro: 4 contas lidas, 7 posts de hoje")
    assert text.endswith("(1 conta sem post hoje)")



def test_profile_reads_accounts_the_search_missed(monkeypatch):
    """A busca do Apify esconde algumas contas (conteúdo sensível para as contas do ator): a conta que veio
    vazia é lida de novo pelo perfil, só ela, e os posts e seguidores entram."""
    posted = datetime.now(timezone.utc).strftime("%a %b %d %H:%M:%S +0000 %Y")
    saved, followers, asked = [], [], []
    monkeypatch.setattr(robo, "save_followers", lambda cur, account, day, n: followers.append((account["username"], n)))
    monkeypatch.setattr(robo, "save_tweets", lambda cur, account, tweets, midnight: saved.append((account["username"], [t["id"] for t in tweets])) or len(tweets))
    monkeypatch.setattr(robo, "apify_fetch", lambda token, handles: [
        {"id": "1", "createdAt": posted, "author": {"userName": "achada", "followers": 9}}])

    def profile(token, handles):
        asked.append(list(handles))
        return [{"id": "2", "createdAt": posted, "viewCount": 17, "author": {"userName": "Escondida", "followers": 40}},
                {"id": "3", "createdAt": posted, "viewCount": 5, "author": {"userName": "pequena", "followers": 2}}]

    monkeypatch.setattr(robo, "apify_profile", profile)
    report = {}
    accounts = [{"id": 1, "username": "achada"}, {"id": 2, "username": "escondida"}, {"id": 3, "username": "pequena"}]
    assert robo.apify_read(FakeConn(), "tok", accounts, robo.today_start_br(), report) == 3
    assert asked == [["escondida", "pequena"]], "uma execução só, com as contas que a busca não trouxe"
    assert ("escondida", ["2"]) in saved and ("pequena", ["3"]) in saved and ("escondida", 40) in followers
    assert report == {"perfil": ["escondida", "pequena"]}, "lidas pelo perfil e não ficam como sem post"


def test_profile_with_only_old_posts_still_counts_as_no_post_today(monkeypatch):
    """O perfil devolve os posts mais novos, que podem ser de ontem: a conta foi lida pelo perfil, mas não
    teve post hoje, e o aviso do fim da leitura tem que dizer isso."""
    monkeypatch.setattr(robo, "save_followers", lambda *a: None)
    monkeypatch.setattr(robo, "apify_fetch", lambda token, handles: [])
    old = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%a %b %d %H:%M:%S +0000 %Y")
    monkeypatch.setattr(robo, "apify_profile", lambda token, handles: [
        {"id": "7", "createdAt": old, "author": {"userName": "quieta", "followers": 3}}])
    monkeypatch.setattr(robo, "save_tweets", lambda cur, account, tweets, midnight: 0)  # nada de hoje
    report = {}
    assert robo.apify_read(FakeConn(), "tok", [{"id": 1, "username": "quieta"}], robo.today_start_br(), report) == 0
    assert report == {"perfil": ["quieta"], "sem_post": ["quieta"]}


def test_profile_failure_does_not_break_the_read(monkeypatch):
    monkeypatch.setattr(robo, "save_followers", lambda *a: None)
    monkeypatch.setattr(robo, "save_tweets", lambda cur, account, tweets, midnight: len(tweets))
    monkeypatch.setattr(robo, "apify_fetch", lambda token, handles: [])

    def broken(token, handles):
        raise robo.ApifyError("o Apify respondeu 500.")

    monkeypatch.setattr(robo, "apify_profile", broken)
    report = {}
    assert robo.apify_read(FakeConn(), "tok", [{"id": 1, "username": "a"}], robo.today_start_br(), report) == 0
    assert report == {"sem_post": ["a"]}


def test_profile_input_and_actor(monkeypatch):
    """A reserva vai pelo Twitter Scraper Unlimited (sem o mínimo de 50 tweets por busca do V2): conta pequena,
    com poucos posts, só é lida assim."""
    assert robo.apify_profile_input(["fulana", "beltrana"]) == {
        "startUrls": ["https://x.com/fulana", "https://x.com/beltrana"],
        "maxItems": 2 * robo.APIFY_PROFILE_ITEMS, "sort": "Latest"}
    sent = []
    monkeypatch.setattr(robo, "apify_call", lambda token, payload, actor=None: sent.append(actor) or [])
    monkeypatch.delenv("APIFY_FALLBACK_ACTOR", raising=False)
    robo.apify_profile("tok", ["fulana"])
    monkeypatch.setenv("APIFY_FALLBACK_ACTOR", "outro/ator")
    robo.apify_profile("tok", ["fulana"])
    assert sent == [robo.APIFY_UNLIMITED, "outro~ator"]


def test_views_never_go_down():
    """No X as views só sobem: se um ator vier sem o campo (ou com menos), fica o número que já estava."""
    log = []
    robo.upsert_sample(FakeCursor(log), 1, "9", datetime.now(robo.BR_TZ), {"like_count": 1, "retweet_count": 0,
                       "reply_count": 0, "quote_count": 0, "bookmark_count": 0, "view_count": 0}, "oi")
    sql = log[0][0]
    assert "view_count = GREATEST(tweet_samples.view_count, EXCLUDED.view_count)" in sql
    assert "impression_count = GREATEST(tweet_samples.impression_count, EXCLUDED.impression_count)" in sql


class LockConn(FakeConn):
    """Conexão falsa que responde ao pg_try_advisory_lock com `free`."""

    def __init__(self, free):
        super().__init__()
        self.free = free

    def cursor(self):
        conn = self

        class Cur(FakeCursor):
            def fetchone(self):
                return {"ok": conn.free}

        return Cur(self.log)


def test_reading_lock_skips_a_second_read_of_the_same_owner(monkeypatch):
    """Botão e cron juntos: a segunda leitura do mesmo dono não chama o Apify (o ator só aceita uma)."""
    calls = []
    monkeypatch.setattr(robo, "_apify_user_round", lambda conn, user, midnight: calls.append("user") or {"contas": 1})
    monkeypatch.setattr(robo, "_apify_admin_read", lambda *a, **k: calls.append("admin"))
    busy = LockConn(False)
    assert robo.apify_user_round(busy, {"id": 7}, robo.today_start_br())["ocupado"] is True
    assert robo.apify_admin_read(busy, {"id": 1, "token": "t"}, datetime.now(timezone.utc), robo.today_start_br(), True) is None
    assert calls == [] and not any("unlock" in sql for sql, _ in busy.log), "sem a trava não solta nada"
    free = LockConn(True)
    assert robo.apify_user_round(free, {"id": 7}, robo.today_start_br()) == {"contas": 1}
    assert calls == ["user"]
    sqls = [sql for sql, _ in free.log]
    assert sqls[0].startswith("SELECT pg_try_advisory_lock") and sqls[-1].startswith("SELECT pg_advisory_unlock")
    assert free.log[0][1] == (robo.READ_LOCK_NS, 7)


def test_reading_lock_is_released_when_the_read_breaks(monkeypatch):
    def boom(conn, user, midnight):
        raise RuntimeError("quebrou")

    monkeypatch.setattr(robo, "_apify_user_round", boom)
    free = LockConn(True)
    with pytest.raises(RuntimeError):
        robo.apify_user_round(free, {"id": 3}, robo.today_start_br())
    assert free.log[-1][0].startswith("SELECT pg_advisory_unlock")


def test_read_now_waits_for_the_cron_read_instead_of_paying_another(monkeypatch):
    answers = iter([False, False, True])  # ocupado, ocupado, livre

    class Seq(LockConn):
        def cursor(self):
            class Cur(FakeCursor):
                def fetchone(self):
                    return {"ok": next(answers)}

            return Cur(self.log)

    monkeypatch.setattr(robo.time, "sleep", lambda s: None)
    conn = Seq(True)
    assert robo.wait_other_read(conn, 5) is True
    assert sum("pg_try_advisory_lock" in sql for sql, _ in conn.log) == 3
    assert robo.wait_other_read(LockConn(True), 5) is False, "ninguém lendo: pode ler"


def test_eiv_item_is_understood():
    """eiv/twitter-scraper (linha do tempo do perfil) devolve o autor achatado e o id em tweetId."""
    import robo

    item = {"tweetId": "123", "createdAt": "2026-11-03T13:16:05.000Z", "viewCount": 17, "likeCount": 2,
            "authorHandle": "Conta1", "authorFollowers": 900, "text": "oi", "isRetweet": False}
    assert robo.tweet_id_of(item) == "123"
    assert robo.apify_author(item) == ("conta1", 900)
    assert robo.tweet_metrics(item)["view_count"] == 17
    assert not robo.is_repost(item)
