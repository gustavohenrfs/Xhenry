# Fase 1: Multiusuário e aprovação (design)

Data: 2026-10-28. Status: aprovado e implementado (com o Apify já na Fase 1, ver abaixo).

## Intenção

Hoje o XHenry é "só do Henrique": tudo pendurado num dono implícito (`local@suzana`), coletores e
configurações globais, e várias rotas de leitura públicas (`/api/dashboard`, `/api/folders`, `/api/live`…).
A Fase 1 transforma isso num sistema "para os amigos": cada pessoa entra por convite, espera aprovação
e, aprovada, vê e mexe **apenas nos próprios dados**. O admin (`henrfs@proton.me`) tem uma aba Usuários
para aprovar e gerar convites, e continua sendo o único com Coletores e controle do robô.

Mudança aprovada depois do rascunho: a leitura via Apify entrou **já na Fase 1**. Quem não é admin cola o
token do Apify em Configurações; o robo.py lê as contas dele pelo Apify (`apify_round`, em paralelo com os
coletores, a cada `APIFY_INTERVAL_MIN` ou na hora pelo "Atualizar dados"). Sem token, o painel mostra
"Token do Apify em falta". Cada usuário também cola o próprio Bearer da API do X (botão Verificar) e a
chave da xAI; as do `.env` valem só para o admin.

## Objetivos

1. Toda tabela de dados de usuário amarrada a um dono; toda rota de dados exige login **aprovado** e filtra
   pelo usuário logado.
2. `users.is_admin` (só o admin) e `users.is_approved` (padrão `false`).
3. Convite: o admin gera um link de uso único; o amigo se cadastra por ele e cai na tela
   "Aguardando aprovação".
4. Aba Usuários (só admin): pedidos pendentes, aprovar, recusar, revogar acesso, gerar/ver convites.
5. Coletores, controle do robô e "Saúde da coleta" só existem para o admin (rota e aba).
6. Configurações (meta de views, chave do Grok), alertas, sininho, push e painel ao vivo por usuário.
7. Nenhum dado existente se perde: tudo do `local@suzana` passa para o admin.

## Fora do escopo (Fase 2 em diante)

- Telegram por usuário. O bot do `.env` continua sendo só do admin.
- Planos, cobrança, limites de contas por usuário.

## Anti-objetivos (falha mesmo que "funcione")

- Qualquer vazamento entre usuários: um id de pasta/conta/alerta de outro dono nunca é lido nem alterado
  (resposta 404, não 403, para não confirmar que existe).
- O admin ficar trancado do lado de fora depois do deploy.
- Perder ou duplicar histórico (`tweet_samples`, `daily_metrics`, `alerts_sent`).
- Amigo gastando recurso pago do admin sem querer (cota da API do X, chave do Grok do `.env`, Telegram).

## Restrições

- Sem ferramenta de migração: colunas e tabelas novas entram por `create_all` + `ALTER TABLE`/`CREATE INDEX
  IF NOT EXISTS` em `ensure_user_auth_columns` (`app/services.py`), idempotente, rodando no startup.
  Funciona em SQLite (testes/local) e Postgres (VPS).
- robo.py é SQL cru em Postgres; continua independente do app.
- Um processo uvicorn só (cache, Cloaker, live hub em memória).
- Textos, comentários e docstrings em português; padrão visual atual (vidro, dois temas, mobile).
- `tests/test_auth_routes.py` fixa a lista de rotas públicas/protegidas e tem que ser atualizado.

## Modelo de dados

Nome da coluna: **`owner_id`** (o que `folders` já usa), em vez de `user_id`, para manter um nome só.

| Tabela | Mudança |
|---|---|
| `users` | `is_admin bool default false`, `is_approved bool default false`, `approved_at`, `invited_by_id` (FK users, nullable) |
| `folders` | já tem `owner_id`. Nada muda. |
| `x_accounts` | `owner_id` (FK users, CASCADE, indexado), preenchido a partir de `folders.owner_id`. Redundante de propósito: robo.py e as consultas filtram sem join. A rota de criar/mover conta garante que bate com o dono da pasta. |
| `tweet_samples`, `daily_metrics`, `health_checks`, `day_baselines`, `folder_daily_metrics`, `sync_runs` (por conta) | **sem coluna nova**: o dono vem pela conta/pasta. Toda leitura já parte da lista de contas do usuário. |
| `user_settings` (nova) | `owner_id` (PK/único), `impression_goal`, `xai_api_key`, `xai_model`. Fase 2 acrescenta `apify_token`. |
| `app_settings` (id=1) | vira só **configuração do sistema**: robô (`robo_enabled`, intervalo, pedido), VAPID, cursor de push. Os campos `impression_goal`/`xai_*` são copiados para o `user_settings` do admin na migração e deixam de ser lidos. |
| `alerts_sent` | `owner_id` nullable, indexado. Alerta de conta = dono da conta. `NULL` = alerta de sistema (robô parado, coletor, erro de coleta), visto só por admins. |
| `push_subscriptions` | `owner_id` (FK users, CASCADE). |
| `collectors` | sem dono: são do sistema, só o admin vê. |
| `invites` (nova) | `token` (aleatório, único), `created_by_id`, `created_at`, `expires_at` (7 dias), `used_by_id`, `used_at`, `note` (opcional, ex.: "João"). Uso único. |

