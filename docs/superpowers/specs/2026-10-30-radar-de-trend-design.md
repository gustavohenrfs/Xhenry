# Radar de Trend + Configurações no celular — design

Data: 2026-10-30. Projeto do XHenry (painel FastAPI + JS puro, textos em português).

## Por quê

O dono do painel monetiza contas no X e quer chegar antes nos assuntos que vão bombar no Brasil (exemplos dele:
a vitória do Lula na eleição, a final do Flamengo no Brasileirão). Hoje ele descobre tarde, por acaso. Ele quer
"tudo": o que está em alta agora, eventos sazonais, posts que estão engajando, o assunto por trás de cada um,
alerta quando um termo dele aparece e uma previsão do que deve virar trend nos próximos dias. Tudo para engajar
as contas.

Sucesso: abrir a aba e em 10 segundos saber sobre o que postar agora e o que preparar para amanhã; ser avisado
no celular quando "Flamengo" (ou outro termo dele) começar a bombar.

## Metas

1. **Configurações no celular** (vai primeiro, é pequeno): o cartão Notificações fica longo demais e partes
   somem para fora da tela.
2. **Aba nova "Radar de trend"** com: Agora no Brasil, Próximos dias (sazonal + previsão), Meus termos (alerta)
   e, ao abrir um assunto, as notícias e os posts que estão engajando.

## Não-metas

- Postar ou agendar posts pelo painel.
- Fontes de fora do Brasil (só `geo=BR` / português).
- Previsão com modelo de IA treinado; a previsão é por regras explicáveis (abaixo).
- Gastar crédito sozinho: nada pago roda sem um clique do usuário.

## Antimetas (falharia mesmo funcionando)

- Custo surpresa: nenhuma leitura automática do radar usa Apify nem Grok.
- Aba lenta ou que trava no celular: a aba lê só o banco; as fontes são lidas em segundo plano.
- Alerta repetido: o mesmo termo avisa no máximo uma vez a cada 6 h por usuário.
- Parecer genérico: a aba segue o visual do XHenry (vidro, laser, temas Noite/Porcelana, cores pelos tokens).

## Restrições

- Sem migração automática: tabelas novas entram pelo `create_all`; índices novos em tabelas existentes vão no
  `ensure_user_auth_columns`.
- Um processo uvicorn só (o vigia em segundo plano já depende disso).
- Toda escrita de rota usa `approved_user`; nada novo público (atualizar `tests/test_auth_routes.py`).
- Classes Tailwind novas exigem `sh scripts/build_css.sh` e subir o `?v=`; preferir regras em `styles.css`.
- Nunca chamar fontes de verdade nos testes (httpx falso, como o X e o Telegram hoje).
- O usuário pediu para não tirar prints; conferir a interface por testes e checagens.

## Parte 1 — Configurações no celular (decisões minhas)

Causa provável do "sumindo": itens de grade com `min-width:auto` deixam o `<select>` e as linhas longas
alargarem o cartão além da tela. Causa do "muito longo": 14 tipos de aviso, um por linha.

- Todo filho de grade em `#pane-settings` ganha `min-width: 0`; `select` e `input` com `max-width: 100%`;
  textos longos quebram (`overflow-wrap: anywhere`) em vez de empurrar a largura.
- "O que avisa" vira **pílulas compactas** que quebram linha (ícone + nome curto, ligada = cor de destaque),
  em vez de 14 linhas de cartão. Igual em todas as larguras.
- No celular (< 768 px), as seções Marcos de views, Não perturbe e Aparelhos viram blocos recolhíveis
  (`<details>`), fechados por padrão, com um resumo do valor atual no título (ex.: "Não perturbe · 22:00–07:00").
  No computador continuam abertos.
- O mesmo cuidado de largura vale para os outros cartões de Configurações (Robô, Atualizar dados, Chaves).
- Teste: checagem de que nenhum elemento de `#pane-settings` passa da largura da tela a 360 px (sem print).

## Parte 2 — Radar de trend

### Fontes (grátis, lidas pelo painel em segundo plano)

