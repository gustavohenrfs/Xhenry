"""Rotas de configurações, notificações, alertas e verificação de post."""

from __future__ import annotations

import base64
import json
import secrets
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.config import ROOT
from app.database import get_db
from app.models import AlertSent, User
from app.routers.auth import approved_user
from app.services import (
    DAY_STEPS,
    HHMM,
    NOTIFY_TYPES,
    POST_STEPS,
    alert_group,
    alert_item,
    alerts_query,
    as_utc,
    ask_grok,
    get_settings,
    notify_prefs,
    now_utc,
    settings_payload,
    user_settings,
    wants,
    xai_key,
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


# Filtro do histórico (Alertas recentes): grupo da tela → tipos de alerts_sent.
ALERT_FILTERS = {
    "views": {"viral", "marco_post", "marco_dia", "meta", "resumo"},
    "queda": {"queda"},
    "ban": {"ban"},
    "falha": {"coletor", "erro", "silencio", "apify"},
    "acesso": {"acesso"},
}


class QuietIn(BaseModel):
    on: bool | None = None
    from_: str | None = Field(default=None, alias="from", max_length=5)
    to: str | None = Field(default=None, max_length=5)


class NotifyIn(BaseModel):
    kinds: dict[str, bool] | None = None
    post_step: int | None = None
    day_step: int | None = None
    quiet: QuietIn | None = None


def notify_payload(db: Session, user: User) -> dict[str, Any]:
    row = user_settings(db, user.id)
    return {
        "prefs": notify_prefs(row),
        "types": [
            {"key": key, "label": label, "emoji": emoji}
            for key, label, emoji, admin_only in NOTIFY_TYPES
            if user.is_admin or not admin_only
        ],
        "post_steps": list(POST_STEPS),
        "day_steps": list(DAY_STEPS),
        "goal": row.impression_goal,
    }


@router.get("/api/notifications")
def read_notifications(user: User = Depends(approved_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    return notify_payload(db, user)


@router.put("/api/notifications")
def write_notifications(payload: NotifyIn, user: User = Depends(approved_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Configurações → Notificações: tipos ligados, marcos de views e não perturbe."""
    row = user_settings(db, user.id)
    prefs = notify_prefs(row)
    if payload.kinds:
        unknown = set(payload.kinds) - set(prefs["kinds"])
        if unknown:
            raise HTTPException(400, "Tipo de notificação desconhecido.")
        prefs["kinds"].update(payload.kinds)
    if payload.post_step is not None:
        if payload.post_step not in POST_STEPS:
            raise HTTPException(400, "Marco por post inválido.")
        prefs["post_step"] = payload.post_step
    if payload.day_step is not None:
        if payload.day_step not in DAY_STEPS:
            raise HTTPException(400, "Marco do dia inválido.")
        prefs["day_step"] = payload.day_step
    if payload.quiet is not None:
        for key, value in (("from", payload.quiet.from_), ("to", payload.quiet.to)):
            if value is not None:
                if not HHMM.match(value):
                    raise HTTPException(400, "Horário inválido. Use HH:MM.")
                prefs["quiet"][key] = value
        if payload.quiet.on is not None:
            prefs["quiet"]["on"] = payload.quiet.on
    row.notify_prefs = json.dumps(prefs)
    db.commit()
    return notify_payload(db, user)


@router.get("/api/alerts")
def recent_alerts(
    limit: int = Query(default=8, ge=1, le=50),
    group: str | None = Query(default=None),
    user: User = Depends(approved_user),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    """Histórico: últimos alertas do usuário (painel, robô e vigia), todos os tipos, do mais novo para o
    mais antigo. ?group= filtra (views, queda, ban, falha, acesso)."""
    query = alerts_query(user)
    if group:
        if group not in ALERT_FILTERS:
            raise HTTPException(400, "Filtro inválido.")
        query = query.where(AlertSent.kind.in_(ALERT_FILTERS[group]))
    rows = db.scalars(query.order_by(AlertSent.sent_at.desc(), AlertSent.id.desc()).limit(limit)).all()
    return [
        {"kind": row.kind, "group": alert_group(row), "sent_at": as_utc(row.sent_at).isoformat(), "text": row.detail or ""}
        for row in rows
    ]


@router.get("/api/viral")
def viral_alerts(
    limit: int = Query(default=20, ge=1, le=50), user: User = Depends(approved_user), db: Session = Depends(get_db)
) -> list[dict[str, Any]]:
    """Sininho do topo: as notificações do usuário (todo tipo ligado em Notificações), do mais novo para
    o mais antigo. O histórico completo fica em /api/alerts."""
    prefs = notify_prefs(user_settings(db, user.id))
    rows = db.scalars(alerts_query(user).order_by(AlertSent.id.desc()).limit(limit * 4)).all()
    return [alert_item(db, row) for row in rows if wants(prefs, row)][:limit]


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
