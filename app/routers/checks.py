"""Rotas de Telegram, configurações e verificação de post."""

from __future__ import annotations

import base64
import secrets
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import ROOT
from app.database import get_db
from app.models import DEFAULT_TZ, AlertSent, User
from app.routers.auth import admin_user, approved_user
from app.services import (
    alerts_query,
    as_utc,
    ask_grok,
    get_settings,
    now_utc,
    send_telegram,
    settings_payload,
    telegram_creds,
    user_settings,
    viral_item,
    viral_message,
    xai_key,
    zone,
)

router = APIRouter()


class SettingsIn(BaseModel):
    impression_goal: int | None = Field(default=None, ge=1, le=10_000_000)
    # Chaves: ausente = não mexe, "" = apaga, texto = grava.
    xai_api_key: str | None = Field(default=None, max_length=500)
    x_bearer_token: str | None = Field(default=None, max_length=500)
    apify_token: str | None = Field(default=None, max_length=500)


@router.get("/api/settings")
def read_settings(user: User = Depends(approved_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    return settings_payload(db, user)


@router.put("/api/settings")
def write_settings(payload: SettingsIn, user: User = Depends(approved_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    row = user_settings(db, user.id)
    if payload.impression_goal is not None:
        row.impression_goal = payload.impression_goal
    for field in ("xai_api_key", "x_bearer_token", "apify_token"):
        value = getattr(payload, field)
        if value is not None:
            setattr(row, field, value.strip() or None)
    if payload.apify_token is not None:
        # Token novo (ou apagado): o aviso antigo do robô não vale mais; com token, lê já.
        row.apify_status = row.apify_detail = None
        row.apify_run_requested_at = now_utc() if row.apify_token and not user.is_admin else None
        if row.apify_token and user.is_admin:
            get_settings(db).robo_run_requested_at = now_utc()  # o admin lê pela rodada do robô
    db.commit()
    return settings_payload(db, user)


@router.get("/api/alerts")
def recent_alerts(
    limit: int = Query(default=8, ge=1, le=50), user: User = Depends(approved_user), db: Session = Depends(get_db)
) -> list[dict[str, Any]]:
    """Últimos alertas do usuário (painel, robô e vigia), do mais novo para o mais antigo."""
    rows = db.scalars(alerts_query(user).order_by(AlertSent.sent_at.desc(), AlertSent.id.desc()).limit(limit)).all()
    return [
        {"kind": row.kind, "sent_at": as_utc(row.sent_at).isoformat(), "text": row.detail or ""}
        for row in rows
    ]


@router.get("/api/viral")
def viral_alerts(
    limit: int = Query(default=20, ge=1, le=50), user: User = Depends(approved_user), db: Session = Depends(get_db)
) -> list[dict[str, Any]]:
    """Sininho do topo: só os alertas de views (todos os níveis) das contas do usuário."""
    rows = db.scalars(
        select(AlertSent).where(AlertSent.kind == "viral", AlertSent.owner_id == user.id).order_by(AlertSent.id.desc()).limit(limit)
    ).all()
    return [viral_item(db, row) for row in rows]


@router.post("/api/telegram/test")
def test_telegram(user: User = Depends(admin_user), db: Session = Depends(get_db)) -> dict[str, bool]:
    row = user_settings(db, user.id)
    when = datetime.now(zone(DEFAULT_TZ)).strftime("%d/%m/%Y %H:%M")
    send_telegram(
        *telegram_creds(db),
        viral_message("Teste", "xhenry", row.impression_goal, when, 1, 1, "https://x.com/xhenry"),
    )
    return {"ok": True}


@router.post("/api/posts/review")
async def review_post(
    caption: str = Form(...),
    media: UploadFile | None = File(default=None),
    user: User = Depends(approved_user),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    text = caption.strip()
    if not text:
        raise HTTPException(400, "A legenda é obrigatória.")
    if len(text) > 2000:
        raise HTTPException(400, "Legenda acima de 2000 caracteres.")
    key = xai_key(db, user)
    if not key:
        raise HTTPException(503, "Cole a sua chave da xAI em Configurações para usar o Grok.")
    image_uri = None
    tmp_path = None
    if media is not None and media.filename:
        raw = await media.read()
        if len(raw) > 8_000_000:
            raise HTTPException(400, "Imagem acima de 8 MB.")
        kind = (media.content_type or "").lower()
        if kind not in {"image/jpeg", "image/jpg", "image/png"}:
            raise HTTPException(400, "Use JPG ou PNG. A API de visão não aceita outro formato.")
        suffix = "png" if kind == "image/png" else "jpeg"
        folder = ROOT / "data" / "tmp"
        folder.mkdir(parents=True, exist_ok=True)
        tmp_path = folder / f"{secrets.token_hex(8)}.{suffix}"
        tmp_path.write_bytes(raw)
        image_uri = f"data:image/{suffix};base64,{base64.b64encode(raw).decode()}"
    try:
        verdict = ask_grok(key, text, image_uri)
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
    return {"verdict": verdict}