| Fonte | O que dá | Como |
|---|---|---|
| X Brasil (trends24.in/brazil) | trending topics do X Brasil, 24 listas (uma por hora) | HTML: `.list-container` → `h3[data-timestamp]` + `a.trend-link` em ordem |
| Google Trends | buscas em alta no Brasil, volume aproximado e notícias ligadas | RSS `trends.google.com/trending/rss?geo=BR` (`ht:approx_traffic`, `ht:news_item`) |
| Google Notícias | manchetes principais do Brasil | RSS `news.google.com/rss?hl=pt-BR&gl=BR&ceid=BR:pt-419` |
| Wikipédia | páginas mais lidas em português ontem | API REST `pageviews/top/pt.wikipedia/all-access/AAAA/MM/DD` (sem as páginas especiais) |
| Calendário sazonal | datas brasileiras que vêm por aí | tabela no código (abaixo) |

Conferido em 2026-10-30: as quatro fontes de rede responderam 200 com dado bom. Reddit respondeu 403: fica de fora.
Risco: trends24 é raspagem de HTML; se o layout mudar, a fonte falha sozinha (o resto continua) e o cartão mostra
"X Brasil indisponível". Alternativa paga já existe se precisar: um ator de trends no Apify.

**Ritmo:** o vigia (`app/watchdog.py`) chama `radar.refresh()` a cada 15 min (X e Google Trends), 30 min
(Notícias) e 6 h (Wikipédia). `RADAR=off` desliga. Cada fonte falha sozinha (timeout de 15 s), sem derrubar as
outras. Também lido 1 vez ao abrir a aba se a última leitura tiver mais de 15 min (o botão "Atualizar" da aba
força).

### Dados (tabelas novas)

- `radar_snapshots`: id, source (`x`, `google`, `news`, `wiki`), fetched_at, ok, error.
- `radar_items`: snapshot_id, source, rank, title, norm (título normalizado: minúsculas, sem acento, sem `#`,
  espaços únicos), volume (texto do Google, ex. "2000+", ou null), url, extra (JSON: notícias ligadas, hora em
  que apareceu no X…). Mantém 7 dias.
- `radar_terms`: owner_id, term, norm, created_at, last_alert_at. Único por (owner_id, norm).
- `radar_post_cache`: norm, fetched_at, posts (JSON), owner_id de quem pagou. Vale 30 min.

### Como um "assunto" é montado

Itens de fontes diferentes com o mesmo `norm` (ou um contendo o outro, mínimo 4 letras) viram **um assunto**.
Cada assunto tem:

- **Fontes**: selos X / Google / Notícias / Wikipédia.
- **Calor** (0–100): soma por fonte de (1 − posição/tamanho da lista), peso X 40, Google 30, Notícias 20,
  Wikipédia 10, mais bônus pelo volume do Google. Mostrado como barra de calor.
- **Tempo em alta no X**: quantas das 24 listas horárias têm o termo ("em alta há 5 h").
- **Direção**: subindo / estável / caindo, comparando a posição na lista mais nova com a de 2 h antes; "novo"
  se apareceu na última hora.
- **Categoria** (palpite por palavras-chave, editável no código): Futebol, Política, Famosos/TV, Música,
  Economia, Outros. Serve de filtro.

### Previsão "Próximos dias" (regras, sem IA)

1. **Sazonal**: datas dos próximos 14 dias do calendário, com "em 3 dias" e uma dica de ângulo de post.
2. **Pegando fogo**: assuntos novos ou subindo que estão em 2+ fontes, ou que subiram 10+ posições no X nas
   últimas 3 h. Rótulo "pode virar trend".
3. **Volta todo ano**: datas do calendário com histórico (ex.: Black Friday) aparecem com 7 dias de antecedência.

Calendário (no código, recalculado por ano): Ano Novo, Carnaval e Quarta de Cinzas (pela Páscoa), Dia da Mulher,
Páscoa, Tiradentes, Dia do Trabalho, Dia das Mães (2º domingo de maio), Dia dos Namorados (12/06), Festa Junina,
Dia dos Pais (2º domingo de agosto), Independência, Dia das Crianças, Halloween, Finados, Black Friday (última
sexta de novembro), Consciência Negra, Natal, Réveillon; e eleições (1º e último domingo de outubro, anos pares).

