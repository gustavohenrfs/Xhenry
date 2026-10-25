"""Cloaker: gera N variantes de uma imagem ou vídeo com micro-alterações.

Cada variante sai com hash (sha256) próprio e metadados limpos. As alterações
são pequenas demais para serem notadas: recorte de poucos pixels seguido de
redimensionamento ao tamanho original, leve ajuste de brilho/contraste, ruído
de ±1 em poucos pixels e recodificação com qualidade sorteada.

Módulo puro (sem FastAPI) para ser testável. Erros de entrada levantam
`CloakerError`, cuja mensagem já é o texto em português mostrado ao usuário.
"""

from __future__ import annotations

import hashlib
import io
import json
import random
import re
import secrets
import subprocess
import tempfile
import unicodedata
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import imageio_ffmpeg
import numpy as np
from PIL import Image, ImageEnhance, ImageOps, UnidentifiedImageError

from app.config import ROOT

IMAGE_TYPES = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}
VIDEO_TYPES = {"video/mp4": "mp4", "video/quicktime": "mov"}
MAX_IMAGE_BYTES = 15_000_000
MAX_VIDEO_BYTES = 100_000_000
MAX_COPIES_IMAGE = 20
MAX_COPIES_VIDEO = 10
INTENSITIES = {"leve", "media"}
MIN_SIDE = 16
FFMPEG_TIMEOUT = 120

_PIL_FORMATS = {"jpg": "JPEG", "png": "PNG", "webp": "WEBP"}
_TYPE_ALIASES = {"image/jpg": "image/jpeg", "image/pjpeg": "image/jpeg"}
_EXIF_IFD = 0x8769
_DATETIME_ORIGINAL = 0x9003
_IMAGE_UNIQUE_ID = 0xA420

# Faixas das micro-alterações por intensidade.
_PARAMS = {
    "leve": {"crop": (1, 2), "tone": 0.02, "noise": 0.0005, "quality": (90, 95), "crf": (20, 23)},
    "media": {"crop": (2, 4), "tone": 0.04, "noise": 0.002, "quality": (86, 93), "crf": (22, 26)},
}


class CloakerError(ValueError):
    """Entrada inválida; a mensagem vai direto para o usuário."""


@dataclass
class Variant:
    name: str
    data: bytes
    sha256: str
    size: int


def slugify(filename: str) -> str:
    """Nome seguro para o zip e as entradas: sem pasta, acento ou espaço."""
    stem = Path(filename.replace("\\", "/")).name.rsplit(".", 1)[0]
    ascii_stem = unicodedata.normalize("NFKD", stem).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_stem.lower()).strip("-")[:40].strip("-")
    return slug or "midia"


def _random_moment(rng: random.Random) -> datetime:
    return datetime.now(timezone.utc) - timedelta(seconds=rng.randint(0, 30 * 24 * 3600))


# ---------------------------------------------------------------- imagem


def _open_image(raw: bytes, ext: str) -> Image.Image:
    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
    except (UnidentifiedImageError, OSError) as exc:
        raise CloakerError("Não foi possível abrir a imagem. O arquivo pode estar corrompido.") from exc
    if img.format != _PIL_FORMATS[ext]:
        raise CloakerError("O conteúdo do arquivo não corresponde ao formato informado.")
    if getattr(img, "is_animated", False):
        raise CloakerError("Imagens animadas não são suportadas.")
    if min(img.size) < MIN_SIDE:
        raise CloakerError(f"Imagem pequena demais (mínimo {MIN_SIDE} px por lado).")
    return img


def _fresh_exif(rng: random.Random) -> bytes:
    exif = Image.Exif()
    ifd = exif.get_ifd(_EXIF_IFD)
    ifd[_DATETIME_ORIGINAL] = _random_moment(rng).strftime("%Y:%m:%d %H:%M:%S")
    ifd[_IMAGE_UNIQUE_ID] = "%032x" % rng.getrandbits(128)
    return exif.tobytes()


