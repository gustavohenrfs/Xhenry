# Radar de trend — plano de implementação (Fase 2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** aba "Radar" com o que está em alta no Brasil agora (X, Google Trends, Google Notícias, Wikipédia), datas
sazonais e previsões dos próximos dias, tudo grátis e lido em segundo plano.

**Architecture:** `app/radar_sources.py` (parsers puros + normalização), `app/seasonal.py` (calendário),
`app/radar.py` (leitura das fontes, gravação, montagem dos assuntos e da previsão), `app/routers/radar.py`
(rotas). O vigia chama `radar.refresh_due()` a cada minuto; a aba só lê o banco.

**Tech Stack:** FastAPI, SQLAlchemy, httpx (já no projeto), `xml.etree`, `html.parser`; JS puro.

**Spec:** `docs/superpowers/specs/2026-10-30-radar-de-trend-design.md` (Parte 2, sem "Meus termos" e sem posts
engajando, que são a Fase 3).

## Global Constraints

- Textos e comentários em português; cores pelos tokens; nada de serviço de terceiros carregado pela página.
- Rotas novas com `approved_user`; `tests/test_auth_routes.py` atualizado.
- Testes nunca vão à rede: as fontes recebem um `fetch(url) -> str` falso.
- Fontes: X a cada 15 min, Google Trends 15 min, Notícias 30 min, Wikipédia 6 h; timeout 15 s; `RADAR=off` desliga.
- Itens guardados 7 dias. Botão Atualizar: no máximo 1 leitura a cada 2 min.
- Pesos do calor: X 40, Google 30, Notícias 20, Wikipédia 10 (+ bônus de volume do Google, até 10).
- Previsão por regras (sem IA): sazonal em 14 dias; "pode virar trend" = novo/subindo em 2+ fontes ou que subiu
  10+ posições no X em 3 h.
- Subir `?v=` de `app.js`/`styles.css`; evitar classes Tailwind novas.

## Review Focus

1. Uma fonte fora do ar (timeout, HTML mudado) não derruba as outras nem a aba; o cartão mostra a fonte em vermelho.
2. Banco recém-criado sem nenhuma leitura: a aba mostra estado vazio com "Atualizar", não erro.
3. Mesmo assunto com grafias diferentes ("#Flamengo", "flamengo", "Flamengo x Palmeiras") junta num só.
4. Títulos com HTML/emoji vindos das fontes nunca viram HTML na página (só `textContent`).
5. Usuário não aprovado recebe 403 em todas as rotas do radar.

---

### Task 1: parsers das fontes e normalização (`app/radar_sources.py`)

**Interfaces — Produces:**
- `norm(text: str) -> str` — minúsculas, sem acento, sem `#`/`@`, só letras/números/espaço, espaços únicos.
- `parse_x(html: str) -> list[dict]` — `[{"at": datetime_utc, "items": [str, ...]}]`, mais nova primeiro (lê `h3[data-timestamp]` + `a.trend-link` de cada `.list-container`).
- `parse_google(xml: str) -> list[dict]` — `{"title", "volume", "news": [{"title","url","source"}]}` em ordem.
- `parse_news(xml: str) -> list[dict]` — `{"title", "url", "source"}` (título sem o " - Fonte" do fim).
- `parse_wiki(data: dict) -> list[dict]` — `{"title", "url", "views"}` sem páginas de namespace (`:`) nem `Página_principal`.

- [ ] Testes em `tests/test_radar.py` com amostras pequenas no próprio arquivo: `norm("#Flamengo ")=="flamengo"`, `norm("São Paulo")=="sao paulo"`; `parse_x` devolve 2 horas com a ordem certa e datas UTC; `parse_google` lê `200+` e as notícias; `parse_news` corta " - G1"; `parse_wiki` tira `Especial:Pesquisar`; HTML vazio/quebrado → `[]` (nunca exceção).
- [ ] Rodar → FAIL; implementar; rodar → PASS.

### Task 2: calendário sazonal (`app/seasonal.py`)

**Produces:** `upcoming(today: date, days: int = 14) -> list[dict]` — `{"date", "name", "in_days", "tip", "kind": "sazonal"}` em ordem de data.

- [ ] Testes: Carnaval 2027 = 09/02/2027 (Páscoa 28/03/2027); Dia das Mães 2027 = 09/05; Black Friday 2026 = 27/11; eleição em ano par (1º domingo de outubro e último domingo de outubro); `upcoming(date(2026,10,30))` contém Finados (in_days 3) e não contém nada além de 14 dias.
- [ ] FAIL → implementar (datas da spec; Páscoa pelo algoritmo de Meeus) → PASS.

