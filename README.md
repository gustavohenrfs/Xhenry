# XHenry

Painel para monitorar contas do X organizadas em pastas: saúde da conta (Search Ban, Ghost Ban, suspensa), Views, Curtidas, Retweets, Replies e Seguidores, melhor horário para postar, alertas no Telegram, revisão de post com o Grok (xAI) e geração de cópias de mídia sem metadados (aba Metadados).

As métricas dos tweets são coletadas pelo `robo.py` (na raiz deste repositório), que roda no cron do VPS e grava no mesmo banco. O painel só lê o banco; a API oficial do X é usada apenas para a verificação de saúde.

## Dashboard

- **Views** com a linha dos últimos dias, **views por post** (agora e no período anterior), **seguidores ganhos** no período, curtidas, retweets, replies, citações e salvamentos. Cada número mostra a variação contra o período anterior do mesmo tamanho (Hoje contra ontem inteiro, 7 dias contra os 7 anteriores; Total não compara).
- **Interações:** a rosca mostra de onde vêm as interações e, no meio, a taxa (interações ÷ views).
- **Melhor post:** o post com mais views no período, com o link para abrir no X.
- **Saúde da leitura:** contas atrasadas (sem leitura há mais de dois intervalos do Apify) e a última leitura do robô.
- **Meta do dia:** barra no cartão de Views com as views de hoje contra a meta de Configurações (Meta de views do dia).
- **Mapa de calor** em Melhor Horário (média de views por dia da semana e hora), **ranking** dos 10 posts do período com link, **alertas recentes** enviados ao Telegram e, em cada conta, a linha dos últimos 14 dias e a variação das views.
- **Sininho** (canto superior direito): os alertas de views em quatro níveis (Post viralizando, POST BOMBANDO, 100K e 1M), com a conta, o texto, as views de agora e o link. Os mesmos níveis vão para o Telegram. Clicar no sino pede permissão para notificar e inscreve o aparelho no Web Push: cada alerta novo vira notificação com o logo no Windows e no celular, mesmo com o painel fechado (precisa de HTTPS). O botão Testar manda uma notificação de teste para o aparelho.
- **Instalar o app**: o botão Instalar no topo põe o XHenry na tela inicial do celular ou como app no computador. No iPhone, pelo Safari: Compartilhar → Adicionar à Tela de Início (as notificações do iPhone só funcionam com o app instalado).
- Salvamentos aparecem depois que o `robo.py` atualizado coletar (antes ele gravava sempre 0).
- **Ao vivo:** o painel recebe um aviso do servidor (`/api/live`) quando o robô grava dados ou chega um alerta, e atualiza só a parte que mudou, sem F5. O topo mostra "ao vivo"; os cartões que mudaram ganham uma passada de laser.
- **Gaveta da conta:** clicar num cartão de conta abre o gráfico de views e seguidores por dia, os melhores posts e o histórico de verificações, com Verificar agora e Abrir no X.
- **Contas:** busca por nome, @ ou pasta e ordem por Views, Status, Pasta ou Nome; faixa na cor da pasta.
- **Busca rápida** (`Ctrl+K` ou `/`): abas, ações e contas. **Atalhos:** `1` a `7` trocam de aba, `T` troca o tema, `R` atualiza os dados, `?` lista os atalhos.
- **Tema claro e escuro** (botão no topo; segue o sistema até você escolher). Barra lateral recolhida no computador (abre ao passar o mouse); no celular, abas embaixo com "Mais".
- **Selos vermelhos** nas abas: Dashboard com os alertas não vistos, Usuários com os pedidos de acesso esperando. A soma aparece no ícone do app instalado.
- **Notificações depois de reiniciar:** no celular chegam sozinhas. No computador só chegam com o navegador rodando; o sininho tem o guia "Continuar recebendo depois de reiniciar" com os passos do Edge ou do Chrome.

## Radar de trend

A aba **Radar** mostra o que está em alta agora, de graça: trending topics do X (trends24), buscas do Google Trends e manchetes do Google Notícias. O painel lê as fontes sozinho (X e Google a cada 15 min, Notícias a cada 30; `RADAR=off` desliga) e junta as que falam do mesmo assunto.

