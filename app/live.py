"""Painel ao vivo: um canal de eventos (Server-Sent Events) avisa o navegador quando algo mudou.

Um vigia só, compartilhado por todos os navegadores abertos, olha o banco a cada POLL_SECONDS numa
consulta só, com índices: o mesmo carimbo do cache do dashboard (escritas pela API, collected_at do
robô, sync_runs) e o último alerta. Quando um pedaço muda, cada navegador recebe {"data": …,
"alerts": …} e recarrega só o que mudou. Os valores são carimbos opacos (nada de dado de conta). A rota
exige login (token em ?token=, o EventSource não manda cabeçalho) e cada um recebe o carimbo dos
próprios alertas. O de dados é
um só para todos: um recarregamento a mais é barato e não mostra nada de ninguém. Um processo uvicorn
só, como o resto do painel; sem ninguém conectado o vigia para.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from typing import Any, AsyncIterator

from sqlalchemy import func, select
from starlette.concurrency import run_in_threadpool

from app import cache
from app.database import SessionLocal
from app.models import AlertSent

log = logging.getLogger("xhenry")
POLL_SECONDS = 2.0
# Comentário vazio de tempos em tempos: o nginx e o navegador não derrubam a conexão parada.
KEEPALIVE_SECONDS = 20.0
# De quanto em quanto tempo confere se o navegador saiu (aba fechada libera a conexão logo).
CHECK_SECONDS = 5.0


def _digest(*parts: Any) -> str:
    return hashlib.blake2b(repr(parts).encode(), digest_size=8).hexdigest()


def snapshot() -> dict[str, Any]:
    """Carimbos de agora: dados do dashboard e último alerta de cada dono."""
    with SessionLocal() as db:
        version = cache.data_version(db)
        alerts = dict(db.execute(select(AlertSent.owner_id, func.max(AlertSent.id)).group_by(AlertSent.owner_id)).all())
    return {
        "data": _digest(*version),
        "alerts": alerts,
    }


def view_for(state: dict[str, Any], user_id: int, is_admin: bool) -> dict[str, str]:
    """O que um navegador recebe: alertas só os dele (e os do sistema, para o admin)."""
    alerts = state["alerts"]
    return {
        "data": state["data"],
        "alerts": _digest(alerts.get(user_id), alerts.get(None) if is_admin else None),
    }


class LiveHub:
    """Vigia único: lê o snapshot enquanto houver alguém ouvindo e acorda todos quando muda."""

    def __init__(self) -> None:
        self.state: dict[str, str] | None = None
        self.listeners = 0
        self.changed = asyncio.Event()
        self.task: asyncio.Task | None = None

    async def _watch(self) -> None:
        while self.listeners > 0:
            try:
                state = await run_in_threadpool(snapshot)
                if state != self.state:
                    self.state = state
                    self.changed.set()
                    self.changed = asyncio.Event()
            except Exception:  # banco fora por um instante: tenta de novo na próxima volta
                log.warning("live: snapshot falhou", exc_info=True)
            await asyncio.sleep(POLL_SECONDS)
        self.task = None

    async def stream(self, disconnected, user_id: int, is_admin: bool) -> AsyncIterator[str]:
        self.listeners += 1
        if self.task is None or self.task.done():  # parado ou cancelado: liga de novo
            self.task = asyncio.create_task(self._watch())
        try:
            if self.state is None:
                self.state = await run_in_threadpool(snapshot)
            sent = None
            idle = 0.0
            while not await disconnected():
                view = view_for(self.state, user_id, is_admin)
                if view != sent:
                    sent = view
                    idle = 0.0
                    yield "data: " + json.dumps(sent) + "\n\n"
                try:
                    await asyncio.wait_for(self.changed.wait(), CHECK_SECONDS)
                except asyncio.TimeoutError:
                    idle += CHECK_SECONDS
                    if idle >= KEEPALIVE_SECONDS:
                        idle = 0.0
                        yield ": ping\n\n"
        finally:
            self.listeners -= 1


hub = LiveHub()
