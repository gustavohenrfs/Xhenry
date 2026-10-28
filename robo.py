"""Coletor de métricas do X para o VPS.

Lê as timelines das contas monitoradas (x_accounts) entrando logado em contas
descartáveis, os coletores (tabela collectors, aba Coletores do painel). As
contas monitoradas nunca são logadas pelo robô. Grava somente tweets de hoje
(America/Sao_Paulo) em tweet_samples.

O cron chama o robô a cada minuto; quem decide se roda é a aba Configurações
do painel (app_settings):
- leitura automática ligada: roda quando a última rodada tem mais que o
  intervalo escolhido (5 min a 24 h);
- desligada: só roda quando alguém clica em "Atualizar Dados";
- "Atualizar Dados" grava um pedido; o robô apaga o pedido e faz uma rodada
  manual, que lê todas as contas (até MANUAL_PER_COLLECTOR por coletor,
  revezando os coletores).

Cada rodada automática:
- pega os coletores livres (ligados, fora de castigo, descansados e abaixo do
  limite do dia) e lê uma conta monitorada por coletor: as coletadas há mais
  tempo primeiro (x_accounts.collected_at);
- para cada par abre o anyx logado no coletor, saindo pelo proxy dele, lê a
  conta e fecha o navegador. Até ROBO_WORKERS pares ao mesmo tempo (padrão 4),
  sempre com coletores diferentes: para o X são contas e IPs diferentes, e um
  coletor nunca abre dois navegadores juntos;
- seguidores no máximo 1x por hora por conta (x_accounts.followers_at);
- pausa aleatória de 0,3 a 2 s entre páginas e entre contas;
- 429 num coletor: nenhum par novo começa, deixa o coletor de castigo e avisa.

Proteção dos coletores (rodada automática): o mesmo coletor só lê de novo
depois de ROBO_COLLECTOR_REST_MIN minutos (padrão 10) e no máximo
ROBO_COLLECTOR_DAILY_MAX leituras por dia (padrão 150; contagem em
ROBO_STATE_DIR/leituras.json). Com coletores de menos, as contas esperam mais
em vez de o coletor ler mais rápido.

O Chrome não aceita proxy com usuário e senha, então cada coletor ganha um
repassador local (ProxyRelay): o Chrome fala com 127.0.0.1 sem senha e o
repassador abre o túnel no proxy de verdade com a senha (http, https ou socks5).

Alertas: o robô só grava em alerts_sent (com o dono, owner_id; NULL = sistema,
vai para o admin) e o painel avisa por push e mostra no sininho e no histórico,
conforme as Notificações de cada usuário. Nada vai para o Telegram (lá ficaram
só o "Esqueceu a senha?" e o aviso do backup).
- Views de um post: Viralizando, POST BOMBANDO, 100K e 1M (VIEW_LEVELS). Cada
  nível sai uma vez por post; se o post pular níveis, só o maior é avisado.
- Marcos do usuário (user_settings.notify_prefs): a cada post_step views num
  post e a cada day_step views no total do dia; e a meta do dia batida
  (impression_goal). Só o maior marco alcançado sai.
- Queda de views: a partir de QUEDA_HORA, se as views de hoje ficarem mais de
  QUEDA_LIMITE abaixo das de ontem. Uma vez por conta por dia.
- Coletor limitado (429), bloqueado, com verificação de humanidade (Cloudflare),
  deslogado ou com proxy fora: uma vez por hora por coletor e motivo.
- Nenhum coletor disponível: no máximo a cada 6 horas.
- Todos os coletores no limite de leituras do dia: uma vez por dia.
- Erro na leitura de uma conta (suspensa, não encontrada, banco): no máximo uma
  mensagem por hora para o mesmo conjunto de falhas.

Botão Testar da aba Coletores: o painel grava collectors.test_requested_at e o
robô, no minuto seguinte (mesmo com a leitura desligada), lê uma conta com
aquele coletor e grava o resultado em test_status/test_detail.

Cada rodada grava um batimento em sync_runs (kind=tweets, account_id NULL).
O vigia do painel usa esse batimento para avisar quando o robô fica parado.
Ghost/Search Ban é com o painel (/api/check), não com o robô.

Leitura híbrida das contas do admin: com o token do Apify dele salvo em
Configurações (user_settings.apify_token), o robô lê todas as contas do admin
pelo Apify a cada app_settings.apify_interval_min (padrão 30) ou na hora pelo
"Atualizar Dados", e os coletores ficam em espera. Os coletores só entram quando
a chamada ao Apify falha (token recusado, sem crédito, rede): na mesma passada,
como uma rodada manual. Conta sem post hoje não é falha. Sem token, tudo funciona
como acima, só com os coletores. O batimento grava por onde leu (stats.via).

Multiusuário: coletores, Telegram e tudo acima valem só para as contas do admin
(users.is_admin). As contas dos outros usuários aprovados são lidas pelo Apify
(apify_round), com o token que cada um colou em Configurações
(user_settings.apify_token), em toda passada do cron, independente da leitura
automática do admin: com a leitura automática do usuário ligada
(user_settings.apify_auto), cada conta no máximo a cada
user_settings.apify_interval_min (padrão 60); sempre que houver pedido do
"Atualizar dados" (user_settings.apify_run_requested_at). O painel também lê
na hora pelo read_now (app/reader.py), sem esperar o cron. Sem token, nada é lido e o painel dele
mostra "Token do Apify em falta" (user_settings.apify_status = sem_token). Os
alertas dessas contas vão só para o painel do dono (alerts_sent.owner_id: sininho,
push e Alertas).

Fica na raiz do repositório do painel e lê o mesmo .env (DATABASE_URL,
TELEGRAM_TOKEN ou TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID). Precisa de
psycopg[binary] e anyx (pip install anyx && playwright install chromium).

    python3 robo.py

Variáveis próprias (ambiente do cron):
- ROBO_ENV_FILE: outro .env, se o usuário do cron não puder ler o do painel.
- ROBO_STATE_DIR: onde ficam a trava e o estado (padrão ~/.xhenry-robo).
- APIFY_ACTOR: ator do Apify que lê as timelines (padrão apidojo~tweet-scraper).
- APIFY_INTERVAL_MIN: intervalo dos usuários sem o próprio (painel antigo; padrão 60).
"""

from __future__ import annotations

import base64
import hashlib
import inspect
import json
import logging
import os
import random
import socket
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import unquote, urlsplit
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
BR_TZ = ZoneInfo("America/Sao_Paulo")
TWEET_LIMIT = 20
# Níveis do alerta de views, do menor para o maior: (sufixo da chave, views acima de, título).
# A chave é viral:{conta}:{tweet} no primeiro nível e viral:{conta}:{tweet}:{sufixo} nos outros;
# o painel (sininho e push) lê o nível pelo sufixo. Todos usam kind=viral.
VIEW_LEVELS = (
    ("", 500, "🚀 Post viralizando!"),
    ("bombando", 10_000, "🔥 POST BOMBANDO!"),
    ("100k", 100_000, "💯 Post passou de 100K views!"),
    ("1m", 1_000_000, "👑 Post passou de 1 MILHÃO de views!"),
)
# Pausa aleatória entre a página de perfil e a timeline, e entre um par e outro.
PAUSE_RANGE = (0.3, 2.0)
FOLLOWERS_EVERY = timedelta(hours=1)
# Não começa um par novo depois disso: a próxima rodada fica com o resto.
ROUND_BUDGET = 240
# Rodada manual ("Atualizar Dados"): todas as contas, revezando os coletores.
MANUAL_BUDGET = 600
MANUAL_PER_COLLECTOR = 3
# Folga para o intervalo: o cron dispara no minuto cheio e a rodada anterior terminou no meio dele.
INTERVAL_SLACK = timedelta(seconds=45)
# Timeline: o anyx espera 30 s pela resposta do X e 45 s por página; com o X
# lento isso vira "nothing loaded". Uma segunda tentativa depois de uma pausa.
TIMELINE_WAIT = 60
PAGE_TIMEOUT_MS = 90_000
TIMELINE_TRIES = 2
TIMELINE_RETRY_PAUSE = 15
# Tentar de novo não adianta (e piora o limite): desiste na hora.
NO_RETRY = {"RateLimited", "SessionExpired", "UsageError", "WrongAccount", "ProxyError", "NoLogin"}
# Castigo do coletor por motivo. 429 usa o horário que o X informar, se vier.
PAUSE_FOR = {
    "limite": timedelta(hours=1),
    "bloqueio": timedelta(minutes=30),
    "proxy": timedelta(minutes=15),
    # X/Cloudflare pediu "Verify you are human": insistir piora; alguém resolve no navegador e aperta Testar.
    "verificacao": timedelta(hours=6),
    "sessao": timedelta(hours=24),
}
# O X manda o coletor para o desafio do Cloudflare (__cf_chl_rt_tk na URL) ou para a trava de
# conta (account/access). O anyx chama isso de SessionExpired ("is locked by X"), mas os cookies
# continuam bons: é verificação de humanidade, não sessão caída.
HUMAN_CHALLENGE_MARKS = ("account/access", "__cf_chl")
QUEDA_LIMITE = 0.30
QUEDA_HORA = 23
QUEDA_MIN_ONTEM = 100
RUNS_KEEP_DAYS = 7
# Trava e estado ficam fora do repositório, numa pasta do usuário do cron (o git pull não mexe).
STATE_DIR = Path(os.getenv("ROBO_STATE_DIR") or Path.home() / ".xhenry-robo")
# Sem banco não há alerts_sent: o dedupe do alerta "banco fora" fica neste arquivo.
OFFLINE_STAMP = STATE_DIR / "alerta_banco"
# O cron não empilha rodadas: se a anterior ainda roda, esta sai quieta.
LOCK_FILE = STATE_DIR / "robo.lock"
# Leituras de hoje por coletor (limite diário); só o robô lê e grava.
READS_FILE = STATE_DIR / "leituras.json"
# Padrões das variáveis do .env (ROBO_WORKERS, ROBO_COLLECTOR_REST_MIN, ROBO_COLLECTOR_DAILY_MAX).
# Cada Chrome usa uns 300 a 500 MB: 4 ao mesmo tempo cabem com folga num VPS com 6 GB livres.
WORKERS_DEFAULT = 4
COLLECTOR_REST_MIN_DEFAULT = 10
COLLECTOR_DAILY_MAX_DEFAULT = 150
# O config.json do anyx é um arquivo só, e é nele que fica o proxy de cada coletor. Gravar a entrada
# e abrir o Chrome acontece sob esta trava: em paralelo, uma leitura podia apagar a entrada da outra
# e aquele Chrome sair sem proxy, pelo IP do VPS. As leituras em si correm soltas.
LAUNCH_LOCK = threading.Lock()
READS_LOCK = threading.Lock()

