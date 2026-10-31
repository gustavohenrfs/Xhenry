"""Radar hot: os textos quentes que estão engajando no nicho (+18, público masculino) no X.

Duas fontes, nenhuma por busca de palavra (a busca do X esconde conteúdo sensível das contas que o Apify usa,
então quase nada do nicho aparece por lá):
- **Seus posts que mais bombaram** (grátis): o que o robô já leu das contas do próprio usuário (`tweet_samples`).
- **Contas do nicho** (`hot_sources`, Apify de quem clicou): o usuário lista as @ que ele vê bombando e a caçada
  lê os posts delas pelo perfil, do mesmo jeito que o robô lê as contas do painel. O que serve vai para uma
  biblioteca compartilhada (`hot_texts`).

Cada texto ganha um formato (promessa, enquete, pergunta polêmica, desafio, lista, confissão, provocação), que
diz por que ele engaja e serve de referência para as ideias de post e para o Grok.

Fora, sempre: menor de idade, sem consentimento, incesto (UNSAFE_RE) e chamada para link, bio, grupo, Telegram,
privado, Pix ou VIP (CTA_RE): o nicho aqui é só engajamento, nunca venda por link.
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
from app.models import HotSource, HotText, RadarPostCache, TweetSample, XAccount

HUNT_KEY = "__hot__"  # a última caçada de cada usuário fica em radar_post_cache com norm "__hot__:{owner}"
HUNT_GAP = timedelta(minutes=30)  # só depois de uma caçada que trouxe textos, com as mesmas contas
KEEP = timedelta(days=45)
KEEP_MAX = 600
MIN_LIKES = 10
SHOWN = 40
MAX_SOURCES = 25  # contas do nicho por usuário (uma execução do ator)
PER_ACCOUNT = 20  # posts lidos de cada conta
HUNT_TIMEOUT = 300.0  # o run-sync do Apify espera até 300 s; roda numa thread, a tela acompanha
MINE_DAYS = 90
HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")
_guard = threading.Lock()
_running: set[int] = set()  # donos com caçada em andamento (uma por vez por usuário)

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


def hunt_key(owner_id: int) -> str:
    return f"{HUNT_KEY}:{owner_id}"


def last_hunt(db: Session, owner_id: int) -> RadarPostCache | None:
    return db.scalars(select(RadarPostCache).where(RadarPostCache.norm == hunt_key(owner_id))
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


def clean_handle(raw: str) -> str | None:
    """"@Fulana", "x.com/fulana", "https://twitter.com/fulana/status/1" → "fulana"; inválido → None."""
    text = (raw or "").strip()
    match = re.search(r"(?:x|twitter)\.com/(?:#!/)?@?([A-Za-z0-9_]{1,15})", text)
    handle = match.group(1) if match else text.lstrip("@").strip()
    return handle if HANDLE_RE.match(handle) and handle.lower() not in {"home", "search", "i", "intent"} else None


def sources_of(db: Session, owner_id: int) -> list[str]:
    return list(db.scalars(select(HotSource.handle).where(HotSource.owner_id == owner_id).order_by(HotSource.id)))


def hunt_ready_at(hunt: RadarPostCache | None, handles: list[str]) -> datetime | None:
    """Até quando caçar de novo só devolve a biblioteca: caçada boa (trouxe texto) com as mesmas contas."""
    if hunt is None:
        return None
    info = (hunt.posts or [{}])[0]
    if info.get("error") or not info.get("kept"):
        return None
    if sorted(h.lower() for h in info.get("handles") or []) != sorted(h.lower() for h in handles):
        return None  # mudou a lista de contas: pode caçar na hora
    return _as_utc(hunt.fetched_at) + HUNT_GAP


def mine(db: Session, owner_id: int, now: datetime, fmt: str | None = None, limit: int = SHOWN) -> list[dict[str, Any]]:
    """Os posts do próprio usuário que mais bombaram (90 dias), do que o robô já leu: grátis."""
    rows = db.execute(
        select(TweetSample.tweet_id, TweetSample.text_preview, TweetSample.view_count, TweetSample.like_count,
               TweetSample.reply_count, TweetSample.retweet_count, TweetSample.posted_at, XAccount.username)
        .join(XAccount, XAccount.id == TweetSample.account_id)
        .where(XAccount.owner_id == owner_id, TweetSample.text_preview.is_not(None),
               TweetSample.posted_at >= now - timedelta(days=MINE_DAYS))
    ).all()
    out, seen = [], set()
    for tid, text, views, likes, replies, rts, posted, handle in rows:
        text = (text or "").strip()
        if tid in seen or len(text) < 15 or text.startswith(("RT @", "@")) or unsafe(text) or has_cta(text):
            continue
        seen.add(tid)
        kind = format_of(text)
        if fmt and kind != fmt:
            continue
        label, why = LABELS[kind]
        out.append({"id": None, "tweet_id": tid, "text": text, "author": handle, "views": views or 0, "likes": likes or 0,
                    "replies": replies or 0, "retweets": rts or 0, "fmt": kind, "fmt_label": label, "why": why,
                    "score": score_of(likes or 0, rts or 0, replies or 0, views or 0),
                    "url": f"https://x.com/{handle}/status/{tid}" if handle else None,
                    "posted_at": _as_utc(posted).isoformat() if posted else None})
    out.sort(key=lambda item: -item["score"])
    return out[:limit]


def library(db: Session, owner_id: int, fmt: str | None = None, limit: int = SHOWN,
            now: datetime | None = None) -> dict[str, Any]:
    """A tela: textos das contas do nicho (biblioteca), os seus que mais bombaram, as contas e a última caçada."""
    now = now or _now()
    query = select(HotText).order_by(HotText.score.desc(), HotText.id.desc())
    if fmt:
        query = query.where(HotText.fmt == fmt)
    rows = db.scalars(query.limit(limit)).all()
    counts = dict(db.execute(select(HotText.fmt, func.count()).group_by(HotText.fmt)).all())
    handles = sources_of(db, owner_id)
    hunt = last_hunt(db, owner_id)
    at = _as_utc(hunt.fetched_at) if hunt else None
    ready = hunt_ready_at(hunt, handles)
    return {
        "items": [item_of(row) for row in rows],
        "mine": mine(db, owner_id, now, fmt),
        "sources": handles,
        "max_sources": MAX_SOURCES,
        "formats": [{"key": key, "label": label, "why": why, "count": counts.get(key, 0)}
                    for key, (label, why) in LABELS.items()],
        "total": sum(counts.values()),
        "hunted_at": at.isoformat() if at else None,
        "next_hunt_at": ready.isoformat() if ready and ready > now else None,
        "last": (hunt.posts or [{}])[0] if hunt else None,
        "hunting": owner_id in _running,
    }


def hunt_body(handles: list[str]) -> dict[str, Any]:
    """O mesmo pedido do robô (leitura por perfil, que o ator faz bem): posts mais novos de cada conta."""
    return {"twitterHandles": handles, "maxItems": PER_ACCOUNT * len(handles), "sort": "Latest"}


def _stamp(db: Session, owner_id: int, now: datetime, info: dict[str, Any]) -> None:
    for old in db.scalars(select(RadarPostCache).where(RadarPostCache.norm == hunt_key(owner_id))):
        db.delete(old)
    db.flush()
    db.add(RadarPostCache(norm=hunt_key(owner_id), owner_id=owner_id, fetched_at=now, posts=[info]))
    db.commit()


def hunt_now(db: Session, owner_id: int, token: str, handles: list[str], now: datetime | None = None) -> dict[str, Any]:
    """Uma caçada no Apify: guarda os textos e deixa o diagnóstico (lidos por conta, aproveitados, motivos, erro)."""
    from app import radar

    now = now or _now()
    try:
        items = radar.apify_run(token, hunt_body(handles), timeout=HUNT_TIMEOUT)
    except radar.RadarError as exc:
        _stamp(db, owner_id, now, {"error": str(exc), "read": 0, "kept": 0, "new": 0, "handles": handles})
        raise
    per_handle = {handle: 0 for handle in handles}
    lower = {handle.lower(): handle for handle in handles}
    for item in items:
        author = item.get("author") if isinstance(item.get("author"), dict) else {}
        name = str(author.get("userName") or "").lower()
        if name in lower:
            per_handle[lower[name]] += 1
    reasons: dict[str, int] = {}
    rows = hot_rows(items, reasons)
    new = store(db, rows, owner_id, now)
    info = {"read": len(items), "kept": len(rows), "new": new, "reasons": reasons, "per_handle": per_handle,
            "handles": handles}
    _stamp(db, owner_id, now, info)
    return info


def start_hunt(owner_id: int, token: str | None, db: Session, now: datetime | None = None) -> dict[str, Any]:
    """Começa a caçada das contas do nicho em segundo plano; logo depois de uma boa, só devolve a biblioteca."""
    now = now or _now()
    handles = sources_of(db, owner_id)
    if not handles:
        raise ValueError("Adicione pelo menos uma conta do nicho para caçar (as @ que você vê bombando).")
    ready = hunt_ready_at(last_hunt(db, owner_id), handles)
    if ready and ready > now:
        return library(db, owner_id, now=now) | {"cached": True}
    if not (token or "").strip():
        raise PermissionError("Token do Apify em falta. Cole o seu em Configurações para caçar textos quentes.")
    with _guard:
        if owner_id in _running:
            return library(db, owner_id, now=now) | {"cached": False}  # já está caçando: não paga duas vezes
        _running.add(owner_id)
    key = token.strip()

    def work() -> None:
        from app.database import SessionLocal

        try:
            with SessionLocal() as own:
                hunt_now(own, owner_id, key, handles)
        except Exception:  # o erro legível já ficou gravado na caçada (ou é defeito, que vai para o log)
            import logging

            logging.getLogger("xhenry").exception("radar hot: caçada falhou")
        finally:
            with _guard:
                _running.discard(owner_id)

    run_in_background(work)
    return library(db, owner_id, now=now) | {"cached": False, "hunting": True}


def run_in_background(work) -> None:
    threading.Thread(target=work, name="radar-hot-cacar", daemon=True).start()


def examples(db: Session, n: int = 6, owner_id: int | None = None) -> list[str]:
    """Os melhores textos (os do próprio usuário primeiro, depois a biblioteca), um por formato primeiro:
    referência de tom para o Grok."""
    texts = [item["text"] for item in mine(db, owner_id, _now(), limit=20)] if owner_id else []
    texts += [row.text for row in db.scalars(select(HotText).order_by(HotText.score.desc()).limit(80))]
    out, used = [], set()
    for text in texts:
        kind = format_of(text)
        if kind not in used and text not in out:
            used.add(kind)
            out.append(text)
    out += [text for text in texts if text not in out]
    return [" ".join(text.split())[:240] for text in out[:n]]