### Migração no startup (idempotente)

1. Cria colunas/tabelas novas.
2. Admin: o usuário cujo email é `ADMIN_EMAIL` (`.env`, padrão `henrfs@proton.me`) recebe
   `is_admin = is_approved = true` a cada startup. Se ele ainda não existe, nada é movido e o log avisa;
   ele se cadastra com `ALLOW_REGISTRATION=true` (já existente) e vira admin no próximo startup... ou já no
   próprio cadastro, se o email bater com `ADMIN_EMAIL`.
3. Se o admin existe: pastas do `local@suzana` passam para ele; `x_accounts.owner_id` é preenchido pela
   pasta onde estiver nulo; `alerts_sent.owner_id` é preenchido pela conta da chave (`viral:{account_id}:…`,
   e as outras chaves que tenham account_id), o resto fica `NULL` (sistema); `push_subscriptions` órfãs vão
   para o admin; `user_settings` do admin é criado a partir de `app_settings` id=1.
4. Usuários que já existiam (além do admin) entram como **pendentes**: o admin aprova com um clique.
5. `ensure_owner()` e o `local@suzana` deixam de ser usados (a linha pode ficar, sem pastas).

Antes do deploy: rodar `scripts/backup.sh` à mão.

## Autenticação e aprovação

- Duas dependências em `app/routers/auth.py`:
  - `current_user` (já existe): token válido, usuário ativo. Usada só por `/api/me` (GET), logout/troca de
    senha e a tela de espera.
  - `approved_user` (nova): `current_user` + `is_approved`, senão **403 com `code: "pending"`**. É a padrão
    de todas as rotas de dados.
  - `admin_user` (nova): `approved_user` + `is_admin`, senão 403.
- `/api/me` devolve `is_admin` e `is_approved`; o front decide qual tela mostrar.
- Login de pendente funciona (recebe token) e cai na tela de espera.
- Revogar acesso: `is_approved=false` + `token_version += 1` (derruba sessões abertas).
- `DELETE /api/me` (apagar a própria conta) é proibido para o admin (apagaria tudo em cascata).

### Convite

- Admin gera em Usuários → "Gerar convite": `POST /api/admin/invites` devolve `PUBLIC_URL/#invite=<token>`
  com botão copiar. Lista de convites mostra pendente/usado/expirado e permite cancelar.
- O link abre o modal já no cadastro (nome, email, senha). `POST /api/register` aceita `invite`; com convite
  válido e não usado, cria o usuário pendente e marca o convite como usado, na mesma transação.
- Sem convite, `/api/register` continua 403, salvo `ALLOW_REGISTRATION=true` (fica só como porta de
  emergência para criar o admin).
- Ao cadastrar, o admin recebe aviso: Telegram ("👤 Novo pedido de acesso: Nome (email)") e um selo vermelho
  na aba Usuários.

### Tela "Aguardando aprovação"

Tela cheia no mesmo estilo do login: logo, "Seu acesso está aguardando aprovação", nome/email, botão
"Verificar de novo" (chama `/api/me`; também checa sozinho a cada 30 s) e "Sair". Ao ser aprovado, entra
direto no painel.

### Esqueci a senha

Continua mandando o link para o Telegram do `.env` (o do admin). Para amigos, o admin repassa o link.
Telegram por usuário fica para depois.

## Rotas

Regra: tudo em `/api/` que lê ou escreve dados passa a exigir `approved_user` e filtrar por `owner_id`.
Objeto de outro dono → 404.

| Rota | Antes | Depois |
|---|---|---|
| `GET /api/dashboard`, `/api/suggestions`, `GET /api/folders`, `/api/accounts/archived`, `POST /api/refresh-all` | públicas | `approved_user`, só dados do usuário. Cache (`app/cache.py`) com `owner_id` na chave. |
| `/api/live` (SSE) | pública | exige login. `EventSource` não manda header, então o token vai em `?token=` (só nessa rota). Resumos por usuário: `data` (o carimbo global serve; recarregar à toa é barato e não vaza nada), `alerts` = maior id de alerta **do usuário**, `collectors` só para admin. |
| pastas/contas POST/PATCH/DELETE, `/api/accounts/{id}/detail` | login | `approved_user` + dono. Criar conta só em pasta do usuário; mover só para pasta do usuário. |
| `/api/settings` GET/PUT | login, linha global | `approved_user`, lê/grava `user_settings` do usuário. |
| `/api/alerts`, `/api/viral` | login, todos | `approved_user`; usuário comum vê os alertas dele, admin vê os dele + os de sistema (`NULL`). |
| `/api/posts/review` (Grok) | login | `approved_user`; usa a chave do usuário. Fallback `XAI_API_KEY` do `.env` **só para o admin**. |
| `/api/push/*` | login | `approved_user`; inscrição grava `owner_id`. `/api/push/renew` continua pública (mantém o dono da linha antiga). |
| `/api/cloaker*` | login | `approved_user` (amigos podem usar). |
| `/api/check*` (API do X, cota do admin) | login | `admin_user` na Fase 1. |
| `/api/collectors*`, `/api/robo*` | login | `admin_user`. |
| `/api/telegram/test` | login | `admin_user`. |
| `/api/admin/users` GET, `/api/admin/users/{id}/approve`, `/reject` (apaga pendente), `/revoke` | — | novas, `admin_user`. Admin não pode revogar a si mesmo. |
| `/api/admin/invites` GET/POST, `DELETE /api/admin/invites/{id}` | — | novas, `admin_user`. |

