import re
from pathlib import Path

ROOT = Path(__file__).parent
MAIN_PY = ROOT / "main.py"
ACCOUNTS_PY = ROOT / "app" / "routes" / "accounts.py"
CHECKS_PY = ROOT / "app" / "routes" / "checks.py"
SCHEMAS_PY = ROOT / "app" / "schemas.py"
NEW_MAIN = ROOT / "main_clean.py"

def extract_routes(content):
    # Simple extraction based on @app. decorators
    routes = re.findall(r'(@app\.(get|post|patch|delete|put)\([^\)]+\)\s*\n(?:async )?def .+?(?=\n@|\Z))', content, re.DOTALL)
    return [r[0] for r in routes]

def main():
    content = MAIN_PY.read_text(encoding="utf-8")

    # Extract schemas (Pydantic models)
    schemas = re.findall(r'class \w+In\(BaseModel\):.+?(?=\nclass|\n\ndef|\Z)', content, re.DOTALL)
    schemas_code = "\n\n".join(schemas)

    # Extract routes
    all_routes = extract_routes(content)

    accounts_routes = [r for r in all_routes if any(x in r for x in ["folder", "account", "dashboard", "refresh"])]
    checks_routes = [r for r in all_routes if any(x in r for x in ["telegram", "posts/review"])]

    # Write schemas
    SCHEMAS_PY.write_text(f"""from __future__ import annotations
from pydantic import BaseModel, Field
from app.models import FolderKind

LOCAL_EMAIL = "local@suzana"

{schemas_code}
""", encoding="utf-8")

    # Write accounts.py
    ACCOUNTS_PY.write_text(f"""from __future__ import annotations
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy.orm import Session
from app.database import get_db
from app.utils import ensure_owner, normalize_username
import app.models as models

router = APIRouter()

{chr(10).join(accounts_routes)}
""", encoding="utf-8")

    # Write checks.py
    CHECKS_PY.write_text(f"""from __future__ import annotations
from fastapi import APIRouter, Depends, Form, File, UploadFile, HTTPException
from sqlalchemy.orm import Session
from app.database import get_db
import app.models as models

router = APIRouter()

{chr(10).join(checks_routes)}
""", encoding="utf-8")

    # Create clean main.py
    clean_main = '''from __future__ import annotations
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
'''
    NEW_MAIN.write_text(clean_main, encoding="utf-8")

    print("Refatoração concluída com sucesso!")
    print(f"- Schemas salvos em: {SCHEMAS_PY}")
    print(f"- Rotas de accounts em: {ACCOUNTS_PY}")
    print(f"- Rotas de checks em: {CHECKS_PY}")
    print(f"- Novo main.py limpo em: {NEW_MAIN}")

if __name__ == "__main__":
    main()
