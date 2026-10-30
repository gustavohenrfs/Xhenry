"""Radar de trend: lê as fontes grátis em segundo plano, guarda as leituras e monta os assuntos para a aba.

- refresh(): busca as fontes pedidas (cada uma falha sozinha) e grava uma leitura por fonte em radar_snapshots.
  A leitura do X traz as 24 listas horárias de uma vez (extra.hour, 0 = a mais nova).
- refresh_due(): as fontes vencidas pelo ritmo (X e Google 15 min, Notícias 30 min, Wikipédia 6 h). O vigia
  (app/watchdog.py) chama a cada minuto; RADAR=off desliga.
- board(): junta itens das fontes que falam do mesmo assunto (mesmo norm, ou um contido no outro por palavra
  inteira, 4+ letras), calcula o calor, há quanto tempo está em alta no X e a direção, e monta "Próximos dias"
  (datas sazonais + o que pode virar trend). Só lê o banco.
- check_terms(): depois de cada leitura, termo de um usuário (radar_terms) em alta vira alerta kind=radar.
- engaging_posts(): os posts mais fortes sobre um assunto, buscados no Apify de quem clicou (cache de 30 min).

Guarda só 2 dias de leituras: a do X já traz as 24 horas, mais que isso só ocuparia o banco.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from urllib.parse import quote

import httpx
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app import radar_ideas
from app import radar_sources as src
from app import seasonal
from app.models import DEFAULT_TZ, RadarItem, RadarPostCache, RadarSnapshot, RadarTerm

log = logging.getLogger("xhenry")

SOURCES = ("x", "google", "news", "wiki", "x_world", "google_world")
INTERVALS = {"x": 15, "google": 15, "news": 30, "wiki": 360, "x_world": 15, "google_world": 15}  # minutos
WEIGHTS = {"x": 40, "google": 30, "news": 20, "wiki": 10}
# Cada região usa as suas leituras com os nomes genéricos da tela (x, google, news, wiki).
REGIONS = {
    "br": {"x": "x", "google": "google", "news": "news", "wiki": "wiki"},
    "world": {"x": "x_world", "google": "google_world"},
}
# Nunca bom pra engajar (e nunca ganha ideia de post): morte, tragédia, crime, doença e qualquer coisa com criança.
BLOCK_RE = re.compile(
    r"\b(?:morte|mortes|morre|morreu|morrem|morto|mortos|faleceu|falece|falecimento|velorio|enterro|luto|tragedia|"
    r"acidente|acidentes|crime|crimes|assassin\w*|homicidio|feminicidio|estupro|abuso|violencia|tiroteio|baleado|"
    r"ataque|atentado|massacre|sequestro|desaparecid\w*|doenca|cancer|hospital|internad\w*|uti|vitima|vitimas|"
    r"enchente|desabamento|incendio|guerra|bomba|crianca|criancas|menor|menores|infantil|bebe|bebes|adolescente|"
    r"pedofil\w*|suicidio|overdose|preso|presa|prisao|policia)\b")
# Quanto cada categoria rende para conta de texto (+ bônus de data e clima e do calor do assunto).
ENGAGE_BASE = {"Futebol": 80, "Famosos/TV": 70, "Música": 65, "Política": 55, "Outros": 40, "Economia": 30}
ENGAGE_BONUS_RE = re.compile(
    r"\b(?:feriado|carnaval|natal|reveillon|ano novo|black friday|halloween|namorados|dia das maes|dia dos pais|"
    r"pascoa|sao joao|festa junina|calor|frio|chuva|praia|verao|sextou|final|classico|jogo|bbb|reality|show)\b")
KEEP = timedelta(days=2)
TIMEOUT = 15.0
WIKI_TOP = 40
MAX_TOPICS = 60
MANUAL_GAP = timedelta(minutes=2)
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"
# Palavras (já normalizadas) que dão a categoria do assunto; a primeira que bater vence, nesta ordem.
CATEGORIES = [
    ("Futebol", r"flamengo|palmeiras|corinthians|sao paulo fc|spfc|vasco|botafogo|fluminense|gremio|internacional|"
                r"cruzeiro|atletico|santos|bahia|fortaleza|brasileirao|libertadores|copa do brasil|sul americana|"
                r"selecao|neymar|ancelotti|futebol|campeonato|gol|var|tecnico|\w+ x \w+"),
    ("Política", r"lula|bolsonaro|stf|congresso|camara|senado|eleicao|eleicoes|governo|ministro|presidente|moraes|"
                 r"pec|pl|anistia|tarcisio|flavio|janones|deputado|senador|golpe|direita|esquerda|voto|trump|milei"),
    ("Música", r"show|album|clipe|anitta|ludmilla|kpop|bts|musica|cantor|cantora|lollapalooza|rock in rio|turne|"
               r"spotify|feat|single"),
    ("Famosos/TV", r"bbb|novela|globo|avenida brasil|ator|atriz|reality|a fazenda|netflix|serie|filme|influencer|"
                   r"famoso|famosa|casamento|morre|morreu|faleceu|resumo"),
    ("Economia", r"dolar|bolsa|ibovespa|pix|inss|salario|imposto|dividendo|inflacao|juros|selic|bitcoin|"
                 r"petrobras|banco|preco|gasolina|black friday"),
]
CATEGORY_RE = [(name, re.compile(r"\b(?:" + pattern + r")\b")) for name, pattern in CATEGORIES]
_manual_lock = threading.Lock()


class RadarError(Exception):
    """A fonte respondeu, mas sem o formato esperado."""


def radar_enabled() -> bool:
    return os.getenv("RADAR", "on").strip().lower() not in {"0", "off", "false", "no"}


def http_fetch(url: str) -> str:
    with httpx.Client(timeout=TIMEOUT, follow_redirects=True, headers={"User-Agent": UA}) as client:
        response = client.get(url)
        response.raise_for_status()
        return response.text


def _now() -> datetime:
    return datetime.now(timezone.utc)


def parse_iso(text: str) -> datetime:
    return datetime.fromisoformat(text)


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def read_source(source: str, fetch: Callable[[str], str], now: datetime | None = None) -> list[dict[str, Any]]:
    """Itens prontos para gravar: rank, title, norm, volume, url, extra. Lista vazia vira RadarError."""
    rows: list[dict[str, Any]] = []
    if source in ("x", "x_world"):
        for hour, block in enumerate(src.parse_x(fetch(src.URLS[source]))[:24]):
            for rank, title in enumerate(block["items"], 1):
                rows.append({"rank": rank, "title": title, "url": "https://x.com/search?q=" + quote(title),
                             "extra": {"hour": hour, "at": block["at"].isoformat()}})
    elif source in ("google", "google_world"):
        geo = "BR" if source == "google" else "US"
        for rank, item in enumerate(src.parse_google(fetch(src.URLS[source])), 1):
            rows.append({"rank": rank, "title": item["title"], "volume": item["volume"],
                         "url": f"https://trends.google.com/trends/explore?geo={geo}&q=" + quote(item["title"]),
                         "extra": {"news": item["news"][:5]}})
    elif source == "news":
        for rank, item in enumerate(src.parse_news(fetch(src.URLS["news"])), 1):
            rows.append({"rank": rank, "title": item["title"], "url": item["url"], "extra": {"source": item["source"]}})
    elif source == "wiki":
        day = ((now or _now()) - timedelta(days=1)).strftime("%Y/%m/%d")
        items = src.parse_wiki(json.loads(fetch(src.URLS["wiki"].format(date=day)) or "{}"))
        for rank, item in enumerate(items[:WIKI_TOP], 1):
            rows.append({"rank": rank, "title": item["title"], "url": item["url"], "extra": {"views": item["views"]}})
    else:
        raise RadarError(f"fonte desconhecida: {source}")
    if not rows:
        raise RadarError("a página veio sem assuntos (o formato pode ter mudado)")
    for row in rows:
        row["norm"] = src.norm(row["title"])
    return [row for row in rows if row["norm"]]


def refresh(db: Session, sources: list[str] | tuple[str, ...] | None = None,
            fetch: Callable[[str], str] | None = None, now: datetime | None = None) -> dict[str, bool]:
    """Lê as fontes e grava uma leitura por fonte (ok ou com o erro). Apaga as leituras de mais de 2 dias."""
    fetch = fetch or http_fetch
    now = now or _now()
    result: dict[str, bool] = {}
    for source in sources or SOURCES:
        snapshot = RadarSnapshot(source=source, fetched_at=now)
        try:
            rows = read_source(source, fetch, now)
            snapshot.items = [RadarItem(source=source, rank=row["rank"], title=row["title"][:400], norm=row["norm"][:400],
                                        volume=row.get("volume"), url=row.get("url"), extra=row.get("extra"))
                              for row in rows]
            snapshot.ok = True
        except Exception as exc:  # cada fonte falha sozinha
            snapshot.ok, snapshot.error = False, f"{type(exc).__name__}: {exc}"[:500]
            log.warning("radar: %s falhou: %s", source, snapshot.error)
        db.add(snapshot)
        result[source] = snapshot.ok
    old = select(RadarSnapshot.id).where(RadarSnapshot.fetched_at < now - KEEP)
    # synchronize_session=False: no SQLite as datas voltam sem fuso e a comparação em Python quebraria.
    db.execute(delete(RadarItem).where(RadarItem.snapshot_id.in_(old)).execution_options(synchronize_session=False))
    db.execute(delete(RadarSnapshot).where(RadarSnapshot.fetched_at < now - KEEP).execution_options(synchronize_session=False))
    db.commit()
    try:
        check_terms(db, now)
    except Exception:
        log.exception("radar: alerta dos termos falhou")
        db.rollback()
    return result


def last_reads(db: Session) -> dict[str, RadarSnapshot]:
    """A leitura mais nova de cada fonte (com ou sem erro)."""
    newest = select(RadarSnapshot.source, func.max(RadarSnapshot.id).label("id")).group_by(RadarSnapshot.source).subquery()
    rows = db.scalars(select(RadarSnapshot).join(newest, RadarSnapshot.id == newest.c.id)).all()
    return {row.source: row for row in rows}


def refresh_due(db: Session, now: datetime | None = None) -> list[str]:
    now = now or _now()
    reads = last_reads(db)
    due = []
    for source in SOURCES:
        last = reads.get(source)
        if last is None or now - _as_utc(last.fetched_at) >= timedelta(minutes=INTERVALS[source]) - timedelta(seconds=30):
            due.append(source)
    return due


def refresh_manual(db: Session, fetch: Callable[[str], str] | None = None) -> bool:
    """Botão Atualizar: lê tudo, no máximo uma vez a cada 2 min. False = pedido cedo demais."""
    with _manual_lock:
        reads = last_reads(db)
        newest = max((_as_utc(r.fetched_at) for r in reads.values()), default=None)
        if newest and len(reads) == len(SOURCES) and _now() - newest < MANUAL_GAP:
            return False
        refresh(db, SOURCES, fetch)
        return True


# ---- montagem dos assuntos ----

def _contains(big: str, small: str) -> bool:
    return len(small) >= 4 and re.search(r"(?<![a-z0-9])" + re.escape(small) + r"(?![a-z0-9])", big) is not None


def _same(a: str, b: str) -> bool:
    return a == b or _contains(a, b) or _contains(b, a)


def _volume_bonus(volume: str | None) -> float:
    digits = re.sub(r"[^0-9]", "", volume or "")
    if not digits:
        return 0.0
    return min(10.0, math.log10(max(1, int(digits))) * 2.5)


def category_of(text: str) -> str:
    for name, pattern in CATEGORY_RE:
        if pattern.search(text):
            return name
    return "Outros"


def _items(db: Session, snapshot: RadarSnapshot | None) -> list[RadarItem]:
    if snapshot is None or not snapshot.ok:
        return []
    return list(db.scalars(select(RadarItem).where(RadarItem.snapshot_id == snapshot.id).order_by(RadarItem.rank)).all())


def _last_ok(db: Session, source: str) -> RadarSnapshot | None:
    return db.scalars(select(RadarSnapshot).where(RadarSnapshot.source == source, RadarSnapshot.ok.is_(True))
                      .order_by(RadarSnapshot.id.desc()).limit(1)).first()


def _x_hours(items: list[RadarItem]) -> list[list[RadarItem]]:
    hours: dict[int, list[RadarItem]] = {}
    for item in items:
        hours.setdefault(int((item.extra or {}).get("hour", 0)), []).append(item)
    return [sorted(hours[h], key=lambda i: i.rank) for h in sorted(hours)]


def _rank_in(hour: list[RadarItem], norm: str) -> int | None:
    for item in hour:
        if _same(item.norm, norm):
            return item.rank
    return None


def blocked_text(text: str) -> bool:
    return BLOCK_RE.search(src.norm(text)) is not None


def engage_of(topic: dict[str, Any]) -> int:
    """0–100: o quanto o assunto rende para post de texto (futebol, reality, show, data…); bloqueado = 0."""
    if topic["blocked"]:
        return 0
    text = " ".join([topic["norm"]] + [src.norm(n["title"]) for n in topic["news"][:3]])
    base = 40 if radar_ideas.is_person(topic["norm"], topic["category"]) else ENGAGE_BASE.get(topic["category"], 40)
    score = base + (15 if ENGAGE_BONUS_RE.search(text) else 0) + topic["heat"] * 0.2
    if topic.get("sources") == ["wiki"]:
        score *= 0.6  # só a Wikipédia: gente lida ontem, quase nunca é assunto do X agora
    return max(1, min(100, round(score)))


def build_topics(db: Session, region: str = "br") -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[list[RadarItem]]]:
    """(assuntos, manchetes sem assunto, horas do X) da região. No Brasil, títulos em escrita não latina saem."""
    names = REGIONS[region]

    def read(kind: str) -> list[RadarItem]:
        if kind not in names:
            return []
        items = _items(db, _last_ok(db, names[kind]))
        return [i for i in items if src.latin(i.title)] if region == "br" else items

    x_hours = _x_hours(read("x"))
    google, news, wiki = read("google"), read("news"), read("wiki")
    topics: list[dict[str, Any]] = []

    def topic_for(item: RadarItem) -> dict[str, Any]:
        for topic in topics:
            if _same(topic["norm"], item.norm):
                return topic
        topic = {"norm": item.norm, "title": item.title, "sources": {}, "news": [], "volume": None, "url": item.url}
        topics.append(topic)
        return topic

    lists = [("x", x_hours[0] if x_hours else []), ("google", google), ("wiki", wiki)]
    for source, items in lists:
        size = max(1, len(items))
        for item in items:
            topic = topic_for(item)
            if source not in topic["sources"]:
                topic["sources"][source] = {"rank": item.rank, "score": WEIGHTS[source] * (1 - (item.rank - 1) / size)}
            if source == "google":
                topic["volume"] = item.volume
                topic["news"] += (item.extra or {}).get("news", [])
    headlines = []
    for item in news:
        hits = [t for t in topics if _contains(item.norm, t["norm"])]
        entry = {"title": item.title, "url": item.url, "source": (item.extra or {}).get("source", "")}
        if not hits:
            headlines.append(entry)
            continue
        for topic in hits:
            topic["news"].append(entry)
            if "news" not in topic["sources"]:
                topic["sources"]["news"] = {"rank": item.rank, "score": WEIGHTS["news"] * (1 - (item.rank - 1) / max(1, len(news)))}
    for topic in topics:
        ranks = [_rank_in(hour, topic["norm"]) for hour in x_hours]
        now_rank = ranks[0] if ranks else None
        topic["rank_x"] = now_rank
        topic["hours_x"] = sum(1 for rank in ranks if rank is not None)
        earlier = next((rank for rank in ranks[2:3] if rank is not None), None) if len(ranks) > 2 else None
        if now_rank is None:
            topic["direction"] = "estavel"
        elif not any(rank is not None for rank in ranks[1:]):
            topic["direction"] = "novo"
        elif earlier is None or earlier - now_rank >= 3:
            topic["direction"] = "subindo"
        elif now_rank - earlier >= 3:
            topic["direction"] = "caindo"
        else:
            topic["direction"] = "estavel"
        three_ago = ranks[3] if len(ranks) > 3 else None
        topic["climb_3h"] = (three_ago - now_rank) if now_rank and three_ago else 0
        heat = sum(part["score"] for part in topic["sources"].values()) + _volume_bonus(topic["volume"])
        topic["heat"] = max(1, min(100, round(heat)))
        text = " ".join([topic["norm"]] + [src.norm(n["title"]) for n in topic["news"][:3]])
        topic["category"] = category_of(text)
        topic["ranks"] = {name: part["rank"] for name, part in topic["sources"].items()}
        topic["sources"] = [name for name in WEIGHTS if name in topic["sources"]]
        seen, unique = set(), []
        for item in topic["news"]:
            if item["url"] not in seen:
                seen.add(item["url"])
                unique.append(item)
        topic["news"] = unique[:6]
        if not topic["title"].startswith("#"):
            topic["title"] = radar_ideas.clean_title(topic["title"])  # "mônica fonseca" → "Mônica Fonseca"
        topic["blocked"] = blocked_text(text)
        topic["engage"] = engage_of(topic)
    topics.sort(key=lambda t: (-t["heat"], t["rank_x"] or 99))
    return topics[:MAX_TOPICS], headlines[:12], x_hours


def predictions(topics: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """O que pode virar trend: novo ou subindo em 2+ fontes, ou que subiu 10+ posições no X em 3 h."""
    out = []
    for topic in topics:
        reasons = []
        if topic["direction"] in ("novo", "subindo") and len(topic["sources"]) >= 2:
            reasons.append(f"{'novo' if topic['direction'] == 'novo' else 'subindo'} e em {len(topic['sources'])} fontes")
        if topic["climb_3h"] >= 10:
            reasons.append(f"subiu {topic['climb_3h']} posições no X em 3 h")
        elif topic["rank_x"] and topic["rank_x"] <= 10 and 1 <= topic["hours_x"] <= 2:
            reasons.append("entrou no top 10 do X em menos de 2 h")
        if reasons:
            out.append({"kind": "previsao", "name": topic["title"], "norm": topic["norm"], "tip": ("; ".join(reasons) + ".")[0].upper() + ("; ".join(reasons) + ".")[1:],
                        "in_days": 0, "date": None, "heat": topic["heat"]})
    return sorted(out, key=lambda item: -item["heat"])[:8]


def board(db: Session, now: datetime | None = None, region: str = "br") -> dict[str, Any]:
    now = now or _now()
    names = REGIONS[region]
    reads = {kind: read for kind, real in names.items() if (read := last_reads(db).get(real)) is not None}
    topics, headlines, _ = build_topics(db, region)
    today = now.astimezone(DEFAULT_TZ_INFO).date()
    ok_times = [_as_utc(r.fetched_at) for r in reads.values() if r.ok]
    public = [{key: t[key] for key in ("norm", "title", "sources", "ranks", "heat", "rank_x", "hours_x", "direction",
                                         "volume", "category", "url", "engage", "blocked")}
              | {"news": t["news"][:2], "ideas": radar_ideas.ideas(t)} for t in topics]
    return {
        "region": region,
        "updated_at": max(ok_times).isoformat() if ok_times else None,
        "sources": {kind: {"ok": reads[kind].ok, "at": _as_utc(reads[kind].fetched_at).isoformat(), "error": reads[kind].error}
                    if kind in reads else {"ok": None, "at": None, "error": None} for kind in names},
        "topics": public,
        "headlines": headlines,
        # Datas sazonais são do calendário brasileiro: só no Brasil.
        "upcoming": predictions(topics) + (seasonal.upcoming(today) if region == "br" else []),
    }


def topic_detail(db: Session, norm: str, region: str = "br") -> dict[str, Any] | None:
    topics, _, x_hours = build_topics(db, region)
    key = src.norm(norm)
    topic = next((t for t in topics if t["norm"] == key), None) or next((t for t in topics if _same(t["norm"], key)), None)
    if topic is None:
        return None
    history = []
    for hour in reversed(x_hours):  # mais velha primeiro, para o gráfico
        at = (hour[0].extra or {}).get("at") if hour else None
        history.append({"at": at, "rank": _rank_in(hour, topic["norm"])})
    detail = {key: value for key, value in topic.items() if key != "climb_3h"}
    detail["history_x"] = history
    detail["ideas"] = radar_ideas.ideas(topic)
    return detail


def _zone():
    from zoneinfo import ZoneInfo

    return ZoneInfo(DEFAULT_TZ)


DEFAULT_TZ_INFO = _zone()


# ---- Meus termos (alerta) ----

MAX_TERMS = 30
ALERT_WINDOW_HOURS = 6


def term_hits(term_norm: str, topics: list[dict[str, Any]], headlines: list[dict[str, Any]]) -> dict[str, Any] | None:
    """O assunto em alta que bate com o termo (por palavra inteira), ou uma manchete que o cita."""
    for topic in topics:
        if _same(topic["norm"], term_norm) or _contains(topic["norm"], term_norm):
            return topic
    for item in headlines:
        if _contains(src.norm(item["title"]), term_norm) or src.norm(item["title"]) == term_norm:
            return {"title": item["title"], "sources": ["news"], "ranks": {}, "url": item["url"], "norm": term_norm}
    return None


def my_terms(db: Session, owner_id: int, topics: list[dict[str, Any]], headlines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = db.scalars(select(RadarTerm).where(RadarTerm.owner_id == owner_id).order_by(RadarTerm.created_at, RadarTerm.id)).all()
    out = []
    for row in rows:
        hit = term_hits(row.norm, topics, headlines)
        out.append({"id": row.id, "term": row.term, "norm": row.norm, "hot": hit is not None,
                    "topic": hit["norm"] if hit else None})
    return out


def alert_text(term: str, hit: dict[str, Any]) -> str:
    where = " · ".join(
        f"{src_label} #{hit['ranks'][name]}" if hit["ranks"].get(name) else src_label
        for name, src_label in (("x", "X Brasil"), ("google", "Google"), ("news", "Notícias"), ("wiki", "Wikipédia"))
        if name in hit["sources"]
    )
    return f"📡 {term} em alta\nEm: {where}\nAssunto: {hit['title']}\nhttps://x.com/search?q={quote(term)}"


def check_terms(db: Session, now: datetime | None = None) -> int:
    """Depois de cada leitura: termo de alguém em alta vira alerta (uma vez por termo a cada janela de 6 h)."""
    from app.services import record_alert

    terms = db.scalars(select(RadarTerm)).all()
    if not terms:
        return 0
    topics, headlines, _ = build_topics(db)
    now = now or _now()
    window = now.astimezone(DEFAULT_TZ_INFO).strftime("%Y-%m-%d") + f"-{now.astimezone(DEFAULT_TZ_INFO).hour // ALERT_WINDOW_HOURS}"
    sent = 0
    for term in terms:
        hit = term_hits(term.norm, topics, headlines)
        if hit and record_alert(db, f"radar:{term.owner_id}:{term.norm}:{window}", "radar", alert_text(term.term, hit), term.owner_id):
            sent += 1
    return sent


# ---- Posts que estão engajando (Apify de quem clicou, guardado 30 min para todos) ----

POSTS_TTL = timedelta(minutes=30)
POSTS_SHOWN = 10
APIFY_SEARCH_TIMEOUT = 150.0
APIFY_ERRORS = {
    401: "o Apify recusou o token (401). Confira o token em Configurações.",
    402: "a conta do Apify está sem crédito (402).",
    403: "o Apify negou o acesso (403). O plano gratuito não roda esse ator pela API.",
    404: "ator do Apify não encontrado (404).",
    408: "o Apify demorou demais (408). Tente de novo.",
    429: "limite de chamadas do Apify (429). Tente de novo em instantes.",
}


def apify_search(token: str, term: str) -> list[dict[str, Any]]:
    """Busca os posts em português mais fortes do momento sobre o termo (mesmo ator do robô, sort Top)."""
    import robo

    url = f"{robo.APIFY_API}/acts/{robo.apify_actor()}/run-sync-get-dataset-items?timeout={int(APIFY_SEARCH_TIMEOUT) - 10}"
    body = {"searchTerms": [term], "sort": "Top", "tweetLanguage": "pt", "maxItems": 20}
    try:
        response = httpx.post(url, json=body, headers={"Authorization": f"Bearer {token}"}, timeout=APIFY_SEARCH_TIMEOUT)
    except httpx.HTTPError as exc:
        raise RadarError(f"sem resposta do Apify ({type(exc).__name__}).") from exc
    if response.status_code >= 400:
        raise RadarError(APIFY_ERRORS.get(response.status_code, f"o Apify respondeu {response.status_code}."))
    try:
        items = response.json()
    except ValueError as exc:
        raise RadarError("o Apify devolveu algo que não é JSON.") from exc
    return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []


def post_rows(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Posts prontos para a tela: sem repost, mais views primeiro, no máximo POSTS_SHOWN."""
    import robo

    rows = []
    for item in items:
        tid = robo.tweet_id_of(item)
        if not tid or robo.is_repost(item):
            continue
        author = item.get("author") if isinstance(item.get("author"), dict) else {}
        handle = str(robo.pick(author, "userName", "username", "screen_name", default="") or "")
        metrics = robo.tweet_metrics(item)
        posted = robo.parse_created_at(robo.pick(item, "createdAt", "created_at"))
        rows.append({
            "id": tid, "author": handle, "name": str(robo.pick(author, "name", default="") or handle),
            "text": (robo.tweet_text_of(item) or "")[:280], "views": metrics["view_count"], "likes": metrics["like_count"],
            "retweets": metrics["retweet_count"], "replies": metrics["reply_count"],
            "posted_at": posted.isoformat() if posted else None,
            "url": f"https://x.com/{handle}/status/{tid}" if handle else robo.pick(item, "url", "twitterUrl", default=None),
        })
    rows.sort(key=lambda row: -(row["views"] or 0))
    return rows[:POSTS_SHOWN]


def engaging_posts(db: Session, owner_id: int, token: str | None, term: str) -> dict[str, Any]:
    """Posts do cache (30 min, de qualquer usuário) ou uma busca nova com o token de quem pediu."""
    key = src.norm(term)
    if not key:
        raise ValueError("Termo vazio.")
    cutoff = _now() - POSTS_TTL
    cached = db.scalars(select(RadarPostCache).where(RadarPostCache.norm == key).order_by(RadarPostCache.id.desc()).limit(1)).first()
    if cached is not None and _as_utc(cached.fetched_at) >= cutoff:
        return {"term": term, "posts": cached.posts or [], "fetched_at": _as_utc(cached.fetched_at).isoformat(), "cached": True}
    if not (token or "").strip():
        raise PermissionError("Token do Apify em falta. Cole o seu em Configurações para buscar os posts.")
    rows = post_rows(apify_search(token.strip(), term))
    now = _now()
    db.execute(delete(RadarPostCache).where(RadarPostCache.norm == key).execution_options(synchronize_session=False))
    db.add(RadarPostCache(norm=key, owner_id=owner_id, fetched_at=now, posts=rows))
    db.commit()
    return {"term": term, "posts": rows, "fetched_at": now.isoformat(), "cached": False}
