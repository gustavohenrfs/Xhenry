"""Rota do Cloaker: upload de mídia → zip com N variantes."""

from __future__ import annotations

import base64
import json

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import Response

from app.cloaker import MAX_VIDEO_BYTES, CloakerError, build_variants, manifest, slugify, zip_variants
from app.routers.auth import current_user

router = APIRouter()


@router.post("/api/cloaker")
def cloak(
    media: UploadFile = File(...),
    copies: int = Form(5),
    intensity: str = Form("leve"),
    fresh_meta: bool = Form(False),
    _user=Depends(current_user),
) -> Response:
    # Rota síncrona de propósito: Pillow e ffmpeg rodam no threadpool do FastAPI.
    raw = media.file.read(MAX_VIDEO_BYTES + 1)  # lê no máximo 1 byte além do limite
    filename = media.filename or "midia"
    try:
        variants = build_variants(raw, media.content_type or "", filename, copies, intensity, fresh_meta)
    except CloakerError as exc:
        raise HTTPException(400, str(exc)) from exc
    header = base64.b64encode(json.dumps(manifest(variants)).encode()).decode()
    return Response(
        content=zip_variants(variants),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="cloaker-{slugify(filename)}.zip"',
            "X-Cloaker-Manifest": header,
        },
    )
