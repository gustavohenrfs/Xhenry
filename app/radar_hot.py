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
HUNT_GAP = timedelta(hours=2)
HUNT_MAX_ITEMS = 120
KEEP = timedelta(days=45)
KEEP_MAX = 600
MIN_LIKES = 30
_hunting = threading.Lock()
SHOWN = 40

# Buscas do nicho na sintaxe do X: só português, sem resposta, sem link (conta de texto), com curtida mínima.
_TAIL = " lang:pt -filter:replies -filter:links"
HOT_QUERIES = [
    '"vocês homens"' + _TAIL + " min_faves:300",
    '"os homens gostam"' + _TAIL + " min_faves:300",
    '"homens" "estou curiosa"' + _TAIL + " min_faves:200",
    '"se o" ganhar "eu mando"' + _TAIL + " min_faves:200",
    'prometo nude' + _TAIL + " min_faves:200",
    '"fantasias sexuais"' + _TAIL + " min_faves:300",
    'homens gozam' + _TAIL + " min_faves:300",
    '"sinceramente homens"' + _TAIL + " min_faves:200",
    '"peluda ou lisinha"' + _TAIL + " min_faves:200",
    '"se esse post bater"' + _TAIL + " min_faves:300",
]

# (chave, nome na tela, por que funciona, regex no texto normalizado). A ordem decide: o primeiro que bate vale.
FORMATS = [
    ("promessa", "Promessa", "Promessa condicional: o público comenta para cobrar e torcer.",
     r"\bse (?:o |a |os |as )?[a-z0-9 ]{1,40}\b(?:ganhar|perder|passar|bater|chegar|vencer|fizer|tiver)\b|"
     r"\bprometo\b|\beu mando\b|\beu posto\b|\bse esse post\b"),
    ("lista", "Lista", "Lista com gancho: a curiosidade puxa para a próxima linha.",
     r"\b(?:fantasias|coisas|motivos|tipos|sinais)\b.{0,40}\bque (?:toda|todo|eu|voce|voces)\b|\ba primeira e\b|"
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


def hot_rows(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Itens do Apify → textos que servem: sem repost, resposta, link, venda, conteúdo proibido ou texto curto."""
    import robo

    rows, seen = [], set()
    for item in items:
        tid = robo.tweet_id_of(item)
        text = (robo.tweet_text_of(item) or "").strip()
        if not tid or tid in seen or robo.is_repost(item) or len(text) < 15 or text.startswith("@"):
            continue
        if unsafe(text) or has_cta(text) or item.get("isReply") is True:
            continue
        metrics = robo.tweet_metrics(item)
        if metrics["like_count"] < MIN_LIKES:
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
    return {
        "items": [item_of(row) for row in rows],
        "formats": [{"key": key, "label": label, "why": why, "count": counts.get(key, 0)}
                    for key, (label, why) in LABELS.items()],
        "total": sum(counts.values()),
        "hunted_at": at.isoformat() if at else None,
        "next_hunt_at": (at + HUNT_GAP).isoformat() if at and at + HUNT_GAP > now else None,
        "last": (hunt.posts or [{}])[0] if hunt else None,
    }


def hunt(db: Session, owner_id: int, token: str | None, now: datetime | None = None) -> dict[str, Any]:
    """Caçada nova no Apify (token de quem clicou) ou, dentro de HUNT_GAP, a biblioteca sem gastar nada."""
    from app import radar

    now = now or _now()
    previous = last_hunt(db)
    if previous is not None and _as_utc(previous.fetched_at) + HUNT_GAP > now:
        return library(db, now=now) | {"cached": True}
    if not (token or "").strip():
        raise PermissionError("Token do Apify em falta. Cole o seu em Configurações para caçar textos quentes.")
    if not _hunting.acquire(blocking=False):
        return library(db, now=now) | {"cached": True}  # outra pessoa já está caçando: ninguém paga duas vezes
    try:
        items = radar.apify_run(token.strip(), {"searchTerms": HOT_QUERIES, "sort": "Top", "tweetLanguage": "pt",
                                               "maxItems": HUNT_MAX_ITEMS})
        rows = hot_rows(items)
        new = store(db, rows, owner_id, now)
        for old in db.scalars(select(RadarPostCache).where(RadarPostCache.norm == HUNT_KEY)):
            db.delete(old)
        db.flush()
        db.add(RadarPostCache(norm=HUNT_KEY, owner_id=owner_id, fetched_at=now,
                              posts=[{"read": len(items), "kept": len(rows), "new": new}]))
        db.commit()
    finally:
        _hunting.release()
    return library(db, now=now) | {"cached": False}


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
