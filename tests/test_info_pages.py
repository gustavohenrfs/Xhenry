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
    end = APP_JS.find("// ---------- Métricas:", start)
    return APP_JS[start:end if end > 0 else None]


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


def test_guide_keeps_everything_from_the_owner_reference(html):
    """Tudo o que estava no ajudaabas.html do dono entrou no guia (reescrito)."""
    for fact in ("Light Ranker", "reply 9", "1 curtida de alguém que não seja você", "pune também quem curtiu",
                 "de 500 para 12 mil", "newsletter", "o próprio post já respondeu", "ACC Program", "3 links comportados",
                 "União Europeia e Reino Unido", "mensagem direta só manual", "corrente de seguir", "Spaces",
                 "Salvar é a nova curtida", "pelo menos 1 por conta", "Reputation.scala:36", "DoNotAmplify",
                 "NotGraduated", "DownrankSpamReply", "40% personalidade"):
        assert fact in html, fact
    week = html[html.index('class="week-table"'):html.index("<p>Mistura")]
    assert week.count('role="row"') == 8 and "Terça" in week


def test_warm_up_follows_the_house_method(html):
    section = html[html.index('id="algo-aquecer"'):html.index("</section>", html.index('id="algo-aquecer"'))]
    assert "de 5 a 7 dias, visando só post em texto" in section
    days = [part.split("</time>")[0] for part in section.split("<time>")[1:]]
    assert days == ["Dia 0", "Dia 1", "Dia 2", "Dia 3", "Dia 4", "Dia 5", "Dia 6", "Dia 7+"]
    for step in ("por 24 h a 48 h", "perfil verificado", "1 página grande", "nome de usuário", "link na bio",
                 "2 ou 3 posts de texto", "3 posts de texto por dia"):
        assert step in section, step
    assert "4 semanas" not in html and 'id="warm-antiga"' not in html
    assert "De 5 a 7 dias, visando só post em texto" in APP_JS and "Como aquecer uma conta antiga" not in APP_JS


def test_rt_page_has_the_real_map_and_a_minimal_example(html):
    assert 'id="rede-map"' in html and 'class="rede-demo"' in html and html.count("<animateMotion") == 3


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
