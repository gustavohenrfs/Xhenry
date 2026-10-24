"""Rotas de página, saúde e manutenção."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, Response
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import INDEX, ROOT
from app.database import get_db
from app.services import token_configured

router = APIRouter()


@router.get("/avatars/{filename}")
def avatar_file(filename: str) -> FileResponse:
    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(404, "Avatar não encontrado.")
    stem, dot, ext = filename.partition(".")
    if not stem.isdigit() or ext not in {"jpg", "png"} or dot != ".":
        raise HTTPException(404, "Avatar não encontrado.")
    path = ROOT / "data" / "avatars" / filename
    if not path.is_file():
        raise HTTPException(404, "Avatar não encontrado.")
    media = "image/png" if ext == "png" else "image/jpeg"
    return FileResponse(path, media_type=media, headers={"Cache-Control": "no-store"})


@router.get("/")
def root() -> FileResponse:
    if not INDEX.is_file():
        raise HTTPException(500, "index.html não está ao lado do main.py.")
    return FileResponse(INDEX, media_type="text/html; charset=utf-8", headers={"Cache-Control": "no-store"})


@router.get("/favicon.ico")
def favicon() -> Response:
    return Response(status_code=204)


@router.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "service": "xhenry", "token_configured": token_configured()}


@router.get("/fix-db")
def fix_is_archived_column(db: Session = Depends(get_db)) -> dict[str, str]:
    """Rota temporária para adicionar colunas novas.
    Acesse via navegador: https://seu-app.onrender.com/fix-db
    Depois de usar, pode remover esta rota.
    """
    try:
        db.execute(text("""
            ALTER TABLE x_accounts 
            ADD COLUMN IF NOT EXISTS is_archived BOOLEAN NOT NULL DEFAULT FALSE;
        """))
        db.execute(text("""
            ALTER TABLE users 
            ADD COLUMN IF NOT EXISTS logo_url VARCHAR(512);
        """))
        db.commit()
        return {"status": "ok", "message": "Colunas atualizadas com sucesso!"}
    except Exception as e:
        return {"status": "error", "message": str(e)}
