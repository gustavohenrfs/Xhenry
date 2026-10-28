"""Aba Usuários (só o admin): pedidos de acesso, aprovação e links de convite.

O amigo se cadastra pelo link (#invite=…), fica pendente (approved_user devolve 403) e entra quando o
admin aprova. Revogar tira a aprovação e derruba as sessões abertas; recusar apaga o pedido.
"""

from __future__ import annotations

import secrets
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Folder, Invite, User, XAccount
from app.routers.auth import admin_user, avatar_link, public_base_url, revoke_sessions
from app.services import as_utc, now_utc

router = APIRouter(dependencies=[Depends(admin_user)])
INVITE_DAYS = 7


class InviteIn(BaseModel):
    note: str | None = Field(default=None, max_length=120)


def iso(value) -> str | None:
    return as_utc(value).isoformat() if value else None


def user_row(user: User, accounts: int) -> dict[str, Any]:
    return {
        "id": user.id,
        "email": user.email,
        "nome_exibicao": user.nome_exibicao or user.name,
        "avatar_url": avatar_link(user),
        "is_admin": bool(user.is_admin),
        "is_approved": bool(user.is_approved),
        "created_at": iso(user.created_at),
        "approved_at": iso(user.approved_at),
        "accounts": accounts,
    }


def invite_row(invite: Invite, base: str, used_by: str | None) -> dict[str, Any]:
    now = now_utc()
    if invite.used_at is not None:
        status = "usado"
    elif as_utc(invite.expires_at) < now:
        status = "vencido"
    else:
        status = "aberto"
    return {
        "id": invite.id,
        "note": invite.note,
        "status": status,
        # O link só volta enquanto pode ser usado.
        "link": f"{base}/#invite={invite.token}" if status == "aberto" else None,
        "created_at": iso(invite.created_at),
        "expires_at": iso(invite.expires_at),
        "used_at": iso(invite.used_at),
        "used_by": used_by,
    }


def target(db: Session, user_id: int, admin: User) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(404, "Usuário não encontrado.")
    if user.id == admin.id:
        raise HTTPException(400, "Isso não vale para a sua própria conta.")
    return user


@router.get("/api/admin/users")
def list_users(db: Session = Depends(get_db)) -> dict[str, Any]:
    counts = dict(
        db.execute(
            select(Folder.owner_id, func.count(XAccount.id))
            .join(XAccount, XAccount.folder_id == Folder.id)
            .where(XAccount.is_archived.is_(False))
            .group_by(Folder.owner_id)
        ).all()
    )
    users = db.scalars(select(User).order_by(User.is_approved, User.created_at.desc(), User.id.desc())).all()
    rows = [user_row(user, counts.get(user.id, 0)) for user in users]
    return {
        "pending": [row for row in rows if not row["is_approved"]],
        "approved": [row for row in rows if row["is_approved"]],
    }


@router.post("/api/admin/users/{user_id}/approve")
def approve(user_id: int, admin: User = Depends(admin_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    user = target(db, user_id, admin)
    if not user.is_approved:
        user.is_approved = True
        user.approved_at = now_utc()
        db.commit()
    return user_row(user, 0)


@router.post("/api/admin/users/{user_id}/revoke")
def revoke(user_id: int, admin: User = Depends(admin_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Tira o acesso sem apagar nada: volta a pendente e as sessões abertas caem."""
    user = target(db, user_id, admin)
    user.is_approved = False
    user.approved_at = None
    revoke_sessions(user)
    db.commit()
    return user_row(user, 0)


@router.delete("/api/admin/users/{user_id}", status_code=204)
def reject(user_id: int, admin: User = Depends(admin_user), db: Session = Depends(get_db)) -> Response:
    """Recusar um pedido: apaga a conta pendente. Aprovado precisa ser revogado antes."""
    user = target(db, user_id, admin)
    if user.is_approved:
        raise HTTPException(400, "Revogue o acesso antes de apagar este usuário.")
    db.delete(user)
    db.commit()
    return Response(status_code=204)


@router.get("/api/admin/invites")
def list_invites(request: Request, db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    base = public_base_url(request)
    emails = dict(db.execute(select(User.id, User.email)).all())
    invites = db.scalars(select(Invite).order_by(Invite.id.desc()).limit(50)).all()
    return [invite_row(row, base, emails.get(row.used_by_id)) for row in invites]


@router.post("/api/admin/invites", status_code=201)
def create_invite(
    payload: InviteIn, request: Request, admin: User = Depends(admin_user), db: Session = Depends(get_db)
) -> dict[str, Any]:
    invite = Invite(
        token=secrets.token_urlsafe(24),
        note=(payload.note or "").strip() or None,
        created_by_id=admin.id,
        created_at=now_utc(),
        expires_at=now_utc() + timedelta(days=INVITE_DAYS),
    )
    db.add(invite)
    db.commit()
    db.refresh(invite)
    return invite_row(invite, public_base_url(request), None)


@router.delete("/api/admin/invites/{invite_id}", status_code=204)
def cancel_invite(invite_id: int, db: Session = Depends(get_db)) -> Response:
    invite = db.get(Invite, invite_id)
    if invite is None:
        raise HTTPException(404, "Convite não encontrado.")
    if invite.used_at is not None:
        raise HTTPException(400, "Este convite já foi usado.")
    db.delete(invite)
    db.commit()
    return Response(status_code=204)