def cloak_image(raw: bytes, ext: str, rng: random.Random, intensity: str, fresh_meta: bool) -> bytes:
    """Uma variante da imagem, no mesmo formato e tamanho do original."""
    params = _PARAMS[intensity]
    img = ImageOps.exif_transpose(_open_image(raw, ext))
    if img.mode == "P":
        img = img.convert("RGBA" if "transparency" in img.info else "RGB")
    elif img.mode not in {"RGB", "RGBA", "L"}:
        img = img.convert("RGBA" if "A" in img.getbands() else "RGB")
    if ext == "jpg" and img.mode == "RGBA":
        img = img.convert("RGB")
    width, height = img.size

    lo, hi = params["crop"]
    box = (rng.randint(lo, hi), rng.randint(lo, hi), width - rng.randint(lo, hi), height - rng.randint(lo, hi))
    img = img.crop(box).resize((width, height), Image.Resampling.LANCZOS)

    # Brilho e contraste mexem só nas cores; o alfa fica intacto.
    alpha = img.getchannel("A") if img.mode == "RGBA" else None
    color = img.convert("RGB") if alpha is not None else img
    tone = params["tone"]
    color = ImageEnhance.Brightness(color).enhance(rng.uniform(1 - tone, 1 + tone))
    color = ImageEnhance.Contrast(color).enhance(rng.uniform(1 - tone, 1 + tone))

    pixels = np.array(color, dtype=np.int16)
    count = max(1, int(width * height * params["noise"]))
    noise_rng = np.random.default_rng(rng.getrandbits(64))
    ys = noise_rng.integers(0, height, count)
    xs = noise_rng.integers(0, width, count)
    delta = noise_rng.choice(np.array([-1, 1], dtype=np.int16), size=pixels[ys, xs].shape)
    pixels[ys, xs] = pixels[ys, xs] + delta
    img = Image.fromarray(np.clip(pixels, 0, 255).astype(np.uint8), color.mode)
    if alpha is not None:
        img.putalpha(alpha)

    img.info = {}  # sem ICC, EXIF ou chunks de texto herdados do original
    options: dict = {}
    if fresh_meta:
        options["exif"] = _fresh_exif(rng)
    if ext in {"jpg", "webp"}:
        options["quality"] = rng.randint(*params["quality"])
    if ext == "png":
        options["compress_level"] = rng.randint(6, 9)
    buf = io.BytesIO()
    img.save(buf, _PIL_FORMATS[ext], **options)
    return buf.getvalue()


# ----------------------------------------------------------------- vídeo


def _ffmpeg() -> str:
    return imageio_ffmpeg.get_ffmpeg_exe()


def _video_size(src: Path) -> tuple[int, int]:
    info = subprocess.run(
        [_ffmpeg(), "-hide_banner", "-i", str(src)], capture_output=True, text=True, timeout=FFMPEG_TIMEOUT
    ).stderr
    match = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", info)
    if not match:
        raise CloakerError("O arquivo não é um vídeo válido.")
    width, height = int(match.group(1)), int(match.group(2))
    if min(width, height) < MIN_SIDE:
        raise CloakerError(f"Vídeo pequeno demais (mínimo {MIN_SIDE} px por lado).")
    return width, height


def cloak_video(src: Path, dst: Path, rng: random.Random, intensity: str, fresh_meta: bool = False) -> None:
    """Uma variante do vídeo em mp4 (H.264 + AAC), com dimensões pares."""
    params = _PARAMS[intensity]
    width, height = _video_size(src)
    crop = rng.randint(*params["crop"])
    tone = params["tone"]
    filters = ",".join([
        f"crop=iw-{2 * crop}:ih-{2 * crop}",
        f"scale={width - width % 2}:{height - height % 2}",  # libx264 exige lados pares
        f"eq=brightness={rng.uniform(-tone, tone):.4f}:contrast={rng.uniform(1 - tone, 1 + tone):.4f}",
        "format=yuv420p",
    ])
    cmd = [
        _ffmpeg(), "-y", "-loglevel", "error", "-i", str(src),
        "-map", "0:v:0", "-map", "0:a?", "-map_metadata", "-1", "-map_chapters", "-1",
        "-vf", filters,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", str(rng.randint(*params["crf"])),
        "-c:a", "aac", "-b:a", f"{rng.choice([128, 160, 192])}k",
        # bitexact tira a tag "Lavf" do arquivo; o "Lavc libx264" da trilha é gravado pelo muxer e fica
        "-fflags", "+bitexact", "-flags:v", "+bitexact", "-flags:a", "+bitexact",
        "-movflags", "+faststart",
    ]
    if fresh_meta:
        cmd += ["-metadata", "creation_time=" + _random_moment(rng).strftime("%Y-%m-%dT%H:%M:%S.000000Z")]
    cmd.append(str(dst))
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=FFMPEG_TIMEOUT)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise CloakerError("Não foi possível processar o vídeo.") from exc


