# XHenry

Painel para monitorar contas do X organizadas em pastas: saúde da conta (Search Ban, Ghost Ban, suspensa), Views, Curtidas, Retweets, Replies e Seguidores, melhor horário para postar, alertas no Telegram, revisão de post com o Grok (xAI) e geração de cópias de mídia sem metadados (aba Metadados).

As métricas dos tweets são coletadas pelo `robo.py`, que roda no VPS (fora deste repositório) e grava no mesmo banco. O painel só lê o banco; a API oficial do X é usada apenas para a verificação de saúde.

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
- Todas as contas que entram enxergam e editam os mesmos dados (não há separação por usuário).

## Configuração (`.env`)

| Variável | Para quê |
|---|---|
| `DATABASE_URL` | PostgreSQL em produção. Ausente: SQLite local. |
| `SECRET_KEY` | Assina os tokens de login. Obrigatória. |
| `ALLOW_REGISTRATION` | `true` abre o cadastro. Padrão: fechado. |
| `PUBLIC_URL` | Endereço público, usado no link de redefinição de senha. |
| `X_BEARER_TOKEN` | Só para Verificar (saúde da conta). Sem ele o painel abre e Verificar responde 503. |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `XAI_API_KEY` | Alternativa ao que se salva em Configurações (o que está salvo lá tem prioridade). |
| `ROBO_SILENCE_MIN` | Avisa no Telegram se o robô ficar esse tempo sem coletar. Padrão: 15. |
| `AUTO_CHECK_MINUTES` | Verificação automática de saúde. Padrão: desligada (gasta cota da API do X). |
| `WATCHDOG` | `off` desliga o vigia em segundo plano. |
| `BACKUP_DIR`, `BACKUP_KEEP_DAYS` | Pasta e retenção do backup diário. |

O `.env.example` traz todas comentadas.

## Alertas no Telegram

- **Post viralizando** e **queda de views** (hoje x ontem, a partir das 23h): enviados pelo `robo.py`.
- **Erro na coleta**: enviado pelo `robo.py`.
- **Robô parado**: o painel avisa quando o robô fica mais de `ROBO_SILENCE_MIN` minutos sem coletar.
- **Search Ban / Ghost Ban**: o painel avisa quando uma verificação muda o status da conta.
- **Backup falhou**: enviado pelo `scripts/backup.sh`.

## VPS

- Rode com um único worker do uvicorn (o progresso da aba Metadados e o vigia ficam em memória).
- No nginx: `client_max_body_size 110M` e `proxy_read_timeout 1860s` (vídeos da aba Metadados).
- `robo.py` no cron a cada 5 minutos.
- Backup diário: `scripts/backup.sh` no cron (instruções no topo do arquivo). Precisa do `postgresql-client`.

## Testes

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

Os testes usam um SQLite descartável; a API do X e o Telegram são simulados, nunca chamados.
