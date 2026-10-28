"""Cache em memória do /api/dashboard: trocar de período ou abrir o painel de novo sai na hora.

Cada resposta fica guardada já em JSON, com a "versão dos dados" de quando foi montada:
- um contador que sobe a cada escrita pela API (POST/PUT/PATCH/DELETE, ver ForgetOnWrite em main.py);
- o maior collected_at de tweet_samples e daily_metrics (o robo.py grava NOW() em toda leitura);
- o maior id de sync_runs (verificações de saúde, inclusive as do vigia);
- o maior id de tweet_samples e daily_metrics (linha nova no mesmo segundo de outra).
Se a versão mudou, monta de novo. Mesmo sem mudança, vive no máximo TTL_SECONDS, porque "hoje"
depende da hora. Vale para um processo uvicorn só (o Cloaker já exige isso); com mais de um, cada
processo teria o próprio cache.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from typing import Any, Callable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import DailyMetric, SyncRun, TweetSample

TTL_SECONDS = 60
MAX_ENTRIES = 64

_lock = threading.Lock()
_generation = 0
_entries: dict[Any, tuple[float, tuple, bytes, str]] = {}


def forget() -> None:
    """Alguma escrita aconteceu: tudo que está guardado deixa de valer."""
    global _generation
    with _lock:
        _generation += 1
        _entries.clear()


def data_version(db: Session) -> tuple:
    stamps = db.execute(
        select(
            select(func.max(TweetSample.collected_at)).scalar_subquery(),
            select(func.max(DailyMetric.collected_at)).scalar_subquery(),
            select(func.max(SyncRun.id)).scalar_subquery(),
            # O maior id muda a cada linha nova, mesmo gravada no mesmo segundo de outra (o carimbo de
            # tempo do SQLite é por segundo; gravações seguidas pareciam a mesma versão).
            select(func.max(TweetSample.id)).scalar_subquery(),
            select(func.max(DailyMetric.id)).scalar_subquery(),
        )
    ).one()
    return (_generation, *(str(value) for value in stamps))


def dumps(payload: Any) -> bytes:
    # Mesmo formato do JSONResponse do FastAPI.
    return json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")


def cached_json(db: Session, key: Any, build: Callable[[], Any]) -> tuple[bytes, str]:
    """Devolve (corpo JSON, etag) do cache ou de build(), guardando o resultado."""
    version = data_version(db)
    now = time.monotonic()
    with _lock:
        hit = _entries.get(key)
    if hit and hit[1] == version and now - hit[0] < TTL_SECONDS:
        return hit[2], hit[3]
    body = dumps(build())
    etag = '"' + hashlib.blake2b(body, digest_size=12).hexdigest() + '"'
    with _lock:
        if len(_entries) >= MAX_ENTRIES:
            _entries.clear()
        if version[0] == _generation:  # uma escrita no meio da montagem: não guarda
            _entries[key] = (now, version, body, etag)
    return body, etag


class ForgetOnWrite:
    """Middleware ASGI: depois de qualquer escrita em /api/ o cache é descartado."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        write = scope["type"] == "http" and scope["method"] not in ("GET", "HEAD", "OPTIONS")
        try:
            await self.app(scope, receive, send)
        finally:
            if write and scope["path"].startswith("/api/"):
                forget()
