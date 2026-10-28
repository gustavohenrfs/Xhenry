"""Inscrição do navegador no Web Push do sininho."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import PushSubscription, User
from app.push import PushUnavailable, public_key, send_one, test_payload
from app.routers.auth import approved_user

router = APIRouter(dependencies=[Depends(approved_user)])
# Sem login: o service worker não enxerga o token do localStorage. Ver renew().
public_router = APIRouter()


class PushKeys(BaseModel):
    p256dh: str = Field(min_length=20, max_length=200)
    auth: str = Field(min_length=8, max_length=100)


class SubscriptionIn(BaseModel):
    endpoint: str = Field(min_length=12, max_length=2048)
    keys: PushKeys
    user_agent: str | None = Field(default=None, max_length=300)


class EndpointIn(BaseModel):
    endpoint: str = Field(min_length=12, max_length=2048)


class RenewIn(SubscriptionIn):
    old_endpoint: str = Field(min_length=12, max_length=2048)


def find(db: Session, endpoint: str) -> PushSubscription | None:
    return db.scalar(select(PushSubscription).where(PushSubscription.endpoint == endpoint))


@router.get("/api/push/key")
def push_key(db: Session = Depends(get_db)) -> dict[str, str]:
    return {"public_key": public_key(db)}


@router.post("/api/push/subscribe")
def subscribe(payload: SubscriptionIn, user: User = Depends(approved_user), db: Session = Depends(get_db)) -> dict[str, bool]:
    if not payload.endpoint.startswith("https://"):
        raise HTTPException(400, "Endereço de push inválido.")
    row = find(db, payload.endpoint) or PushSubscription(endpoint=payload.endpoint)
    # O mesmo navegador com outra pessoa logada passa a ser dela.
    row.owner_id = user.id
    row.p256dh, row.auth = payload.keys.p256dh, payload.keys.auth
    row.user_agent = payload.user_agent
    db.add(row)
    db.commit()
    return {"ok": True}


@public_router.post("/api/push/renew")
def renew(payload: RenewIn, db: Session = Depends(get_db)) -> dict[str, bool]:
    """O navegador trocou a inscrição sozinho (pushsubscriptionchange no sw.js): põe a nova no lugar.

    Não pede login, mas só troca uma inscrição que já existe: o endereço antigo é uma URL secreta do
    serviço de push que só aquele navegador conhece, então serve de prova.
    """
    old = find(db, payload.old_endpoint)
    if old is None:
        raise HTTPException(404, "Inscrição antiga não encontrada.")
    if not payload.endpoint.startswith("https://"):
        raise HTTPException(400, "Endereço de push inválido.")
    current = find(db, payload.endpoint)
    if current is not None and current is not old:
        db.delete(old)  # a página já reinscreveu este navegador; só some com a antiga
        row = current
    else:
        row = old
        row.endpoint = payload.endpoint
    row.p256dh, row.auth = payload.keys.p256dh, payload.keys.auth
    if payload.user_agent:
        row.user_agent = payload.user_agent
    db.commit()
    return {"ok": True}


@router.post("/api/push/unsubscribe")
def unsubscribe(payload: EndpointIn, user: User = Depends(approved_user), db: Session = Depends(get_db)) -> dict[str, bool]:
    row = find(db, payload.endpoint)
    if row is not None and row.owner_id == user.id:
        db.delete(row)
        db.commit()
    return {"ok": True}


@router.post("/api/push/test")
def push_test(payload: EndpointIn, user: User = Depends(approved_user), db: Session = Depends(get_db)) -> dict[str, bool]:
    """Botão Testar do sininho: manda uma notificação só para este navegador."""
    row = find(db, payload.endpoint)
    if row is None or row.owner_id != user.id:
        raise HTTPException(404, "Este navegador não está inscrito. Ative as notificações de novo.")
    try:
        result = send_one(db, row, test_payload())
    except PushUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    if result == "gone":
        raise HTTPException(410, "A inscrição deste navegador expirou. Ative as notificações de novo.")
    if result != "ok":
        raise HTTPException(502, "O serviço de push do navegador recusou o envio. Tente de novo em instantes.")
    return {"ok": True}
