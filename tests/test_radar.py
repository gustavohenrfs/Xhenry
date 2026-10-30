"""Radar de trend: parsers das fontes (amostras pequenas, nada vai à rede), calendário e montagem dos assuntos."""

from __future__ import annotations

from datetime import date, datetime, timezone

from app import radar_sources as src

X_HTML = """<html><body><div id=timeline-container>
<div class=list-container><h3 class=title data-timestamp=1793390644.004>Fri Oct 30 2026</h3><ol class=trend-card__list>
<li><span class=trend-name><a href="https://twitter.com/search?q=%23Flamengo" class=trend-link>#Flamengo</a></span></li>
<li><span class=trend-name><a href="https://twitter.com/search?q=Lula" class=trend-link>Lula</a></span></li>
</ol></div>
<div class=list-container><h3 class=title data-timestamp=1793387044.004>Fri Oct 30 2026</h3><ol class=trend-card__list>
<li><span class=trend-name><a href="https://twitter.com/search?q=Lula" class=trend-link>Lula</a></span></li>
</ol></div>
</div></body></html>"""

GOOGLE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss xmlns:ht="https://trends.google.com/trending/rss" version="2.0"><channel>
<item><title>flamengo</title><ht:approx_traffic>2000+</ht:approx_traffic>
<ht:news_item><ht:news_item_title>Flamengo vence &amp; lidera</ht:news_item_title>
<ht:news_item_url>https://ge.globo.com/x</ht:news_item_url><ht:news_item_source>ge</ht:news_item_source></ht:news_item>
</item>
<item><title>anitta</title><ht:approx_traffic>200+</ht:approx_traffic></item>
</channel></rss>"""

NEWS_XML = """<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>
<item><title>Lula sanciona lei do salário - G1</title><link>https://news.google.com/a</link><source url="https://g1.globo.com">G1</source></item>
<item><title>Chuva forte em SP - Folha de S.Paulo</title><link>https://news.google.com/b</link></item>
</channel></rss>"""

def test_norm_joins_spellings():
    assert src.norm("#Flamengo ") == "flamengo"
    assert src.norm("São  Paulo!") == "sao paulo"
    assert src.norm("@Anitta") == "anitta"


def test_parse_x_reads_hours_newest_first():
    hours = src.parse_x(X_HTML)
    assert [h["items"] for h in hours] == [["#Flamengo", "Lula"], ["Lula"]]
    assert hours[0]["at"] == datetime.fromtimestamp(1793390644.004, tz=timezone.utc)
    assert hours[0]["at"] > hours[1]["at"]


def test_parse_google_reads_volume_and_news():
    items = src.parse_google(GOOGLE_XML)
    assert [i["title"] for i in items] == ["flamengo", "anitta"]
    assert items[0]["volume"] == "2000+"
    assert items[0]["news"] == [{"title": "Flamengo vence & lidera", "url": "https://ge.globo.com/x", "source": "ge"}]
    assert items[1]["news"] == []


def test_parse_news_cuts_the_source_suffix():
    items = src.parse_news(NEWS_XML)
    assert items[0] == {"title": "Lula sanciona lei do salário", "url": "https://news.google.com/a", "source": "G1"}
    assert items[1]["title"] == "Chuva forte em SP" and items[1]["source"] == "Folha de S.Paulo"


def test_broken_input_never_raises():
    assert src.parse_x("<html>mudou tudo</html>") == []
    assert src.parse_google("não é xml") == []
    assert src.parse_news("") == []


# ---- calendário sazonal ----

def test_movable_dates():
    from app import seasonal

    dates = {name: day for day, name in seasonal.dates_of(2027)}
    assert dates["Carnaval"] == date(2027, 2, 9)          # Páscoa 28/03/2027
    assert dates["Dia das Mães"] == date(2027, 5, 9)
    assert dates["Dia dos Pais"] == date(2027, 8, 8)
    assert "Eleições (1º turno)" not in dates, "eleição só em ano par"
    dates_2026 = {name: day for day, name in seasonal.dates_of(2026)}
    assert dates_2026["Black Friday"] == date(2026, 11, 27)
    assert dates_2026["Eleições (1º turno)"] == date(2026, 10, 4)
    assert dates_2026["Eleições (2º turno)"] == date(2026, 10, 25)


def test_upcoming_window():
    from app import seasonal

    items = seasonal.upcoming(date(2026, 10, 30))
    finados = next(i for i in items if i["name"] == "Finados")
    assert finados["in_days"] == 3 and finados["kind"] == "sazonal" and finados["tip"]
    assert all(0 <= i["in_days"] <= 14 for i in items)
    assert [i["date"] for i in items] == sorted(i["date"] for i in items)
    assert any(i["name"] == "Halloween" for i in items)
    late_dec = seasonal.upcoming(date(2026, 12, 28))
    assert any(i["name"] == "Ano Novo" and i["date"] == "2027-01-01" for i in late_dec), "vira o ano"


# ---- leitura, gravação e montagem (banco de teste, fetch falso) ----


import pytest  # noqa: E402

from app import radar  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.models import RadarItem, RadarSnapshot  # noqa: E402



def fake_fetch(url):
    if "trends24" in url:
        return X_HTML
    if "trending/rss" in url:
        return GOOGLE_XML
    if "news.google" in url:
        return NEWS_XML
    raise AssertionError(url)


@pytest.fixture
def db():
    from fastapi.testclient import TestClient

    from main import app

    with TestClient(app):  # cria as tabelas
        pass
    with SessionLocal() as session:
        session.execute(RadarItem.__table__.delete())
        session.execute(RadarSnapshot.__table__.delete())
        session.commit()
        yield session


def test_refresh_saves_one_read_per_source(db):
    assert radar.refresh(db, fetch=fake_fetch) == {"x": True, "google": True, "news": True,
                                                   "x_world": True, "google_world": True}
    assert db.query(RadarSnapshot).count() == 5
    x_items = db.query(RadarItem).filter(RadarItem.source == "x").all()
    assert sorted((i.extra["hour"], i.rank, i.title) for i in x_items) == [(0, 1, "#Flamengo"), (0, 2, "Lula"), (1, 1, "Lula")]


def test_one_source_down_does_not_stop_the_others(db):
    def flaky(url):
        if "trends24" in url:
            raise TimeoutError("demorou")
        return fake_fetch(url)

    assert radar.refresh(db, ["x", "google", "news"], flaky) == {"x": False, "google": True, "news": True}
    board = radar.board(db)
    assert board["sources"]["x"]["ok"] is False and "demorou" in board["sources"]["x"]["error"]
    assert any(t["norm"] == "flamengo" for t in board["topics"]), "o Google continua"


def test_board_joins_sources_and_scores(db):
    radar.refresh(db, fetch=fake_fetch)
    topics = {t["norm"]: t for t in radar.board(db)["topics"]}
    fla = topics["flamengo"]
    assert fla["sources"] == ["x", "google"] and fla["volume"] == "2000+" and fla["category"] == "Futebol"
    assert fla["direction"] == "novo" and fla["hours_x"] == 1 and fla["rank_x"] == 1
    lula = topics["lula"]
    assert lula["hours_x"] == 2 and set(lula["sources"]) == {"x", "news"}, "a manchete entra no Lula"
    assert lula["category"] == "Política"
    assert fla["heat"] > topics["anitta"]["heat"], "duas fontes no topo esquentam mais"


def test_headlines_without_topic_and_upcoming(db):
    radar.refresh(db, fetch=fake_fetch)
    board = radar.board(db)
    assert [h["title"] for h in board["headlines"]] == ["Chuva forte em SP"]
    kinds = {item["kind"] for item in board["upcoming"]}
    assert "sazonal" in kinds or board["upcoming"] == [] or "previsao" in kinds
    assert any(i["kind"] == "previsao" and i["norm"] == "flamengo" for i in board["upcoming"]), "novo em 2 fontes"


def test_refresh_due_and_pruning(db):
    from datetime import timedelta

    now = datetime.now(timezone.utc)
    radar.refresh(db, fetch=fake_fetch, now=now - timedelta(days=3))
    assert radar.refresh_due(db, now) == list(radar.SOURCES)
    radar.refresh(db, list(radar.SOURCES), fake_fetch, now - timedelta(minutes=20))
    assert db.query(RadarSnapshot).count() == 5, "leituras de mais de 2 dias somem"
    assert radar.refresh_due(db, now) == ["x", "google", "x_world", "google_world"], "notícias (30 min) ainda valem"


def test_topic_detail_has_24h_history(db):
    radar.refresh(db, fetch=fake_fetch)
    detail = radar.topic_detail(db, "#LULA")
    assert detail["title"] == "Lula" and [h["rank"] for h in detail["history_x"]] == [1, 2]
    assert radar.topic_detail(db, "nada disso") is None


def test_prediction_reasons_read_well():
    topics = [
        {"title": "A", "norm": "a", "direction": "subindo", "sources": ["x", "google"], "climb_3h": 0, "rank_x": 30, "hours_x": 5, "heat": 50},
        {"title": "B", "norm": "b", "direction": "estavel", "sources": ["x"], "climb_3h": 12, "rank_x": 3, "hours_x": 6, "heat": 40},
        {"title": "C", "norm": "c", "direction": "novo", "sources": ["x"], "climb_3h": 0, "rank_x": 4, "hours_x": 1, "heat": 30},
        {"title": "D", "norm": "d", "direction": "estavel", "sources": ["x"], "climb_3h": 0, "rank_x": 20, "hours_x": 9, "heat": 60},
    ]
    tips = {p["norm"]: p["tip"] for p in radar.predictions(topics)}
    assert tips == {"a": "Subindo e em 2 fontes.", "b": "Subiu 12 posições no X em 3 h.", "c": "Entrou no top 10 do X em menos de 2 h."}


def test_watchdog_reads_due_sources(db, monkeypatch):
    from app import watchdog

    calls = []
    monkeypatch.setenv("RADAR", "on")
    monkeypatch.setattr(radar, "refresh", lambda session, sources, *a, **k: calls.append(list(sources)))
    assert watchdog.run_radar(db) == list(radar.SOURCES) and calls == [list(radar.SOURCES)]
    monkeypatch.setenv("RADAR", "off")
    assert watchdog.run_radar(db) == []


# ---- rotas ----

def test_radar_routes(db, monkeypatch):
    from fastapi.testclient import TestClient

    from conftest import admin_row
    from app.routers.auth import current_user
    from main import app

    radar.refresh(db, fetch=fake_fetch)
    monkeypatch.setattr(radar, "http_fetch", fake_fetch)
    app.dependency_overrides[current_user] = admin_row
    try:
        with TestClient(app) as client:
            board = client.get("/api/radar").json()
            assert board["refreshing"] is False, "leitura nova e RADAR=off: não lê de novo"
            assert {t["norm"] for t in board["topics"]} >= {"flamengo", "lula"}
            assert set(board["sources"]) == {"x", "google", "news"}
            assert client.post("/api/radar/refresh").status_code == 429, "acabou de ler"
            topic = client.get("/api/radar/topic", params={"norm": "Flamengo"}).json()
            assert topic["title"] == "#Flamengo" and len(topic["history_x"]) == 2
            assert client.get("/api/radar/topic", params={"norm": "zzzz"}).status_code == 404
    finally:
        app.dependency_overrides.clear()


def test_manual_refresh_after_the_gap(db, monkeypatch):
    from datetime import timedelta

    radar.refresh(db, fetch=fake_fetch, now=datetime.now(timezone.utc) - timedelta(minutes=5))
    assert radar.refresh_manual(db, fake_fetch) is True
    assert radar.refresh_manual(db, fake_fetch) is False


def test_radar_tab_markup():
    from app.config import ROOT

    html = (ROOT / "templates/index.html").read_text(encoding="utf-8")
    assert '<section id="pane-radar"' in html
    assert html.count('data-tab="radar"') == 2, "barra lateral e barra de baixo"
    assert 'data-tab="archived" class="more-item"' in html, "Arquivadas foi para o Mais no celular"
    js = (ROOT / "static/js/app.js").read_text(encoding="utf-8")
    assert 'const MORE_TABS = ["archived", "tools", "settings"];' in js
    assert '["radar", "Radar"]' in js and "innerHTML" not in js.split("// ---------- Radar de trend ----------")[1].split("// ---------- busca rápida")[0]