log = logging.getLogger("robo")


def env_int(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int((os.getenv(name) or "").strip() or default)
    except ValueError:
        value = default
    return max(low, min(high, value))


def workers() -> int:
    return env_int("ROBO_WORKERS", WORKERS_DEFAULT, 1, 12)


def collector_rest() -> timedelta:
    return timedelta(minutes=env_int("ROBO_COLLECTOR_REST_MIN", COLLECTOR_REST_MIN_DEFAULT, 0, 24 * 60))


def daily_max() -> int:
    return env_int("ROBO_COLLECTOR_DAILY_MAX", COLLECTOR_DAILY_MAX_DEFAULT, 1, 10_000)


def reads_today() -> dict[str, int]:
    """Leituras de hoje (fuso de São Paulo) por id de coletor; dia novo começa do zero."""
    today = datetime.now(BR_TZ).date().isoformat()
    try:
        data = json.loads(READS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data.get("reads", {}) if data.get("date") == today else {}


def count_read(collector_id: int) -> None:
    with READS_LOCK:
        reads = reads_today()
        reads[str(collector_id)] = reads.get(str(collector_id), 0) + 1
        try:
            READS_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = READS_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps({"date": datetime.now(BR_TZ).date().isoformat(), "reads": reads}), encoding="utf-8")
            os.replace(tmp, READS_FILE)
        except OSError:
            log.warning("não gravou %s; o limite diário fica sem esta leitura", READS_FILE)


class ProxyError(Exception):
    """O proxy do coletor recusou ou não respondeu."""


class NoLogin(Exception):
    """Coletor sem cookies no painel e sem login salvo no anyx do VPS."""


def load_env(path: Path) -> None:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def database_url() -> str:
    url = (os.getenv("DATABASE_URL") or "").strip()
    if not url:
        raise SystemExit("DATABASE_URL ausente no .env")
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]
    if "+psycopg" in url:
        url = url.replace("postgresql+psycopg://", "postgresql://", 1)
    return url