- **Brasil ou Internacional:** o seletor no topo troca a região. Brasil tira os trends em outras escritas (tailandês, árabe…); Internacional usa o X mundial e o Google dos EUA.
- **Bom pra engajar** (ligado por padrão, só Brasil): o nicho das contas +18 com público masculino. Sobem futebol brasileiro, lutas/UFC, F1 e outros esportes, games, carros, resenha (churrasco, cerveja, sextou), política e reality; jogo estrangeiro, novela, K-pop, beleza, moda e signos ficam de fora. Morte, tragédia, crime, doença e qualquer coisa com criança nunca aparecem nesse filtro nem ganham ideia de post.
- **Gancho do momento** e **ideias de post:** o assunto mais engajável vem no topo com uma frase pronta no tom das contas ("Se o Flamengo ganhar hoje eu vou fazer uma coisa muuuito pra frente rs"); cada assunto da lista traz a primeira ideia com **Copiar** e **Postar** (abre o X com o texto). As frases são só para engajamento: nunca chamam para link, bio, grupo ou Telegram. Na gaveta, 4–5 ideias e **Mais ideias com Grok** (chave xAI do usuário). As frases são sugestivas, nunca explícitas, e nunca sexualizam uma pessoa real: com nome de gente, falam do burburinho. As datas de "Próximos dias" também abrem ideias.
- **Em alta agora:** cada assunto com a barra de calor, de quais fontes veio, se é novo, se está subindo ou caindo, há quanto tempo está em alta no X e o volume de buscas; filtros por Futebol, Política, Famosos/TV, Música e Economia.
- **Próximos dias:** o que pode virar trend (novo ou subindo em várias fontes, ou que subiu rápido no X) e as datas sazonais dos próximos 14 dias com uma dica de post.
- **Ao tocar num assunto:** a posição no X hora a hora nas últimas 24 h, as notícias ligadas, os links para o X e o Google, o botão **Alertar este termo** e o botão **Ver posts que estão engajando** (busca no X pelo seu Apify, gasta créditos; o resultado fica guardado 30 min).
- **Meus termos:** cadastre até 30 palavras ("Flamengo", "Lula"…). Quando uma aparecer no radar, chega notificação (no máximo uma vez a cada 6 h por termo). Liga e desliga em Configurações → Notificações (tipo Radar).

## Leitura pelo Apify

O `robo.py` lê as timelines pelo Apify (ator `APIFY_ACTOR`, padrão `apidojo~tweet-scraper`), com o token que cada usuário cola em Configurações. Nenhuma conta monitorada é logada.

- **Contas do admin:** Configurações → Atualizar dados liga ou desliga a leitura automática e escolhe o intervalo do Apify (15 min a 24 h; padrão 30). Cada leitura lê todas as contas, com os seguidores.
- **Contas dos outros usuários:** cada um escolhe a própria leitura automática e o intervalo (padrão 60 min).
- **Atualizar dados** (topo do Dashboard) lê na hora pelo Apify, mesmo com a leitura automática desligada.
- Sem token do Apify, nada é lido e o painel mostra "Token do Apify em falta". Falha do Apify (token recusado, sem crédito, rede) vira aviso no painel e o robô tenta de novo no próximo intervalo.

## Rodar localmente

```bash
python -m pip install -r requirements.txt
copy .env.example .env
python -m uvicorn main:app --host 127.0.0.1 --port 8000
```

Abra http://127.0.0.1:8000. Sem `DATABASE_URL`, o painel usa um SQLite em `data/suzana.db`.

## Login

O painel exige login para tudo que altera dados ou gasta cota (pastas, contas, verificações, configurações, Metadados, revisão de post). Dashboard e leituras ficam abertos.

- Defina `SECRET_KEY` no `.env` (obrigatória para entrar).
- O cadastro fica fechado por padrão. Para criar uma conta, ligue `ALLOW_REGISTRATION=true`, cadastre-se pela tela de login e desligue de novo.
- **Esqueceu a senha?** O link de redefinição (15 min, uso único) é enviado para o chat do Telegram configurado. Defina `PUBLIC_URL` com o endereço público do painel para o link sair certo.
- Redefinir ou trocar a senha desconecta todos os aparelhos que estavam logados (quem trocou pelo painel continua dentro).
- Todas as contas que entram enxergam e editam os mesmos dados (não há separação por usuário).

## Configuração (`.env`)

