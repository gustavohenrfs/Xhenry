"""Rotas de página e saúde."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.config import INDEX, ROOT
from app.database import get_db
from app.live import hub, snapshot, view_for
from app.routers.auth import approved_user, current_user
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
    # no-cache: o navegador sempre confere, mas recebe 304 sem corpo quando nada mudou.
    return FileResponse(INDEX, media_type="text/html; charset=utf-8", headers={"Cache-Control": "no-cache"})


@router.get("/sw.js")
def service_worker() -> FileResponse:
    """Service worker do push na raiz, para valer no site inteiro (em /static ele só cobriria /static)."""
    return FileResponse(
        ROOT / "static" / "sw.js",
        media_type="text/javascript; charset=utf-8",
        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"},
    )


@router.get("/manifest.webmanifest")
def manifest() -> FileResponse:
    """Manifesto do app instalável (nome, ícones, cor)."""
    return FileResponse(
        ROOT / "static" / "manifest.webmanifest",
        media_type="application/manifest+json",
        headers={"Cache-Control": "no-cache"},
    )


def live_user(request: Request, token: str = "", db: Session = Depends(get_db)):
    """Login do /api/live: o EventSource não manda cabeçalho, então o token vem em ?token=."""
    header = request.headers.get("authorization", "")
    raw = token or (header[7:] if header.lower().startswith("bearer ") else "")
    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=raw) if raw else None
    return approved_user(current_user(creds, db))


@router.get("/api/live")
async def live(request: Request, once: bool = False, user=Depends(live_user)) -> Response:
    """Painel ao vivo (app/live.py): eventos com os carimbos de dados e alertas.

    ?once=1 devolve só o carimbo atual em JSON (testes e diagnóstico).
    """
    user_id, is_admin = user.id, bool(user.is_admin)
    if once:
        return JSONResponse(view_for(await run_in_threadpool(snapshot), user_id, is_admin))
    return StreamingResponse(
        hub.stream(request.is_disconnected, user_id, is_admin),
        media_type="text/event-stream",
        # X-Accel-Buffering: o nginx entrega cada evento na hora em vez de juntar num buffer.
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/favicon.ico")
def favicon() -> Response:
    return Response(status_code=204)


@router.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "service": "xhenry", "token_configured": token_configured()}
