"""Rotas de registro, login e perfil."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from base64 import urlsafe_b64decode, urlsafe_b64encode
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Invite, User
from app.services import admin_email, as_utc, now_utc, send_telegram, telegram_creds

log = logging.getLogger("xhenry")
router = APIRouter()
_bearer = HTTPBearer(auto_error=False)
TOKEN_TTL = 60 * 60 * 24 * 7
RESET_TTL = 60 * 15
RESET_COOLDOWN = 60
# email -> último envio do link. Em memória: basta com um único worker, como no VPS.
_reset_sent: dict[str, float] = {}
RESET_INVALID = "Link inválido ou expirado. Peça um novo."


class RegisterIn(BaseModel):
    nome: str = Field(min_length=1, max_length=120)
    email: str = Field(min_length=3, max_length=320)
    senha: str = Field(min_length=8, max_length=128)
    # Link de convite (#invite=…) gerado pelo admin na aba Usuários.
    invite: str | None = Field(default=None, max_length=64)


class LoginIn(BaseModel):
    email: str
    senha: str


class ChangePasswordIn(BaseModel):
    nova_senha: str = Field(min_length=8, max_length=128)


class ResetRequestIn(BaseModel):
    email: str = Field(min_length=3, max_length=320)


class ResetConfirmIn(BaseModel):
    token: str = Field(min_length=1, max_length=2048)
    nova_senha: str = Field(min_length=8, max_length=128)


def _b64(raw: bytes) -> str:
    return urlsafe_b64encode(raw).rstrip(b"=").decode()


def _b64_decode(raw: str) -> bytes:
    return urlsafe_b64decode(raw + "=" * (-len(raw) % 4))


def secret_key() -> str:
    key = os.getenv("SECRET_KEY", "").strip()
    if not key:
        raise HTTPException(503, "Defina SECRET_KEY no .env antes de registrar ou entrar.")
    return key


def registration_open() -> bool:
    """Cadastro só com ALLOW_REGISTRATION=true; ausente ou qualquer outro valor fecha."""
    return os.getenv("ALLOW_REGISTRATION", "").strip().lower() in {"1", "true", "yes", "on"}


def hash_password(password: str) -> str:
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


def _reset_key() -> bytes:
    # Chave derivada: token de login não vale como link de redefinição, e vice-versa.
    return hmac.new(secret_key().encode(), b"password-reset", hashlib.sha256).digest()


def _sign(claims: dict[str, Any], key: bytes) -> str:
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    body = _b64(json.dumps(claims, separators=(",", ":")).encode())
    sig = hmac.new(key, f"{header}.{body}".encode(), hashlib.sha256).digest()
    return f"{header}.{body}.{_b64(sig)}"


def _verify(token: str, key: bytes) -> dict[str, Any]:
    """Payload do token se a assinatura confere e não expirou; senão ValueError."""
    header, body, sig = token.split(".")
    expected = hmac.new(key, f"{header}.{body}".encode(), hashlib.sha256).digest()
    if not hmac.compare_digest(_b64_decode(sig), expected):
        raise ValueError("assinatura")
    payload = json.loads(_b64_decode(body))
    if int(payload["exp"]) < int(time.time()):
        raise ValueError("expirado")
    return payload


def issue_token(user_id: int, version: int = 0) -> str:
    claims = {"sub": str(user_id), "exp": int(time.time()) + TOKEN_TTL, "ver": version}
    return _sign(claims, secret_key().encode())


def read_token(token: str) -> tuple[int, int]:
    """(id do usuário, versão do token). Token de antes da versão conta como 0."""
    try:
        payload = _verify(token, secret_key().encode())
        return int(payload["sub"]), int(payload.get("ver", 0))
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(401, "Token inválido.") from exc


def revoke_sessions(user: User) -> None:
    # Todo token emitido antes disto deixa de valer em current_user.
    user.token_version = (user.token_version or 0) + 1


def _password_mark(user: Any) -> str:
    # Muda junto com a senha: depois de redefinir, o mesmo link deixa de valer.
    return hmac.new(_reset_key(), (user.senha_hash or "").encode(), hashlib.sha256).hexdigest()[:32]


def issue_reset_token(user: Any, ttl: int = RESET_TTL) -> str:
    return _sign({"sub": str(user.id), "exp": int(time.time()) + ttl, "pwd": _password_mark(user)}, _reset_key())


def read_reset_token(token: str, db: Session) -> User:
    try:
        payload = _verify(token, _reset_key())
        user = db.get(User, int(payload["sub"]))
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(400, RESET_INVALID) from exc
    if user is None or not user.is_active or not hmac.compare_digest(str(payload.get("pwd", "")), _password_mark(user)):
        raise HTTPException(400, RESET_INVALID)
    return user


def public_base_url(request: Request) -> str:
    """PUBLIC_URL do .env (atrás do nginx a requisição chega como 127.0.0.1); senão a URL pedida."""
    return (os.getenv("PUBLIC_URL", "").strip() or str(request.base_url)).rstrip("/")


def reset_message(email: str, link: str) -> str:
    return (
        "🔐 Pedido de redefinição de senha no XHenry\n"
        f"👤 Conta: {email}\n"
        f"🔗 Link (vale {RESET_TTL // 60} min, uso único): {link}\n"
        "Se não foi você, ignore esta mensagem."
    )


def normalize_email(value: str) -> str:
    email = value.strip().lower()
    if "@" not in email or " " in email or email.startswith("@") or email.endswith("@"):
        raise HTTPException(400, "Email inválido.")
    return email


def public_user(user: Any) -> dict[str, Any]:
    return {
        "id": user.id,
        "email": user.email,
        "nome_exibicao": user.nome_exibicao or user.name,
        "avatar_url": user.avatar_url,
        "logo_url": user.logo_url,
        "is_admin": bool(user.is_admin),
        "is_approved": bool(user.is_approved),
    }


PENDING = "Seu acesso está aguardando aprovação do administrador."


def current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: Session = Depends(get_db),
):
    if creds is None or not creds.credentials:
        raise HTTPException(401, "Login necessário.")
    user_id, version = read_token(creds.credentials)
    user = db.get(User, user_id)
    if user is None or not user.is_active:
        raise HTTPException(401, "Usuário não encontrado.")
    if version != (user.token_version or 0):
        raise HTTPException(401, "Sessão encerrada. Entre novamente.")
    return user


def approved_user(user: User = Depends(current_user)) -> User:
    """Logado e aprovado pelo admin: a dependência de toda rota com dados. Pendente leva 403 "pending"
    (o painel mostra a tela de espera)."""
    if not user.is_approved:
        raise HTTPException(403, PENDING, headers={"X-Access": "pending"})
    return user


def admin_user(user: User = Depends(approved_user)) -> User:
    """Só o admin: Coletores, robô, Usuários, Telegram e verificação com o token do .env."""
    if not user.is_admin:
        raise HTTPException(403, "Só o administrador pode fazer isso.")
    return user


def open_invite(db: Session, token: str | None) -> Invite | None:
    """Convite que ainda vale (existe, não foi usado, não venceu)."""
    if not token:
        return None
    invite = db.scalar(select(Invite).where(Invite.token == token.strip()))
    if invite is None or invite.used_at is not None or as_utc(invite.expires_at) < now_utc():
        return None
    return invite


def new_request_message(user: User) -> str:
    return (
        "👤 Novo pedido de acesso no XHenry\n"
        f"Nome: {user.nome_exibicao or user.name}\n"
        f"Email: {user.email}\n"
        "Aprove na aba Usuários do painel."
    )


@router.post("/api/register", status_code=201)
def register(payload: RegisterIn, db: Session = Depends(get_db)) -> dict[str, Any]:
    """Cadastro pelo link de convite (uso único). Quem entra fica pendente até o admin aprovar.

    ALLOW_REGISTRATION=true abre o cadastro sem convite (porta de emergência para criar o admin).
    """
    invite = open_invite(db, payload.invite)
    if payload.invite and invite is None:
        raise HTTPException(403, "Convite inválido, já usado ou vencido. Peça um novo ao administrador.")
    if invite is None and not registration_open():
        raise HTTPException(403, "Cadastro desativado. Peça acesso ao administrador.")
    email = normalize_email(payload.email)
    if db.scalar(select(User).where(User.email == email)):
        raise HTTPException(409, "Já existe uma conta com esse email.")
    is_admin = email == admin_email()
    user = User(
        email=email,
        name=payload.nome.strip(),
        nome_exibicao=payload.nome.strip(),
        senha_hash=hash_password(payload.senha),
        is_admin=is_admin,
        is_approved=is_admin,
        approved_at=now_utc() if is_admin else None,
        invited_by_id=invite.created_by_id if invite else None,
    )
    db.add(user)
    db.flush()
    if invite is not None:
        invite.used_by_id, invite.used_at = user.id, now_utc()
    db.commit()
    db.refresh(user)
    if not user.is_approved:
        try:
            send_telegram(*telegram_creds(db), new_request_message(user))
        except HTTPException as exc:
            log.warning("aviso de novo pedido não enviado: %s", exc.detail)
    return public_user(user)


@router.get("/api/config")
def public_config(invite: str | None = None, db: Session = Depends(get_db)) -> dict[str, Any]:
    """O que a tela de login pode oferecer. Pública: roda antes de entrar. ?invite= diz se o convite vale."""
    token, chat = telegram_creds(db)
    payload: dict[str, Any] = {"allow_registration": registration_open(), "password_reset": bool(token and chat)}
    if invite is not None:
        payload["invite_valid"] = open_invite(db, invite) is not None
    return payload


@router.post("/api/login")
def login(payload: LoginIn, db: Session = Depends(get_db)) -> dict[str, Any]:
    email = normalize_email(payload.email)
    user = db.scalar(select(User).where(User.email == email))
    if user is None or not verify_password(payload.senha, user.senha_hash):
        raise HTTPException(401, "Email ou senha incorretos.")
    return {"access_token": issue_token(user.id, user.token_version or 0), "token_type": "bearer", "user": public_user(user)}


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
        if not raw or len(raw) > 10_000_000:
            raise HTTPException(400, "A imagem é muito grande ou o formato é inválido")
        kind = (avatar.content_type or "").lower()
        if kind not in {"image/jpeg", "image/jpg", "image/png"}:
            raise HTTPException(400, "A imagem é muito grande ou o formato é inválido")
        mime = "image/png" if kind == "image/png" else "image/jpeg"
        b64 = base64.b64encode(raw).decode("ascii")
        row.avatar_url = f"data:{mime};base64,{b64}"
    db.commit()
    db.refresh(row)
    return public_user(row)


@router.post("/api/change-password")
def change_password(payload: ChangePasswordIn, user=Depends(current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    row = db.get(User, user.id)
    if row is None:
        raise HTTPException(401, "Usuário não encontrado.")
    row.senha_hash = hash_password(payload.nova_senha)
    revoke_sessions(row)
    db.commit()
    # Os outros aparelhos saem; quem trocou continua com o token novo.
    return {"ok": True, "access_token": issue_token(row.id, row.token_version)}


@router.post("/api/password-reset/request")
def request_password_reset(payload: ResetRequestIn, request: Request, db: Session = Depends(get_db)) -> dict[str, Any]:
    secret_key()
    token, chat = telegram_creds(db)
    if not token or not chat:
        raise HTTPException(503, "Recuperação indisponível: o bot do Telegram não está configurado.")
    email = normalize_email(payload.email)
    user = db.scalar(select(User).where(User.email == email))
    now = time.time()
    # Mesma resposta com ou sem conta, para não revelar quais emails existem.
    if user is not None and user.is_active and now - _reset_sent.get(email, 0.0) >= RESET_COOLDOWN:
        _reset_sent[email] = now
        link = f"{public_base_url(request)}/#reset={issue_reset_token(user)}"
        try:
            send_telegram(token, chat, reset_message(email, link))
        except HTTPException as exc:
            log.warning("telegram reset não enviado: %s", exc.detail)
    return {"ok": True, "detail": "Se o email estiver cadastrado, o link foi enviado ao Telegram do administrador."}


@router.post("/api/password-reset/confirm")
def confirm_password_reset(payload: ResetConfirmIn, db: Session = Depends(get_db)) -> dict[str, Any]:
    user = read_reset_token(payload.token, db)
    user.senha_hash = hash_password(payload.nova_senha)
    revoke_sessions(user)
    db.commit()
    _reset_sent.pop(user.email, None)
    return {"ok": True}


@router.delete("/api/me", status_code=204)
def delete_me(user=Depends(current_user), db: Session = Depends(get_db)):
    if user.is_admin:
        # Apagaria em cascata as pastas e contas do admin e deixaria o painel sem ninguém para aprovar.
        raise HTTPException(400, "O administrador não pode apagar a própria conta.")
    row = db.get(User, user.id)
    if row:
        db.delete(row)
        db.commit()
    return None
