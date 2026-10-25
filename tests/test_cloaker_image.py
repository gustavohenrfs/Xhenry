"""Motor de imagem do Cloaker."""

from __future__ import annotations

import hashlib
import io
import json
import zipfile

import pytest
from conftest import image_bytes, p3_icc
from PIL import Image

from app.cloaker import CloakerError, build_variants, manifest, zip_variants

EXIF_IFD = 0x8769
IMAGE_UNIQUE_ID = 0xA420
MAKE = 0x010F
ORIENTATION = 0x0112


def _open(variant) -> Image.Image:
    return Image.open(io.BytesIO(variant.data))


def test_jpeg_variants_distinct_and_same_size():
    raw = image_bytes("JPEG", (64, 48))
    variants = build_variants(raw, "image/jpeg", "foto.jpg", copies=5, seed=1)
    hashes = [v.sha256 for v in variants]
    assert len(set(hashes)) == 5
    assert hashlib.sha256(raw).hexdigest() not in hashes
    for v in variants:
        img = _open(v)
        assert img.format == "JPEG"
        assert img.size == (64, 48)
        assert v.size == len(v.data)
        assert v.sha256 == hashlib.sha256(v.data).hexdigest()


def test_png_alpha_preserved():
    variants = build_variants(image_bytes("PNG", mode="RGBA"), "image/png", "logo.png", copies=2, seed=2)
    for v in variants:
        img = _open(v)
        assert img.format == "PNG"
        assert img.mode == "RGBA"
        assert v.name.endswith(".png")
        # o alfa do gradiente não pode ter virado opaco nem transparente total
        lo, hi = img.getchannel("A").getextrema()
        assert lo < 50 and hi > 150  # gradiente de entrada vai de 0 a 189


def test_webp_supported():
    variants = build_variants(image_bytes("WEBP"), "image/webp", "arte.webp", copies=2, seed=3)
    assert all(_open(v).format == "WEBP" for v in variants)


def test_exif_removed():
    exif = Image.Exif()
    exif[MAKE] = "Canon"
    raw = image_bytes("JPEG", exif=exif)
    assert Image.open(io.BytesIO(raw)).getexif().get(MAKE) == "Canon"
    variant = build_variants(raw, "image/jpeg", "c.jpg", copies=1, seed=4)[0]
    assert MAKE not in _open(variant).getexif()


def test_exif_orientation_applied():
    exif = Image.Exif()
    exif[ORIENTATION] = 6
    raw = image_bytes("JPEG", size=(40, 20), exif=exif)
    img = _open(build_variants(raw, "image/jpeg", "deitada.jpg", copies=1, seed=5)[0])
    assert img.size == (20, 40)
    assert ORIENTATION not in img.getexif()


def test_fresh_meta_sets_unique_id():
    variants = build_variants(image_bytes(), "image/jpeg", "f.jpg", copies=2, fresh_meta=True, seed=6)
    ids = [_open(v).getexif().get_ifd(EXIF_IFD).get(IMAGE_UNIQUE_ID) for v in variants]
    assert all(ids) and ids[0] != ids[1]


def test_seed_is_deterministic():
    raw = image_bytes()
    a = build_variants(raw, "image/jpeg", "s.jpg", copies=3, intensity="media", seed=42)
    b = build_variants(raw, "image/jpeg", "s.jpg", copies=3, intensity="media", seed=42)
    assert [v.sha256 for v in a] == [v.sha256 for v in b]


def test_rejects_animated_gif_and_fake_extension():
    gif = io.BytesIO()
    frames = [Image.new("RGB", (32, 32), c) for c in ("red", "blue")]
    frames[0].save(gif, "GIF", save_all=True, append_images=frames[1:])
    with pytest.raises(CloakerError):
        build_variants(gif.getvalue(), "image/gif", "a.gif", copies=1)

    webp = io.BytesIO()
    frames[0].save(webp, "WEBP", save_all=True, append_images=frames[1:])
    with pytest.raises(CloakerError, match="animad"):
        build_variants(webp.getvalue(), "image/webp", "a.webp", copies=1)

    with pytest.raises(CloakerError, match="Formato não suportado"):
        build_variants(gif.getvalue(), "image/png", "gif-disfarcado.png", copies=1)
    with pytest.raises(CloakerError):
        build_variants(b"isto nao e imagem", "image/png", "texto.png", copies=1)


