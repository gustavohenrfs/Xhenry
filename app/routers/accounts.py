"""Rotas de pastas, contas, métricas e refresh."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import DEFAULT_TZ, Folder, FolderKind, XAccount
from app.services import (
    build_suggestions,
    check_handle,
    dashboard,
    ensure_owner,
    normalize_username,
    period_window,
    selected_accounts,
    token_configured,
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
def list_folders(db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    rows = db.scalars(select(Folder).where(Folder.is_archived.is_(False)).order_by(Folder.name)).all()
    return [
        {
            "id": row.id,
            "name": row.name,
            "kind": row.kind.value,
            "color": row.color,
            "description": row.description,
            "accounts": len(row.accounts),
        }
        for row in rows
    ]


@router.post("/api/folders", status_code=201)
def create_folder(payload: FolderIn, db: Session = Depends(get_db)) -> dict[str, Any]:
    if not payload.color.startswith("#") or len(payload.color) != 7:
        raise HTTPException(400, "Cor inválida. Use #RRGGBB.")
    owner = ensure_owner(db)
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
def update_folder(folder_id: int, payload: FolderIn, db: Session = Depends(get_db)) -> dict[str, Any]:
    folder = db.get(Folder, folder_id)
    if folder is None:
        raise HTTPException(404, "Pasta não encontrada.")
    name = payload.name.strip()
    exists = db.scalar(select(Folder).where(Folder.owner_id == folder.owner_id, Folder.name == name, Folder.id != folder.id))
    if exists:
        raise HTTPException(409, "Já existe uma pasta com esse nome.")
    folder.name = name
    db.commit()
    return {"id": folder.id, "name": folder.name}


@router.delete("/api/folders/{folder_id}", status_code=204)
def delete_folder(folder_id: int, db: Session = Depends(get_db)) -> Response:
    folder = db.get(Folder, folder_id)
    if folder is None:
        raise HTTPException(404, "Pasta não encontrada.")
    db.delete(folder)
    db.commit()
    return Response(status_code=204)


@router.post("/api/accounts", status_code=201)
def create_account(payload: AccountIn, db: Session = Depends(get_db)) -> dict[str, Any]:
    folder = db.get(Folder, payload.folder_id)
    if folder is None or folder.is_archived:
        raise HTTPException(404, "Pasta não encontrada.")
    username = normalize_username(payload.username)
    exists = db.scalar(
        select(XAccount).where(XAccount.folder_id == folder.id, XAccount.username == username)
    )
    if exists:
        raise HTTPException(409, "Essa conta já está na pasta.")
    account = XAccount(
        folder_id=folder.id,
        username=username,
        display_name=(payload.display_name or "").strip() or None,
        timezone=DEFAULT_TZ,
    )
    db.add(account)
    db.commit()
    db.refresh(account)
    warning = None
    checked = None
    try:
        checked = check_handle(db, username, folder.id)
    except HTTPException as exc:
        if exc.status_code == 503:
            warning = str(exc.detail)
        else:
            warning = str(exc.detail)
    return {"id": account.id, "folder_id": folder.id, "username": username, "warning": warning, "check": checked}


@router.delete("/api/accounts/{account_id}", status_code=204)
def delete_account(account_id: int, db: Session = Depends(get_db)) -> Response:
    account = db.get(XAccount, account_id)
    if account is None:
        raise HTTPException(404, "Conta não encontrada.")
    db.delete(account)
    db.commit()
    return Response(status_code=204)


@router.patch("/api/accounts/{account_id}")
def update_account(account_id: int, payload: AccountUpdate, db: Session = Depends(get_db)) -> dict[str, Any]:
    account = db.get(XAccount, account_id)
    if account is None:
        raise HTTPException(404, "Conta não encontrada.")
    if payload.display_name is not None:
        account.display_name = payload.display_name.strip() or None
    if payload.is_archived is not None:
        account.is_archived = payload.is_archived
    db.commit()
    db.refresh(account)
    return {"id": account.id, "display_name": account.display_name, "is_archived": account.is_archived}


@router.get("/api/check/{handle}")
def check_account(
    handle: str,
    folder_id: int | None = Query(default=None),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return check_handle(db, normalize_username(handle), folder_id)


@router.post("/api/check-all")
def check_all(folder_id: int | None = Query(default=None), db: Session = Depends(get_db)) -> dict[str, Any]:
    if not token_configured():
        raise HTTPException(503, "Defina X_BEARER_TOKEN no ambiente ou no .env ao lado do main.py.")
    accounts = selected_accounts(db, folder_id)
    if len(accounts) > 30:
        raise HTTPException(400, "Máximo de 30 contas por verificação. Selecione uma pasta.")
    results = []
    seen: set[str] = set()
    for account in accounts:
        if account.username in seen:
            continue
        seen.add(account.username)
        results.append(check_handle(db, account.username, folder_id))
    return {"checked": len(results), "results": results}


@router.post("/api/refresh-all")
def refresh_all(folder_id: int | None = Query(default=None), db: Session = Depends(get_db)) -> dict[str, Any]:
    if not token_configured():
        raise HTTPException(503, "Defina X_BEARER_TOKEN no ambiente ou no .env ao lado do main.py.")
    accounts = selected_accounts(db, folder_id)
    refreshed = 0
    for account in accounts:
        try:
            check_handle(db, account.username, folder_id)
            refreshed += 1
        except Exception:
            pass
    return {"refreshed": refreshed, "total": len(accounts)}


@router.get("/api/dashboard")
def get_dashboard(
    period: str = Query(default="7d"),
    folder_id: int | None = Query(default=None),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return dashboard(db, period, folder_id)


@router.get("/api/suggestions")
def get_suggestions(
    period: str = Query(default="7d"),
    folder_id: int | None = Query(default=None),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    start, end = period_window(period)
    return build_suggestions(db, selected_accounts(db, folder_id), start, end)
