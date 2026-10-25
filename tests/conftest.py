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

    def _make(audio: bool = True, size: str = "65x47", rotation: int = 0) -> bytes:
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
        if rotation:
            # Mesma marcação de rotação (displaymatrix) que celulares gravam em vídeos em pé.
            rotated = tmp_path / f"rot_{rotation}.mp4"
            subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-display_rotation", str(rotation),
                            "-i", str(out), "-c", "copy", str(rotated)], check=True)
            out = rotated
        return out.read_bytes()

    return _make


def p3_icc() -> bytes:
    """Perfil ICC v2 mínimo de Display P3 (matriz + gama 2.2), montado à mão para os testes."""
    import struct

    def s15(v: float) -> bytes:
        return struct.pack(">i", round(v * 65536))

    def xyz(x: float, y: float, z: float) -> bytes:
        return b"XYZ " + b"\0" * 4 + s15(x) + s15(y) + s15(z)

    text = b"Display P3 teste\0"
    desc = b"desc" + b"\0" * 4 + struct.pack(">I", len(text)) + text + b"\0" * 8 + b"\0" * 3 + b"\0" * 67
    curve = b"curv" + b"\0" * 4 + struct.pack(">IH", 1, 0x0233) + b"\0" * 2  # gama 2.2 (u8Fixed8)
    blobs = {
        b"desc": desc,
        b"wtpt": xyz(0.9642, 1.0, 0.8249),
        b"rXYZ": xyz(0.5151, 0.2412, -0.0011),
        b"gXYZ": xyz(0.2920, 0.6922, 0.0419),
        b"bXYZ": xyz(0.1571, 0.0666, 0.7841),
        b"rTRC": curve, b"gTRC": curve, b"bTRC": curve,
    }
    table, data = b"", b""
    offset = 128 + 4 + 12 * len(blobs)
    for sig, blob in blobs.items():
        blob += b"\0" * (-len(blob) % 4)
        table += sig + struct.pack(">II", offset + len(data), len(blob))
        data += blob
    body = struct.pack(">I", len(blobs)) + table + data
    header = (struct.pack(">I", 128 + len(body)) + b"\0" * 4 + bytes([2, 0x10, 0, 0])
              + b"mntrRGB XYZ " + b"\0" * 12 + b"acsp" + b"\0" * 24 + b"\0" * 4
              + s15(0.9642) + s15(1.0) + s15(0.8249) + b"\0" * 48)
    assert len(header) == 128
    return header + body


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
