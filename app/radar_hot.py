"""Radar hot: os textos quentes do nicho (+18, público masculino) que estão engajando no X.

"Caçar textos quentes" roda o ator do Apify (o mesmo do robô, com o token de quem clicou) com buscas do nicho
na sintaxe de busca do X e guarda o que veio numa biblioteca compartilhada (`hot_texts`). Cada texto ganha um
formato (promessa, enquete, pergunta polêmica, desafio, lista, confissão, provocação), que diz por que ele
engaja e serve de referência para as ideias de post e para o Grok.

Fora da biblioteca, sempre: menor de idade, sem consentimento, incesto (UNSAFE_RE) e chamada para link, bio,
grupo, Telegram, privado, Pix ou VIP (CTA_RE): o nicho aqui é só engajamento, nunca venda por link.
"""

from __future__ import annotations

import re
import threading
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app import radar_sources as src
from app.models import HotText, RadarPostCache

HUNT_KEY = "__hot__"  # a última caçada fica em radar_post_cache com esse norm
HUNT_GAP = timedelta(hours=2)  # só depois de uma caçada que trouxe textos; vazia ou com erro não trava nada
KEEP = timedelta(days=45)
KEEP_MAX = 600
MIN_LIKES = 30
SHOWN = 40
# Limites do ator apidojo/tweet-scraper: no máximo 5 buscas por execução e cada busca precisa render pelo
# menos 50 posts (abaixo disso ele cobra o mínimo e devolve pouco ou nada). Curtida mínima vai no campo
# minimumFavorites (min_faves: na busca não é documentado).
HUNT_BATCH = 5
PER_QUERY = 50
MIN_FAVORITES = 100
HUNT_TIMEOUT = 300.0  # o run-sync do Apify espera até 300 s; roda numa thread, a tela acompanha
# Buscas do nicho (amplas o bastante para render 50 posts cada). Cada caçada usa as 5 seguintes, em rodízio.
_TAIL = " -filter:replies -filter:links"
QUERY_POOL = [
    "vocês homens" + _TAIL,
    "os homens gostam" + _TAIL,
    "homens gozam" + _TAIL,
    "prometo nude" + _TAIL,
    "se esse post bater" + _TAIL,
    "peluda ou lisinha" + _TAIL,
    "estou curiosa homens" + _TAIL,
    "fantasias sexuais" + _TAIL,
    "sinceramente homens" + _TAIL,
    "duvido um homem" + _TAIL,
    "homem que aguenta" + _TAIL,
    "qual vocês preferem" + _TAIL,
    "mando nude" + _TAIL,
    "homens preferem" + _TAIL,
    "confesso que" + _TAIL,
]
HOT_QUERIES = QUERY_POOL  # nome antigo
_hunting = threading.Lock()
_state: dict[str, Any] = {"running": False, "started_at": None}

# (chave, nome na tela, por que funciona, regex no texto normalizado). A ordem decide: o primeiro que bate vale.
FORMATS = [
    ("promessa", "Promessa", "Promessa condicional: o público comenta para cobrar e torcer.",
     r"\bse (?:o |a |os |as )?[a-z0-9 ]{1,40}\b(?:ganhar|perder|passar|bater|chegar|vencer|fizer|tiver)\b|"
     r"\bprometo\b|\beu mando\b|\beu posto\b|\bse esse post\b"),
    ("lista", "Lista", "Lista com gancho: a curiosidade puxa para a próxima linha.",
     r"\b(?:fantasias|coisas|motivos|tipos|sinais)\b.{0,40}\bque (?:toda|todo|eu|voce|voces)\b|\ba primeira e\b|\bo primeiro e\b|"
     r"\b\d+ coisas\b"),
    ("enquete", "Enquete", "Duas opções: todo mundo tem opinião e responde.",
     r"\b[a-z]+ ou [a-z]+\b[^?]{0,40}\?|\bqual (?:voces|vcs) preferem\b|\bescolhe(?:m)? (?:um|uma)\b"),
    ("desafio", "Desafio", "Desafio ao ego: quem discorda comenta para provar.",
     r"\b(?:duvido|aposto|desafio|quem aguenta|ninguem aguenta|aguentaria|aguentam|consegue(?:m)?)\b"),
    ("confissao", "Confissão", "Confissão ou curiosidade: convida a explicar e a contar.",
     r"\b(?:confesso|confissao|admito|estou curiosa|to curiosa|curiosa)\b"),
    ("pergunta", "Pergunta polêmica", "Pergunta direto aos homens: mexe com o ego e vira discussão.", r"\?"),
]
FALLBACK = ("provocacao", "Provocação", "Duplo sentido: cada um entende do seu jeito e responde.")
FORMAT_RE = [(key, re.compile(pattern)) for key, _, _, pattern in FORMATS]
LABELS = {key: (label, why) for key, label, why, _ in FORMATS} | {FALLBACK[0]: FALLBACK[1:]}

