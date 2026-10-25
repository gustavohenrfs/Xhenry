"""Rota do Cloaker: upload de mídia → zip com N variantes."""

from __future__ import annotations

import base64
import json
import shutil
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask

from app.cloaker import MAX_VIDEO_BYTES, CloakerBusy, CloakerError, build_variants, manifest, slugify, write_zip
from app.config import ROOT
from app.database import get_db
from app.routers.auth import current_user

router = APIRouter()


@router.post("/api/cloaker")
def cloak(
    media: UploadFile = File(...),
    copies: int = Form(5),
    intensity: str = Form("leve"),
    fresh_meta: bool = Form(False),
    _user=Depends(current_user),
    db: Session = Depends(get_db),
) -> FileResponse:
    # Rota síncrona de propósito: Pillow e ffmpeg rodam no threadpool do FastAPI.
    # A sessão (a mesma usada por current_user) é devolvida ao pool antes do trabalho pesado.
    db.close()
    raw = media.file.read(MAX_VIDEO_BYTES + 1)  # lê no máximo 1 byte além do limite
    filename = media.filename or "midia"
    tmp_root = ROOT / "data" / "tmp"
    tmp_root.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(dir=tmp_root))
    try:
        variants = build_variants(raw, media.content_type or "", filename, copies, intensity, fresh_meta, workdir=work)
        del raw
        zip_path = work / "cloaker.zip"
        write_zip(variants, zip_path)
    except CloakerBusy as exc:
        shutil.rmtree(work, ignore_errors=True)
        raise HTTPException(429, str(exc)) from exc
    except CloakerError as exc:
        shutil.rmtree(work, ignore_errors=True)
        raise HTTPException(400, str(exc)) from exc
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise
    header = base64.b64encode(json.dumps(manifest(variants)).encode()).decode()
    return FileResponse(
        zip_path,
        media_type="application/zip",
        filename=f"cloaker-{slugify(filename)}.zip",
        headers={"X-Cloaker-Manifest": header},
        background=BackgroundTask(shutil.rmtree, work, ignore_errors=True),  # apaga depois de enviar
    )
