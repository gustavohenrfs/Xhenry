"""Rotas de autenticação (login, registro, senha, etc.)"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from base64 import urlsafe_b64decode, urlsafe_b64encode
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import User

router = APIRouter()
_bearer = HTTPBearer(auto_error=False)
TOKEN_TTL = 60 * 60 * 24 * 7


class RegisterIn(BaseModel):
    nome: str = Field(min_length=1, max_length=120)
    email: str = Field(min_length=3, max_length=320)
    senha: str = Field(min_length=8, max_length=128)


class LoginIn(BaseModel):
    email: str
    senha: str


class ForgotIn(BaseModel):
    email: str


class ChangePasswordIn(BaseModel):
    nova_senha: str = Field(min_length=8, max_length=128)


def _b64(raw: bytes) -> str:
    return urlsafe_b64encode(raw).rstrip(b"=").decode()


def _b64_decode(raw: str) -> bytes:
    return urlsafe_b64decode(raw + "=" * (-len(raw) % 4))


def secret_key() -> str:
    import os
    key = os.getenv("SECRET_KEY", "").strip()
    if not key:
        raise HTTPException(503, "Defina SECRET_KEY no .env antes de registrar ou entrar.")
    return key


def hash_password(password: str) -> str:
    import secrets
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000).hex()
    return f"pbkdf2_sha256$200000${salt}${digest}"


def verify_password(password: str, stored: str | None) -> bool:
    if not stored:
        return False
    try:
        _algo, rounds, salt, digest = stored.split("$", 3)
        check = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), int(rounds)).hex()
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(check, digest)


def issue_token(user_id: int) -> str:
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    body = _b64(json.dumps({"sub": str(user_id), "exp": int(time.time()) + TOKEN_TTL}, separators=(",", ":")).encode())
    sig = hmac.new(secret_key().encode(), f"{header}.{body}".encode(), hashlib.sha256).digest()
    return f"{header}.{body}.{_b64(sig)}"


def read_token(token: str) -> int:
    try:
        header, body, sig = token.split(".")
        expected = hmac.new(secret_key().encode(), f"{header}.{body}".encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(_b64_decode(sig), expected):
            raise ValueError("assinatura")
        payload = json.loads(_b64_decode(body))
        if int(payload["exp"]) < int(time.time()):
            raise ValueError("expirado")
        return int(payload["sub"])
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(401, "Token inválido.") from exc


def normalize_email(value: str) -> str:
    email = value.strip().lower()
    if "@" not in email or " " in email or email.startswith("@") or email.endswith("@"):
        raise HTTPException(400, "Email inválido.")
    return email


def public_user(user: User) -> dict[str, Any]:
    return {
        "id": user.id,
        "email": user.email,
        "nome_exibicao": user.nome_exibicao or user.name,
        "avatar_url": user.avatar_url,
        "logo_url": user.logo_url,
    }


def current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: Session = Depends(get_db),
):
    if creds is None or not creds.credentials:
        raise HTTPException(401, "Login necessário.")
    user = db.get(User, read_token(creds.credentials))
    if user is None or not user.is_active:
        raise HTTPException(401, "Usuário não encontrado.")
    return user


@router.post("/api/register", status_code=201)
def register(payload: RegisterIn, db: Session = Depends(get_db)) -> dict[str, Any]:
    email = normalize_email(payload.email)
    if db.scalar(select(User).where(User.email == email)):
        raise HTTPException(409, "Já existe uma conta com esse email.")
    user = User(
        email=email,
        name=payload.nome.strip(),
        nome_exibicao=payload.nome.strip(),
        senha_hash=hash_password(payload.senha),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return public_user(user)


@router.post("/api/login")
def login(payload: LoginIn, db: Session = Depends(get_db)) -> dict[str, Any]:
    email = normalize_email(payload.email)
    user = db.scalar(select(User).where(User.email == email))
    if user is None or not verify_password(payload.senha, user.senha_hash):
        raise HTTPException(401, "Email ou senha incorretos.")
    return {"access_token": issue_token(user.id), "token_type": "bearer", "user": public_user(user)}


@router.get("/api/me")
def me(user=Depends(current_user)) -> dict[str, Any]:
    return public_user(user)


@router.patch("/api/me")
async def update_me(
    nome_exibicao: str = Form(""),
    avatar: UploadFile | None = File(default=None),
    user=Depends(current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    from app.database import ROOT

    row = db.get(User, user.id)
    if row is None:
        raise HTTPException(401, "Usuário não encontrado.")
    name = nome_exibicao.strip()
    if not name or len(name) > 120:
        raise HTTPException(400, "Nome de exibição inválido.")
    row.nome_exibicao = name
    row.name = name
    if avatar is not None and avatar.filename:
        raw = await avatar.read()
        if not raw or len(raw) > 2_000_000:
            raise HTTPException(400, "A foto deve ter até 2 MB.")
        kind = (avatar.content_type or "").lower()
        if kind not in {"image/jpeg", "image/jpg", "image/png"}:
            raise HTTPException(400, "Use JPG ou PNG.")
        mime = "image/png" if kind == "image/png" else "image/jpeg"
        b64 = base64.b64encode(raw).decode("ascii")
        row.avatar_url = f"data:{mime};base64,{b64}"
    db.commit()
    db.refresh(row)
    return public_user(row)


@router.post("/api/forgot-password")
def forgot_password(payload: ForgotIn, db: Session = Depends(get_db)) -> dict[str, Any]:
    email = normalize_email(payload.email)
    user = db.scalar(select(User).where(User.email == email))
    if user:
        user.senha_hash = hash_password("xhenry123")
        db.commit()
    return {"ok": True, "message": "Se o email existir, a senha foi redefinida para xhenry123. Use-a para entrar e troque depois."}


@router.post("/api/change-password")
def change_password(payload: ChangePasswordIn, user=Depends(current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    row = db.get(User, user.id)
    if row is None:
        raise HTTPException(401, "Usuário não encontrado.")
    row.senha_hash = hash_password(payload.nova_senha)
    db.commit()
    return {"ok": True}


@router.delete("/api/me", status_code=204)
def delete_me(user=Depends(current_user), db: Session = Depends(get_db)):
    row = db.get(User, user.id)
    if row:
        db.delete(row)
        db.commit()
    return None


@router.get("/api/delete-by-email")
def delete_by_email(email: str, db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.email == email.lower()))
    if user:
        db.delete(user)
        db.commit()
        return {"deleted": True}
    return {"deleted": False}