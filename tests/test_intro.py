"""Intro de abertura: aparece na primeira pintura, segue o carregamento e nunca prende o painel."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from main import app

ROOT = Path(__file__).resolve().parent.parent
APP_JS = (ROOT / "static" / "js" / "app.js").read_text(encoding="utf-8")
CSS = (ROOT / "static" / "css" / "styles.css").read_text(encoding="utf-8")


def test_intro_is_in_the_first_paint():
    with TestClient(app) as client:
        html = client.get("/").text
    body = html[html.index("<body"):]
    assert body.index('id="intro"') < body.index('id="bg-orbs"'), "a intro é a primeira coisa do <body>"
    assert html.count("<script>") == 1, "continua um só script inline (tema + intro curta)"
    assert 'sessionStorage.getItem("intro_seen")' in html and 'dataset.intro="short"' in html
    assert 'rel="preload" href="/static/img/logo-256.webp"' in html and 'src="/static/img/logo-256.webp"' in html
    intro = body[body.index('id="intro"'):body.index('id="bg-orbs"')]
    assert "Monitoramento Real" in intro and 'id="intro-fill"' in intro and 'id="intro-step"' in intro


def test_intro_never_traps_the_panel():
    assert "animation: intro-failsafe .6s ease 12s forwards;" in CSS, "sem JS, some sozinha"
    assert "@media (prefers-reduced-motion: reduce)" in CSS[CSS.index("Intro \"ignição\""):]
    boot = APP_JS[APP_JS.index("async function boot() {"):APP_JS.index("async function bootPanel() {")]
    assert "try { await bootPanel(); } finally { endIntro(); }" in boot, "sai em qualquer caminho do boot"
    assert "setTimeout(remove, 1200)" in APP_JS, "sai mesmo sem o evento de fim da animação"
