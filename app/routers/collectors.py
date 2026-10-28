"""Aba Coletores: contas descartáveis que o robo.py usa para ler as timelines.

Tudo aqui é só do admin, inclusive a listagem: mesmo mascarados, proxy e cookies não são públicos.
Os outros usuários leem pelo Apify e nem veem a aba.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.collectors import dump_cookies, mask_cookies, mask_proxy, parse_cookies, parse_proxy, probe_proxy
from app.database import get_db
from app.models import Collector, XAccount
from app.routers.auth import admin_user
from app.services import as_utc, normalize_username, now_utc

router = APIRouter(dependencies=[Depends(admin_user)])


class CollectorIn(BaseModel):
    handle: str = Field(min_length=1, max_length=16)
    proxy: str | None = Field(default=None, max_length=1000)
    cookies: str | None = Field(default=None, max_length=200_000)


class CollectorUpdate(BaseModel):
    # None mantém o que está salvo; "" apaga.
    proxy: str | None = Field(default=None, max_length=1000)
    cookies: str | None = Field(default=None, max_length=200_000)
    is_active: bool | None = None


def _iso(value: datetime | None) -> str | None:
    return as_utc(value).isoformat() if value else None


def collector_payload(row: Collector) -> dict[str, Any]:
    paused = row.paused_until is not None and as_utc(row.paused_until) > now_utc()
    state = "off" if not row.is_active else "paused" if paused else "ready"
    return {
        "id": row.id,
        "handle": row.handle,
        "proxy": mask_proxy(row.proxy),
        "cookies": mask_cookies(row.cookies),
        "is_active": row.is_active,
        "state": state,
        "paused_until": _iso(row.paused_until) if paused else None,
        "last_used_at": _iso(row.last_used_at),
        "last_status": row.last_status,
        "last_error": (row.last_error or "")[:300] or None,
        # Botão Testar: "pending" enquanto o robô não leu; depois o mesmo vocabulário de last_status.
        "test_status": "pending" if row.test_requested_at else row.test_status,
        "test_detail": (row.test_detail or "")[:300] or None,
        "test_at": _iso(row.test_at),
    }


def _proxy(raw: str) -> str | None:
    if not raw.strip():
        return None
    try:
        return parse_proxy(raw)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


def _cookies(raw: str) -> str | None:
    if not raw.strip():
        return None
    try:
        return dump_cookies(parse_cookies(raw))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


def _get(db: Session, collector_id: int) -> Collector:
    row = db.get(Collector, collector_id)
    if row is None:
        raise HTTPException(404, "Coletor não encontrado.")
    return row


@router.get("/api/collectors")
def list_collectors(db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    return [collector_payload(row) for row in db.scalars(select(Collector).order_by(Collector.handle))]


@router.post("/api/collectors", status_code=201)
def create_collector(payload: CollectorIn, db: Session = Depends(get_db)) -> dict[str, Any]:
    handle = normalize_username(payload.handle)
    # O robô entra logado no coletor: se fosse uma conta monitorada, o risco cairia nela.
    if db.scalar(select(func.count()).select_from(XAccount).where(XAccount.username == handle)):
        raise HTTPException(400, f"@{handle} é uma conta monitorada. Use uma conta descartável como coletor.")
    if db.scalar(select(Collector).where(Collector.handle == handle)):
        raise HTTPException(409, f"@{handle} já é um coletor.")
    row = Collector(handle=handle, proxy=_proxy(payload.proxy or ""), cookies=_cookies(payload.cookies or ""))
    db.add(row)
    db.commit()
    db.refresh(row)
    return collector_payload(row)


@router.patch("/api/collectors/{collector_id}")
def update_collector(collector_id: int, payload: CollectorUpdate, db: Session = Depends(get_db)) -> dict[str, Any]:
    row = _get(db, collector_id)
    changed = False
    if payload.proxy is not None:
        row.proxy = _proxy(payload.proxy)
        changed = True
    if payload.cookies is not None:
        row.cookies = _cookies(payload.cookies)
        changed = True
    if payload.is_active is not None:
        changed = changed or (payload.is_active and not row.is_active)
        row.is_active = payload.is_active
    if changed:
        # Proxy ou cookies novos (ou religar): o robô tenta de novo já na próxima rodada.
        row.paused_until = None
        row.last_status = None
        row.last_error = None
    db.commit()
    db.refresh(row)
    return collector_payload(row)


@router.post("/api/collectors/{collector_id}/test")
def test_collector(collector_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    """Botão Testar: tira o castigo, testa o proxy na hora e pede ao robô uma leitura com este coletor.

    O painel não abre navegador (o anyx e o cofre de cookies são do usuário do robô): a leitura de
    verdade sai no próximo minuto do cron, que grava test_status/test_detail; a aba faz polling.
    """
    row = _get(db, collector_id)
    if db.scalar(select(func.count()).select_from(XAccount).where(func.lower(XAccount.username) == row.handle.lower())):
        raise HTTPException(400, f"@{row.handle} é uma conta monitorada. O robô nunca entra logado nela.")
    problem = probe_proxy(row.proxy) if row.proxy else None
    now = now_utc()
    row.paused_until = None
    row.test_at = now
    if problem:
        # Proxy fora: não adianta o robô abrir o navegador.
        row.test_requested_at = None
        row.test_status = "proxy"
        row.test_detail = problem[:500]
    else:
        row.test_requested_at = now
        row.test_status = None
        row.test_detail = ("Proxy funcionando. " if row.proxy else "Sem proxy (sai pelo IP do VPS). ") + (
            "O robô lê uma conta com este coletor no próximo minuto."
        )
    db.commit()
    db.refresh(row)
    return collector_payload(row)


@router.delete("/api/collectors/{collector_id}", status_code=204)
def delete_collector(collector_id: int, db: Session = Depends(get_db)) -> Response:
    db.delete(_get(db, collector_id))
    db.commit()
    return Response(status_code=204)
