"""Motor de vídeo do Cloaker."""

from __future__ import annotations

import hashlib
import re
import subprocess

import pytest
from conftest import duration_of, probe

from app import cloaker
from app.cloaker import CloakerBusy, CloakerError, build_variants


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


def test_rotated_phone_video_keeps_portrait(make_video, tmp_path):
    variants = build_variants(make_video(size="160x90", rotation=90), "video/mp4", "pe.mp4", copies=1, seed=7)
    info = probe(_write_all(tmp_path, variants)[0])
    assert re.search(r"\b90x160\b", info)
    assert "SAR 1:1" in info or "SAR" not in info


def test_disguised_concat_file_refused(tmp_path):
    with pytest.raises(CloakerError, match="vídeo válido"):
        build_variants(b"ffconcat version 1.0\nfile 'entrada.mp4'\n", "video/mp4", "x.mp4", copies=1)


def test_video_busy_when_slot_taken(make_video):
    raw = make_video()
    assert cloaker._VIDEO_SLOTS.acquire(blocking=False)
    try:
        with pytest.raises(CloakerBusy):
            build_variants(raw, "video/mp4", "fila.mp4", copies=1)
    finally:
        cloaker._VIDEO_SLOTS.release()


def test_video_to_disk_with_workdir(make_video, tmp_path):
    variants = build_variants(make_video(), "video/mp4", "d.mp4", copies=2, seed=8, workdir=tmp_path)
    for v in variants:
        assert v.data == b"" and v.path is not None and v.path.parent == tmp_path
        assert v.sha256 == hashlib.sha256(v.path.read_bytes()).hexdigest()
        assert v.size == v.path.stat().st_size


def test_ffmpeg_killed_reports_server_limit(make_video, monkeypatch):
    # No Render Free o ffmpeg é morto por falta de memória (SIGKILL); não é culpa do arquivo.
    raw = make_video()
    real_run = subprocess.run

    def fake_run(cmd, *args, **kwargs):
        if "libx264" in cmd:
            raise subprocess.CalledProcessError(-9, cmd)
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(cloaker.subprocess, "run", fake_run)
    with pytest.raises(CloakerError) as info:
        build_variants(raw, "video/mp4", "grande.mp4", copies=1)
    assert str(info.value) == SERVER_LIMIT


SERVER_LIMIT = "O vídeo é muito grande para o servidor atual. Tente um vídeo menor ou faça upgrade."


def test_ffmpeg_timeout_reports_server_limit(make_video, monkeypatch):
    raw = make_video()
    real_run = subprocess.run

    def fake_run(cmd, *args, **kwargs):
        if "libx264" in cmd:
            raise subprocess.TimeoutExpired(cmd, 1)
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(cloaker.subprocess, "run", fake_run)
    with pytest.raises(CloakerError) as info:
        build_variants(raw, "video/mp4", "lento.mp4", copies=1)
    assert str(info.value) == SERVER_LIMIT


def test_job_budget_reports_server_limit(make_video, monkeypatch):
    monkeypatch.setattr(cloaker, "VIDEO_JOB_BUDGET", 0)
    with pytest.raises(CloakerError) as info:
        build_variants(make_video(), "video/mp4", "orcamento.mp4", copies=1)
    assert str(info.value) == SERVER_LIMIT


def test_render_free_video_limits():
    with pytest.raises(CloakerError, match="Vídeo acima de 20 MB"):
        build_variants(b"\0" * 20_000_001, "video/mp4", "grande.mp4", copies=1)
    with pytest.raises(CloakerError, match="entre 1 e 3 cópias"):
        build_variants(b"\0" * 10, "video/mp4", "muitas.mp4", copies=4)


def test_mov_comes_out_as_mp4(make_video):
    variants = build_variants(make_video(), "video/quicktime", "Gravação.MOV", copies=1, seed=6)
    assert variants[0].name == "gravacao_01.mp4"
