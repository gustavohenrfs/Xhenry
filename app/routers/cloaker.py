"""Rota do Cloaker: upload de mídia → zip com N variantes."""

from __future__ import annotations

import base64
import json
import re
import shutil
import tempfile
import threading
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask

from app.cloaker import MAX_VIDEO_BYTES, CloakerBusy, CloakerError, build_variants, manifest, slugify, write_zip
from app.config import ROOT
from app.database import get_db
from app.routers.auth import approved_user

router = APIRouter()

# Progresso dos jobs em andamento, consultado pela aba enquanto o POST não responde.
# Fica em memória: vale enquanto o uvicorn roda com um único worker (como no VPS).
_JOB_ID = re.compile(r"[0-9a-f]{16,64}")
_progress: dict[str, dict[str, Any]] = {}
_progress_lock = threading.Lock()


def _set_progress(job_id: str | None, **state: Any) -> None:
    if job_id:
        with _progress_lock:
            _progress[job_id] = state


def _clear_progress(job_id: str | None) -> None:
    if job_id:
        with _progress_lock:
            _progress.pop(job_id, None)


def _cleanup(work: Path, job_id: str | None) -> None:
    shutil.rmtree(work, ignore_errors=True)
    _clear_progress(job_id)


@router.get("/api/cloaker/progress/{job_id}", dependencies=[Depends(approved_user)])
def cloak_progress(job_id: str) -> dict[str, Any]:
    with _progress_lock:
        state = _progress.get(job_id)
    if state is None:
        raise HTTPException(404, "Job não encontrado.")
    return state


@router.post("/api/cloaker")
def cloak(
    media: UploadFile = File(...),
    copies: int = Form(5),
    intensity: str = Form("leve"),
    fresh_meta: bool = Form(False),
    job_id: str | None = Form(None),
    _user=Depends(approved_user),
    db: Session = Depends(get_db),
) -> FileResponse:
    # Rota síncrona de propósito: Pillow e ffmpeg rodam no threadpool do FastAPI.
    # A sessão (a mesma usada por approved_user) é devolvida ao pool antes do trabalho pesado.
    db.close()
    if job_id is not None and not _JOB_ID.fullmatch(job_id):
        raise HTTPException(400, "Identificador de progresso inválido.")
    raw = media.file.read(MAX_VIDEO_BYTES + 1)  # lê no máximo 1 byte além do limite
    filename = media.filename or "midia"
    tmp_root = ROOT / "data" / "tmp"
    tmp_root.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(dir=tmp_root))
    try:
        variants = build_variants(
            raw, media.content_type or "", filename, copies, intensity, fresh_meta, workdir=work,
            on_progress=lambda done, total: _set_progress(job_id, stage="processing", done=done, total=total),
        )
        del raw
        _set_progress(job_id, stage="zipping", done=len(variants), total=len(variants))
        zip_path = work / "cloaker.zip"
        write_zip(variants, zip_path)
    except CloakerBusy as exc:
        _cleanup(work, job_id)
        raise HTTPException(429, str(exc)) from exc
    except CloakerError as exc:
        _cleanup(work, job_id)
        raise HTTPException(400, str(exc)) from exc
    except BaseException:
        _cleanup(work, job_id)
        raise
    header = base64.b64encode(json.dumps(manifest(variants)).encode()).decode()
    return FileResponse(
        zip_path,
        media_type="application/zip",
        filename=f"cloaker-{slugify(filename)}.zip",
        headers={"X-Cloaker-Manifest": header},
        background=BackgroundTask(_cleanup, work, job_id),  # apaga depois de enviar
    )
