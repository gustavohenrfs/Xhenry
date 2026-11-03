"""IDs do index.html: um id repetido faz o getElementById pegar o elemento errado.

Foi assim que "Criar pasta" quebrou: o seletor de pasta do cabeçalho ganhou um <span id="folder-name">
antes do campo do formulário, o app.js lia o .value do span (undefined) e o servidor recusava (422).
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HTML = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
APP_JS = (ROOT / "static" / "js" / "app.js").read_text(encoding="utf-8")


def test_index_html_has_no_repeated_id():
    ids = Counter(re.findall(r'\sid="([^"]+)"', HTML))
    repeated = sorted(name for name, count in ids.items() if count > 1)
    assert not repeated, f"ids repetidos: {repeated}"


def test_folder_form_reads_its_own_input():
    form = HTML[HTML.index('<form id="folder-form"'):HTML.index("</form>", HTML.index('<form id="folder-form"'))]
    field = re.search(r'<input id="([^"]+)"', form).group(1)
    submit = APP_JS[APP_JS.index('getElementById("folder-form").addEventListener'):]
    submit = submit[:submit.index("});")]
    assert f'getElementById("{field}").value' in submit


def test_every_id_the_script_reads_exists():
    """Um getElementById("x") sem elemento x no HTML devolve null e quebra na hora do clique."""
    used = set(re.findall(r'getElementById\("([^"]+)"\)', APP_JS))
    present = set(re.findall(r'\sid="([^"]+)"', HTML))
    created = set(re.findall(r'\.id\s*=\s*"([^"]+)"', APP_JS)) | set(re.findall(r'id="([^"]+)"', APP_JS))
    missing = sorted(used - present - created)
    assert not missing, f"ids lidos pelo app.js e ausentes do index.html: {missing}"
