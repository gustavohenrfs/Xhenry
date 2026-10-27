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
from app.models import DEFAULT_TZ, AlertSent
from app.routers.auth import current_user
from app.services import (
    as_utc,
    ask_grok,
    get_settings,
    send_telegram,
    settings_payload,
    telegram_creds,
    viral_message,
    xai_key,
    zone,
)

router = APIRouter()


class SettingsIn(BaseModel):
    impression_goal: int | None = Field(default=None, ge=1, le=10_000_000)
    xai_api_key: str | None = None


@router.get("/api/settings", dependencies=[Depends(current_user)])
def read_settings(db: Session = Depends(get_db)) -> dict[str, Any]:
    return settings_payload(get_settings(db))


@router.put("/api/settings", dependencies=[Depends(current_user)])
def write_settings(payload: SettingsIn, db: Session = Depends(get_db)) -> dict[str, Any]:
    row = get_settings(db)
    if payload.impression_goal is not None:
        row.impression_goal = payload.impression_goal
    if payload.xai_api_key:
        row.xai_api_key = payload.xai_api_key.strip()
    db.commit()
    return settings_payload(row)


@router.get("/api/alerts", dependencies=[Depends(current_user)])
def recent_alerts(limit: int = Query(default=8, ge=1, le=50), db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    """Últimos alertas enviados ao Telegram (painel, robô e vigia), do mais novo para o mais antigo."""
    rows = db.scalars(select(AlertSent).order_by(AlertSent.sent_at.desc(), AlertSent.id.desc()).limit(limit)).all()
    return [
        {"kind": row.kind, "sent_at": as_utc(row.sent_at).isoformat(), "text": row.detail or ""}
        for row in rows
    ]


@router.post("/api/telegram/test", dependencies=[Depends(current_user)])
def test_telegram(db: Session = Depends(get_db)) -> dict[str, bool]:
    row = get_settings(db)
    when = datetime.now(zone(DEFAULT_TZ)).strftime("%d/%m/%Y %H:%M")
    send_telegram(
        *telegram_creds(db),
        viral_message("Teste", "xhenry", row.impression_goal, when, 1, 1, "https://x.com/xhenry"),
    )
    return {"ok": True}


@router.post("/api/posts/review", dependencies=[Depends(current_user)])
async def review_post(
    caption: str = Form(...),
    media: UploadFile | None = File(default=None),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    text = caption.strip()
    if not text:
        raise HTTPException(400, "A legenda é obrigatória.")
    if len(text) > 2000:
        raise HTTPException(400, "Legenda acima de 2000 caracteres.")
    key = xai_key(db)
    if not key:
        raise HTTPException(503, "Defina a chave da xAI em Configurações ou em XAI_API_KEY.")
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
