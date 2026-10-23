from __future__ import annotations
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from app.database import Base, engine, SessionLocal
from app.utils import ensure_owner
from app.routes.auth import router as auth_router
from app.routes.accounts import router as accounts_router
from app.routes.checks import router as checks_router

@asynccontextmanager
async def lifespan(_app: FastAPI):
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        ensure_owner(db)
    yield

app = FastAPI(title="XHenry", version="1.0.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

app.include_router(auth_router)
app.include_router(accounts_router)
app.include_router(checks_router)

@app.get("/")
async def root(request):
    return templates.TemplateResponse(request=request, name="index.html")
