""""Atualizar dados" na hora: o painel lê pelo Apify sem esperar a passada do cron (até 1 minuto).

Roda robo.read_now numa thread (a mesma leitura do robô, com o token do usuário) e guarda em memória
quem está sendo lido, para a tela saber quando acabou. Só com Postgres (o robô é SQL de Postgres); sem
ele, ou se a thread quebrar, o pedido fica gravado e o cron faz a leitura, como antes. Um processo
uvicorn só, como o resto do painel.
"""

from __future__ import annotations

import logging
import threading

from app import cache
from app.database import SessionLocal, engine
from app.models import AppSetting, UserSetting
from app.services import now_utc

log = logging.getLogger("xhenry")
_lock = threading.Lock()
_running: set[int] = set()


def available() -> bool:
    return engine.dialect.name == "postgresql"


def running(owner_id: int) -> bool:
    with _lock:
        return owner_id in _running


def _leave_for_cron(owner_id: int, is_admin: bool) -> None:
    """A leitura na hora quebrou: grava o pedido e o cron tenta no minuto seguinte."""
    with SessionLocal() as db:
        if is_admin:
            settings = db.get(AppSetting, 1)
            if settings is not None:
                settings.robo_run_requested_at = now_utc()
        else:
            row = db.query(UserSetting).filter(UserSetting.owner_id == owner_id).first()
            if row is not None:
                row.apify_run_requested_at = now_utc()
        db.commit()


def _read(owner_id: int, is_admin: bool) -> None:
    try:
        import robo

        result = robo.read_now(owner_id)
        log.info("leitura na hora do usuário %s: %s", owner_id, result)
    except Exception:
        log.exception("leitura na hora do usuário %s falhou; fica para o cron", owner_id)
        try:
            _leave_for_cron(owner_id, is_admin)
        except Exception:
            log.exception("não gravou o pedido para o cron")
    finally:
        with _lock:
            _running.discard(owner_id)
        cache.forget()  # o dashboard monta de novo com o que acabou de chegar


def start(owner_id: int, is_admin: bool) -> bool:
    """Começa a leitura agora. False = indisponível aqui (SQLite): quem chamou grava o pedido do cron."""
    if not available():
        return False
    with _lock:
        if owner_id in _running:
            return True  # já está lendo: o segundo clique só espera a mesma leitura
        _running.add(owner_id)
    threading.Thread(target=_read, args=(owner_id, is_admin), name=f"ler-agora-{owner_id}", daemon=True).start()
    return True
