"""Seção Info: divisor na barra, Algoritmo do X, Rede de RT, Suporte I.A (local, sem API) e Suporte (WhatsApp)."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from main import app

ROOT = Path(__file__).resolve().parent.parent
APP_JS = (ROOT / "static" / "js" / "app.js").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def html():
    with TestClient(app) as client:
        return client.get("/").text


def info_block() -> str:
    start = APP_JS.index("// ---------- Info: Algoritmo do X")
    return APP_JS[start:]


def test_sidebar_has_the_info_group_and_its_pages(html):
    assert html.count('class="nav-divider"><span>Info</span>') == 1 and 'class="more-divider"><span>Info</span>' in html
    for tab in ("algo", "rede", "ia", "suporte"):
        assert html.count(f'data-tab="{tab}"') == 2, tab  # barra lateral e "Mais" do celular
        assert f'id="pane-{tab}"' in html, tab
    nav = html[html.index('<nav id="nav"'):html.index("</nav>", html.index('<nav id="nav"'))]
    assert nav.index("nav-divider") > nav.index('data-tab="tools"') and nav.index("nav-divider") < nav.index('data-tab="users"'), \
        "o divisor separa as abas de cima do grupo Info"
    for name in ("algo", "rede", "ia", "suporte"):
        assert f'{name}: "' in APP_JS.split("const titles")[1].split("};")[0], name
    assert '"algo", "rede", "ia", "suporte"' in APP_JS.split("const MORE_TABS")[1].split(";")[0]


def test_algorithm_guide_is_complete(html):
    for anchor in ("algo-fabrica", "algo-ama", "algo-mata", "algo-camadas", "algo-janelas", "algo-reply", "algo-adulto",
                   "algo-aquecer", "algo-rotina", "algo-playbook", "algo-erros", "algo-kpis", "algo-fontes"):
        assert f'id="{anchor}"' in html, anchor
    toc = html[html.index('class="info-toc"'):html.index("</nav>", html.index('class="info-toc"'))]
    for href in toc.split('href="#')[1:]:
        assert f'id="{href.split(chr(34))[0]}"' in html, "índice aponta para uma parte que existe"
    assert 'id="weights-scale"' in html and "const WEIGHTS = [" in APP_JS


def test_support_goes_to_the_owner_whatsapp(html):
    assert 'href="https://wa.me/5564996013028"' in html and "(64) 99601-3028" in html
    assert 'const SUPPORT_NUMBER = "5564996013028";' in APP_JS


def test_ia_is_local_and_never_calls_an_api():
    block = info_block()
    assert "api(" not in block and "fetch(" not in block and "XMLHttpRequest" not in block, "Suporte I.A não gasta API"
    assert block.count("{ q: ") >= 30, "base de perguntas grande"
    for text in ("link", "grupo", "telegram"):
        assert f"chama no {text}" not in block.lower()


@pytest.mark.skipif(shutil.which("node") is None, reason="sem Node para rodar a lógica do Suporte I.A")
def test_ia_answers_questions_written_many_ways(tmp_path):
    script = tmp_path / "ia.js"
    script.write_text(r'''
const fs = require("fs");
const src = fs.readFileSync(process.argv[2], "utf8");
const start = src.indexOf("const IA_KB = [");
const end = src.indexOf("function iaRelated(");
function plain(text) { return String(text || "").normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase(); }
eval(src.slice(start, end).replace(/^const /gm, "var "));
const out = {};
for (const q of JSON.parse(process.argv[3])) { const r = iaFind(q); out[q] = r ? r.q : null; }
console.log(JSON.stringify(out));
''', encoding="utf-8")
    cases = {
        "como faço pra viralizar?": "Como viralizar no X?",
        "como aquecer conta": "Como aquecer uma conta nova?",
        "minha conta foi suspensa o que faço": "Como evitar que a conta caia?",
        "to com shadowban": "O que é shadowban (ghost ban e search ban)?",
        "que horas é melhor postar": "Qual o melhor horário para postar?",
        "conta +18 como configurar": "Como configurar uma conta +18 do jeito certo?",
        "posso por link do onlyfans no post?": "Link na bio ou no post?",
        "rt ou citação?": "Repost ou citação: qual vale mais?",
        "receita de bolo": None,
        "qual o melhor time do brasil": None,
    }
    result = subprocess.run(["node", str(script), str(ROOT / "static" / "js" / "app.js"), json.dumps(list(cases))],
                            capture_output=True, text=True, timeout=30, check=True)
    assert json.loads(result.stdout) == cases
