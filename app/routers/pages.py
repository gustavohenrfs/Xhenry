"""Rotas de página, saúde e manutenção."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, Response
from sqlalchemy.orm import Session

from app.config import INDEX, ROOT
from app.database import get_db
from app.routers.auth import current_user
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


from app.models import DailyMetric
from sqlalchemy import select, desc


@router.get("/debug-metrics", dependencies=[Depends(current_user)])
def debug_metrics(db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    """Rota temporária para debug de fuso horário.
    Retorna os últimos 5 registros de daily_metrics.
    Acesse via navegador: /debug-metrics
    """
    rows = db.scalars(
        select(DailyMetric)
        .order_by(desc(DailyMetric.metric_date))
        .limit(5)
    ).all()
    return [
        {
            "metric_date": str(r.metric_date),
            "impressions": r.impressions,
            "likes": r.likes,
        }
        for r in rows
    ]

