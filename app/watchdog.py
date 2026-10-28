"""Vigia em segundo plano, dentro do uvicorn.

- Avisa no Telegram quando o robo.py para de coletar (sem batimento OK há mais
  de ROBO_SILENCE_MIN minutos, ou de 2 intervalos + 5 min quando o intervalo
  da aba Configurações é maior). O robô grava um batimento por rodada em
  sync_runs (kind=tweets, account_id NULL). Sem nenhum batimento ainda (robô
  antigo) ou com a coleta automática desligada, fica quieto.
- Empurra por Web Push cada alerta de views novo do robô (app/push.py) para os
  navegadores inscritos no sininho, com o painel aberto ou não.
- Se AUTO_CHECK_MINUTES > 0, verifica a saúde das contas nesse intervalo; o
  alerta de Ghost/Search Ban sai do check_handle (dispatch_alerts). Gasta cota
  da API do X (3 chamadas por conta), por isso vem desligado. Só as contas do
  admin, com o token do .env.

Desligue tudo com WATCHDOG=off. Pensado para um único worker, como no VPS;
com mais de um, o dedupe de alerts_sent evita mensagem repetida.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import DEFAULT_TZ, SyncKind, SyncRun, SyncStatus, User, XAccount
from app.push import push_new_alerts
from app.services import (
    as_utc,
    check_handle,
    get_settings,
    now_utc,
    owned_accounts,
    robot_interval,
    token_configured,
    try_send,
    zone,
)

log = logging.getLogger("xhenry")
TICK_SECONDS = 60
PENDING_GRACE = timedelta(minutes=5)


def _env_int(name: str, default: int) -> int:
    try:
        return max(0, int(os.getenv(name, "").strip() or default))
    except ValueError:
        return default


def watchdog_enabled() -> bool:
    return os.getenv("WATCHDOG", "on").strip().lower() not in {"0", "off", "false", "no"}


def silence_limit(interval_min: int = 5) -> timedelta:
    base = _env_int("ROBO_SILENCE_MIN", 15) or 15
    return timedelta(minutes=max(base, 2 * interval_min + 5))


def last_robot_beat(db: Session) -> datetime | None:
    """Fim da última rodada do robô que coletou algo (ok ou parcial)."""
    value = db.scalar(
        select(func.max(SyncRun.finished_at)).where(
            SyncRun.kind == SyncKind.TWEETS,
            SyncRun.account_id.is_(None),
            SyncRun.status.in_([SyncStatus.OK, SyncStatus.PARTIAL]),
        )
    )
    return as_utc(value) if value is not None else None


def check_silence(db: Session, now: datetime | None = None) -> bool:
    settings = get_settings(db)
    if not settings.robo_enabled:
        return False
    last = last_robot_beat(db)
    if last is None:
        return False
    now = now or now_utc()
    pending = settings.robo_run_requested_at
    if pending is not None and now - as_utc(pending) < PENDING_GRACE:
        return False  # acabou de religar ou de pedir coleta: o robô pega no próximo minuto
    quiet = now - last
    if quiet <= silence_limit(robot_interval(db, settings)):
        return False
    when = last.astimezone(zone(DEFAULT_TZ)).strftime("%d/%m/%Y %H:%M")
    text = (
        "🔇 Robô parado\n"
        f"Nenhuma coleta há {int(quiet.total_seconds() // 60)} min (última: {when}).\n"
        "Confira o robo.py no VPS: cron, sessão do anyx e conexão com o banco."
    )
    # Uma mensagem por apagão: a chave muda quando o robô volta e para de novo.
    return try_send(db, f"silencio:{last.isoformat()}", "silencio", text)


def run_auto_check(db: Session, now: datetime | None = None) -> int:
    minutes = _env_int("AUTO_CHECK_MINUTES", 0)
    if not minutes or not token_configured():
        return 0
    admin = db.scalar(select(User).where(User.is_admin.is_(True)).order_by(User.id).limit(1))
    if admin is None:
        return 0
    cutoff = (now or now_utc()) - timedelta(minutes=minutes)
    accounts = db.scalars(
        owned_accounts(admin.id)
        .where(XAccount.is_active.is_(True), XAccount.is_archived.is_(False))
        .order_by(XAccount.id)
    ).all()
    due: list[str] = []
    for account in accounts:
        stale = account.last_health_checked_at is None or as_utc(account.last_health_checked_at) < cutoff
        if stale and account.username not in due:
            due.append(account.username)
    checked = 0
    for handle in due:
        try:
            check_handle(db, admin, handle, None)
            checked += 1
        except HTTPException as exc:
            # Token recusado ou ausente vale para todas: para e tenta no próximo ciclo.
            log.warning("auto-check @%s falhou: %s", handle, exc.detail)
            db.rollback()
            break
    return checked


def run_once(now: datetime | None = None) -> None:
    with SessionLocal() as db:
        check_silence(db, now)
        try:
            push_new_alerts(db)
        except Exception:
            log.exception("push dos alertas falhou nesta rodada")
            db.rollback()
        run_auto_check(db, now)


async def watch_forever() -> None:
    while True:
        try:
            await asyncio.to_thread(run_once)
        except Exception:
            log.exception("vigia falhou nesta rodada")
        await asyncio.sleep(TICK_SECONDS)
