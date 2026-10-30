# Configurações no celular — plano de implementação (Fase 1)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** o cartão Notificações (e os outros de Configurações) cabe na tela do celular e fica curto.

**Architecture:** os tipos de aviso ganham um nome curto na API e viram pílulas; no celular, Marcos de views, Não
perturbe e Aparelhos viram `<details>` recolhíveis; uma regra de CSS impede que filhos de grade alarguem os
cartões.

**Tech Stack:** FastAPI (`app/services.py`), JS puro (`static/js/app.js`), CSS (`static/css/styles.css`), HTML
estático (`templates/index.html`).

**Spec:** `docs/superpowers/specs/2026-10-30-radar-de-trend-design.md` (Parte 1).

## Global Constraints

- Textos da interface e comentários em português; frases com inicial maiúscula só no começo.
- Cores só pelos tokens (`--fg`, `--muted`, `--line`, `--accent`, `--field`…); nada fixo que precise seguir o tema.
- Evitar classes Tailwind novas (exigiriam `sh scripts/build_css.sh`); regras novas vão em `styles.css`.
- Subir `?v=` de `app.js` e `styles.css` em `index.html` (hoje `app.js?v=41`, `styles.css?v=24`).
- Sem prints de tela (pedido do usuário): a largura é conferida por medição do DOM.

## Review Focus

1. Tela de 360 px: nenhum elemento de `#pane-settings` mais largo que a tela (rolagem lateral zero).
2. Um `<select>` com opção longa ("1.000.000 views no dia") não empurra o cartão.
3. Usuário comum não vê a pílula "Acesso" (só admin), como hoje.
4. Recolher/abrir os blocos não perde o valor salvo nem o "salvando" (`busyField`) dos campos.
5. No computador (≥ 768 px) os blocos ficam abertos e com a mesma aparência de hoje.

---

### Task 1: nome curto dos tipos de aviso

**Files:**
- Modify: `app/services.py` (`NOTIFY_TYPES` e o que monta `types` em `GET /api/notifications`)
- Test: `tests/test_notifications.py`

**Interfaces:**
- Produces: cada item de `GET /api/notifications` → `types[]` passa a ter `short: str` além de `key`, `label`, `emoji`.

- [ ] **Step 1: teste que falha**

```python
def test_types_have_short_names(client):
    types = client.get("/api/notifications").json()["types"]
    short = {t["key"]: t["short"] for t in types}
    assert short["viral"] == "Viralizando" and short["ban"] == "Restrição" and short["falha"] == "Falhas"
    assert all(len(t["short"]) <= 14 for t in types)
```

(usar a fixture de cliente que o arquivo já tem).

- [ ] **Step 2:** `python -m pytest tests/test_notifications.py -q` → FAIL (`KeyError: 'short'`).
- [ ] **Step 3:** `NOTIFY_TYPES` vira tuplas `(chave, rótulo, emoji, só_admin, curto)`. Curtos: viral
  "Viralizando", bombando "Bombando", 100k "100K", 1m "1M", marco_post "Marco do post", marco_dia "Marco do dia",
  meta "Meta do dia", resumo "Resumo", queda "Queda", ban "Restrição", falha "Falhas", acesso "Acesso". Ajustar
  todo `for key, *_ in NOTIFY_TYPES` / desempacotamentos e incluir `short` no payload.
- [ ] **Step 4:** `python -m pytest -q` → tudo passa.

### Task 2: pílulas, blocos recolhíveis e largura no celular

**Files:**
- Modify: `templates/index.html` (`#notify-card`), `static/js/app.js` (`paintNotify`, `loadDevices`),
  `static/css/styles.css` (`.notify-types`, `.notify-type`, `#pane-settings`)
- Test: `tests/test_notifications.py` (marcação) + medição em Edge sem interface (sem print)

**Interfaces:**
- Consumes: `types[].short` da Task 1.

- [ ] **Step 1: teste de marcação que falha**

```python
def test_settings_mobile_markup():
    html = (ROOT / "templates/index.html").read_text(encoding="utf-8")
    for block in ("notify-steps-block", "notify-quiet-block", "notify-devices-block"):
        assert f'<details id="{block}"' in html
    css = (ROOT / "static/css/styles.css").read_text(encoding="utf-8")
    assert "#pane-settings .grid > *" in css and "min-width: 0" in css
```

- [ ] **Step 2:** rodar → FAIL.
- [ ] **Step 3: HTML.** As três seções (Marcos de views, Não perturbe, Aparelhos) viram
  `<details id="notify-steps-block|notify-quiet-block|notify-devices-block" class="notify-block" open>` com
  `<summary>` = título + `<span class="notify-sum">` (resumo do valor atual). Os campos de dentro não mudam de id.
- [ ] **Step 4: JS.**
  - `paintNotify`: cada tipo vira `label.notify-pill` (checkbox escondido visualmente mas focável + emoji +
    `type.short`, `title` = `type.label`); classe `on` quando ligado.
  - Resumos: Marcos → "Post: 10.000 · Dia: desligado" (`stepLabel`); Não perturbe → "22:00–07:00" ou
    "Desligado"; Aparelhos → "N aparelho(s)" (em `loadDevices`).
  - No carregamento e em `matchMedia("(max-width: 767px)")` `change`: abaixo de 768 px remove `open` dos três
    `<details>`; acima, põe `open`. O usuário pode abrir/fechar à mão depois.
- [ ] **Step 5: CSS.**
  - `#pane-settings .grid > *, #pane-settings section { min-width: 0; }`;
    `#pane-settings select, #pane-settings input { max-width: 100%; }`;
    `#pane-settings p, #pane-settings span { overflow-wrap: anywhere; }`.
  - `.notify-types` vira `display:flex; flex-wrap:wrap; gap:.4rem`; `.notify-pill` no estilo de `.chip`
    (borda `--line-strong`, raio 999px, `--field` de fundo; `.on` = `--accent` com `--on-accent`), alvo de toque
    ≥ 40 px de altura, anel de foco visível.
  - `details.notify-block > summary`: linha com o título à esquerda, resumo em `--muted` e uma seta que gira ao
    abrir; sem o marcador nativo. No computador (`min-width: 768px`) o `summary` some de clique (aparência de
    título como hoje).
  - Subir `?v=` de `app.js` e `styles.css`.
- [ ] **Step 6:** `python -m pytest -q` e `node --check static/js/app.js` → passam.
- [ ] **Step 7: medição a 360 px (sem print).** Servidor de demonstração (SQLite descartável, admin logado) +
  Edge `--headless=new --window-size=360,800` abrindo Configurações e enviando ao servidor
  `document.documentElement.scrollWidth` e a lista de elementos de `#pane-settings` com
  `getBoundingClientRect().right > innerWidth`. Esperado: `scrollWidth == 360` e lista vazia; repetir a 1280 px
  com os três `<details>` abertos.
- [ ] **Step 8:** mostrar o resumo ao usuário e esperar o OK antes do commit (fluxo por fases).