# Nunca entra (nem nas ideias): menor de idade, sem consentimento, incesto.
UNSAFE_RE = re.compile(
    r"\b(?:novinha|novinhas|novinho|novinhos|ninfet\w*|colegia\w*|lolita\w*|teen|teens|pirralh\w*|crianca\w*|"
    r"menor|menores|menina de \d+|adolescente\w*|infantil|pedofil\w*|estupr\w*|abusad\w*|abuso|dopad\w*|"
    r"drogad\w*|dormindo|desacordad\w*|inconsciente|sem consentimento|forcad\w*|incesto|enteada|enteado|"
    r"irma|irmao|sobrinha|sobrinho|madrasta|padrasto)\b")
# Chamada para fora do X (link, bio, grupo, Telegram, venda): fora da biblioteca e de qualquer ideia.
CTA_RE = re.compile(
    r"(?:https?://|www\.|\blink\b|\bbio\b|telegram|\btg\b|\bgrupo\b|\bcanal\b|privacy|onlyfans|\bonly\b|\bpv\b|"
    r"\bprivado\b|\bdm\b|\bpix\b|\bvip\b|assinatura|\bassina\b|\bcupom\b|\bpromocao\b|whats\w*|\bzap\b)")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _plain(text: str) -> str:
    """Como src.norm, mas mantém o "?" (pergunta e enquete dependem dele)."""
    flat = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()
    return " ".join(re.sub(r"[^a-z0-9?]+", " ", flat).split())


def format_of(text: str) -> str:
    key = _plain(text)
    for name, pattern in FORMAT_RE:
        if pattern.search(key):
            return name
    return FALLBACK[0]


def unsafe(text: str) -> bool:
    return UNSAFE_RE.search(src.norm(text)) is not None


def has_cta(text: str) -> bool:
    return CTA_RE.search(src.norm(text) + " " + (text or "").lower()) is not None