def pick(obj: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        if isinstance(obj, dict) and name in obj and obj[name] is not None:
            return obj[name]
        if hasattr(obj, name):
            value = getattr(obj, name)
            if value is not None:
                return value
    return default


def as_int(value: Any) -> int:
    if value is None or value is False:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def parse_created_at(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        posted = value
    elif isinstance(value, (int, float)):
        posted = datetime.fromtimestamp(value, tz=timezone.utc)
    else:
        text = str(value).strip()
        if not text:
            return None
        try:
            posted = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            try:
                posted = datetime.strptime(text, "%a %b %d %H:%M:%S %z %Y")
            except ValueError:
                return None
    if posted.tzinfo is None:
        posted = posted.replace(tzinfo=timezone.utc)
    return posted.astimezone(timezone.utc)


def today_start_br() -> datetime:
    now = datetime.now(BR_TZ)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def tweet_id_of(tweet: Any) -> str:
    raw = pick(tweet, "id", "tweet_id", "rest_id", "id_str")
    return str(raw).strip() if raw is not None else ""


def tweet_text_of(tweet: Any) -> str | None:
    text = pick(tweet, "text", "full_text", "content")
    if not text:
        return None
    return str(text).replace("\n", " ")[:280]


def tweet_metrics(tweet: Any) -> dict[str, int]:
    nested = pick(tweet, "public_metrics", "legacy", default={}) or {}
    likes = as_int(
        pick(tweet, "like_count", "favorite_count", "likes", "favourites_count", "likeCount")
        or pick(nested, "like_count", "favorite_count", "likes")
    )
    rts = as_int(
        pick(tweet, "retweet_count", "repost_count", "retweets", "retweetCount")
        or pick(nested, "retweet_count", "repost_count")
    )
    replies = as_int(
        pick(tweet, "reply_count", "replies", "replyCount") or pick(nested, "reply_count")
    )
    quotes = as_int(pick(tweet, "quote_count", "quotes", "quoteCount") or pick(nested, "quote_count"))
    bookmarks = as_int(pick(tweet, "bookmark_count", "bookmarks", "bookmarkCount") or pick(nested, "bookmark_count"))
    raw_views = pick(tweet, "view_count", "impression_count", "impressions", "viewCount") or pick(
        nested, "view_count", "impression_count"
    )
    if raw_views is None:
        raw_views = pick(tweet, "views")
    if isinstance(raw_views, dict):
        raw_views = pick(raw_views, "count", "value")
    views = as_int(raw_views)
    return {
        "like_count": likes,
        "retweet_count": rts,
        "reply_count": replies,
        "quote_count": quotes,
        "bookmark_count": bookmarks,
        "view_count": views,
    }


def tune_anyx() -> None:
    """Ajustes que o anyx não expõe: --no-sandbox como root e mais espera pela timeline.

    Mexe em detalhes internos do anyx (testado na 0.1.5). Se a versão do VPS for
    outra e o detalhe não existir, só registra no log e segue com o padrão.
    """
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        try:
            from anyx.browsers import chromium

            real = chromium.subprocess
            if not getattr(real, "robo_no_sandbox", False):

                class _NoSandbox:
                    robo_no_sandbox = True

                    def __getattr__(self, name: str) -> Any:
                        return getattr(real, name)

                    @staticmethod
                    def Popen(args: Any, *rest: Any, **kwargs: Any) -> Any:
                        if isinstance(args, list) and args and "--no-sandbox" not in args:
                            args = [args[0], "--no-sandbox", *args[1:]]
                        return real.Popen(args, *rest, **kwargs)

                chromium.subprocess = _NoSandbox()
            log.info("rodando como root: Chrome com --no-sandbox")
        except (ImportError, AttributeError):
            log.warning("rodando como root e não deu para pôr --no-sandbox nesta versão do anyx")
    try:
        from anyx import read

        original = read._collect
        if "first_wait" in inspect.signature(original).parameters and not getattr(original, "robo_wait", False):

            def _collect(*args: Any, **kwargs: Any) -> Any:
                kwargs.setdefault("first_wait", TIMELINE_WAIT)
                return original(*args, **kwargs)

            _collect.robo_wait = True
            read._collect = _collect
    except (ImportError, AttributeError, TypeError, ValueError):
        log.warning("não deu para aumentar a espera da timeline nesta versão do anyx")


def slow_pages(session: Any) -> None:
    """Mais tempo para cada página carregar (o anyx fixa 45 s)."""
    ctx = getattr(session, "ctx", None)
    if ctx is not None and callable(getattr(ctx, "set_default_timeout", None)):
        ctx.set_default_timeout(PAGE_TIMEOUT_MS)


def block_reason(exc: BaseException) -> str | None:
    """Motivo legível quando o X não entregou a página ao coletor; None para outros erros."""
    kind = type(exc).__name__
    text = str(exc)
    if "nothing loaded" in text:
        return "o X não entregou a timeline (nenhum UserTweets)"
    if "Timeout" in kind or "Timeout" in text:
        return "a página do X não carregou a tempo"
    if kind == "UIChanged":
        return "o X mostrou outra página (desafio \"Verify you are human\"?)"
    return None


def fetch_timeline(session: Any, username: str) -> list[Any]:
    """iter_timeline com uma segunda tentativa; erros que não melhoram tentando sobem direto."""
    for attempt in range(1, TIMELINE_TRIES + 1):
        try:
            return iter_timeline(session, username)
        except Exception as exc:
            if attempt == TIMELINE_TRIES or type(exc).__name__ in NO_RETRY:
                raise
            log.warning("@%s timeline falhou (%s); tentando de novo em %ss", username, exc, TIMELINE_RETRY_PAUSE)
            time.sleep(TIMELINE_RETRY_PAUSE)
    return []


def iter_timeline(session: Any, username: str) -> list[Any]:
    result = session.timeline(username, limit=TWEET_LIMIT)
    if result is None:
        return []
    if isinstance(result, dict):
        for key in ("data", "tweets", "items", "timeline"):
            rows = result.get(key)
            if isinstance(rows, list):
                return rows[:TWEET_LIMIT]
        return []
    if isinstance(result, list):
        return result[:TWEET_LIMIT]
    tweets = getattr(result, "tweets", None)
    if isinstance(tweets, list):
        return tweets[:TWEET_LIMIT]
    try:
        return list(result)[:TWEET_LIMIT]
    except TypeError:
        return []


def already_alerted(cur, key: str) -> bool:
    cur.execute("SELECT 1 FROM alerts_sent WHERE dedupe_key = %s LIMIT 1", (key,))
    return cur.fetchone() is not None


def mark_alerted(cur, key: str, kind: str, text: str, owner_id: int | None = None) -> None:
    cur.execute(
        """
        INSERT INTO alerts_sent (owner_id, dedupe_key, kind, detail, sent_at)
        VALUES (%s, %s, %s, %s, NOW())
        ON CONFLICT (dedupe_key) DO NOTHING
        """,
        (owner_id, key, kind, text[:500]),
    )


def alert_once(cur, key: str, kind: str, text: str, owner_id: int | None = None) -> bool:
    """Registra em alerts_sent; a mesma chave nunca sai duas vezes. owner_id = dono da conta (NULL nos
    alertas do sistema). O painel (app/push.py) avisa por push e mostra no sininho e em Alertas."""
    if already_alerted(cur, key):
        return False
    mark_alerted(cur, key, kind, text, owner_id)
    return True


def view_level(views: int) -> tuple[str, int, str] | None:
    """O maior nível de VIEW_LEVELS que o post passou, ou None abaixo do primeiro."""
    reached = [level for level in VIEW_LEVELS if views > level[1]]
    return reached[-1] if reached else None


def view_alert_key(account_id: int, tweet_id: str, suffix: str) -> str:
    return f"viral:{account_id}:{tweet_id}" + (f":{suffix}" if suffix else "")


def fmt_int(value: int) -> str:
    return f"{value:,}".replace(",", ".")


def views_drop(hoje: int, ontem: int) -> int | None:
    """% de queda quando hoje ficou mais de QUEDA_LIMITE abaixo de ontem; None se não caiu."""
    if ontem < QUEDA_MIN_ONTEM or hoje >= ontem * (1 - QUEDA_LIMITE):
        return None
    return round((1 - hoje / ontem) * 100)


def check_views_drop(cur, account: dict[str, Any], today: date, now: datetime | None = None) -> bool:
    now = now or datetime.now(BR_TZ)
    if now.hour < QUEDA_HORA:
        return False
    yesterday = today - timedelta(days=1)
    cur.execute(
        "SELECT metric_date, views FROM daily_metrics WHERE account_id = %s AND metric_date IN (%s, %s)",
        (account["id"], today, yesterday),
    )
    views = {row["metric_date"]: as_int(row["views"]) for row in cur.fetchall()}
    hoje, ontem = views.get(today, 0), views.get(yesterday, 0)
    pct = views_drop(hoje, ontem)
    if pct is None:
        return False
    username = account["username"]
    text = (
        "📉 Queda de views\n"
        f"👤 Conta: {account.get('display_name') or username} (@{username})\n"
        f"👁 Hoje: {hoje}\n"
        f"📅 Ontem: {ontem}\n"
        f"🔻 Queda: {pct}%"
    )
    sent = alert_once(cur, f"queda:{account['id']}:{today.isoformat()}", "queda", text, account.get("owner_id"))
    if sent:
        log.info("alerta queda @%s: %s -> %s (-%s%%)", username, ontem, hoje, pct)
    return sent


def error_summary(errors: list[tuple[str, str, str]]) -> str:
    lines = ["❌ Erro na leitura"]
    for who, step, message in errors[:10]:
        lines.append(f"• {who} — {step}: {message[:160]}")
    if len(errors) > 10:
        lines.append(f"… e mais {len(errors) - 10}.")
    return "\n".join(lines)


def alert_errors(cur, errors: list[tuple[str, str, str]]) -> bool:
    """Uma mensagem por hora para o mesmo conjunto de falhas (conta + etapa)."""
    if not errors:
        return False
    hour = datetime.now(BR_TZ).strftime("%Y-%m-%dT%H")
    which = hashlib.sha1("|".join(sorted(f"{who}:{step}" for who, step, _ in errors)).encode()).hexdigest()[:12]
    return alert_once(cur, f"erro:{hour}:{which}", "erro", error_summary(errors))


def alert_offline(step: str, exc: BaseException) -> None:
    """Banco fora: sem alerts_sent não há como avisar o painel; fica no log do cron (uma vez por hora)
    e o vigia do painel avisa "Robô parado" quando o batimento atrasar."""
    hour = datetime.now(BR_TZ).strftime("%Y-%m-%dT%H")
    try:
        if OFFLINE_STAMP.read_text(encoding="utf-8").strip() == hour:
            return
    except OSError:
        pass
    log.error("%s", error_summary([("robô", step, f"{type(exc).__name__}: {exc}")]))
    try:
        OFFLINE_STAMP.write_text(hour, encoding="utf-8")
    except OSError:
        log.warning("não gravou %s", OFFLINE_STAMP)


def record_run(cur, started: datetime, status: str, error: str | None, stats: dict[str, Any]) -> None:
    """Batimento da rodada (o vigia do painel lê) e limpeza dos antigos."""
    cur.execute(
        """
        INSERT INTO sync_runs (account_id, folder_id, kind, status, started_at, finished_at, error, stats)
        VALUES (NULL, NULL, 'tweets', %s, %s, NOW(), %s, %s::json)
        """,
        (status, started, error, json.dumps(stats)),
    )
    cur.execute(
        """
        DELETE FROM sync_runs
        WHERE kind = 'tweets' AND account_id IS NULL AND started_at < NOW() - make_interval(days => %s)
        """,
        (RUNS_KEEP_DAYS,),
    )


def upsert_sample(cur, account_id: int, tweet_id: str, posted_local: datetime, metrics: dict[str, int], preview: str | None) -> None:
    views = metrics["view_count"]
    cur.execute(
        """
        INSERT INTO tweet_samples (
            account_id, tweet_id, posted_at, timezone_used, hour_local, weekday,
            text_preview, like_count, retweet_count, reply_count, quote_count,
            bookmark_count, impression_count, view_count, collected_at
        ) VALUES (
            %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s,
            %s, %s, %s, NOW()
        )
        ON CONFLICT (account_id, tweet_id) DO UPDATE SET
            posted_at = EXCLUDED.posted_at,
            timezone_used = EXCLUDED.timezone_used,
            hour_local = EXCLUDED.hour_local,
            weekday = EXCLUDED.weekday,
            text_preview = EXCLUDED.text_preview,
            like_count = EXCLUDED.like_count,
            retweet_count = EXCLUDED.retweet_count,
            reply_count = EXCLUDED.reply_count,
            quote_count = EXCLUDED.quote_count,
            bookmark_count = EXCLUDED.bookmark_count,
            impression_count = EXCLUDED.impression_count,
            view_count = EXCLUDED.view_count,
            collected_at = NOW()
        """,
        (
            account_id,
            tweet_id,
            posted_local,
            "America/Sao_Paulo",
            posted_local.hour,
            posted_local.weekday(),
            preview,
            metrics["like_count"],
            metrics["retweet_count"],
            metrics["reply_count"],
            metrics["quote_count"],
            metrics.get("bookmark_count", 0),
            views,
            views,
        ),
    )


def recompute_today(cur, account_id: int, day) -> None:
    cur.execute(
        """
        INSERT INTO daily_metrics (
            account_id, metric_date, source, posts_count, likes, retweets, replies,
            quotes, bookmarks, views, impressions, collected_at
        )
        SELECT
            %s,
            %s,
            'public_api',
            COUNT(*),
            COALESCE(SUM(like_count), 0),
            COALESCE(SUM(retweet_count), 0),
            COALESCE(SUM(reply_count), 0),
            COALESCE(SUM(quote_count), 0),
            COALESCE(SUM(bookmark_count), 0),
            COALESCE(SUM(view_count), 0),
            COALESCE(SUM(view_count), 0),
            NOW()
        FROM tweet_samples
        WHERE account_id = %s
          AND (posted_at AT TIME ZONE 'America/Sao_Paulo')::date = %s
          -- faixa larga em UTC só para o índice (account_id, posted_at) entrar; o filtro exato é o de cima
          AND posted_at >= %s::date - INTERVAL '1 day'
          AND posted_at < %s::date + INTERVAL '2 days'
        ON CONFLICT (account_id, metric_date) DO UPDATE SET
            posts_count = EXCLUDED.posts_count,
            likes = EXCLUDED.likes,
            retweets = EXCLUDED.retweets,
            replies = EXCLUDED.replies,
            quotes = EXCLUDED.quotes,
            bookmarks = EXCLUDED.bookmarks,
            views = EXCLUDED.views,
            impressions = EXCLUDED.impressions,
            collected_at = NOW()
        """,
        (account_id, day, account_id, day, day, day),
    )


def fetch_followers(session: Any, username: str) -> int | None:
    """Lê o perfil e devolve o nº de seguidores (None se não conseguir)."""
    profile = None
    for method in ("user", "get_user", "profile", "user_by_username"):
        fn = getattr(session, method, None)
        if not callable(fn):
            continue
        try:
            profile = fn(username)
        except Exception as exc:
            # Limite, sessão caída ou proxy fora valem para o coletor inteiro: sobe para run_pair.
            if type(exc).__name__ in NO_RETRY or block_reason(exc) or "ERR_PROXY" in str(exc) or "ERR_TUNNEL" in str(exc):
                raise
            log.exception("@%s %s() falhou", username, method)
            continue
        if profile is not None:
            break
    if profile is None:
        return None
    nested = pick(profile, "public_metrics", "legacy", "stats", default={}) or {}
    raw = pick(
        profile, "followers", "followers_count", "follower_count", "normal_followers_count"
    )
    if raw is None:
        raw = pick(nested, "followers", "followers_count", "follower_count", "normal_followers_count")
    if isinstance(raw, dict):
        raw = pick(raw, "count", "value")
    if raw is None:
        return None
    return as_int(raw)


def upsert_followers(cur, account_id: int, day, followers: int) -> None:
    """Grava seguidores em daily_metrics (hoje), mesmo sem tweets no dia."""
    cur.execute(
        """
        INSERT INTO daily_metrics (
            account_id, metric_date, source, posts_count, likes, retweets, replies,
            quotes, bookmarks, views, impressions,
            followers_count, collected_at
        ) VALUES (%s, %s, 'public_api', 0, 0, 0, 0, 0, 0, 0, 0, %s, NOW())
        ON CONFLICT (account_id, metric_date) DO UPDATE SET
            followers_count = EXCLUDED.followers_count,
            collected_at = NOW()
        """,
        (account_id, day, followers),
    )


def pause() -> None:
    time.sleep(random.uniform(*PAUSE_RANGE))


# ---- repassador de proxy ----


def _read_head(sock: socket.socket, limit: int = 65536) -> bytes:
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data += chunk
        if len(data) > limit:
            raise ProxyError("cabeçalho grande demais")
    return data


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    data = b""
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise ProxyError("o proxy fechou a conexão")
        data += chunk
    return data


def _pipe(src: socket.socket, dst: socket.socket) -> None:
    try:
        while True:
            chunk = src.recv(65536)
            if not chunk:
                break
            dst.sendall(chunk)
    except OSError:
        pass
    finally:
        for sock in (src, dst):
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


class ProxyRelay:
    """Proxy HTTP local, sem senha, que abre cada túnel CONNECT no proxy do coletor com a senha."""

    def __init__(self, upstream: str, timeout: float = 20.0):
        parts = urlsplit(upstream)
        self.scheme = parts.scheme.lower()
        if self.scheme not in ("http", "https", "socks5", "socks5h") or not parts.hostname or not parts.port:
            raise ProxyError("proxy inválido no banco (use o formato da aba Coletores)")
        self.host, self.port = parts.hostname, parts.port
        self.user = unquote(parts.username) if parts.username is not None else None
        self.password = unquote(parts.password) if parts.password is not None else ""
        self.timeout = timeout
        self.failures: list[str] = []
        self.server = socket.create_server(("127.0.0.1", 0))
        self.url = f"http://127.0.0.1:{self.server.getsockname()[1]}"
        threading.Thread(target=self._serve, daemon=True).start()

    def open_tunnel(self, host: str, port: int) -> socket.socket:
        try:
            sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        except OSError as exc:
            raise ProxyError(f"não conectou em {self.host}:{self.port} ({exc})") from exc
        try:
            if self.scheme.startswith("socks5"):
                self._socks5(sock, host, port)
            else:
                if self.scheme == "https":
                    sock = ssl.create_default_context().wrap_socket(sock, server_hostname=self.host)
                self._connect(sock, host, port)
        except (OSError, ProxyError) as exc:
            sock.close()
            raise exc if isinstance(exc, ProxyError) else ProxyError(str(exc)) from exc
        sock.settimeout(None)
        return sock

    def _connect(self, sock: socket.socket, host: str, port: int) -> None:
        lines = [f"CONNECT {host}:{port} HTTP/1.1", f"Host: {host}:{port}"]
        if self.user is not None:
            token = base64.b64encode(f"{self.user}:{self.password}".encode()).decode()
            lines.append(f"Proxy-Authorization: Basic {token}")
        sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
        status = _read_head(sock).split(b"\r\n", 1)[0].decode("latin-1")
        code = status.split(" ")[1] if len(status.split(" ")) > 1 else "?"
        if code == "407":
            raise ProxyError("o proxy recusou usuário/senha (407)")
        if code != "200":
            raise ProxyError(f"o proxy respondeu {status or 'nada'}")

    def _socks5(self, sock: socket.socket, host: str, port: int) -> None:
        sock.sendall(b"\x05\x01\x02" if self.user is not None else b"\x05\x01\x00")
        version, method = _recv_exact(sock, 2)
        if version != 5 or method == 0xFF:
            raise ProxyError("o proxy socks5 recusou o método de login")
        if method == 2:
            user, password = (self.user or "").encode(), self.password.encode()
            sock.sendall(b"\x01" + bytes([len(user)]) + user + bytes([len(password)]) + password)
            if _recv_exact(sock, 2)[1] != 0:
                raise ProxyError("o proxy socks5 recusou usuário/senha")
        name = host.encode("idna")
        sock.sendall(b"\x05\x01\x00\x03" + bytes([len(name)]) + name + port.to_bytes(2, "big"))
        reply = _recv_exact(sock, 4)
        if reply[1] != 0:
            raise ProxyError(f"o proxy socks5 não abriu {host}:{port} (código {reply[1]})")
        skip = {1: 4, 4: 16}.get(reply[3])
        _recv_exact(sock, (skip if skip is not None else _recv_exact(sock, 1)[0]) + 2)

    def probe(self) -> None:
        """Abre e fecha um túnel até o x.com antes de gastar um navegador."""
        self.open_tunnel("x.com", 443).close()

    def _serve(self) -> None:
        while True:
            try:
                client, _ = self.server.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(client,), daemon=True).start()

    def _handle(self, client: socket.socket) -> None:
        try:
            first = _read_head(client).split(b"\r\n", 1)[0].decode("latin-1").split(" ")
            if len(first) < 2 or first[0] != "CONNECT":
                # O X é todo https; pedido http puro não passa.
                client.sendall(b"HTTP/1.1 405 Method Not Allowed\r\nContent-Length: 0\r\n\r\n")
                client.close()
                return
            host, _, port = first[1].rpartition(":")
            upstream = self.open_tunnel(host.strip("[]"), int(port))
        except (OSError, ValueError, ProxyError) as exc:
            if isinstance(exc, ProxyError):
                self.failures.append(str(exc))
            try:
                client.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
            except OSError:
                pass
            client.close()
            return
        client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
        threading.Thread(target=_pipe, args=(upstream, client), daemon=True).start()
        _pipe(client, upstream)

    def close(self) -> None:
        try:
            self.server.close()
        except OSError:
            pass


# ---- coletores no anyx ----


def stop_browser(handle: str) -> None:
    """Fecha o Chrome que o anyx deixou aberto para o coletor (RAM e proxy velho)."""
    try:
        from anyx.browsers.chromium import Chromium

        Chromium.stop(handle)
    except Exception:
        log.warning("não fechou o navegador de @%s", handle, exc_info=True)


def prepare_collector(collector: dict[str, Any], relay_url: str | None) -> None:
    """Põe os cookies do painel no cofre do anyx e aponta o Chrome do coletor para o repassador."""
    from anyx import config as anyx_config
    from anyx import vault

    handle = collector["handle"]
    if collector.get("cookies"):
        vault.save(handle, json.loads(collector["cookies"]))
    elif not vault.load(handle):
        raise NoLogin("sem cookies no painel e sem login no anyx do VPS")
    cfg = anyx_config.load()
    entry = cfg["accounts"].setdefault(handle.lower(), {})
    entry["browser"] = "chromium"
    if relay_url:
        entry["proxy"] = relay_url
    else:
        entry.pop("proxy", None)
    anyx_config.save(cfg)
    # Confere o que ficou gravado: um coletor com proxy nunca pode abrir o Chrome sem ele.
    saved = (anyx_config.load().get("accounts") or {}).get(handle.lower(), {}).get("proxy")
    if saved != relay_url:
        raise ProxyError("o anyx não guardou o proxy do coletor; leitura cancelada para não sair pelo IP do VPS")


@contextmanager
def reader(anyx: Any, collector: dict[str, Any]) -> Iterator[tuple[Any, ProxyRelay | None]]:
    """Sessão do anyx logada no coletor, saindo pelo proxy dele. Fecha tudo no fim.

    Gravar o proxy no config.json do anyx e abrir o Chrome (que lê esse arquivo) acontecem juntos sob
    LAUNCH_LOCK; com o Chrome aberto, o proxy dele já está fixo e a leitura corre em paralelo.
    """
    handle = collector["handle"]
    stop_browser(handle)  # sobra de uma rodada que caiu no meio
    relay = ProxyRelay(collector["proxy"]) if collector.get("proxy") else None
    try:
        if relay:
            relay.probe()
        with ExitStack() as stack:
            with LAUNCH_LOCK:
                prepare_collector(collector, relay.url if relay else None)
                session = stack.enter_context(anyx.Session(handle))
            slow_pages(session)
            yield session, relay
    finally:
        stop_browser(handle)
        if relay:
            relay.close()


def classify(exc: BaseException, relay: ProxyRelay | None = None) -> str:
    """limite, verificacao, sessao, proxy, bloqueio (culpa do coletor), conta (da conta lida) ou erro."""
    kind = type(exc).__name__
    text = str(exc)
    if any(mark in text for mark in HUMAN_CHALLENGE_MARKS):
        return "verificacao"
    if kind == "ProxyError" or "ERR_PROXY" in text or "ERR_TUNNEL" in text:
        return "proxy"
    if kind == "RateLimited":
        return "limite"
    if kind in ("SessionExpired", "WrongAccount", "NoLogin") or (kind == "UsageError" and "cookies" in text):
        return "sessao"
    if block_reason(exc):
        # Com o repassador falhando no meio, a página não carrega por causa do proxy, não do X.
        return "proxy" if relay is not None and relay.failures else "bloqueio"
    if kind == "NotFound":
        return "conta"
    return "erro"


def pause_until(kind: str, exc: BaseException, now: datetime) -> datetime:
    reset = getattr(exc, "reset", None)
    if kind == "limite" and reset:
        return datetime.fromtimestamp(int(reset) + 60, tz=timezone.utc)
    return now + PAUSE_FOR[kind]


def collector_alert(kind: str, collector: dict[str, Any], account: dict[str, Any], exc: BaseException,
                    until: datetime, relay: ProxyRelay | None) -> str:
    who = f"@{collector['handle']}"
    target = f"@{account['username']}"
    back = until.astimezone(BR_TZ).strftime("%d/%m %H:%M")
    detail = (relay.failures[-1] if kind == "proxy" and relay and relay.failures else f"{type(exc).__name__}: {exc}")[:300]
    if kind == "limite":
        return f"⛔ X limitou o coletor {who} (429) ao ler {target}.\nRodada parada. {who} volta às {back}."
    if kind == "bloqueio":
        return (
            f"🚫 X bloqueou o coletor {who} ao ler {target}.\n"
            f"Motivo: {block_reason(exc)}\n"
            f"{who} pausado até {back}. Se repetir, troque o proxy ou o coletor."
        )
    if kind == "verificacao":
        return (
            f"🛡️ X pediu verificação de humanidade ao coletor {who} ao ler {target} (Cloudflare).\n"
            f"{who} pausado até {back}. Para liberar: entre no x.com com {who} (de preferência pelo "
            f"mesmo proxy), resolva a verificação e aperte Testar na aba Coletores."
        )
    if kind == "sessao":
        return (
            f"🔑 Coletor {who} deslogado ou com cookies inválidos.\n"
            f"Cole cookies novos na aba Coletores (ele volta na hora).\nDetalhe: {detail}"
        )
    return f"🔌 Proxy do coletor {who} não respondeu.\nDetalhe: {detail}\n{who} pausado até {back}."


def load_collectors(cur, rest: timedelta | None = None) -> tuple[list[dict[str, Any]], int, datetime | None, dict[str, int]]:
    """Coletores livres (os usados há mais tempo primeiro), total cadastrado, próximo fim de castigo e
    quantos ficaram de fora por descanso ou pelo limite do dia.

    Livre = ligado, fora de castigo, abaixo de daily_max() leituras hoje e, com rest, sem ler há pelo
    menos esse tempo. Um coletor com o mesmo @ de uma conta monitorada nunca é usado: o painel já
    recusa, isto é a segunda trava para o robô não entrar logado numa conta que gera dinheiro.
    """
    cur.execute(
        """
        SELECT c.id, c.handle, c.proxy, c.cookies, c.is_active, c.paused_until, c.last_used_at
        FROM collectors c
        WHERE NOT EXISTS (SELECT 1 FROM x_accounts a WHERE lower(a.username) = lower(c.handle))
        ORDER BY c.last_used_at NULLS FIRST, c.id
        """
    )
    rows = list(cur.fetchall())
    now = datetime.now(timezone.utc)
    reads, cap = reads_today(), daily_max()
    free, held = [], {"descansando": 0, "no_limite": 0}
    for row in rows:
        if not row["is_active"] or (row["paused_until"] and row["paused_until"] > now):
            continue
        if reads.get(str(row["id"]), 0) >= cap:
            held["no_limite"] += 1
        elif rest and row["last_used_at"] and now - row["last_used_at"] < rest:
            held["descansando"] += 1
        else:
            free.append(row)
    paused = [r["paused_until"] for r in rows if r["is_active"] and r["paused_until"] and r["paused_until"] > now]
    return free, len(rows), min(paused) if paused else None, held


def load_accounts(cur, limit: int) -> list[dict[str, Any]]:
    """Contas do admin (as únicas lidas pelos coletores), as coletadas há mais tempo primeiro."""
    cur.execute(
        """
        SELECT a.id, a.owner_id, a.username, a.display_name, a.followers_at
        FROM x_accounts a
        JOIN users u ON u.id = a.owner_id
        WHERE a.is_active IS TRUE
          AND COALESCE(a.is_archived, FALSE) IS FALSE
          AND u.is_admin IS TRUE
        ORDER BY a.collected_at NULLS FIRST, a.id
        LIMIT %s
        """,
        (limit,),
    )
    return list(cur.fetchall())


def mark_collector(cur, collector_id: int, status: str, error: str | None, until: datetime | None) -> None:
    cur.execute(
        """
        UPDATE collectors
        SET last_used_at = NOW(), last_status = %s, last_error = %s, paused_until = %s
        WHERE id = %s
        """,
        (status, error, until, collector_id),
    )


def mark_collected(cur, account_id: int) -> None:
    cur.execute("UPDATE x_accounts SET collected_at = NOW() WHERE id = %s", (account_id,))


def followers_due(account: dict[str, Any], now: datetime) -> bool:
    last = account.get("followers_at")
    return last is None or now - last >= FOLLOWERS_EVERY


def read_account(cur, session: Any, account: dict[str, Any], midnight: datetime) -> int:
    """Lê uma conta monitorada pela sessão do coletor. Devolve os tweets de hoje gravados."""
    username = account["username"]
    if followers_due(account, datetime.now(timezone.utc)):
        followers = fetch_followers(session, username)
        save_followers(cur, account, midnight.date(), followers)
        pause()
    return save_tweets(cur, account, fetch_timeline(session, username), midnight)


def save_followers(cur, account: dict[str, Any], today: date, followers: int | None) -> None:
    username = account["username"]
    if followers is not None:
        upsert_followers(cur, account["id"], today, followers)
        log.info("@%s — %s seguidores", username, followers)
    else:
        log.warning("@%s — seguidores não encontrados no perfil", username)
    cur.execute("UPDATE x_accounts SET followers_at = NOW() WHERE id = %s", (account["id"],))


def save_tweets(cur, account: dict[str, Any], tweets: list[Any], midnight: datetime) -> int:
    """Grava os posts de hoje, avisa os níveis de views, refaz o dia e marca a conta como lida."""
    username = account["username"]
    account_id = account["id"]
    owner_id = account.get("owner_id")
    display = account.get("display_name") or username
    today = midnight.date()
    saved = 0
    for tweet in tweets:
        tid = tweet_id_of(tweet)
        if not tid:
            continue
        posted = parse_created_at(pick(tweet, "created_at", "createdAt", "date", "posted_at"))
        if posted is None:
            continue
        local = posted.astimezone(BR_TZ)
        if local < midnight:
            continue
        metrics = tweet_metrics(tweet)
        upsert_sample(cur, account_id, tid, local, metrics, tweet_text_of(tweet))
        saved += 1
        views = metrics["view_count"]
        level = view_level(views)
        if level is not None:
            suffix, _, title = level
            when = local.strftime("%d/%m/%Y %H:%M")
            link = link_of(username, tid)
            text = (
                f"{title}\n"
                f"👤 Conta: {display} (@{username})\n"
                f"👁 Views: {fmt_int(views)}\n"
                f"⏰ Horário do post: {when}\n"
                f"❤️ Curtidas: {metrics['like_count']}\n"
                f"🔁 Retweets: {metrics['retweet_count']}\n"
                f"🔗 Link do Post: {link}"
            )
            if alert_once(cur, view_alert_key(account_id, tid, suffix), "viral", text, owner_id):
                log.info("alerta views (%s) @%s tweet %s (%s)", suffix or "viral", username, tid, views)
        check_post_milestone(cur, account, tid, views, link_of(username, tid))
    recompute_today(cur, account_id, today)
    check_views_drop(cur, account, today)
    check_day_totals(cur, owner_id, today)
    mark_collected(cur, account_id)
    return saved


def link_of(username: str, tweet_id: str) -> str:
    return f"https://x.com/{username}/status/{tweet_id}"


_NOTIFY_COLUMN: list[bool] = []


def has_notify_prefs(cur) -> bool:
    """Painel antigo, sem user_settings.notify_prefs: sem marcos (sem erro que desfaça a transação)."""
    if not _NOTIFY_COLUMN:
        cur.execute(
            "SELECT 1 FROM information_schema.columns WHERE table_name = 'user_settings' AND column_name = 'notify_prefs'"
        )
        _NOTIFY_COLUMN.append(cur.fetchone() is not None)
    return _NOTIFY_COLUMN[0]


def notify_prefs(cur, owner_id: int | None) -> dict[str, Any]:
    """Passo dos marcos (por post e do dia) e a meta do dia do dono; 0 = desligado."""
    prefs = {"post_step": 0, "day_step": 0, "goal": 0}
    if owner_id is None or not has_notify_prefs(cur):
        return prefs
    cur.execute("SELECT notify_prefs, impression_goal FROM user_settings WHERE owner_id = %s", (owner_id,))
    row = cur.fetchone()
    if row is None:
        return prefs
    try:
        saved = json.loads(row["notify_prefs"] or "{}")
    except ValueError:
        saved = {}
    prefs["post_step"] = as_int(saved.get("post_step"))
    prefs["day_step"] = as_int(saved.get("day_step"))
    prefs["goal"] = as_int(row["impression_goal"])
    return prefs


def milestone(views: int, step: int) -> int:
    """Quantos passos inteiros as views já passaram (0 = nenhum)."""
    return views // step if step > 0 else 0


def check_post_milestone(cur, account: dict[str, Any], tweet_id: str, views: int, link: str) -> bool:
    """A cada post_step views num post (10 mil, 20 mil…): só o maior marco alcançado sai."""
    owner_id = account.get("owner_id")
    step = notify_prefs(cur, owner_id)["post_step"]
    reached = milestone(views, step)
    if not reached:
        return False
    username = account["username"]
    text = (
        f"📈 Post passou de {fmt_int(reached * step)} views\n"
        f"👤 Conta: {account.get('display_name') or username} (@{username})\n"
        f"👁 Views: {fmt_int(views)}\n"
        f"🔗 Link do Post: {link}"
    )
    return alert_once(cur, f"marco_post:{account['id']}:{tweet_id}:{step}:{reached}", "marco_post", text, owner_id)


def day_views(cur, owner_id: int, today: date) -> int:
    cur.execute(
        """
        SELECT COALESCE(SUM(d.views), 0) AS total
        FROM daily_metrics d JOIN x_accounts a ON a.id = d.account_id
        WHERE a.owner_id = %s AND d.metric_date = %s AND COALESCE(a.is_archived, FALSE) IS FALSE
        """,
        (owner_id, today),
    )
    return as_int(cur.fetchone()["total"])


def check_day_totals(cur, owner_id: int | None, today: date) -> None:
    """Total de views do dia de todas as contas do dono: meta batida (uma vez por dia) e marcos a cada
    day_step (só o maior alcançado)."""
    if owner_id is None:
        return
    prefs = notify_prefs(cur, owner_id)
    if not prefs["goal"] and not prefs["day_step"]:
        return
    total = day_views(cur, owner_id, today)
    day = today.isoformat()
    if prefs["goal"] and total >= prefs["goal"]:
        alert_once(cur, f"meta:{owner_id}:{day}", "meta",
                   f"🎯 Meta do dia batida\n👁 Views hoje: {fmt_int(total)}\n🏁 Meta: {fmt_int(prefs['goal'])}", owner_id)
    reached = milestone(total, prefs["day_step"])
    if reached:
        alert_once(cur, f"marco_dia:{owner_id}:{day}:{prefs['day_step']}:{reached}", "marco_dia",
                   f"📊 Dia passou de {fmt_int(reached * prefs['day_step'])} views\n👁 Views hoje: {fmt_int(total)}", owner_id)


# ---- Apify (contas de quem não é admin) ----

APIFY_API = "https://api.apify.com/v2"
APIFY_ACTOR_DEFAULT = "apidojo~tweet-scraper"
APIFY_INTERVAL_DEFAULT = 60
APIFY_TIMEOUT = 150  # o run-sync do Apify espera até 300 s; aqui bem menos, para não segurar o cron
APIFY_PER_ACCOUNT = 20  # posts pedidos por conta (o mesmo TWEET_LIMIT dos coletores)
APIFY_MAX_ACCOUNTS = 25  # contas por chamada; mais que isso vira lotes em paralelo
APIFY_PARALLEL = 4  # chamadas ao Apify ao mesmo tempo (cada uma é um run do ator)
APIFY_ROUND_ACCOUNTS = 200  # contas de um usuário numa passada
NO_APIFY_TOKEN = "Token do Apify em falta. Cole o seu em Configurações para o robô ler as suas contas."


class ApifyError(Exception):
    """Falha ao chamar o Apify; a mensagem vai para o painel do dono."""


def apify_actor() -> str:
    # Na URL da API o ator vai como "dono~nome"; aceita também "dono/nome".
    return (os.getenv("APIFY_ACTOR") or APIFY_ACTOR_DEFAULT).strip().replace("/", "~")


def apify_interval() -> timedelta:
    return timedelta(minutes=env_int("APIFY_INTERVAL_MIN", APIFY_INTERVAL_DEFAULT, 5, 24 * 60))


def apify_input(handles: list[str]) -> dict[str, Any]:
    return {"twitterHandles": handles, "maxItems": APIFY_PER_ACCOUNT * len(handles), "sort": "Latest"}


def apify_fetch(token: str, handles: list[str]) -> list[dict[str, Any]]:
    """Roda o ator e devolve os itens do dataset (posts). Erros viram ApifyError com o motivo."""
    url = f"{APIFY_API}/acts/{apify_actor()}/run-sync-get-dataset-items?timeout={APIFY_TIMEOUT - 10}"
    req = urllib.request.Request(
        url,
        data=json.dumps(apify_input(handles)).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=APIFY_TIMEOUT) as response:
            body = json.loads(response.read().decode("utf-8") or "[]")
    except urllib.error.HTTPError as exc:
        reason = {
            401: "o Apify recusou o token (401). Confira o token em Configurações.",
            402: "a conta do Apify está sem crédito (402).",
            403: "o Apify negou o acesso (403). O plano gratuito não roda esse ator pela API.",
            404: f"ator {apify_actor()} não encontrado no Apify (404).",
            408: "o Apify demorou demais (408). Tenta de novo na próxima passada.",
            429: "limite de chamadas do Apify (429). Tenta de novo na próxima passada.",
        }.get(exc.code, f"o Apify respondeu {exc.code}.")
        raise ApifyError(reason) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ApifyError(f"sem resposta do Apify ({type(exc).__name__}).") from exc
    except ValueError as exc:
        raise ApifyError("o Apify devolveu algo que não é JSON.") from exc
    if not isinstance(body, list):
        raise ApifyError("o Apify não devolveu a lista de posts.")
    return [item for item in body if isinstance(item, dict)]


REPOST_FIELDS = ("retweetedTweet", "retweeted_tweet", "retweeted_status", "retweetedStatus", "retweet")


def is_repost(item: dict[str, Any]) -> bool:
    """Repost (retweet) que a conta fez: o Apify devolve com a data do repost, mas com o texto e os
    números do post original, que pode ser de outro dia ou de outra conta. Nunca é post da conta, então
    não entra (nem com a data do original: contaria as views de outra pessoa ou o mesmo post duas
    vezes). Citação (quote) é post da própria conta e continua valendo."""
    if item.get("isRetweet") is True or item.get("is_retweet") is True:
        return True
    if any(isinstance(item.get(name), dict) and item.get(name) for name in REPOST_FIELDS):
        return True
    text = pick(item, "text", "full_text", "fullText", default="") or ""
    return str(text).lstrip().startswith("RT @")


def apify_author(item: dict[str, Any]) -> tuple[str, int | None]:
    """(@ em minúsculas, seguidores) do autor do post."""
    author = item.get("author") if isinstance(item.get("author"), dict) else {}
    handle = pick(author, "userName", "username", "screen_name", default="") or ""
    raw = pick(author, "followers", "followers_count", "followersCount")
    return str(handle).lower(), (as_int(raw) if raw is not None else None)


def apify_users(cur) -> list[dict[str, Any]]:
    """Usuários aprovados que não são o admin e têm alguma conta ativa."""
    cur.execute(
        """
        SELECT u.id, s.apify_token, s.apify_run_requested_at,
               COALESCE(s.apify_auto, TRUE) AS apify_auto,
               COALESCE(s.apify_interval_min, %s) AS apify_interval_min
        FROM users u
        LEFT JOIN user_settings s ON s.owner_id = u.id
        WHERE u.is_approved IS TRUE
          AND u.is_admin IS FALSE
          AND EXISTS (
              SELECT 1 FROM x_accounts a
              WHERE a.owner_id = u.id AND a.is_active IS TRUE AND COALESCE(a.is_archived, FALSE) IS FALSE
          )
        ORDER BY u.id
        """,
        (int(apify_interval().total_seconds() // 60),),
    )
    return list(cur.fetchall())


def apify_accounts(cur, owner_id: int, everything: bool, interval_min: int | None = None) -> list[dict[str, Any]]:
    """Contas do usuário para ler agora: todas com o botão, senão as que passaram do intervalo."""
    cur.execute(
        """
        SELECT id, owner_id, username, display_name, followers_at
        FROM x_accounts
        WHERE owner_id = %s
          AND is_active IS TRUE
          AND COALESCE(is_archived, FALSE) IS FALSE
          AND (%s OR collected_at IS NULL OR collected_at < NOW() - make_interval(mins => %s))
        ORDER BY collected_at NULLS FIRST, id
        LIMIT %s
        """,
        (owner_id, everything, interval_min or int(apify_interval().total_seconds() // 60), APIFY_ROUND_ACCOUNTS),
    )
    return list(cur.fetchall())


def set_apify_status(cur, owner_id: int, status: str, detail: str | None) -> None:
    """Resultado da última tentativa no painel do dono; também apaga o pedido do botão."""
    cur.execute(
        """
        INSERT INTO user_settings (owner_id, impression_goal, apify_status, apify_detail, apify_checked_at)
        VALUES (%s, 500, %s, %s, NOW())
        ON CONFLICT (owner_id) DO UPDATE SET
            apify_status = EXCLUDED.apify_status,
            apify_detail = EXCLUDED.apify_detail,
            apify_checked_at = NOW(),
            apify_run_requested_at = NULL
        """,
        (owner_id, status, (detail or "")[:500] or None),
    )


def apify_read(conn, token: str, accounts: list[dict[str, Any]], midnight: datetime) -> int:
    """Lê as contas pelo Apify (lotes de APIFY_MAX_ACCOUNTS) e grava como os coletores: posts de hoje,
    alertas de views, o dia refeito e seguidores (em toda leitura: vêm junto com os posts).

    Devolve os posts de hoje gravados. Falha da chamada sobe como ApifyError antes de gravar o lote;
    conta sem post nenhum não é falha.
    """
    batches = [accounts[start:start + APIFY_MAX_ACCOUNTS] for start in range(0, len(accounts), APIFY_MAX_ACCOUNTS)]
    if len(batches) > 1:
        # Vários lotes: as chamadas vão juntas (cada uma é um run do ator); gravar fica numa conexão só.
        with ThreadPoolExecutor(max_workers=min(APIFY_PARALLEL, len(batches)), thread_name_prefix="apify-lote") as pool:
            answers = list(pool.map(lambda batch: apify_fetch(token, [a["username"] for a in batch]), batches))
    else:
        answers = [apify_fetch(token, [a["username"] for a in batch]) for batch in batches]
    saved = 0
    for batch, items in zip(batches, answers):
        by_author: dict[str, list[dict[str, Any]]] = {}
        followers: dict[str, int] = {}
        for item in items:
            handle, count = apify_author(item)
            if not handle or not tweet_id_of(item):
                continue  # {"noResults": true} e afins
            if count is not None:
                followers[handle] = count  # o repost também traz os seguidores da conta
            if is_repost(item):
                continue
            by_author.setdefault(handle, []).append(item)
        with conn.cursor() as cur:
            for account in batch:
                handle = account["username"].lower()
                if handle in followers:
                    save_followers(cur, account, midnight.date(), followers[handle])
                saved += save_tweets(cur, account, by_author.get(handle, []), midnight)
        conn.commit()
    return saved


def apify_user_round(conn, user: dict[str, Any], midnight: datetime) -> dict[str, Any]:
    """Lê pelo Apify as contas vencidas de um usuário, numa chamada só. Devolve o resumo."""
    owner_id = user["id"]
    requested = user["apify_run_requested_at"] is not None
    if not requested and user.get("apify_auto") is False:
        return {"owner": owner_id, "contas": 0}  # leitura automática desligada: só pelo botão
    with conn.cursor() as cur:
        accounts = apify_accounts(cur, owner_id, requested, user.get("apify_interval_min"))
    conn.commit()
    if not accounts:
        if requested:  # nada para ler (tudo arquivado): só fecha o pedido
            with conn.cursor() as cur:
                set_apify_status(cur, owner_id, "ok", "nenhuma conta ativa para ler")
            conn.commit()
        return {"owner": owner_id, "contas": 0}
    token = (user["apify_token"] or "").strip()
    day = midnight.date().isoformat()
    if not token:
        with conn.cursor() as cur:
            set_apify_status(cur, owner_id, "sem_token", NO_APIFY_TOKEN)
            alert_once(cur, f"apify:{owner_id}:sem_token:{day}", "apify", f"🔑 {NO_APIFY_TOKEN}", owner_id)
            # Sem token a fila não anda; marca como vista para não reavisar a cada minuto.
            for account in accounts:
                mark_collected(cur, account["id"])
        conn.commit()
        log.info("apify: usuário %s sem token (%s contas esperando)", owner_id, len(accounts))
        return {"owner": owner_id, "contas": 0, "erro": "sem_token"}
    try:
        saved = apify_read(conn, token, accounts, midnight)
    except ApifyError as exc:
        conn.rollback()
        with conn.cursor() as cur:
            set_apify_status(cur, owner_id, "erro", str(exc))
            alert_once(cur, f"apify:{owner_id}:erro:{day}", "apify", f"⚠️ Leitura pelo Apify falhou: {exc}", owner_id)
            for account in accounts:  # tenta de novo no próximo intervalo, não a cada minuto
                mark_collected(cur, account["id"])
        conn.commit()
        log.warning("apify: usuário %s: %s", owner_id, exc)
        return {"owner": owner_id, "contas": 0, "erro": str(exc)}
    with conn.cursor() as cur:
        set_apify_status(cur, owner_id, "ok", f"{len(accounts)} contas lidas, {saved} posts de hoje")
    conn.commit()
    log.info("apify: usuário %s — %s contas, %s posts de hoje", owner_id, len(accounts), saved)
    return {"owner": owner_id, "contas": len(accounts), "tweets": saved}


def apify_round(connect, midnight: datetime) -> list[dict[str, Any]]:
    """Todos os usuários do Apify, um de cada vez, cada um com o próprio token. Erro de um não para os outros."""
    results = []
    with connect() as conn:
        with conn.cursor() as cur:
            try:
                users = apify_users(cur)
            except Exception as exc:  # painel antigo, sem as tabelas do multiusuário
                conn.rollback()
                log.warning("apify: sem as tabelas do multiusuário (%s); atualize o painel", type(exc).__name__)
                return []
        conn.commit()
        for user in users:
            try:
                results.append(apify_user_round(conn, user, midnight))
            except Exception:
                conn.rollback()
                log.exception("apify: usuário %s falhou", user["id"])
    return results


def run_pair(conn, anyx: Any, collector: dict[str, Any], account: dict[str, Any], midnight: datetime) -> tuple[str, int, str | None]:
    """Um coletor lê uma conta. Devolve (resultado, tweets gravados, detalhe do erro)."""
    handle, username = collector["handle"], account["username"]
    relay_seen: list[ProxyRelay | None] = [None]
    try:
        with reader(anyx, collector) as (session, relay):
            relay_seen[0] = relay
            with conn.cursor() as cur:
                saved = read_account(cur, session, account, midnight)
                mark_collector(cur, collector["id"], "ok", None, None)
            conn.commit()
        log.info("@%s leu @%s — %s tweets de hoje", handle, username, saved)
        return "ok", saved, None
    except Exception as exc:
        conn.rollback()
        relay = relay_seen[0]
        kind = classify(exc, relay)
        detail = f"{type(exc).__name__}: {exc}"[:500]
        log.warning("@%s ao ler @%s: %s (%s)", handle, username, kind, detail)
        hour = datetime.now(BR_TZ).strftime("%Y-%m-%dT%H")
        with conn.cursor() as cur:
            if kind in PAUSE_FOR:
                until = pause_until(kind, exc, datetime.now(timezone.utc))
                mark_collector(cur, collector["id"], kind, detail, until)
                text = collector_alert(kind, collector, account, exc, until, relay)
                alert_once(cur, f"coletor:{kind}:{collector['id']}:{hour}", "coletor", text)
            else:
                # Culpa da conta lida (suspensa, sumiu) ou erro desconhecido: o coletor segue e a conta
                # vai para o fim da fila, senão uma conta quebrada travaria o rodízio.
                mark_collector(cur, collector["id"], "ok" if kind == "conta" else "erro", None if kind == "conta" else detail, None)
                mark_collected(cur, account["id"])
        conn.commit()
        return kind, 0, detail


def no_collector_alert(cur, total: int, next_free: datetime | None) -> None:
    if total == 0:
        why = "nenhum coletor cadastrado. Adicione um na aba Coletores."
    elif next_free:
        why = f"todos de castigo; o primeiro volta às {next_free.astimezone(BR_TZ).strftime('%d/%m %H:%M')}."
    else:
        why = "todos desligados na aba Coletores."
    now = datetime.now(BR_TZ)
    key = f"sem_coletor:{now.date().isoformat()}:{now.hour // 6}"
    alert_once(cur, key, "coletor", f"⚠️ Robô sem coletor: {why}\nNenhuma timeline foi lida.")


def round_plan(conn) -> tuple[bool, bool]:
    """(rodar?, manual?) conforme a aba Configurações. Apaga o pedido do botão ao aceitá-lo.

    Painel antigo, sem as colunas: roda sempre, como antes (cron a cada 5 min).
    """
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT robo_enabled, robo_interval_min, robo_run_requested_at FROM app_settings WHERE id = 1")
            row = cur.fetchone()
    except Exception as exc:
        conn.rollback()
        log.warning("app_settings sem as opções do robô (%s); rodando sempre", type(exc).__name__)
        return True, False
    if row is None:
        return True, False
    if row["robo_run_requested_at"] is not None:
        with conn.cursor() as cur:
            cur.execute("UPDATE app_settings SET robo_run_requested_at = NULL WHERE id = 1")
        conn.commit()
        return True, True
    if not row["robo_enabled"]:
        return False, False
    with conn.cursor() as cur:
        cur.execute("SELECT MAX(finished_at) AS last FROM sync_runs WHERE kind = 'tweets' AND account_id IS NULL")
        last = cur.fetchone()["last"]
    conn.commit()
    interval = timedelta(minutes=max(1, int(row["robo_interval_min"] or 5)))
    due = last is None or datetime.now(timezone.utc) - last >= interval - INTERVAL_SLACK
    return due, False


def pairs_for(collectors: list[dict[str, Any]], accounts: list[dict[str, Any]], manual: bool) -> list[tuple[dict, dict]]:
    """Automática: uma conta por coletor. Manual: todas as contas, revezando os coletores."""
    if not manual:
        return list(zip(collectors, accounts))
    return [(collectors[i % len(collectors)], account) for i, account in enumerate(accounts)]


def daily_limit_alert(cur, capped: int) -> None:
    key = f"coletor_limite_dia:{datetime.now(BR_TZ).date().isoformat()}"
    alert_once(cur, key, "coletor", (
        f"🧯 Os {capped} coletores livres já fizeram {daily_max()} leituras hoje (limite por coletor).\n"
        "A leitura volta amanhã. Para ler mais, cadastre mais coletores ou suba ROBO_COLLECTOR_DAILY_MAX."
    ))


def lanes_of(pairs: list[tuple[dict, dict]]) -> list[list[tuple[dict, dict]]]:
    """Uma fila por coletor, na ordem dos pares: um coletor nunca abre dois navegadores ao mesmo tempo."""
    lanes: dict[int, list[tuple[dict, dict]]] = {}
    for collector, account in pairs:
        lanes.setdefault(collector["id"], []).append((collector, account))
    return list(lanes.values())


def run_in_parallel(connect, anyx: Any, pairs: list[tuple[dict, dict]], midnight: datetime, deadline: float,
                    size: int) -> list[tuple[dict, dict, str, int, str | None]]:
    """Até `size` filas ao mesmo tempo, cada uma com a própria conexão com o banco.

    Um 429 faz nenhum par novo começar (os que estão no meio terminam); castigo encerra só a fila do
    coletor punido.
    """
    stop = threading.Event()
    done: list[tuple[dict, dict, str, int, str | None]] = []
    done_lock = threading.Lock()

    def lane_job(lane: list[tuple[dict, dict]], first: bool) -> None:
        if not first:
            pause()  # as filas não começam todas no mesmo instante
        with connect() as conn:
            for index, (collector, account) in enumerate(lane):
                if stop.is_set() or time.monotonic() > deadline:
                    return
                if index:
                    pause()
                kind, saved, detail = run_pair(conn, anyx, collector, account, midnight)
                count_read(collector["id"])
                with done_lock:
                    done.append((collector, account, kind, saved, detail))
                if kind == "limite":
                    stop.set()
                    return
                if kind in PAUSE_FOR:
                    return

    lanes = lanes_of(pairs)
    with ThreadPoolExecutor(max_workers=size, thread_name_prefix="leitura") as pool:
        futures = [pool.submit(lane_job, lane, index < size) for index, lane in enumerate(lanes)]
        for future in futures:
            future.result()  # um erro inesperado numa fila aparece aqui (run_pair já trata os do X)
    return done


def collect(conn, anyx: Any, midnight: datetime, manual: bool = False, connect=None) -> dict[str, Any]:
    """Uma rodada: pares coletor + conta, até workers() ao mesmo tempo (um só, sem `connect`)."""
    budget = MANUAL_BUDGET if manual else ROUND_BUDGET
    deadline = time.monotonic() + budget
    with conn.cursor() as cur:
        # A manual (botão Atualizar dados) não espera o descanso: quem apertou quer tudo agora. O limite
        # do dia vale sempre.
        collectors, total, next_free, held = load_collectors(cur, None if manual else collector_rest())
        limit = len(collectors) * (MANUAL_PER_COLLECTOR if manual else 1)
        accounts = load_accounts(cur, limit) if collectors else []
        if not collectors:
            if held["no_limite"] and not held["descansando"]:
                daily_limit_alert(cur, held["no_limite"])
            elif not held["descansando"]:
                no_collector_alert(cur, total, next_free)
    conn.commit()
    size = min(workers(), len(collectors)) if connect else 1
    log.info("%s coletores livres de %s (%s descansando, %s no limite do dia); %s contas nesta rodada, %s por vez%s",
             len(collectors), total, held["descansando"], held["no_limite"], len(accounts), max(1, size),
             " (manual)" if manual else "")
    stats: dict[str, Any] = {"coletores": len(collectors), "contas": 0, "tweets": 0, "falhas": 0, "parada_429": False,
                             "manual": manual, "em_paralelo": max(1, size), **held}
    errors: list[tuple[str, str, str]] = []
    pairs = pairs_for(collectors, accounts, manual)
    if size > 1:
        done = run_in_parallel(connect, anyx, pairs, midnight, deadline, size)
    else:
        done = []
        benched: set[int] = set()  # coletores que tomaram castigo nesta rodada
        for index, (collector, account) in enumerate(pairs):
            if time.monotonic() > deadline:
                log.warning("rodada passou de %ss; o resto fica para a próxima", budget)
                break
            if collector["id"] in benched:
                continue
            if index:
                pause()
            kind, saved, detail = run_pair(conn, anyx, collector, account, midnight)
            count_read(collector["id"])
            done.append((collector, account, kind, saved, detail))
            if kind in PAUSE_FOR:
                benched.add(collector["id"])
            if kind == "limite":
                break
    if len(done) < len(pairs) and not any(kind == "limite" for _, _, kind, _, _ in done):
        log.warning("rodada parou em %s de %s pares (tempo ou castigo); o resto fica para a próxima", len(done), len(pairs))
    for collector, account, kind, saved, detail in done:
        stats["contas"] += 1
        stats["tweets"] += saved
        if kind != "ok":
            stats["falhas"] += 1
        if kind in ("conta", "erro"):
            errors.append((f"@{account['username']}", f"lida por @{collector['handle']}", detail or kind))
        if kind == "limite":
            stats["parada_429"] = True
            log.warning("429 no coletor @%s: nenhum par novo nesta rodada", collector["handle"])
    if errors:
        with conn.cursor() as cur:
            alert_errors(cur, errors)
        conn.commit()
    stats["erros"] = errors
    return stats


def pending_tests(conn) -> list[dict[str, Any]]:
    """Coletores com o botão Testar apertado, o pedido mais antigo primeiro. Painel antigo: nenhum."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT c.id, c.handle, c.proxy, c.cookies, c.is_active, c.paused_until,
                       EXISTS (SELECT 1 FROM x_accounts a WHERE lower(a.username) = lower(c.handle)) AS monitored
                FROM collectors c
                WHERE c.test_requested_at IS NOT NULL
                ORDER BY c.test_requested_at, c.id
                """
            )
            rows = list(cur.fetchall())
        conn.commit()
        return rows
    except Exception as exc:
        conn.rollback()
        log.warning("collectors sem as colunas do teste (%s); atualize o painel", type(exc).__name__)
        return []


def record_test(conn, collector_id: int, status: str, detail: str | None) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE collectors
            SET test_requested_at = NULL, test_status = %s, test_detail = %s, test_at = NOW()
            WHERE id = %s
            """,
            (status, (detail or "")[:500] or None, collector_id),
        )
    conn.commit()


def run_tests(conn, anyx: Any, midnight: datetime, tests: list[dict[str, Any]]) -> None:
    """Botão Testar: cada coletor pedido lê a próxima conta da fila na hora (é uma leitura normal,
    grava os posts e pode dar castigo e alerta como qualquer outra)."""
    for index, collector in enumerate(tests):
        if collector["monitored"]:
            # Segunda trava: nunca entrar logado numa conta que gera dinheiro.
            record_test(conn, collector["id"], "erro", "este @ é uma conta monitorada; o robô não entra nele")
            continue
        with conn.cursor() as cur:
            accounts = load_accounts(cur, 1)
        conn.commit()
        if not accounts:
            record_test(conn, collector["id"], "erro", "nenhuma conta monitorada para ler")
            continue
        if index:
            pause()
        account = accounts[0]
        log.info("teste do coletor @%s lendo @%s", collector["handle"], account["username"])
        kind, saved, detail = run_pair(conn, anyx, collector, account, midnight)
        count_read(collector["id"])
        if kind == "ok":
            text = f"leu @{account['username']}: {saved} posts de hoje"
        else:
            text = f"ao ler @{account['username']}: {detail or kind}"
        record_test(conn, collector["id"], kind, text)


@contextmanager
def single_run() -> Iterator[bool]:
    """True se esta é a única rodada rodando (flock); no Windows não trava."""
    try:
        import fcntl
    except ImportError:
        yield True
        return
    with open(LOCK_FILE, "w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        yield True


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    env_file = Path(os.getenv("ROBO_ENV_FILE") or ROOT / ".env")
    try:
        load_env(env_file)
    except PermissionError:
        log.error("sem permissão para ler %s: dê leitura ao usuário do cron ou use ROBO_ENV_FILE", env_file)
        return 1
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    with single_run() as alone:
        if not alone:
            log.info("rodada anterior ainda em andamento; saindo")
            return 0
        return run_round()


def run_round() -> int:
    started = datetime.now(timezone.utc)
    midnight = today_start_br()

    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError:
        log.error("Instale psycopg: pip install 'psycopg[binary]'")
        return 1

    try:
        # Sem timeout, banco inacessível deixa o robô pendurado e o cron empilha rodadas.
        conn = psycopg.connect(database_url(), row_factory=dict_row, connect_timeout=10)
    except Exception as exc:
        log.exception("conexão com o banco falhou")
        alert_offline("banco", exc)
        return 1

    # Contas dos outros usuários pelo Apify: em paralelo com os coletores, toda passada do cron (cada
    # conta respeita o próprio intervalo) e sem depender da leitura automática do admin.
    connect = lambda: psycopg.connect(database_url(), row_factory=dict_row, connect_timeout=10)  # noqa: E731
    apify = ThreadPoolExecutor(max_workers=1, thread_name_prefix="apify")
    apify_job = apify.submit(apify_round, connect, midnight)
    try:
        return collectors_round(conn, started, midnight)
    finally:
        try:
            apify_job.result()
        except Exception:
            log.exception("leitura pelo Apify falhou")
        apify.shutdown()


APIFY_ADMIN_INTERVAL_DEFAULT = 30
JUST_FAILED = timedelta(minutes=3)


def admin_apify(conn) -> dict[str, Any] | None:
    """Token do Apify do admin e se a leitura venceu (leitura ligada e a última tentativa mais velha que
    app_settings.apify_interval_min). None sem token: o admin lê só pelos coletores, como antes."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT u.id, s.apify_token, s.apify_checked_at, s.apify_status, a.robo_enabled,
                       COALESCE(a.apify_interval_min, %s) AS interval_min
                FROM users u
                JOIN user_settings s ON s.owner_id = u.id
                LEFT JOIN app_settings a ON a.id = 1
                WHERE u.is_admin IS TRUE
                ORDER BY u.id
                LIMIT 1
                """,
                (APIFY_ADMIN_INTERVAL_DEFAULT,),
            )
            row = cur.fetchone()
        conn.commit()
    except Exception as exc:  # painel antigo, sem as colunas: só coletores
        conn.rollback()
        log.warning("sem as opções do Apify do admin (%s); só coletores", type(exc).__name__)
        return None
    if row is None or not (row["apify_token"] or "").strip():
        return None
    interval = timedelta(minutes=max(1, int(row["interval_min"] or APIFY_ADMIN_INTERVAL_DEFAULT)))
    last = row["apify_checked_at"]
    now = datetime.now(timezone.utc)
    due = row["robo_enabled"] is not False and (last is None or now - last >= interval - INTERVAL_SLACK)
    # O painel (read_now) acabou de ver o Apify falhar e deixou o pedido para os coletores.
    just_failed = row["apify_status"] == "erro" and last is not None and now - last < JUST_FAILED
    return {"id": row["id"], "token": row["apify_token"].strip(), "due": due, "just_failed": just_failed}


def admin_accounts(cur) -> list[dict[str, Any]]:
    """Todas as contas ativas do admin (a leitura pelo Apify não tem limite de coletor)."""
    return load_accounts(cur, 100_000)


def apify_admin_read(conn, admin: dict[str, Any], started: datetime, midnight: datetime, manual: bool) -> str | None:
    """Lê as contas do admin pelo Apify. None = deu certo (batimento gravado); texto = motivo da falha,
    e aí os coletores assumem nesta mesma passada."""
    with conn.cursor() as cur:
        accounts = admin_accounts(cur)
    conn.commit()
    log.info("início pelo Apify%s — %s contas, tweets depois de %s", " (Atualizar Dados)" if manual else "",
             len(accounts), midnight.isoformat())
    try:
        saved = apify_read(conn, admin["token"], accounts, midnight)
    except ApifyError as exc:
        conn.rollback()
        reason = str(exc)
        hour = datetime.now(BR_TZ).strftime("%Y-%m-%dT%H")
        with conn.cursor() as cur:
            set_apify_status(cur, admin["id"], "erro", reason + " Os coletores assumiram.")
            alert_once(cur, f"apify:{admin['id']}:erro:{hour}", "apify",
                       f"⚠️ Apify falhou: {reason}\nOs coletores assumiram a leitura.", admin["id"])
        conn.commit()
        log.warning("apify do admin falhou (%s); coletores assumem", reason)
        return reason
    with conn.cursor() as cur:
        set_apify_status(cur, admin["id"], "ok", f"{len(accounts)} contas lidas, {saved} posts de hoje")
        record_run(cur, started, "ok", None, {"via": "apify", "contas": len(accounts), "tweets": saved,
                                               "falhas": 0, "manual": manual})
    conn.commit()
    log.info("fim pelo Apify — %s contas, %s tweets", len(accounts), saved)
    return None


def read_now(owner_id: int) -> str:
    """"Atualizar dados" do painel (app/reader.py): lê as contas do usuário pelo Apify na hora, sem
    esperar o cron. Devolve "ok", "erro" (o motivo fica em user_settings) ou "sem_apify".

    Admin: se o Apify falhar, grava o pedido do robô e o cron chama os coletores no minuto seguinte
    (sem repetir o Apify). Postgres e DATABASE_URL vêm do mesmo .env do painel.
    """
    import psycopg
    from psycopg.rows import dict_row

    started = datetime.now(timezone.utc)
    midnight = today_start_br()
    with psycopg.connect(database_url(), row_factory=dict_row, connect_timeout=10) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT u.id, u.is_admin, s.apify_token, COALESCE(s.apify_interval_min, 60) AS apify_interval_min
                FROM users u LEFT JOIN user_settings s ON s.owner_id = u.id
                WHERE u.id = %s
                """,
                (owner_id,),
            )
            user = cur.fetchone()
        conn.commit()
        if user is None or not (user["apify_token"] or "").strip():
            return "sem_apify"
        if user["is_admin"]:
            error = apify_admin_read(conn, {"id": owner_id, "token": user["apify_token"].strip()}, started, midnight, True)
            if error is not None:
                with conn.cursor() as cur:
                    cur.execute("UPDATE app_settings SET robo_run_requested_at = NOW() WHERE id = 1")
                conn.commit()
                return "erro"
            return "ok"
        result = apify_user_round(conn, {
            "id": owner_id, "apify_token": user["apify_token"], "apify_run_requested_at": started,
            "apify_auto": True, "apify_interval_min": user["apify_interval_min"],
        }, midnight)
        return "erro" if result.get("erro") else "ok"


def collectors_round(conn, started: datetime, midnight: datetime) -> int:
    """As contas do admin: pelo Apify quando ele tem token (coletores em espera), senão ou se o Apify
    falhar, pelos coletores, como sempre foi."""
    import psycopg
    from psycopg.rows import dict_row

    with conn:
        try:
            tests = pending_tests(conn)
            run, manual = round_plan(conn)
            admin = admin_apify(conn)
        except Exception as exc:
            log.exception("não leu as opções do robô")
            alert_offline("banco", exc)
            return 1
        apify_error = None
        if admin is not None:
            # Com Apify, os coletores não seguem o próprio intervalo: só entram se a chamada falhar,
            # e aí leem tudo, como no "Atualizar Dados".
            if manual and admin["just_failed"]:
                apify_error = "o Apify acabou de falhar no Atualizar dados"
            elif manual or admin["due"]:
                apify_error = apify_admin_read(conn, admin, started, midnight, manual)
            run, manual = apify_error is not None, apify_error is not None or manual
        if not run and not tests:
            return 0  # desligado ou dentro do intervalo: sai quieto (o cron chama de novo em 1 min)
        try:
            import anyx
        except ImportError:
            log.error("Instale anyx no VPS: pip install anyx && playwright install chromium")
            return 1
        tune_anyx()
        if tests:
            # Testes primeiro e mesmo com a leitura desligada: quem apertou está olhando a tela.
            try:
                run_tests(conn, anyx, midnight, tests)
            except Exception as exc:
                log.exception("teste de coletor falhou")
                conn.rollback()
                try:  # fecha os pedidos que sobraram, senão eles voltariam a cada minuto
                    with conn.cursor() as cur:
                        cur.execute(
                            "UPDATE collectors SET test_requested_at = NULL, test_status = 'erro', test_detail = %s,"
                            " test_at = NOW() WHERE test_requested_at IS NOT NULL",
                            (f"o teste falhou no robô: {type(exc).__name__}: {exc}"[:500],),
                        )
                    conn.commit()
                except Exception:
                    conn.rollback()
        if not run:
            return 0  # só testes: sem batimento, para não mexer no intervalo da leitura
        log.info("início%s — tweets depois de %s", " (Atualizar Dados)" if manual else "", midnight.isoformat())
        try:
            # Cada leitura em paralelo usa a própria conexão com o banco (psycopg não é de várias threads).
            connect = lambda: psycopg.connect(database_url(), row_factory=dict_row, connect_timeout=10)  # noqa: E731
            stats = collect(conn, anyx, midnight, manual, connect)
        except Exception as exc:
            log.exception("rodada falhou")
            message = f"{type(exc).__name__}: {exc}"
            if "collectors" in message and ("does not exist" in message or "não existe" in message):
                message += " — atualize o painel (ele cria a tabela collectors ao iniciar)"
            try:
                conn.rollback()
                with conn.cursor() as cur:
                    alert_errors(cur, [("robô", "rodada", message)])
                    record_run(cur, started, "failed", message[:500], {"etapa": "rodada"})
                conn.commit()
            except Exception:
                log.exception("banco caiu junto")
                alert_offline("rodada", exc)
            return 1
        errors = stats.pop("erros")
        stats["via"] = "coletores"
        if apify_error:
            stats["apify_erro"] = apify_error[:300]
        status = "partial" if stats["falhas"] or not stats["coletores"] else "ok"
        with conn.cursor() as cur:
            record_run(cur, started, status, error_summary(errors)[:2000] if errors else None, stats)
        conn.commit()
    log.info("fim — %s contas, %s tweets, %s falhas (%s)", stats["contas"], stats["tweets"], stats["falhas"], status)
    return 0


if __name__ == "__main__":
    sys.exit(main())
