"""Rotas de Telegram, configurações e verificação de post."""

from __future__ import annotations

import base64
import re
import secrets
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import ROOT
from app.database import get_db
from app.models import DEFAULT_TZ, AlertSent, TweetSample, XAccount
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


def viral_item(db: Session, row: AlertSent) -> dict[str, Any]:
    """Um alerta "Post viralizando" do robô com a conta, o link e as views de agora.

    A chave do robô é viral:{conta}:{tweet}; o texto do alerta só entra se a conta foi apagada.
    """
    text = row.detail or ""
    _, account_id, tweet_id = (row.dedupe_key.split(":", 2) + ["", ""])[:3]
    account = db.get(XAccount, int(account_id)) if account_id.isdigit() else None
    sample = None
    if account is not None and tweet_id:
        sample = db.scalar(
            select(TweetSample).where(TweetSample.account_id == account.id, TweetSample.tweet_id == tweet_id)
        )
    found = re.search(r"Conta:\s*(.*?)\s*\(@(\w+)\)", text)
    username = account.username if account else (found.group(2) if found else "")
    link = re.search(r"https://x\.com/\S+", text)
    views = re.search(r"Views:\s*([\d.]+)", text)
    return {
        "id": row.id,
        "sent_at": as_utc(row.sent_at).isoformat(),
        "username": username,
        "display_name": (account.display_name if account else (found.group(1) if found else None)) or None,
        "profile_image_url": account.profile_image_url if account else None,
        "url": f"https://x.com/{username}/status/{tweet_id}" if username and tweet_id.isdigit() else (link.group(0) if link else None),
        "text": sample.text_preview if sample else None,
        "views": int(sample.view_count or 0) if sample else (int(views.group(1).replace(".", "")) if views else None),
        "likes": int(sample.like_count or 0) if sample else None,
    }


@router.get("/api/viral", dependencies=[Depends(current_user)])
def viral_alerts(limit: int = Query(default=20, ge=1, le=50), db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    """Sininho do topo: só os alertas de post viralizando, do mais novo para o mais antigo."""
    rows = db.scalars(
        select(AlertSent).where(AlertSent.kind == "viral").order_by(AlertSent.id.desc()).limit(limit)
    ).all()
    return [viral_item(db, row) for row in rows]


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