def test_mismatched_extension_uses_real_format():
    # WebP salvo como .jpg é comum em imagens baixadas da web.
    variant = build_variants(image_bytes("WEBP"), "image/jpeg", "baixada.jpg", copies=1, seed=9)[0]
    assert variant.name == "baixada_01.webp"
    assert _open(variant).format == "WEBP"
    png = build_variants(image_bytes("PNG"), "image/jpeg", "falsa.jpg", copies=1, seed=9)[0]
    assert png.name == "falsa_01.png" and _open(png).format == "PNG"


def _flat_jpeg(color, icc=None) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (48, 48), color).save(buf, "JPEG", quality=100, **({"icc_profile": icc} if icc else {}))
    return buf.getvalue()


def test_wide_gamut_converted_to_srgb_before_stripping():
    color = (60, 170, 90)
    plain = _open(build_variants(_flat_jpeg(color), "image/jpeg", "a.jpg", copies=1, seed=10)[0])
    p3 = _open(build_variants(_flat_jpeg(color, p3_icc()), "image/jpeg", "b.jpg", copies=1, seed=10)[0])
    assert "icc_profile" not in p3.info
    # Verde P3 em sRGB fica mais saturado: o canal verde sobe e o vermelho desce em relação ao sem perfil.
    r_plain, g_plain, _ = plain.getpixel((24, 24))
    r_p3, g_p3, _ = p3.getpixel((24, 24))
    assert r_p3 < r_plain - 8
    assert g_p3 >= g_plain


def test_broken_icc_profile_does_not_fail():
    variant = build_variants(_flat_jpeg((60, 170, 90), b"lixo" * 40), "image/jpeg", "c.jpg", copies=1, seed=11)[0]
    assert _open(variant).format == "JPEG"


def test_rejects_too_many_pixels():
    big = io.BytesIO()
    Image.new("L", (7000, 7000)).save(big, "PNG")  # 49 MP, arquivo pequeno
    with pytest.raises(CloakerError, match="megapixels"):
        build_variants(big.getvalue(), "image/png", "grande.png", copies=1)


def test_decompression_bomb_is_400_not_500(monkeypatch):
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 1000)  # 64x48 passa de 2x o limite
    with pytest.raises(CloakerError):
        build_variants(image_bytes("PNG"), "image/png", "bomba.png", copies=1)


def test_grayscale_png_transparency_kept():
    buf = io.BytesIO()
    gray = Image.new("L", (32, 32), 200)
    gray.paste(0, (8, 8, 24, 24))  # quadrado central com a cor-chave transparente
    gray.save(buf, "PNG", transparency=0)
    img = _open(build_variants(buf.getvalue(), "image/png", "cinza.png", copies=1, seed=8)[0])
    assert img.mode in {"LA", "RGBA"}
    assert img.getchannel("A").getextrema()[0] == 0


def test_image_progress_callback():
    calls = []
    build_variants(image_bytes(), "image/jpeg", "p.jpg", copies=3, seed=12, on_progress=lambda d, t: calls.append((d, t)))
    assert calls == [(0, 3), (1, 3), (2, 3), (3, 3)]


def test_copies_bounds():
    raw = image_bytes()
    for copies in (0, 21):
        with pytest.raises(CloakerError, match="cópias"):
            build_variants(raw, "image/jpeg", "x.jpg", copies=copies)
    with pytest.raises(CloakerError):
        build_variants(raw, "image/jpeg", "x.jpg", copies=1, intensity="forte")


def test_zip_contains_variants_and_manifest():
    variants = build_variants(image_bytes(), "image/jpeg", "z.jpg", copies=2, seed=7)
    with zipfile.ZipFile(io.BytesIO(zip_variants(variants))) as zf:
        assert sorted(zf.namelist()) == ["manifest.json", "z_01.jpg", "z_02.jpg"]
        assert json.loads(zf.read("manifest.json")) == manifest(variants)
        assert zf.read("z_01.jpg") == variants[0].data
    assert manifest(variants)[0] == {"name": "z_01.jpg", "sha256": variants[0].sha256, "size": variants[0].size}
