"""Controle do robo.py pelo painel: leitura automática, intervalo e "Atualizar Dados" (ler agora).

O robô roda no cron do VPS a cada minuto e lê estas opções em app_settings no começo de cada
execução: lê as contas do admin pelo Apify (token dele em Configurações) quando a última leitura tem
mais que o intervalo e a leitura automática está ligada, ou quando há pedido do botão (apaga o pedido).

/api/robo é só do admin. Os outros usuários leem pelo Apify com o próprio token: para eles há só
/api/apify (estado da última leitura) e /api/apify/run (o "Atualizar dados" deles).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import reader
from app.database import get_db
from app.models import SyncKind, SyncRun, User
from app.routers.auth import admin_user, approved_user
from app.services import (
    admin_apify_active,
    apply_pause,
    pause_payload,
    as_utc,
    get_settings,
    now_utc,
    robot_interval,
    selected_accounts,
    settings_payload,
    user_settings,
)

router = APIRouter()
user_router = APIRouter()

# Cada leitura pelo Apify cobra no mínimo 50 posts: nada abaixo de 15 min.
APIFY_INTERVALS = (15, 30, 60, 120, 180, 360, 720, 1440)


class PauseIn(BaseModel):
    on: bool | None = None
    from_: str | None = Field(default=None, alias="from", max_length=5)
    to: str | None = Field(default=None, max_length=5)


class ApifyIn(BaseModel):
    auto: bool | None = None
    interval_min: int | None = None
    pause: PauseIn | None = None


class RoboIn(BaseModel):
    enabled: bool | None = None
    apify_interval_min: int | None = None
    pause: PauseIn | None = None


def robo_payload(db: Session) -> dict[str, Any]:
    row = get_settings(db)
    interval = robot_interval(row)
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
            "error": last.error,
        }
        if row.robo_enabled:
            next_run = (finished + timedelta(minutes=interval)).isoformat()
    requested = row.robo_run_requested_at
    admin = db.scalar(select(User).where(User.is_admin.is_(True)).order_by(User.id).limit(1))
    apify = user_settings(db, admin.id) if admin else None
    return {
        "enabled": row.robo_enabled,
        "apify": {
            "active": admin_apify_active(db),
            "interval_min": row.apify_interval_min,
            "intervals": list(APIFY_INTERVALS),
            "status": apify.apify_status if apify else None,
            "detail": apify.apify_detail if apify else None,
            "checked_at": as_utc(apify.apify_checked_at).isoformat() if apify and apify.apify_checked_at else None,
        },
        "requested_at": as_utc(requested).isoformat() if requested else None,
        "pause": pause_payload(apify) if apify else {"on": True, "from": "00:00", "to": "06:00"},
        # Leitura na hora (app/reader.py) em andamento: a tela espera ela acabar.
        "running": bool(admin and reader.running(admin.id)),
        "asked_at": now_utc().isoformat(),
        "last_run": last_run,
        "next_run_at": next_run,
    }


@router.get("/api/robo", dependencies=[Depends(admin_user)])
def read_robo(db: Session = Depends(get_db)) -> dict[str, Any]:
    return robo_payload(db)


@router.put("/api/robo", dependencies=[Depends(admin_user)])
def write_robo(payload: RoboIn, db: Session = Depends(get_db)) -> dict[str, Any]:
    row = get_settings(db)
    if payload.apify_interval_min is not None:
        if payload.apify_interval_min not in APIFY_INTERVALS:
            raise HTTPException(400, "Intervalo do Apify inválido.")
        row.apify_interval_min = payload.apify_interval_min
    if payload.pause is not None:
        admin = db.scalar(select(User).where(User.is_admin.is_(True)).order_by(User.id).limit(1))
        if admin is not None:
            apply_pause(user_settings(db, admin.id), payload.pause.on, payload.pause.from_, payload.pause.to)
    if payload.enabled is not None:
        if payload.enabled and not row.robo_enabled:
            row.robo_run_requested_at = now_utc()  # religou: lê já, sem esperar o intervalo
        row.robo_enabled = payload.enabled
    db.commit()
    return robo_payload(db)


@router.post("/api/robo/run")
def request_run(user: User = Depends(admin_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Botão "Atualizar dados": lê na hora pelo Apify (app/reader.py); sem Postgres o robô pega o
    pedido na próxima passada do cron (até 1 min)."""
    if not admin_apify_active(db):
        raise HTTPException(400, "Token do Apify em falta. Cole o seu em Configurações.")
    if not reader.start(user.id, True):
        row = get_settings(db)
        row.robo_run_requested_at = now_utc()
        db.commit()
    return robo_payload(db)


def collection_health(db: Session, admin: User) -> dict[str, Any]:
    """Cartão "Saúde da leitura": contas do admin que o Apify não lê há tempo demais."""
    now = now_utc()
    payload = robo_payload(db)
    accounts = selected_accounts(db, admin.id, None)
    # Cada leitura pelo Apify lê todas as contas: atrasada é a que passou de dois intervalos.
    late_after = max(60, 2 * payload["apify"]["interval_min"])
    stale = []
    for account in accounts:
        when = as_utc(account.collected_at) if account.collected_at else None
        if when is None or now - when > timedelta(minutes=late_after):
            stale.append(
                {
                    "username": account.username,
                    "display_name": account.display_name,
                    "collected_at": when.isoformat() if when else None,
                }
            )
    stale.sort(key=lambda item: item["collected_at"] or "")
    payload.update(
        {
            "accounts": len(accounts),
            "fresh": len(accounts) - len(stale),
            "stale": stale,
            "late_after_min": late_after,
        }
    )
    return payload


@router.get("/api/robo/health")
def read_health(user: User = Depends(admin_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    return collection_health(db, user)


@user_router.get("/api/apify")
def read_apify(user: User = Depends(approved_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Estado da leitura pelo Apify (token definido, último resultado do robô, pedido pendente)."""
    payload = settings_payload(db, user)
    return {
        "token_set": payload["apify_token_set"],
        **payload["apify"],
        "intervals": list(APIFY_INTERVALS),
        "running": reader.running(user.id),
        "pause": pause_payload(user_settings(db, user.id)),
    }


@user_router.put("/api/apify")
def write_apify(payload: ApifyIn, user: User = Depends(approved_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Leitura automática de quem lê pelo Apify: ligada ou não e de quanto em quanto tempo."""
    row = user_settings(db, user.id)
    if payload.interval_min is not None:
        if payload.interval_min not in APIFY_INTERVALS:
            raise HTTPException(400, "Intervalo inválido.")
        row.apify_interval_min = payload.interval_min
    if payload.auto is not None:
        row.apify_auto = payload.auto
    if payload.pause is not None:
        apply_pause(row, payload.pause.on, payload.pause.from_, payload.pause.to)
    db.commit()
    return read_apify(user, db)


@user_router.post("/api/apify/run")
def request_apify_run(user: User = Depends(approved_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    """"Atualizar dados" de quem lê pelo Apify: lê na hora (app/reader.py); sem Postgres, o robô lê
    no próximo minuto."""
    row = user_settings(db, user.id)
    if not (row.apify_token or "").strip():
        raise HTTPException(400, "Token do Apify em falta. Cole o seu em Configurações.")
    if not reader.start(user.id, False):
        row.apify_run_requested_at = now_utc()
        db.commit()
    return read_apify(user, db)
