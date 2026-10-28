"""Rotas de pastas, contas, métricas e refresh. Tudo do usuário logado: de outro dono é 404."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.cache import cached_json
from app.database import get_db
from app.models import DEFAULT_TZ, Collector, Folder, FolderKind, User, XAccount
from app.routers.auth import approved_user
from app.services import (
    NO_X_TOKEN,
    account_detail,
    archived_payload,
    build_suggestions,
    check_handle,
    dashboard,
    normalize_username,
    now_utc,
    owned_account,
    owned_folder,
    period_window,
    selected_accounts,
    x_token,
)

router = APIRouter()


class FolderIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    kind: FolderKind = FolderKind.CUSTOM
    description: str | None = Field(default=None, max_length=2000)
    color: str = "#E0B15A"


class AccountIn(BaseModel):
    folder_id: int
    username: str = Field(min_length=1, max_length=16)
    display_name: str | None = Field(default=None, max_length=80)


class AccountUpdate(BaseModel):
    display_name: str | None = Field(default=None, max_length=80)
    is_archived: bool | None = None


@router.get("/api/folders")
def list_folders(user: User = Depends(approved_user), db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    rows = db.scalars(
        select(Folder).where(Folder.owner_id == user.id, Folder.is_archived.is_(False)).order_by(Folder.name)
    ).all()
    return [
        {
            "id": row.id,
            "name": row.name,
            "kind": row.kind.value,
            "color": row.color,
            "description": row.description,
            "accounts": sum(1 for account in row.accounts if not account.is_archived),
        }
        for row in rows
    ]


@router.post("/api/folders", status_code=201)
def create_folder(payload: FolderIn, owner: User = Depends(approved_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    if not payload.color.startswith("#") or len(payload.color) != 7:
        raise HTTPException(400, "Cor inválida. Use #RRGGBB.")
    name = payload.name.strip()
    exists = db.scalar(select(Folder).where(Folder.owner_id == owner.id, Folder.name == name))
    if exists:
        raise HTTPException(409, "Já existe uma pasta com esse nome.")
    folder = Folder(
        owner_id=owner.id,
        name=name,
        kind=payload.kind,
        description=payload.description.strip() if payload.description else None,
        color=payload.color.upper(),
    )
    db.add(folder)
    db.commit()
    db.refresh(folder)
    return {"id": folder.id, "name": folder.name, "kind": folder.kind.value, "color": folder.color}


@router.patch("/api/folders/{folder_id}")
def update_folder(
    folder_id: int, payload: FolderIn, user: User = Depends(approved_user), db: Session = Depends(get_db)
) -> dict[str, Any]:
    folder = owned_folder(db, user.id, folder_id)
    name = payload.name.strip()
    exists = db.scalar(select(Folder).where(Folder.owner_id == folder.owner_id, Folder.name == name, Folder.id != folder.id))
    if exists:
        raise HTTPException(409, "Já existe uma pasta com esse nome.")
    folder.name = name
    db.commit()
    return {"id": folder.id, "name": folder.name}


@router.delete("/api/folders/{folder_id}", status_code=204)
def delete_folder(folder_id: int, user: User = Depends(approved_user), db: Session = Depends(get_db)) -> Response:
    folder = owned_folder(db, user.id, folder_id)
    db.delete(folder)
    db.commit()
    return Response(status_code=204)


@router.post("/api/accounts", status_code=201)
def create_account(payload: AccountIn, user: User = Depends(approved_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    folder = owned_folder(db, user.id, payload.folder_id)
    if folder.is_archived:
        raise HTTPException(404, "Pasta não encontrada.")
    username = normalize_username(payload.username)
    exists = db.scalar(
        select(XAccount).where(XAccount.folder_id == folder.id, XAccount.username == username)
    )
    if exists and exists.is_archived:
        raise HTTPException(409, "Essa conta está arquivada nesta pasta. Desarquive na aba Arquivadas.")
    if exists:
        raise HTTPException(409, "Essa conta já está na pasta.")
    if user.is_admin and db.scalar(select(Collector).where(Collector.handle == username)):
        # O robô entra logado nos coletores; uma conta monitorada não pode ser um deles. Só vale para o
        # admin: os coletores são dele, e para os outros a resposta revelaria quais @ são coletores.
        raise HTTPException(400, f"@{username} é um coletor. Remova-o da aba Coletores antes de monitorar.")
    account = XAccount(
        folder_id=folder.id,
        owner_id=user.id,
        username=username,
        display_name=(payload.display_name or "").strip() or None,
        timezone=DEFAULT_TZ,
    )
    db.add(account)
    db.commit()
    db.refresh(account)
    return {"id": account.id, "folder_id": folder.id, "username": username, "warning": None, "check": None}


@router.delete("/api/accounts/{account_id}", status_code=204)
def delete_account(account_id: int, user: User = Depends(approved_user), db: Session = Depends(get_db)) -> Response:
    account = owned_account(db, user.id, account_id)
    db.delete(account)
    db.commit()
    return Response(status_code=204)


@router.patch("/api/accounts/{account_id}")
def update_account(
    account_id: int, payload: AccountUpdate, user: User = Depends(approved_user), db: Session = Depends(get_db)
) -> dict[str, Any]:
    account = owned_account(db, user.id, account_id)
    if payload.display_name is not None:
        account.display_name = payload.display_name.strip() or None
    if payload.is_archived is not None and payload.is_archived != account.is_archived:
        # Arquivar só esconde: tweet_samples, daily_metrics e health_checks ficam no banco.
        account.is_archived = payload.is_archived
        account.archived_at = now_utc() if payload.is_archived else None
    db.commit()
    db.refresh(account)
    return {"id": account.id, "display_name": account.display_name, "is_archived": account.is_archived}


@router.get("/api/accounts/archived")
def list_archived(user: User = Depends(approved_user), db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    return archived_payload(db, user.id)


@router.get("/api/check/{handle}")
def check_account(
    handle: str,
    folder_id: int | None = Query(default=None),
    user: User = Depends(approved_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    if folder_id is not None:
        owned_folder(db, user.id, folder_id)
    return check_handle(db, user, normalize_username(handle), folder_id)


@router.post("/api/check-all")
def check_all(
    folder_id: int | None = Query(default=None), user: User = Depends(approved_user), db: Session = Depends(get_db)
) -> dict[str, Any]:
    if not x_token(db, user):
        raise HTTPException(503, NO_X_TOKEN)
    if folder_id is not None:
        owned_folder(db, user.id, folder_id)
    accounts = selected_accounts(db, user.id, folder_id)
    if len(accounts) > 30:
        raise HTTPException(400, "Máximo de 30 contas por verificação. Selecione uma pasta.")
    results = []
    seen: set[str] = set()
    for account in accounts:
        if account.username in seen:
            continue
        seen.add(account.username)
        results.append(check_handle(db, user, account.username, folder_id))
    return {"checked": len(results), "results": results}


@router.post("/api/refresh-all")
def refresh_all(
    period: str = Query(default="7d"),
    folder_id: int | None = Query(default=None),
    user: User = Depends(approved_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    payload = dashboard(db, user.id, period, folder_id)
    payload["refreshed"] = len(payload.get("accounts") or [])
    return payload


@router.get("/api/dashboard")
def get_dashboard(
    request: Request,
    period: str = Query(default="7d"),
    folder_id: int | None = Query(default=None),
    user: User = Depends(approved_user),
    db: Session = Depends(get_db),
) -> Response:
    # Cache em memória (app/cache.py) + ETag: o auto-refresh sem novidade volta 304, sem corpo.
    # O dono entra na chave: cada usuário tem o próprio dashboard.
    body, etag = cached_json(
        db, ("dashboard", user.id, period, folder_id), lambda: dashboard(db, user.id, period, folder_id)
    )
    headers = {"ETag": etag, "Cache-Control": "no-cache"}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return Response(body, media_type="application/json", headers=headers)


@router.get("/api/accounts/{account_id}/detail")
def get_account_detail(
    account_id: int,
    period: str = Query(default="7d"),
    user: User = Depends(approved_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Gaveta da conta no Dashboard."""
    return account_detail(db, user.id, account_id, period)


@router.get("/api/suggestions")
def get_suggestions(
    period: str = Query(default="7d"),
    folder_id: int | None = Query(default=None),
    user: User = Depends(approved_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    start, end = period_window(period)
    return build_suggestions(db, selected_accounts(db, user.id, folder_id), start, end)
