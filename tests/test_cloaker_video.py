"""Motor de vídeo do Cloaker."""

from __future__ import annotations

import hashlib
import re

from conftest import duration_of, probe

from app.cloaker import build_variants


def _write_all(tmp_path, variants):
    paths = []
    for v in variants:
        p = tmp_path / v.name
        p.write_bytes(v.data)
        paths.append(p)
    return paths


def test_video_variants_have_distinct_hashes(make_video):
    raw = make_video()
    variants = build_variants(raw, "video/mp4", "clipe.mp4", copies=3, seed=1)
    hashes = {v.sha256 for v in variants}
    assert len(hashes) == 3
    assert hashlib.sha256(raw).hexdigest() not in hashes
    assert [v.name for v in variants] == ["clipe_01.mp4", "clipe_02.mp4", "clipe_03.mp4"]


def test_video_without_audio_ok(make_video, tmp_path):
    variants = build_variants(make_video(audio=False), "video/mp4", "mudo.mp4", copies=1, seed=2)
    info = probe(_write_all(tmp_path, variants)[0])
    assert "Video:" in info
    assert "Audio:" not in info


def test_video_odd_dimensions_ok(make_video, tmp_path):
    variants = build_variants(make_video(size="65x47"), "video/mp4", "impar.mp4", copies=2, seed=3)
    for path in _write_all(tmp_path, variants):
        info = probe(path)
        assert "h264" in info and "64x46" in info
        assert "Audio:" in info
        assert abs(duration_of(path) - 1.0) <= 0.1


def test_video_metadata_stripped(make_video, tmp_path):
    variants = build_variants(make_video(), "video/mp4", "meta.mp4", copies=1, seed=4)
    info = probe(_write_all(tmp_path, variants)[0])
    assert "ORIGINAL" not in info
    assert "SEGREDO" not in info
    # Só sobra o nome genérico do codificador que o muxer de MP4 grava na trilha
    # (igual em todas as variantes); a tag "Lavf" do arquivo não pode aparecer.
    assert set(re.findall(r"encoder\s*:\s*(.+)", info)) <= {"Lavc libx264"}


def test_video_fresh_meta_sets_creation_time(make_video, tmp_path):
    variants = build_variants(make_video(), "video/mp4", "data.mp4", copies=1, fresh_meta=True, seed=5)
    assert "creation_time" in probe(_write_all(tmp_path, variants)[0])


def test_mov_comes_out_as_mp4(make_video):
    variants = build_variants(make_video(), "video/quicktime", "Gravação.MOV", copies=1, seed=6)
    assert variants[0].name == "gravacao_01.mp4"