| Variável | Para quê |
|---|---|
| `DATABASE_URL` | PostgreSQL em produção. Ausente: SQLite local. |
| `SECRET_KEY` | Assina os tokens de login. Obrigatória. |
| `ALLOW_REGISTRATION` | `true` abre o cadastro. Padrão: fechado. |
| `PUBLIC_URL` | Endereço público, usado no link de redefinição de senha. |
| `X_BEARER_TOKEN` | Só para Verificar (saúde da conta). Sem ele o painel abre e Verificar responde 503. |
| `TELEGRAM_BOT_TOKEN` (ou `TELEGRAM_TOKEN`), `TELEGRAM_CHAT_ID` | O bot do Telegram, um só para o painel, o robô e o backup. Alertas e "Esqueceu a senha?" dependem dele. |
| `XAI_API_KEY` | Alternativa à chave salva em Configurações (a salva lá tem prioridade). |
| `ROBO_SILENCE_MIN` | Avisa se o robô ficar esse tempo sem ler. Padrão: 15 (ou 2 intervalos do Apify + 5 min, se for maior). Com a leitura automática desligada, não avisa. |
| `AUTO_CHECK_MINUTES` | Verificação automática de saúde. Padrão: desligada (gasta cota da API do X). |
| `APIFY_ACTOR`, `APIFY_INTERVAL_MIN` | Ator do Apify que lê as timelines e o intervalo padrão de quem ainda não escolheu o seu. |
| `VAPID_PRIVATE_KEY` | Opcional: chave das notificações push (sem ela, o painel gera e guarda uma). |
| `WATCHDOG` | `off` desliga o vigia em segundo plano. |
| `BACKUP_DIR`, `BACKUP_KEEP_DAYS` | Pasta e retenção do backup diário. |

O `.env.example` traz todas comentadas.

## Alertas no Telegram

- **Post viralizando** e **queda de views** (hoje x ontem, a partir das 23h): enviados pelo `robo.py`.
- **Falha do Apify** e **token do Apify em falta**: enviados pelo `robo.py`.
- **Robô parado**: o painel avisa quando o robô fica mais de `ROBO_SILENCE_MIN` minutos sem coletar.
- **Search Ban / Ghost Ban**: o painel avisa quando uma verificação muda o status da conta.
- **Backup falhou**: enviado pelo `scripts/backup.sh`.

## VPS

- Rode com um único worker do uvicorn (o progresso da aba Metadados, o cache do Dashboard, o vigia e o canal ao vivo ficam em memória).
- Depois de `git pull`: reinicie o uvicorn quando houver rota nova no Python e aperte Ctrl+F5 no navegador. O `robo.py` não precisa reiniciar (o cron chama a versão nova no minuto seguinte).
- O canal ao vivo (`/api/live`) já manda `X-Accel-Buffering: no`; se o indicador ficar em "reconectando…", ponha `proxy_buffering off;` no `location` do painel no nginx.
- No nginx: `client_max_body_size 110M` e `proxy_read_timeout 1860s` (vídeos da aba Metadados).
- Bots de varredura (`/.env`, `/credentials.json`...): bloqueie no nginx, dentro de cada `server { ... }` do painel (porta 80 e 443), para não chegarem ao backend:

  ```nginx
  # Bots de varredura: fecha a conexão sem responder e sem chegar ao backend.
  location ~* ^/(instance/|\.env|credentials\.json|service_account\.json|config\.py) {
      access_log off;
      return 444;
  }
  ```

  Depois: `nginx -t && systemctl reload nginx`. `curl -sv https://SEU_DOMINIO/.env` deve dar "Empty reply from server".
- `robo.py` no cron **a cada minuto**, como um usuário comum (não root). Ele mesmo decide se lê, pela aba Configurações; fora do intervalo, sai na hora.

  ```cron
  * * * * * cd /caminho/do/Xhenry && python3 robo.py >> $HOME/robo.log 2>&1
  ```

  Precisa de `psycopg[binary]` no Python desse usuário. Ele lê o `.env` do painel; se esse usuário não puder lê-lo, aponte outro com `ROBO_ENV_FILE=/caminho/.env` na linha do cron. A trava e o estado ficam em `~/.xhenry-robo` (mude com `ROBO_STATE_DIR`). Atualizar o robô é o mesmo `git pull` do painel.
- Backup diário: `scripts/backup.sh` no cron (instruções no topo do arquivo). Precisa do `postgresql-client`.

## Testes

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

Os testes usam um SQLite descartável; a API do X e o Telegram são simulados, nunca chamados.

O CSS do Tailwind é compilado e vai para o git (`static/css/tailwind.css`). Depois de usar uma classe nova do Tailwind no `index.html` ou no `app.js`, gere de novo com `sh scripts/build_css.sh` (precisa do Node) e suba o `?v=` no `index.html`.

O teste ponta a ponta do robô (`tests/test_robo_e2e.py`) precisa de um Postgres **descartável** (apaga as tabelas) e só roda com `ROBO_TEST_DATABASE_URL=postgresql://...`; sem ela, é pulado.
