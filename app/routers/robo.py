"""Controle do robo.py pelo painel: coleta automática, intervalo e "Atualizar Dados" (coletar agora).

O robô roda no cron do VPS a cada minuto e lê estas opções em app_settings no começo de cada
execução: com a coleta automática desligada, só roda quando há pedido do botão; ligada, roda
quando a última rodada tem mais que o intervalo. Ao começar, ele apaga o pedido.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import SyncKind, SyncRun
from app.routers.auth import current_user
from app.services import as_utc, get_settings, now_utc

router = APIRouter(dependencies=[Depends(current_user)])

ROBO_INTERVALS = (5, 10, 15, 30, 60, 120, 180, 360, 720, 1440)


class RoboIn(BaseModel):
    enabled: bool | None = None
    interval_min: int | None = None


def robo_payload(db: Session) -> dict[str, Any]:
    row = get_settings(db)
    last = db.scalars(
        select(SyncRun)
        .where(SyncRun.kind == SyncKind.TWEETS, SyncRun.account_id.is_(None), SyncRun.finished_at.is_not(None))
        .order_by(SyncRun.finished_at.desc())
        .limit(1)
    ).first()
    last_run = None
    next_run = None
    if last is not None:
        finished = as_utc(last.finished_at)
        stats = last.stats or {}
        last_run = {
            "finished_at": finished.isoformat(),
            "status": last.status.value,
            "contas": stats.get("contas"),
            "tweets": stats.get("tweets"),
            "manual": bool(stats.get("manual")),
        }
        if row.robo_enabled:
            next_run = (finished + timedelta(minutes=row.robo_interval_min)).isoformat()
    requested = row.robo_run_requested_at
    return {
        "enabled": row.robo_enabled,
        "interval_min": row.robo_interval_min,
        "intervals": list(ROBO_INTERVALS),
        "requested_at": as_utc(requested).isoformat() if requested else None,
        "last_run": last_run,
        "next_run_at": next_run,
    }


@router.get("/api/robo")
def read_robo(db: Session = Depends(get_db)) -> dict[str, Any]:
    return robo_payload(db)


@router.put("/api/robo")
def write_robo(payload: RoboIn, db: Session = Depends(get_db)) -> dict[str, Any]:
    row = get_settings(db)
    if payload.interval_min is not None:
        if payload.interval_min not in ROBO_INTERVALS:
            raise HTTPException(400, "Intervalo inválido.")
        row.robo_interval_min = payload.interval_min
    if payload.enabled is not None:
        if payload.enabled and not row.robo_enabled:
            row.robo_run_requested_at = now_utc()  # religou: coleta já, sem esperar o intervalo
        row.robo_enabled = payload.enabled
    db.commit()
    return robo_payload(db)


@router.post("/api/robo/run")
def request_run(db: Session = Depends(get_db)) -> dict[str, Any]:
    """Botão "Atualizar Dados": o robô pega o pedido na próxima passada do cron (até 1 min)."""
    row = get_settings(db)
    row.robo_run_requested_at = now_utc()
    db.commit()
    return robo_payload(db)
