"""Aba Radar de trend: o que está em alta no Brasil, datas sazonais e o que pode virar trend.

Só lê o banco (app/radar.py monta). As fontes são lidas pelo vigia; se a última leitura tiver mais de 15 min
ao abrir a aba, uma leitura começa numa thread e a resposta já sai com o que existe ("refreshing": true).
"""

from __future__ import annotations

import logging
import threading
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app import radar
from app.database import SessionLocal, get_db
from app.routers.auth import approved_user
from app.services import as_utc, now_utc

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


@router.get("/api/radar")
def read_radar(db: Session = Depends(get_db)) -> dict[str, Any]:
    payload = radar.board(db)
    updated = payload["updated_at"]
    stale = updated is None or now_utc() - as_utc(radar.parse_iso(updated)) > STALE
    payload["refreshing"] = bool(stale and radar.radar_enabled() and _refresh_in_background())
    return payload


@router.post("/api/radar/refresh")
def refresh_radar(db: Session = Depends(get_db)) -> dict[str, Any]:
    if not radar.refresh_manual(db):
        raise HTTPException(429, "O radar acabou de ser atualizado. Tente de novo em 2 minutos.")
    payload = radar.board(db)
    payload["refreshing"] = False
    return payload


@router.get("/api/radar/topic")
def read_topic(norm: str = Query(..., min_length=1, max_length=400), db: Session = Depends(get_db)) -> dict[str, Any]:
    detail = radar.topic_detail(db, norm)
    if detail is None:
        raise HTTPException(404, "Esse assunto saiu do radar.")
    return detail
