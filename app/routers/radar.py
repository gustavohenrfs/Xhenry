"""Aba Radar de trend: o que está em alta no Brasil, datas sazonais, o que pode virar trend, os termos que
cada usuário vigia (alerta kind=radar) e os posts que estão engajando (Apify de quem clicou).

Só lê o banco (app/radar.py monta). As fontes são lidas pelo vigia; se a última leitura tiver mais de 15 min
ao abrir a aba, uma leitura começa numa thread e a resposta já sai com o que existe ("refreshing": true).
"""

from __future__ import annotations

import logging
import threading
from uuid import uuid4
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import radar, radar_hot, radar_ideas, radar_keywords, seasonal
from app.database import SessionLocal, get_db
from app.models import HotSource, HotText, RadarTerm, User
from app.radar_sources import norm as norm_of
from app.routers.auth import approved_user
from app.services import as_utc, now_utc, user_settings, xai_key

log = logging.getLogger("xhenry")
router = APIRouter(dependencies=[Depends(approved_user)])
STALE = timedelta(minutes=15)
_background = threading.Lock()


def _refresh_in_background() -> bool:
    """Lê as fontes vencidas numa thread; False se já tem uma rodando."""
    if not _background.acquire(blocking=False):
        return False

    def work() -> None:
        try:
            with SessionLocal() as db:
                radar.refresh(db, radar.refresh_due(db))
        except Exception:
            log.exception("radar: leitura em segundo plano falhou")
        finally:
            _background.release()

    threading.Thread(target=work, name="radar-ler", daemon=True).start()
    return True


HEAT = Query("leve", pattern="^(leve|quente)$")
SEED = Query(0, ge=0, le=10_000)


class TermIn(BaseModel):
    term: str = Field(min_length=1, max_length=80)


class IdeasIn(BaseModel):
    norm: str = Field(min_length=1, max_length=400)
    heat: str = Field(default="leve", pattern="^(leve|quente)$")


class HotGrokIn(BaseModel):
    id: int | None = None  # texto da biblioteca para recriar
    text: str | None = Field(default=None, max_length=300)  # ou uma frase qualquer ("Mais como essa")
    fmt: str | None = Field(default=None, pattern="^[a-z]{3,16}$")  # textos novos todos nesse formato
    trend: bool = False  # textos novos podem pegar carona nos assuntos em alta
    mode: str | None = Field(default=None, pattern="^campeoes$")  # variações dos textos que mais bombaram
    accounts: bool = False  # "Com base nas contas": manda ao Grok o que viralizou nas suas contas e no nicho
    heat: str = Field(default="quente", pattern="^(leve|quente)$")


def proven_for(db: Session, user: User, heat: str) -> list[dict[str, Any]] | None:
    """Os textos que já engajaram (suas contas + contas do nicho): entram nas ideias do nível quente."""
    return radar_hot.proven(db, user.id) if heat == "quente" else None


def with_my_terms(db: Session, user: User, payload: dict[str, Any]) -> dict[str, Any]:
    topics, headlines, _ = radar.build_topics(db)
    payload["my_terms"] = radar.my_terms(db, user.id, topics, headlines)
    return payload


@router.get("/api/radar")
def read_radar(heat: str = HEAT, user: User = Depends(approved_user),
               db: Session = Depends(get_db)) -> dict[str, Any]:
    payload = with_my_terms(db, user, radar.board(db, heat=heat, proven=proven_for(db, user, heat)))
    updated = payload["updated_at"]
    stale = updated is None or now_utc() - as_utc(radar.parse_iso(updated)) > STALE
    payload["refreshing"] = bool(stale and radar.radar_enabled() and _refresh_in_background())
    return payload


@router.post("/api/radar/refresh")
def refresh_radar(heat: str = HEAT, user: User = Depends(approved_user),
                  db: Session = Depends(get_db)) -> dict[str, Any]:
    if not radar.refresh_manual(db):
        raise HTTPException(429, "O radar acabou de ser atualizado. Tente de novo em 2 minutos.")
    payload = with_my_terms(db, user, radar.board(db, heat=heat, proven=proven_for(db, user, heat)))
    payload["refreshing"] = False
    return payload


