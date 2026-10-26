# XHenry

Painel para monitorar contas do X organizadas em pastas: saúde da conta (Search Ban, Ghost Ban, suspensa), Views, Curtidas, Retweets, Replies e Seguidores, melhor horário para postar, alertas no Telegram, revisão de post com o Grok (xAI) e geração de cópias de mídia sem metadados (aba Metadados).

As métricas dos tweets são coletadas pelo `robo.py` (na raiz deste repositório), que roda no cron do VPS e grava no mesmo banco. O painel só lê o banco; a API oficial do X é usada apenas para a verificação de saúde.

## Coletores

O `robo.py` nunca entra logado nas contas monitoradas. Ele lê as timelines com contas descartáveis, os **coletores**, cadastradas na aba Coletores, cada uma com o seu proxy e os seus cookies do x.com.

- Cada rodada automática lê uma conta monitorada por coletor livre, começando pela coletada há mais tempo. Com 3 coletores, são 3 contas por rodada, cada uma saindo pelo IP do seu coletor. Seguidores, no máximo 1 vez por hora por conta.
- **Configurações → Robô:** liga ou desliga a coleta automática e escolhe o intervalo (5 min a 24 h). O robô lê isso a cada minuto.
- **Atualizar Dados** (topo do Dashboard) manda os coletores lerem todas as contas na hora, revezando os coletores (até 3 contas por coletor), mesmo com a coleta automática desligada. O robô pega o pedido em até 1 minuto e o botão espera a rodada terminar.
- Proxy: `ip:porta:usuario:senha` (o formato dos vendedores) ou `http://usuario:senha@ip:porta` (também `https://` e `socks5://`). Cookies: arraste para a aba o arquivo `.json` exportado por uma extensão (Cookie-Editor, EditThisCookie) com a conta logada no x.com; o painel confere se tem o `auth_token`. Sem cookies, o robô usa o login que já estiver no `anyx` do VPS.
- Proxy e cookies ficam em texto puro no banco (o robô precisa deles) e só aparecem mascarados no painel, para quem está logado.
- Uma conta monitorada não pode ser coletor, e vice-versa.
- 429 num coletor para a rodada e deixa o coletor de castigo até o horário que o X informar. Bloqueio, sessão caída e proxy fora também dão castigo e aviso no Telegram. Trocar cookies ou proxy na aba libera o coletor na hora.

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
| `ROBO_SILENCE_MIN` | Avisa no Telegram se o robô ficar esse tempo sem coletar. Padrão: 15 (ou 2 intervalos + 5 min, se o intervalo for maior). Com a coleta automática desligada, não avisa. |
| `AUTO_CHECK_MINUTES` | Verificação automática de saúde. Padrão: desligada (gasta cota da API do X). |
| `WATCHDOG` | `off` desliga o vigia em segundo plano. |
| `BACKUP_DIR`, `BACKUP_KEEP_DAYS` | Pasta e retenção do backup diário. |

O `.env.example` traz todas comentadas.

## Alertas no Telegram

- **Post viralizando** e **queda de views** (hoje x ontem, a partir das 23h): enviados pelo `robo.py`.
- **Coletor limitado (429), bloqueado, deslogado ou com proxy fora** e **robô sem coletor**: enviados pelo `robo.py`.
- **Erro na coleta**: enviado pelo `robo.py`.
- **Robô parado**: o painel avisa quando o robô fica mais de `ROBO_SILENCE_MIN` minutos sem coletar.
- **Search Ban / Ghost Ban**: o painel avisa quando uma verificação muda o status da conta.
- **Backup falhou**: enviado pelo `scripts/backup.sh`.

## VPS

- Rode com um único worker do uvicorn (o progresso da aba Metadados e o vigia ficam em memória).
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
- `robo.py` no cron **a cada minuto**, como o usuário que tem o `anyx` logado (nunca root: o Chrome não abre como root). Ele mesmo decide se roda, pela aba Configurações; fora do intervalo, sai na hora sem abrir o navegador.

  ```cron
  * * * * * cd /caminho/do/Xhenry && python3 robo.py >> $HOME/robo.log 2>&1
  ```

  Precisa de `psycopg[binary]` e `anyx` (`pip install anyx && playwright install chromium`) no Python desse usuário. Ele lê o `.env` do painel; se esse usuário não puder lê-lo, aponte outro com `ROBO_ENV_FILE=/caminho/.env` na linha do cron. A trava e o estado ficam em `~/.xhenry-robo` (mude com `ROBO_STATE_DIR`). Atualizar o robô é o mesmo `git pull` do painel.
- Backup diário: `scripts/backup.sh` no cron (instruções no topo do arquivo). Precisa do `postgresql-client`.

## Testes

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

Os testes usam um SQLite descartável; a API do X e o Telegram são simulados, nunca chamados.

O teste ponta a ponta do robô (`tests/test_robo_e2e.py`) precisa de um Postgres **descartável** (apaga as tabelas) e só roda com `ROBO_TEST_DATABASE_URL=postgresql://...`; sem ela, é pulado.
