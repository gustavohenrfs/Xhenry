from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import AppSetting, Folder, User, XAccount
from app.schemas import LOCAL_EMAIL


def get_settings(db: Session) -> AppSetting:
    row = db.get(AppSetting, 1)
    if row is None:
        row = AppSetting(id=1, impression_goal=500)
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


def ensure_owner(db: Session) -> User:
    user = db.scalar(select(User).where(User.email == LOCAL_EMAIL))
    if user is None:
        user = User(email=LOCAL_EMAIL, name="XHenry", nome_exibicao="XHenry")
        db.add(user)
        db.commit()
        db.refresh(user)
    return user


def normalize_username(value: str) -> str:
    handle = value.strip().lstrip("@").lower()
    if not handle or len(handle) > 15 or not all(ch.isalnum() or ch == "_" for ch in handle):
        from fastapi import HTTPException
        raise HTTPException(400, "Handle inválido. Use 1 a 15 caracteres: letras, números ou _.")
    return handle


def telegram_creds(db: Session) -> tuple[str, str]:
    row = get_settings(db)
    import os
    token = (row.telegram_token or os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    chat = (row.telegram_chat_id or os.getenv("TELEGRAM_CHAT_ID") or "").strip()
    return token, chat


def xai_key(db: Session) -> str:
    row = get_settings(db)
    import os
    return (row.xai_api_key or os.getenv("XAI_API_KEY") or "").strip()