Continuam públicas: `/`, `/health`, `/sw.js`, `/manifest.webmanifest`, `/api/config`, `/api/login`,
`/api/register` (com convite), `/api/password-reset/*`, `/api/push/renew`, `/avatars/*`.

## Push e alertas

- `push_new_alerts`: o cursor continua global (`app_settings.push_last_alert_id`); cada alerta viral só vai
  para as inscrições do `owner_id` dele.
- Alertas de banimento (`dispatch_alerts`, ao verificar conta) gravam `owner_id`. Telegram só sai para
  alertas de contas do admin e de sistema.
- Watchdog: "Robô parado" e auto-check continuam olhando só as contas do admin.

## robo.py (Fase 1)

Mudança mínima:
- A consulta de contas passa a ler só contas cujo `owner_id` é um usuário `is_admin`.
- Os `INSERT INTO alerts_sent` gravam `owner_id` (o dono da conta; `NULL` nos alertas de coletor/robô).
- O teste e2e (`tests/test_robo_e2e.py`) ganha um usuário comum com conta, que o robô não pode ler.

## Frontend (`static/js/app.js`, `templates/index.html`)

- Boot: `/api/me` decide entre login, tela de espera ou painel; `#invite=` abre o cadastro com o convite.
- Admin: abas como hoje + **Usuários** (ícone de pessoas, selo com pendentes). Lista em cartões: pendentes no
  topo com "Aprovar"/"Recusar"; aprovados com data e "Revogar"; seção Convites com "Gerar convite", copiar,
  status e cancelar.
- Usuário comum: sem Coletores, sem Usuários, sem a seção Robô nas Configurações, sem "Saúde da coleta",
  sem o botão "Verificar" (check do X) e sem os campos de Telegram. "Atualizar dados" fica escondido até a
  Fase 2. A paleta `Ctrl+K`, atalhos 1–7 e a barra de baixo no celular respeitam as abas visíveis.
- Ao trocar de usuário, limpar `dash_snapshot`, `dashCache` e os ids vistos do sininho no `localStorage`
  (já limpa no logout; garantir no login de outro email também).
- `?v=` sobe no index.html; recompilar o Tailwind se aparecer classe nova.

## Testes

- Isolamento: dois usuários aprovados, A cria pasta/conta/alerta/inscrição; B não vê nada disso em nenhuma
  rota de leitura e recebe 404 ao tentar ler/alterar/apagar por id. Cache do dashboard não mistura donos.
- Aprovação: pendente faz login, recebe 403 `pending` nas rotas de dados, 200 em `/api/me`; aprovado entra;
  revogado perde a sessão.
- Convite: válido cria pendente e é consumido; usado, expirado e cancelado → erro; sem convite → 403.
- Admin: rotas de admin dão 403 para usuário comum; admin não revoga nem apaga a si mesmo.
- Migração: banco no formato antigo (`local@suzana` com pastas, alertas, inscrições) → tudo vai para o admin;
  rodar duas vezes não muda nada.
- Push: alerta da conta de A só vai para as inscrições de A.
- `test_auth_routes.py` atualizado com a nova lista.
- e2e do robo (Postgres): conta de usuário comum não é lida; alertas gravam `owner_id`.
- Visual: prints headless (Edge) da tela de espera, aba Usuários e painel do amigo, nos dois temas e no celular.

## Decisões que tomei (confira)

1. Coluna chamada `owner_id`, não `user_id`, porque `folders` já usa esse nome.
2. Tabelas de métricas não ganham coluna de dono: herdam pela conta. `x_accounts` ganha `owner_id` redundante.
3. Configurações por usuário numa tabela nova `user_settings`; `app_settings` vira só do sistema.
4. Usuários antigos (fora o admin) entram pendentes.
5. Convite de uso único, válido por 7 dias.
6. Na Fase 1 amigos não têm coleta, nem "Verificar" (cota do X do admin), nem Telegram, nem a chave Grok
   do `.env`; têm painel, pastas, contas, Cloaker, Grok com chave própria e push.
7. Esqueci a senha de amigo chega no seu Telegram.
8. `/api/live` aceita o token por `?token=`.

## Fica para quem constrói

Layout exato da aba Usuários e da tela de espera dentro do design atual, nomes de funções, e a ordem dos
commits dentro da fase.
