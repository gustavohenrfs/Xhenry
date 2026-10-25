"""Fixtures compartilhadas dos testes."""

from __future__ import annotations

import io
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

# Banco SQLite descartável: precisa existir antes de qualquer import de app.*
os.environ["DATABASE_URL"] = "sqlite:///" + (Path(tempfile.mkdtemp()) / "test.db").as_posix()
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import imageio_ffmpeg  # noqa: E402
import pytest  # noqa: E402
from PIL import Image  # noqa: E402

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()


def probe(path: Path) -> str:
    """Saída de `ffmpeg -i` (stderr), com streams, duração e metadados."""
    result = subprocess.run([FFMPEG, "-hide_banner", "-i", str(path)], capture_output=True, text=True)
    return result.stderr


def duration_of(path: Path) -> float:
    match = re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", probe(path))
    assert match, "duração não encontrada"
    h, m, s = match.groups()
    return int(h) * 3600 + int(m) * 60 + float(s)


@pytest.fixture
def make_video(tmp_path):
    """Gera um mp4 de 1 s. Tamanho ímpar por padrão, para exercitar o libx264."""

    def _make(audio: bool = True, size: str = "65x47") -> bytes:
        out = tmp_path / f"src_{size}_{int(audio)}.mp4"
        cmd = [FFMPEG, "-y", "-loglevel", "error",
               "-f", "lavfi", "-i", f"testsrc=size={size}:rate=10:duration=1"]
        if audio:
            cmd += ["-f", "lavfi", "-i", "sine=frequency=440:duration=1"]
        cmd += ["-c:v", "libx264", "-pix_fmt", "yuv444p",
                "-metadata", "title=ORIGINAL", "-metadata", "comment=SEGREDO"]
        if audio:
            cmd += ["-c:a", "aac", "-shortest"]
        cmd.append(str(out))
        subprocess.run(cmd, check=True)
        return out.read_bytes()

    return _make


def image_bytes(fmt: str = "JPEG", size=(64, 48), mode: str = "RGB", exif=None) -> bytes:
    """Imagem com gradiente (não uniforme, para o ruído e o recorte terem efeito)."""
    img = Image.new(mode, size)
    px = img.load()
    for x in range(size[0]):
        for y in range(size[1]):
            base = ((x * 7) % 256, (y * 11) % 256, ((x + y) * 5) % 256)
            px[x, y] = base + ((x * 3) % 256,) if mode == "RGBA" else base
    buf = io.BytesIO()
    kwargs = {"exif": exif} if exif is not None else {}
    img.save(buf, fmt, **kwargs)
    return buf.getvalue()
