"""XHenry — painel de contas do X.

    python -m uvicorn main:app --host 127.0.0.1 --port 8000

O dashboard lê só o banco (tweet_samples / daily_metrics).
Métricas vêm do robo.py no VPS. /api/check usa a API oficial só para saúde da conta.
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware

import app.models  # registra tabelas no metadata
from app.cache import ForgetOnWrite
from app.config import ROOT
from app.database import Base, SessionLocal, engine, load_env_file
from app.routers import accounts, admin, auth, checks, cloaker, pages, push, radar, robo
from app.services import ensure_admin, ensure_user_auth_columns, token_configured
from app.watchdog import watch_forever, watchdog_enabled

load_env_file(ROOT / ".env")
log = logging.getLogger("xhenry")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    Base.metadata.create_all(bind=engine)
    ensure_user_auth_columns()
    with SessionLocal() as db:
        ensure_admin(db)
    log.info("token X: %s", "configurado" if token_configured() else "ausente")
    watcher = asyncio.create_task(watch_forever()) if watchdog_enabled() else None
    yield
    if watcher is not None:
        watcher.cancel()
        with suppress(asyncio.CancelledError):
            await watcher


class CachedStatic(StaticFiles):
    """/static com cache no navegador: com ?v= no link o arquivo nunca muda (troca o v para atualizar),
    então fica guardado por um ano; sem ?v= (imagens), um dia."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        if response.status_code in (200, 304):
            versioned = b"v=" in scope.get("query_string", b"")
            response.headers["Cache-Control"] = (
                "public, max-age=31536000, immutable" if versioned else "public, max-age=86400"
            )
        return response


_docs = None if os.getenv("APP_ENV") == "production" else "/docs"
app = FastAPI(title="XHenry", version="1.0.0", lifespan=lifespan, docs_url=_docs, redoc_url=None)
app.mount("/static", CachedStatic(directory=ROOT / "static"), name="static")
# JSON, HTML, JS e CSS vão comprimidos (o zip do Cloaker e as imagens ficam de fora).
app.add_middleware(GZipMiddleware, minimum_size=1000)
app.add_middleware(ForgetOnWrite)

app.include_router(auth.router)
app.include_router(accounts.router)
app.include_router(checks.router)
app.include_router(cloaker.router)
app.include_router(radar.router)
app.include_router(robo.router)
app.include_router(robo.user_router)
app.include_router(admin.router)
app.include_router(push.router)
app.include_router(push.public_router)
app.include_router(pages.router)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=False)