def score_of(likes: int, retweets: int, replies: int, views: int) -> int:
    """Resposta vale mais: é o que o algoritmo do X mais premia num texto."""
    return int(likes + 2 * retweets + 3 * replies + views // 200)


def hot_rows(items: list[dict[str, Any]], reasons: dict[str, int] | None = None) -> list[dict[str, Any]]:
    """Itens do Apify → textos que servem: sem repost, resposta, link, venda, conteúdo proibido ou texto curto.
    reasons (opcional) conta por que cada item ficou de fora, para o diagnóstico da caçada."""
    import robo

    def skip(reason: str) -> None:
        if reasons is not None:
            reasons[reason] = reasons.get(reason, 0) + 1

    rows, seen = [], set()
    for item in items:
        tid = robo.tweet_id_of(item)
        text = (robo.tweet_text_of(item) or "").strip()
        if not tid:
            skip("vazio")
            continue
        if tid in seen:
            skip("repetido")
            continue
        if robo.is_repost(item) or item.get("isRetweet") is True:
            skip("repost")
            continue
        if text.startswith("@") or item.get("isReply") is True:
            skip("resposta")
            continue
        if len(text) < 15:
            skip("curto")
            continue
        if unsafe(text):
            skip("proibido")
            continue
        if has_cta(text):
            skip("link_ou_venda")
            continue
        metrics = robo.tweet_metrics(item)
        if metrics["like_count"] < MIN_LIKES:
            skip("poucas_curtidas")
            continue
        author = item.get("author") if isinstance(item.get("author"), dict) else {}
        handle = str(robo.pick(author, "userName", "username", "screen_name", default="") or "")[:80]
        posted = robo.parse_created_at(robo.pick(item, "createdAt", "created_at"))
        seen.add(tid)
        rows.append({
            "tweet_id": tid, "author": handle, "text": text[:600],
            "url": f"https://x.com/{handle}/status/{tid}" if handle else robo.pick(item, "url", "twitterUrl", default=None),
            "views": metrics["view_count"], "likes": metrics["like_count"], "retweets": metrics["retweet_count"],
            "replies": metrics["reply_count"], "posted_at": posted, "fmt": format_of(text),
            "score": score_of(metrics["like_count"], metrics["retweet_count"], metrics["reply_count"], metrics["view_count"]),
        })
    return rows


def last_hunt(db: Session) -> RadarPostCache | None:
    return db.scalars(select(RadarPostCache).where(RadarPostCache.norm == HUNT_KEY)
                      .order_by(RadarPostCache.id.desc()).limit(1)).first()


def store(db: Session, rows: list[dict[str, Any]], owner_id: int | None, now: datetime) -> int:
    """Guarda (ou atualiza os números de) cada texto; devolve quantos são novos. Depois poda a biblioteca."""
    known = {row.tweet_id: row for row in db.scalars(select(HotText).where(HotText.tweet_id.in_([r["tweet_id"] for r in rows])))} if rows else {}
    new = 0
    for data in rows:
        row = known.get(data["tweet_id"])
        if row is None:
            db.add(HotText(found_at=now, owner_id=owner_id, **data))
            new += 1
            continue
        for key in ("views", "likes", "retweets", "replies", "score", "fmt", "text"):
            setattr(row, key, data[key])
    db.flush()
    db.execute(delete(HotText).where(HotText.found_at < now - KEEP).execution_options(synchronize_session=False))
    keep = select(HotText.id).order_by(HotText.score.desc(), HotText.id.desc()).limit(KEEP_MAX)
    db.execute(delete(HotText).where(HotText.id.not_in(keep.scalar_subquery())).execution_options(synchronize_session=False))
    return new


def item_of(row: HotText) -> dict[str, Any]:
    label, why = LABELS.get(row.fmt, FALLBACK[1:])
    return {"id": row.id, "text": row.text, "author": row.author, "url": row.url, "views": row.views, "likes": row.likes,
            "retweets": row.retweets, "replies": row.replies, "score": row.score, "fmt": row.fmt, "fmt_label": label,
            "why": why, "posted_at": _as_utc(row.posted_at).isoformat() if row.posted_at else None}


def hunt_ready_at(hunt: RadarPostCache | None) -> datetime | None:
    """Até quando uma nova caçada só devolve a biblioteca: só conta caçada que deu certo e trouxe texto."""
    if hunt is None:
        return None
    info = (hunt.posts or [{}])[0]
    if info.get("error") or not info.get("kept"):
        return None
    return _as_utc(hunt.fetched_at) + HUNT_GAP


def library(db: Session, fmt: str | None = None, limit: int = SHOWN, now: datetime | None = None) -> dict[str, Any]:
    """A biblioteca para a tela: os de nota mais alta (filtro por formato), contagem por formato e a caçada."""
    now = now or _now()
    query = select(HotText).order_by(HotText.score.desc(), HotText.id.desc())
    if fmt:
        query = query.where(HotText.fmt == fmt)
    rows = db.scalars(query.limit(limit)).all()
    counts = dict(db.execute(select(HotText.fmt, func.count()).group_by(HotText.fmt)).all())
    hunt = last_hunt(db)
    at = _as_utc(hunt.fetched_at) if hunt else None
    ready = hunt_ready_at(hunt)
    return {
        "items": [item_of(row) for row in rows],
        "formats": [{"key": key, "label": label, "why": why, "count": counts.get(key, 0)}
                    for key, (label, why) in LABELS.items()],
        "total": sum(counts.values()),
        "hunted_at": at.isoformat() if at else None,
        "next_hunt_at": ready.isoformat() if ready and ready > now else None,
        "last": (hunt.posts or [{}])[0] if hunt else None,
        "hunting": bool(_state["running"]),
    }


def next_batch(hunt: RadarPostCache | None) -> tuple[list[str], int]:
    """As próximas HUNT_BATCH buscas do rodízio e onde a seguinte começa."""
    start = int(((hunt.posts or [{}])[0] if hunt else {}).get("next", 0) or 0) % len(QUERY_POOL)
    batch = [QUERY_POOL[(start + i) % len(QUERY_POOL)] for i in range(HUNT_BATCH)]
    return batch, (start + HUNT_BATCH) % len(QUERY_POOL)


def hunt_body(queries: list[str]) -> dict[str, Any]:
    return {"searchTerms": queries, "sort": "Top", "tweetLanguage": "pt", "minimumFavorites": MIN_FAVORITES,
            "maxItems": PER_QUERY * len(queries)}


def _stamp(db: Session, owner_id: int | None, now: datetime, info: dict[str, Any]) -> None:
    for old in db.scalars(select(RadarPostCache).where(RadarPostCache.norm == HUNT_KEY)):
        db.delete(old)
    db.flush()
    db.add(RadarPostCache(norm=HUNT_KEY, owner_id=owner_id, fetched_at=now, posts=[info]))
    db.commit()


def hunt_now(db: Session, owner_id: int, token: str, now: datetime | None = None) -> dict[str, Any]:
    """Uma caçada no Apify: guarda os textos e deixa o diagnóstico (lidos, aproveitados, motivos, erro)."""
    from app import radar

    now = now or _now()
    queries, following = next_batch(last_hunt(db))
    try:
        items = radar.apify_run(token, hunt_body(queries), timeout=HUNT_TIMEOUT)
    except radar.RadarError as exc:
        # Erro não avança o rodízio nem trava as 2 h: tentar de novo repete as mesmas buscas.
        _stamp(db, owner_id, now, {"error": str(exc), "read": 0, "kept": 0, "new": 0,
                                   "next": QUERY_POOL.index(queries[0]), "queries": queries})
        raise
    reasons: dict[str, int] = {}
    rows = hot_rows(items, reasons)
    new = store(db, rows, owner_id, now)
    info = {"read": len(items), "kept": len(rows), "new": new, "reasons": reasons, "next": following, "queries": queries}
    _stamp(db, owner_id, now, info)
    return info


def start_hunt(owner_id: int, token: str | None, db: Session, now: datetime | None = None) -> dict[str, Any]:
    """Começa uma caçada em segundo plano; dentro de HUNT_GAP de uma caçada boa, só devolve a biblioteca."""
    now = now or _now()
    ready = hunt_ready_at(last_hunt(db))
    if ready and ready > now:
        return library(db, now=now) | {"cached": True}
    if not (token or "").strip():
        raise PermissionError("Token do Apify em falta. Cole o seu em Configurações para caçar textos quentes.")
    if not _hunting.acquire(blocking=False):
        return library(db, now=now) | {"cached": False}  # outra pessoa já está caçando: ninguém paga duas vezes
    _state.update(running=True, started_at=now.isoformat())
    key = token.strip()

    def work() -> None:
        from app.database import SessionLocal

        try:
            with SessionLocal() as own:
                hunt_now(own, owner_id, key)
        except Exception:  # o erro legível já ficou gravado na caçada (ou é um defeito, que vai para o log)
            import logging

            logging.getLogger("xhenry").exception("radar hot: caçada falhou")
        finally:
            _state.update(running=False)
            _hunting.release()

    run_in_background(work)
    return library(db, now=now) | {"cached": False, "hunting": True}


def run_in_background(work) -> None:
    threading.Thread(target=work, name="radar-hot-cacar", daemon=True).start()


def examples(db: Session, n: int = 6) -> list[str]:
    """Os melhores textos da biblioteca, um por formato primeiro: referência de tom para o Grok."""
    rows = db.scalars(select(HotText).order_by(HotText.score.desc()).limit(80)).all()
    out, used = [], set()
    for row in rows:
        if row.fmt not in used:
            used.add(row.fmt)
            out.append(row.text)
    out += [row.text for row in rows if row.text not in out]
    return [" ".join(text.split())[:240] for text in out[:n]]
