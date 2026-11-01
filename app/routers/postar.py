"""Aba Postar: textos guardados por conta (app/postar.py). Tudo do usuário logado; texto de outro é 404."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app import postar
from app.database import get_db
from app.models import User
from app.routers.auth import approved_user

router = APIRouter(dependencies=[Depends(approved_user)])


class DraftIn(BaseModel):
    account_id: int
    text: str = Field(min_length=1, max_length=postar.MAX_TEXT + 200)


class DraftPatch(BaseModel):
    posted: bool | None = None
    text: str | None = Field(default=None, max_length=postar.MAX_TEXT + 200)


@router.get("/api/drafts")
def list_drafts(user: User = Depends(approved_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    return postar.board(db, user.id)


@router.get("/api/drafts/results")
def draft_results(folder_id: int | None = Query(default=None), user: User = Depends(approved_user),
                  db: Session = Depends(get_db)) -> dict[str, Any]:
    """Como os textos se saíram: cada "Postei" ligado ao post real lido pelo robô, somado por frase."""
    return postar.results(db, user.id, folder_id)


@router.post("/api/drafts", status_code=201)
def add_draft(payload: DraftIn, user: User = Depends(approved_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    return postar.save(db, user.id, payload.account_id, payload.text)


@router.patch("/api/drafts/{draft_id}")
def edit_draft(draft_id: int, payload: DraftPatch, user: User = Depends(approved_user),
               db: Session = Depends(get_db)) -> dict[str, Any]:
    return postar.update(db, user.id, draft_id, payload.posted, payload.text)


@router.delete("/api/drafts/{draft_id}", status_code=204)
def delete_draft(draft_id: int, user: User = Depends(approved_user), db: Session = Depends(get_db)) -> Response:
    postar.remove(db, user.id, draft_id)
    return Response(status_code=204)