# --------------------------------------------------------------- geração


def _check_request(raw: bytes, content_type: str, copies: int, intensity: str) -> tuple[str, bool]:
    kind = _TYPE_ALIASES.get(content_type.lower(), content_type.lower())
    if kind not in IMAGE_TYPES and kind not in VIDEO_TYPES:
        raise CloakerError("Formato não suportado. Envie JPG, PNG, WebP, MP4 ou MOV.")
    is_video = kind in VIDEO_TYPES
    if not raw:
        raise CloakerError("O arquivo está vazio.")
    if is_video and len(raw) > MAX_VIDEO_BYTES:
        raise CloakerError("Vídeo acima de 100 MB.")
    if not is_video and len(raw) > MAX_IMAGE_BYTES:
        raise CloakerError("Imagem acima de 15 MB.")
    limit = MAX_COPIES_VIDEO if is_video else MAX_COPIES_IMAGE
    if not 1 <= copies <= limit:
        raise CloakerError(f"Escolha entre 1 e {limit} cópias.")
    if intensity not in INTENSITIES:
        raise CloakerError("Intensidade inválida. Use leve ou media.")
    return kind, is_video


def _variant(name: str, data: bytes) -> Variant:
    return Variant(name=name, data=data, sha256=hashlib.sha256(data).hexdigest(), size=len(data))


def build_variants(
    raw: bytes,
    content_type: str,
    filename: str,
    copies: int,
    intensity: str = "leve",
    fresh_meta: bool = False,
    seed: int | None = None,
) -> list[Variant]:
    """Valida a entrada e gera `copies` variantes com hashes distintos entre si e do original."""
    kind, is_video = _check_request(raw, content_type, copies, intensity)
    rng = random.Random(seed if seed is not None else secrets.randbits(64))
    slug = slugify(filename)
    seen = {hashlib.sha256(raw).hexdigest()}
    variants: list[Variant] = []

    if is_video:
        tmp_root = ROOT / "data" / "tmp"
        tmp_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=tmp_root) as work:
            src = Path(work) / ("entrada." + VIDEO_TYPES[kind])
            src.write_bytes(raw)
            for index in range(1, copies + 1):
                dst = Path(work) / f"saida_{index}.mp4"
                cloak_video(src, dst, rng, intensity, fresh_meta)
                variants.append(_variant(f"{slug}_{index:02d}.mp4", dst.read_bytes()))
        return variants

    ext = IMAGE_TYPES[kind]
    for index in range(1, copies + 1):
        # Colisão é praticamente impossível; mesmo assim, nunca devolve hash repetido.
        for _attempt in range(5):
            data = cloak_image(raw, ext, rng, intensity, fresh_meta)
            digest = hashlib.sha256(data).hexdigest()
            if digest not in seen:
                break
        seen.add(digest)
        variants.append(_variant(f"{slug}_{index:02d}.{ext}", data))
    return variants


def manifest(variants: list[Variant]) -> list[dict]:
    return [{"name": v.name, "sha256": v.sha256, "size": v.size} for v in variants]


def zip_variants(variants: list[Variant]) -> bytes:
    """Zip sem compressão (mídia já é comprimida) com as variantes e o manifest.json."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        for v in variants:
            zf.writestr(v.name, v.data)
        zf.writestr("manifest.json", json.dumps(manifest(variants), indent=2))
    return buf.getvalue()