@router.get("/api/radar/topic")
def read_topic(norm: str = Query(..., min_length=1, max_length=400), heat: str = HEAT,
               user: User = Depends(approved_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    detail = radar.topic_detail(db, norm, heat=heat, proven=proven_for(db, user, heat))
    if detail is None:
        raise HTTPException(404, "Esse assunto saiu do radar.")
    return detail


@router.get("/api/radar/terms")
def list_terms(user: User = Depends(approved_user), db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    topics, headlines, _ = radar.build_topics(db)
    return radar.my_terms(db, user.id, topics, headlines)


@router.post("/api/radar/terms", status_code=201)
def add_term(payload: TermIn, user: User = Depends(approved_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    term = " ".join(payload.term.split())
    key = norm_of(term)
    if not key:
        raise HTTPException(400, "Escreva um termo com letras ou números.")
    if db.scalar(select(RadarTerm.id).where(RadarTerm.owner_id == user.id, RadarTerm.norm == key)):
        raise HTTPException(409, "Você já vigia esse termo.")
    if (db.scalar(select(func.count()).select_from(RadarTerm).where(RadarTerm.owner_id == user.id)) or 0) >= radar.MAX_TERMS:
        raise HTTPException(400, f"Limite de {radar.MAX_TERMS} termos. Remova um para adicionar outro.")
    row = RadarTerm(owner_id=user.id, term=term, norm=key)
    db.add(row)
    db.commit()
    return {"id": row.id, "term": row.term, "norm": row.norm}


@router.delete("/api/radar/terms/{term_id}", status_code=204)
def delete_term(term_id: int, user: User = Depends(approved_user), db: Session = Depends(get_db)) -> Response:
    row = db.get(RadarTerm, term_id)
    if row is None or row.owner_id != user.id:
        raise HTTPException(404, "Termo não encontrado.")
    db.delete(row)
    db.commit()
    return Response(status_code=204)


@router.post("/api/radar/posts")
def engaging_posts(payload: TermIn, user: User = Depends(approved_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Posts mais fortes do momento sobre o assunto. Gasta crédito do Apify de quem clicou (cache de 30 min)."""
    try:
        return radar.engaging_posts(db, user.id, user_settings(db, user.id).apify_token, payload.term.strip())
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(400, str(exc)) from exc
    except radar.RadarError as exc:
        raise HTTPException(502, "A busca no Apify falhou: " + str(exc)) from exc


def idea_subject(db: Session, norm: str) -> tuple[dict[str, Any], str | None]:
    """O assunto (ou a data sazonal) para as ideias de post; 404 se saiu do radar."""
    topic = radar.topic_detail(db, norm)
    if topic is not None:
        return topic, None
    key = norm_of(norm)
    for item in seasonal.upcoming(now_utc().astimezone(radar.DEFAULT_TZ_INFO).date(), 30):
        if norm_of(item["name"]) == key:
            return {"title": item["name"], "norm": key, "category": "Datas", "blocked": False}, "data"
    raise HTTPException(404, "Esse assunto saiu do radar.")


@router.get("/api/radar/ideas")
def read_ideas(norm: str = Query(..., min_length=1, max_length=400), heat: str = HEAT,
               seed: int = SEED, user: User = Depends(approved_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Ideias de post grátis (modelos por tipo de assunto). seed>0 sorteia outra combinação. Sensível = sem ideias."""
    topic, kind = idea_subject(db, norm)
    return {"norm": topic["norm"], "title": topic["title"], "blocked": bool(topic.get("blocked")),
            "ideas": radar_ideas.ideas(topic, kind, heat=heat, seed=seed, proven=proven_for(db, user, heat))}


@router.post("/api/radar/ideas/grok")
def grok_ideas(payload: IdeasIn, user: User = Depends(approved_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Mais ideias com o Grok, pela chave xAI do usuário."""
    topic, _ = idea_subject(db, payload.norm)
    if topic.get("blocked"):
        raise HTTPException(400, "Assunto sensível (morte, tragédia, crime, doença ou criança): sem ideias de post.")
    key = xai_key(db, user)
    if not key:
        raise HTTPException(400, "Cole a sua chave da xAI em Configurações para pedir ideias ao Grok.")
    try:
        prompt = radar_ideas.grok_prompt(topic, payload.heat, radar_hot.proven_refs(db, user.id))
        lines = radar_ideas.parse_lines(radar_ideas.ask_grok_lines(key, prompt), limit=8)
    except ValueError as exc:
        raise HTTPException(502, str(exc)) from exc
    if not lines:
        raise HTTPException(502, "O Grok não devolveu ideias. Tente de novo.")
    return {"norm": topic["norm"], "title": topic["title"], "ideas": lines}


# ---- Radar hot: textos quentes do nicho (+18, público masculino) ----

class SourceIn(BaseModel):
    handle: str = Field(min_length=1, max_length=200)


@router.get("/api/radar/hot")
def hot_library(fmt: str | None = Query(None, pattern="^[a-z]{3,16}$"), user: User = Depends(approved_user),
                db: Session = Depends(get_db)) -> dict[str, Any]:
    """Textos das contas do nicho, os seus que mais bombaram e a última caçada (grátis: só lê o banco)."""
    return radar_hot.library(db, user.id, fmt)


@router.post("/api/radar/hot/hunt")
def hot_hunt(user: User = Depends(approved_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Começa a caçada das contas do nicho no Apify de quem clicou (em segundo plano: a tela acompanha por
    GET /api/radar/hot). Logo depois de uma caçada boa com as mesmas contas, devolve a biblioteca sem gastar."""
    try:
        return radar_hot.start_hunt(user.id, user_settings(db, user.id).apify_token, db)
    except (PermissionError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/api/radar/hot/sources", status_code=201)
def add_source(payload: SourceIn, user: User = Depends(approved_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Conta do nicho para caçar ("@fulana" ou o link do perfil)."""
    handle = radar_hot.clean_handle(payload.handle)
    if handle is None:
        raise HTTPException(400, "Escreva a @ da conta (só letras, números e _, até 15) ou cole o link do perfil.")
    current = radar_hot.sources_of(db, user.id)
    if handle.lower() in {h.lower() for h in current}:
        raise HTTPException(409, "Essa conta já está na lista.")
    if len(current) >= radar_hot.MAX_SOURCES:
        raise HTTPException(400, f"Limite de {radar_hot.MAX_SOURCES} contas. Remova uma para adicionar outra.")
    db.add(HotSource(owner_id=user.id, handle=handle))
    db.commit()
    return {"handle": handle, "sources": radar_hot.sources_of(db, user.id)}


@router.delete("/api/radar/hot/sources/{handle}")
def delete_source(handle: str, user: User = Depends(approved_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    row = db.scalars(select(HotSource).where(HotSource.owner_id == user.id,
                                             func.lower(HotSource.handle) == handle.lstrip("@").lower())).first()
    if row is None:
        raise HTTPException(404, "Conta não encontrada na sua lista.")
    db.delete(row)
    db.commit()
    return {"sources": radar_hot.sources_of(db, user.id)}


@router.get("/api/radar/hot/questions")
def hot_questions(heat: str = Query("quente", pattern="^(leve|quente)$"), seed: int = SEED,
                  mix: int = Query(0, ge=0, le=1_000_000), accounts: bool = False,
                  user: User = Depends(approved_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Perguntas quentes sem assunto (grátis): o banco inteiro (formato e momento do dia) mais as frases montadas
    pelo gerador (mix = semente de "Misturar novas"), e 5 sorteadas. Com accounts=1 ("Com base nas contas")
    entram as que viralizaram de verdade (suas e variações das do nicho)."""
    proven = radar_hot.proven(db, user.id, limit=40) if accounts else None
    info = None
    if proven is not None:  # a tela mostra o que entrou (ou por que nada entrou)
        info = {"lote": sum(1 for p in proven if p["source"] == "lote"), "sua": sum(1 for p in proven if p["source"] == "sua"),
                "nicho": sum(1 for p in proven if p["source"] == "nicho"),
                "rule": radar_hot.VIRAL_RULE}
    bank = radar_ideas.question_bank(heat, proven=proven if heat == "quente" else None, mix=mix)
    from app.postar import format_potential

    return bank | {"seed": seed, "accounts": accounts, "accounts_info": info, "ideas": radar_ideas.hot_questions(heat, seed),
                   "potential": format_potential(db, user.id)}


@router.get("/api/radar/hot/variations")
def hot_variations(text: str = Query(..., min_length=3, max_length=600), seed: int = SEED) -> dict[str, Any]:
    """Variações grátis de um texto que engajou (troca abertura, fecho, meta e ordem; o miolo fica)."""
    clean = " ".join(text.split())
    if radar_hot.unsafe(clean) or radar_hot.has_cta(clean):
        raise HTTPException(400, "Esse texto tem link, venda ou conteúdo proibido: sem variações.")
    lines = radar_ideas.variations(clean, 6, seed)
    return {"text": clean, "ideas": [{"text": line, "fmt": radar_hot.format_of(line),
                                      "fmt_label": radar_hot.LABELS[radar_hot.format_of(line)][0]} for line in lines]}


@router.post("/api/radar/hot/grok")
def hot_grok(payload: HotGrokIn, user: User = Depends(approved_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Grok (chave xAI do usuário): recria um texto da biblioteca ou escreve textos novos no tom do nicho."""
    source = None
    if payload.id is not None:
        row = db.get(HotText, payload.id)
        if row is None:
            raise HTTPException(404, "Esse texto saiu da biblioteca.")
        source = radar_hot.item_of(row)
    elif payload.text and payload.text.strip():
        text = " ".join(payload.text.split())
        if radar_hot.unsafe(text) or radar_hot.has_cta(text):
            raise HTTPException(400, "Esse texto tem link, venda ou conteúdo proibido: o Grok não recria.")
        label, why = radar_hot.LABELS[radar_hot.format_of(text)]
        source = {"id": None, "text": text, "fmt_label": label, "why": why}
    key = xai_key(db, user)
    if not key:
        raise HTTPException(400, "Cole a sua chave da xAI em Configurações para pedir textos ao Grok.")
    hooks = None
    if payload.trend and not source:
        topics, _, _ = radar.build_topics(db)
        hooks = [t["title"] for t in topics if not t["blocked"] and t["engage"] >= 55][:5]
    fmt = payload.fmt if payload.fmt in radar_hot.LABELS else None
    if payload.mode == "campeoes" and not source:
        champions = radar_hot.proven_refs(db, user.id, 5)
        if not champions:
            raise HTTPException(400, "Ainda não tem texto que bombou: espere o robô ler as suas contas ou cace as contas do nicho.")
        prompt = radar_ideas.champions_prompt(payload.heat, champions)
    else:
        refs = radar_hot.proven_refs(db, user.id) if payload.accounts else None
        prompt = radar_ideas.hot_prompt(payload.heat, refs, source, hooks, fmt, uuid4().hex[:6])
    try:
        lines = radar_ideas.parse_lines(radar_ideas.ask_grok_lines(key, prompt), limit=radar_ideas.HOT_COUNT)
    except ValueError as exc:
        raise HTTPException(502, str(exc)) from exc
    lines = [line for line in lines if radar_ideas.fits_day(line)]  # "sextou" só na sexta
    if source:
        lines = [line for line in lines if norm_of(line) != norm_of(source["text"])]
    if not lines:
        raise HTTPException(502, "O Grok não devolveu textos. Tente de novo.")
    return {"source": source["id"] if source else None, "ideas": [
        {"text": line, "fmt": radar_hot.format_of(line), "fmt_label": radar_hot.LABELS[radar_hot.format_of(line)][0]}
        for line in lines]}


# ---- Gerador por palavras-chave (grátis; Turbo com Grok pela chave xAI) ----

class KeywordsGrokIn(BaseModel):
    terms: str = Field(min_length=1, max_length=600)
    heat: str = Field(default="quente", pattern="^(leve|quente)$")


@router.get("/api/radar/keywords")
def keyword_lines(terms: str = Query(..., min_length=1, max_length=600),
                  heat: str = Query("quente", pattern="^(leve|quente)$"), page: int = Query(0, ge=0, le=10_000),
                  mix: int = Query(0, ge=0, le=1_000_000), fmt: str | None = Query(None, pattern="^[a-z]{3,16}$"),
                  user: User = Depends(approved_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Frases montadas com as suas palavras-chave, em todos os formatos, da maior nota viral para a menor, de 12
    em 12 (page). Quando acaba (done), a tela troca a mistura (mix) e o banco vem com combinações novas."""
    from app.postar import format_potential

    try:
        return radar_keywords.generate(terms, heat, page, mix, fmt if fmt in radar_keywords.FORMATS else None,
                                       format_potential(db, user.id))
    except radar_keywords.KeywordError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/api/radar/keywords/grok")
def keyword_grok(payload: KeywordsGrokIn, user: User = Depends(approved_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Turbo com Grok: 10 frases com as mesmas palavras, pelas mesmas regras."""
    try:
        info = radar_keywords.parse(payload.terms)
        prompt = radar_keywords.grok_prompt(payload.terms, payload.heat, radar_hot.proven_refs(db, user.id))
    except radar_keywords.KeywordError as exc:
        raise HTTPException(400, str(exc)) from exc
    key = xai_key(db, user)
    if not key:
        raise HTTPException(400, "Cole a sua chave da xAI em Configurações para usar o Turbo com Grok.")
    try:
        lines = radar_ideas.parse_lines(radar_ideas.ask_grok_lines(key, prompt), limit=radar_ideas.HOT_COUNT)
    except ValueError as exc:
        raise HTTPException(502, str(exc)) from exc
    lines = [line.replace(" / ", "\n\n") for line in lines if radar_ideas.fits_day(line)]
    if info["names"]:  # nome real: nada sexual nas frases do Grok fora da promessa de quem posta
        lines = [line for line in lines if not radar_keywords.SEX_RE.search(norm_of(line))
                 or (radar_keywords.HOOK_START.match(line) is None and radar_hot.format_of(line) == "promessa")]
    if not lines:
        raise HTTPException(502, "O Grok não devolveu frases. Tente de novo.")
    potential = None
    try:
        from app.postar import format_potential
        potential = format_potential(db, user.id)
    except Exception:
        log.exception("radar: estimativa do formato falhou")
    items = []
    for line in lines:
        fmt = radar_hot.format_of(line)
        score = radar_keywords.viral_score(line, fmt, info["terms"], potential)
        tier, tier_label = radar_keywords.tier_of(score)
        items.append({"text": line, "fmt": fmt, "fmt_label": radar_hot.LABELS[fmt][0], "score": score,
                      "tier": tier, "tier_label": tier_label})
    items.sort(key=lambda i: -i["score"])
    return {"terms": info["roles"], "items": items}
