"""Aba Radar de trend: o que está em alta no Brasil, datas sazonais, o que pode virar trend, os termos que
cada usuário vigia (alerta kind=radar) e os posts que estão engajando (Apify de quem clicou).

Só lê o banco (app/radar.py monta). As fontes são lidas pelo vigia; se a última leitura tiver mais de 15 min
ao abrir a aba, uma leitura começa numa thread e a resposta já sai com o que existe ("refreshing": true).
"""

from __future__ import annotations

import logging
import threading
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import radar, radar_ideas, seasonal
from app.database import SessionLocal, get_db
from app.models import RadarTerm, User
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


REGION = Query("br", pattern="^(br|world)$")


class TermIn(BaseModel):
    term: str = Field(min_length=1, max_length=80)


class IdeasIn(BaseModel):
    norm: str = Field(min_length=1, max_length=400)
    region: str = Field(default="br", pattern="^(br|world)$")


def with_my_terms(db: Session, user: User, payload: dict[str, Any]) -> dict[str, Any]:
    topics, headlines, _ = radar.build_topics(db)
    payload["my_terms"] = radar.my_terms(db, user.id, topics, headlines)
    return payload


@router.get("/api/radar")
def read_radar(region: str = REGION, user: User = Depends(approved_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    payload = with_my_terms(db, user, radar.board(db, region=region))
    updated = payload["updated_at"]
    stale = updated is None or now_utc() - as_utc(radar.parse_iso(updated)) > STALE
    payload["refreshing"] = bool(stale and radar.radar_enabled() and _refresh_in_background())
    return payload


@router.post("/api/radar/refresh")
def refresh_radar(region: str = REGION, user: User = Depends(approved_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    if not radar.refresh_manual(db):
        raise HTTPException(429, "O radar acabou de ser atualizado. Tente de novo em 2 minutos.")
    payload = with_my_terms(db, user, radar.board(db, region=region))
    payload["refreshing"] = False
    return payload


@router.get("/api/radar/topic")
def read_topic(norm: str = Query(..., min_length=1, max_length=400), region: str = REGION,
               db: Session = Depends(get_db)) -> dict[str, Any]:
    detail = radar.topic_detail(db, norm, region)
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


def idea_subject(db: Session, norm: str, region: str) -> tuple[dict[str, Any], str | None]:
    """O assunto (ou a data sazonal) para as ideias de post; 404 se saiu do radar."""
    topic = radar.topic_detail(db, norm, region)
    if topic is not None:
        return topic, None
    key = norm_of(norm)
    for item in seasonal.upcoming(now_utc().astimezone(radar.DEFAULT_TZ_INFO).date(), 30):
        if norm_of(item["name"]) == key:
            return {"title": item["name"], "norm": key, "category": "Datas", "blocked": False}, "data"
    raise HTTPException(404, "Esse assunto saiu do radar.")


@router.get("/api/radar/ideas")
def read_ideas(norm: str = Query(..., min_length=1, max_length=400), region: str = REGION,
               db: Session = Depends(get_db)) -> dict[str, Any]:
    """Ideias de post grátis (modelos por tipo de assunto). Assunto sensível volta sem ideias."""
    topic, kind = idea_subject(db, norm, region)
    return {"norm": topic["norm"], "title": topic["title"], "blocked": bool(topic.get("blocked")),
            "ideas": radar_ideas.ideas(topic, kind)}


@router.post("/api/radar/ideas/grok")
def grok_ideas(payload: IdeasIn, user: User = Depends(approved_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Mais ideias com o Grok, pela chave xAI do usuário."""
    topic, _ = idea_subject(db, payload.norm, payload.region)
    if topic.get("blocked"):
        raise HTTPException(400, "Assunto sensível (morte, tragédia, crime, doença ou criança): sem ideias de post.")
    key = xai_key(db, user)
    if not key:
        raise HTTPException(400, "Cole a sua chave da xAI em Configurações para pedir ideias ao Grok.")
    try:
        lines = radar_ideas.parse_lines(radar_ideas.ask_grok_lines(key, radar_ideas.grok_prompt(topic)))
    except ValueError as exc:
        raise HTTPException(502, str(exc)) from exc
    if not lines:
        raise HTTPException(502, "O Grok não devolveu ideias. Tente de novo.")
    return {"norm": topic["norm"], "title": topic["title"], "ideas": lines}