### Task 3: leitura, gravação e montagem (`app/models.py`, `app/radar.py`, vigia)

**Consumes:** Task 1 e 2. **Produces:**
- Modelos `RadarSnapshot(id, source, fetched_at, ok, error)` e `RadarItem(id, snapshot_id FK cascade, source, rank, title, norm, volume, url, extra JSON)`, índice `(source, snapshot_id)`.
- `refresh(db, sources: list[str] | None = None, fetch=None) -> dict[str, bool]` — lê as fontes pedidas (todas por padrão), grava um snapshot por fonte (ok ou com erro), apaga itens com mais de 7 dias. O X grava um snapshot com as 24 horas (`extra.hour` = índice da hora, 0 = mais nova).
- `refresh_due(db, now=None) -> list[str]` — as fontes vencidas pelo ritmo das Global Constraints; o vigia chama `refresh(db, refresh_due(db))` quando não vazio e `RADAR` não é off.
- `board(db, now=None) -> dict` — `{"updated_at", "sources": {src: {"ok", "at", "error"}}, "topics": [...], "upcoming": [...]}`. Tópico: `{"norm", "title", "sources", "heat" 0–100, "rank_x", "hours_x", "direction": "novo|subindo|estavel|caindo", "volume", "category", "news": [...], "url"}`, ordenado por calor, no máximo 60. `upcoming` = sazonal + previsões (`kind: "previsao"`, com o motivo em `tip`).
- Junta por `norm` igual, ou um contido no outro com 4+ letras.

- [ ] Testes: `refresh` com `fetch` falso grava 4 snapshots; fonte que levanta exceção grava `ok=False` e as outras seguem; `board` junta "#Flamengo" (X) e "flamengo" (Google) num tópico com 2 fontes e calor maior que um só do X; `hours_x` conta as horas; `direction` "novo" quando só está na hora mais nova; `refresh_due` respeita os intervalos; itens velhos somem.
- [ ] FAIL → implementar → PASS. Vigia: `run_once` chama o radar dentro de `try` (falha só registra no log).

### Task 4: rotas (`app/routers/radar.py`, `main.py`)

- `GET /api/radar` → `board(db)`; se nunca leu ou a última leitura tem mais de 15 min e o Postgres/SQLite estiver acessível, dispara `refresh` numa thread (não espera) e devolve o que tem com `"refreshing": true`.
- `POST /api/radar/refresh` → lê agora (todas as fontes) e devolve o board; 429 se a última leitura manual foi há menos de 2 min.
- `GET /api/radar/topic?norm=` → o tópico + `history_x` (posição por hora nas 24 h, `null` onde não apareceu); 404 se não existe.
- [ ] Testes de rota (dados semeados com `refresh` falso), 403 para pendente, entrada em `tests/test_auth_routes.py`.
- [ ] Carimbo ao vivo: `app/live.py` inclui `radar` = último `radar_snapshots.id`; app.js recarrega a aba Radar quando muda.

### Task 5: aba Radar (`templates/index.html`, `static/js/app.js`, `static/css/styles.css`)

- Nav lateral: "Radar" logo depois de Dashboard (ícone radar). Barra de baixo no celular: Radar no lugar de Arquivadas; Arquivadas entra em `#more-sheet` e em `MORE_TABS`. `TAB_LIST`, `titles`, `ICONS.radar`, `setTab` → `loadRadar()`, pull-to-refresh e paleta.
- `#pane-radar`: topo (título, "Atualizado há X min", pontos por fonte, botão Atualizar), faixa rolável "Próximos dias", filtros em pílulas (Tudo, Futebol, Política, Famosos/TV, Música, Economia, Outros), lista "Agora no Brasil" (posição, título, selos de fonte, barra de calor laser, seta/novo, "em alta há N h", volume), 3 primeiros em destaque. Esqueleto inicial; `emptyState` sem dados.
- Clique num assunto abre `#drawer` com `renderTopic` (fontes e posições, linha das 24 h no X em Chart.js invertido, notícias com link, "Ver no X" = `https://x.com/search?q=`, "Pesquisar no Google").
- Tudo com `el()`/`textContent`; cores pelos tokens; tilt nos cartões do radar.
- [ ] `node --check`; teste de marcação (pane, nav, bottom-nav); suíte completa; medição a 360 px sem rolagem lateral.
- [ ] Mostrar ao usuário e esperar o OK antes do commit.