### Meus termos (alerta)

- Cada usuário cadastra até 30 termos (ex.: "Flamengo", "Lula"). Comparação pelo `norm`, por palavra inteira.
- A cada leitura do radar, termo que aparece em qualquer fonte grava em `alerts_sent` com `kind=radar`, chave
  `radar:{owner}:{norm}:{janela de 6 h}`, texto "📡 Flamengo está em alta no X Brasil (#3) e no Google" com
  link para a aba.
- Entra no sistema de Notificações que já existe: novo tipo `radar` em `NOTIFY_TYPES` (ligado por padrão), push,
  sininho, histórico (filtro "Radar"), não perturbe.

### Posts que estão engajando (paga, só no clique)

Ao abrir um assunto, botão "Ver posts que estão engajando · usa seus créditos do Apify". Chama o mesmo ator do
robô (`apidojo~tweet-scraper`) com `searchTerms: [termo]`, `sort: "Top"`, `tweetLanguage: "pt"`,
`maxItems: 20`, com o token do próprio usuário. Mostra os 10 com mais views (autor, texto, views, curtidas,
link). Resultado guardado 30 min por termo e mostrado a qualquer usuário que abrir o mesmo assunto nesse tempo
(são posts públicos). Sem token: o botão explica e leva a Configurações. Os campos de saída do ator na busca
precisam ser conferidos numa chamada real antes de confiar nas views (como no robô).

### Tela (decisões minhas, no visual do XHenry)

Aba "Radar" (ícone de radar) para todos os usuários aprovados, na barra lateral depois de Dashboard e, no
celular, na barra de baixo no lugar de Arquivadas (Arquivadas vai para "Mais").

- **Topo**: título, "Atualizado há 4 min · X, Google, Notícias, Wikipédia" com um ponto verde/vermelho por
  fonte, botão Atualizar.
- **Faixa "Próximos dias"** (rolagem lateral no celular): cartões de data sazonal e de "pode virar trend",
  com contagem regressiva.
- **Agora no Brasil** (coluna principal): filtros em pílulas (Tudo, Futebol, Política, Famosos/TV, Música,
  Economia; e por fonte). Cada assunto é uma linha de vidro: posição, nome, selos de fonte, barra de calor com o
  brilho laser, seta subindo/caindo ou selo "novo", "em alta há 5 h" e o volume do Google. Os 3 primeiros com
  destaque maior.
- **Meus termos** (coluna lateral no computador, bloco no celular): campo para adicionar, pílulas removíveis, e
  quais estão em alta agora acesos.
- **Gaveta do assunto** (a mesma gaveta das contas): fontes e posições, linha das 24 h no X (gráfico pequeno de
  posição por hora), notícias ligadas com link, link "Ver no X", botão dos posts engajando, botão "Alertar este
  termo".
- Estados vazios com `emptyState()`; esqueleto de carregamento como no Dashboard; tudo pelos tokens de cor;
  ao vivo pelo `/api/live` quando chegar leitura nova do radar.

### Rotas (todas `approved_user`)

- `GET /api/radar` → `{updated_at, sources:{x:{ok,at}…}, topics:[…], upcoming:[…], my_terms:[…]}`.
- `POST /api/radar/refresh` (no máximo 1 a cada 2 min por instância).
- `GET/POST/DELETE /api/radar/terms`.
- `GET /api/radar/topic?norm=` → detalhe (fontes, linha das 24 h, notícias).
- `POST /api/radar/posts {term}` → posts engajando (Apify do usuário; 400 sem token).

## Fases (uma por vez, com OK entre elas)

1. Configurações no celular.
2. Radar: fontes, tabelas, montagem dos assuntos, sazonal, previsão, aba completa (sem alertas e sem Apify).
3. Meus termos + alerta + posts engajando.

## Deixado para quem constrói

Nomes internos, a lista exata de palavras-chave por categoria, os pesos finos do calor (dentro do descrito), o
texto das dicas sazonais, o desenho do ícone.
