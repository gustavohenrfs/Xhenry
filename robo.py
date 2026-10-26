"""Coletor de métricas do X para o VPS.

Lê as timelines das contas monitoradas (x_accounts) entrando logado em contas
descartáveis, os coletores (tabela collectors, aba Coletores do painel). As
contas monitoradas nunca são logadas pelo robô. Grava somente tweets de hoje
(America/Sao_Paulo) em tweet_samples.

O cron chama o robô a cada minuto; quem decide se roda é a aba Configurações
do painel (app_settings):
- coleta automática ligada: roda quando a última rodada tem mais que o
  intervalo escolhido (5 min a 24 h);
- desligada: só roda quando alguém clica em "Atualizar Dados";
- "Atualizar Dados" grava um pedido; o robô apaga o pedido e faz uma rodada
  manual, que lê todas as contas (até MANUAL_PER_COLLECTOR por coletor,
  revezando os coletores).

Cada rodada automática:
- pega os coletores livres (ligados, fora de castigo) e lê uma conta monitorada
  por coletor: as coletadas há mais tempo primeiro (x_accounts.collected_at);
- para cada par abre o anyx logado no coletor, saindo pelo proxy dele, lê a
  conta, fecha o navegador e passa para o próximo (só 1 Chrome por vez na RAM);
- seguidores no máximo 1x por hora por conta (x_accounts.followers_at);
- pausa aleatória de 0,3 a 2 s entre páginas e entre contas;
- 429 num coletor: para a rodada inteira, deixa o coletor de castigo e avisa.

O Chrome não aceita proxy com usuário e senha, então cada coletor ganha um
repassador local (ProxyRelay): o Chrome fala com 127.0.0.1 sem senha e o
repassador abre o túnel no proxy de verdade com a senha (http, https ou socks5).

Alertas no Telegram:
- Post viralizando (views acima de VIEW_ALERT), uma vez por post.
- Queda de views: a partir de QUEDA_HORA, se as views de hoje ficarem mais de
  QUEDA_LIMITE abaixo das de ontem. Uma vez por conta por dia.
- Coletor limitado (429), bloqueado, deslogado ou com proxy fora: uma vez por
  hora por coletor e motivo.
- Nenhum coletor disponível: no máximo a cada 6 horas.
- Erro na coleta de uma conta (suspensa, não encontrada, banco): no máximo uma
  mensagem por hora para o mesmo conjunto de falhas.

Cada rodada grava um batimento em sync_runs (kind=tweets, account_id NULL).
O vigia do painel usa esse batimento para avisar quando o robô fica parado.
Ghost/Search Ban é com o painel (/api/check), não com o robô.

Fica na raiz do repositório do painel e lê o mesmo .env (DATABASE_URL,
TELEGRAM_TOKEN ou TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID). Precisa de
psycopg[binary] e anyx (pip install anyx && playwright install chromium).

    python3 robo.py

Variáveis próprias (ambiente do cron):
- ROBO_ENV_FILE: outro .env, se o usuário do cron não puder ler o do painel.
- ROBO_STATE_DIR: onde ficam a trava e o estado (padrão ~/.xhenry-robo).
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
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import unquote, urlsplit
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
BR_TZ = ZoneInfo("America/Sao_Paulo")
TWEET_LIMIT = 20
VIEW_ALERT = 500
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
    "sessao": timedelta(hours=24),
}
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

log = logging.getLogger("robo")


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
        pick(tweet, "like_count", "favorite_count", "likes", "favourites_count")
        or pick(nested, "like_count", "favorite_count", "likes")
    )
    rts = as_int(
        pick(tweet, "retweet_count", "repost_count", "retweets")
        or pick(nested, "retweet_count", "repost_count")
    )
    replies = as_int(
        pick(tweet, "reply_count", "replies") or pick(nested, "reply_count")
    )
    quotes = as_int(pick(tweet, "quote_count", "quotes") or pick(nested, "quote_count"))
    raw_views = pick(tweet, "view_count", "impression_count", "impressions") or pick(
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


def telegram_creds() -> tuple[str, str]:
    token = (os.getenv("TELEGRAM_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    chat = (os.getenv("TELEGRAM_CHAT_ID") or "").strip()
    return token, chat


def send_telegram(text: str) -> bool:
    token, chat = telegram_creds()
    if not token or not chat:
        log.warning("Telegram não configurado (TELEGRAM_TOKEN / TELEGRAM_CHAT_ID)")
        return False
    payload = json.dumps({"chat_id": chat, "text": text}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            return 200 <= response.status < 300
    except urllib.error.URLError as exc:
        log.warning("Telegram falhou: %s", exc)
        return False


def already_alerted(cur, key: str) -> bool:
    cur.execute("SELECT 1 FROM alerts_sent WHERE dedupe_key = %s LIMIT 1", (key,))
    return cur.fetchone() is not None


def mark_alerted(cur, key: str, kind: str, text: str) -> None:
    cur.execute(
        """
        INSERT INTO alerts_sent (dedupe_key, kind, detail, sent_at)
        VALUES (%s, %s, %s, NOW())
        ON CONFLICT (dedupe_key) DO NOTHING
        """,
        (key, kind, text[:500]),
    )


def alert_once(cur, key: str, kind: str, text: str) -> bool:
    """Envia e registra em alerts_sent; a mesma chave nunca sai duas vezes."""
    if already_alerted(cur, key):
        return False
    if not send_telegram(text):
        return False
    mark_alerted(cur, key, kind, text)
    return True


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
    sent = alert_once(cur, f"queda:{account['id']}:{today.isoformat()}", "queda", text)
    if sent:
        log.info("alerta queda @%s: %s -> %s (-%s%%)", username, ontem, hoje, pct)
    return sent


def error_summary(errors: list[tuple[str, str, str]]) -> str:
    lines = ["❌ Erro na coleta"]
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
    """Banco fora: não há alerts_sent, então o dedupe por hora fica num arquivo."""
    hour = datetime.now(BR_TZ).strftime("%Y-%m-%dT%H")
    try:
        if OFFLINE_STAMP.read_text(encoding="utf-8").strip() == hour:
            return
    except OSError:
        pass
    if send_telegram(error_summary([("robô", step, f"{type(exc).__name__}: {exc}")])):
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
            0, %s, %s, NOW()
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
            0,
            COALESCE(SUM(view_count), 0),
            COALESCE(SUM(view_count), 0),
            NOW()
        FROM tweet_samples
        WHERE account_id = %s
          AND (posted_at AT TIME ZONE 'America/Sao_Paulo')::date = %s
        ON CONFLICT (account_id, metric_date) DO UPDATE SET
            posts_count = EXCLUDED.posts_count,
            likes = EXCLUDED.likes,
            retweets = EXCLUDED.retweets,
            replies = EXCLUDED.replies,
            quotes = EXCLUDED.quotes,
            views = EXCLUDED.views,
            impressions = EXCLUDED.impressions,
            collected_at = NOW()
        """,
        (account_id, day, account_id, day),
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


@contextmanager
def reader(anyx: Any, collector: dict[str, Any]) -> Iterator[tuple[Any, ProxyRelay | None]]:
    """Sessão do anyx logada no coletor, saindo pelo proxy dele. Fecha tudo no fim."""
    handle = collector["handle"]
    stop_browser(handle)  # sobra de uma rodada que caiu no meio
    relay = ProxyRelay(collector["proxy"]) if collector.get("proxy") else None
    try:
        if relay:
            relay.probe()
        prepare_collector(collector, relay.url if relay else None)
        with anyx.Session(handle) as session:
            slow_pages(session)
            yield session, relay
    finally:
        stop_browser(handle)
        if relay:
            relay.close()


def classify(exc: BaseException, relay: ProxyRelay | None = None) -> str:
    """limite, sessao, proxy, bloqueio (culpa do coletor), conta (da conta lida) ou erro."""
    kind = type(exc).__name__
    text = str(exc)
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
    if kind == "sessao":
        return (
            f"🔑 Coletor {who} deslogado ou com cookies inválidos.\n"
            f"Cole cookies novos na aba Coletores (ele volta na hora).\nDetalhe: {detail}"
        )
    return f"🔌 Proxy do coletor {who} não respondeu.\nDetalhe: {detail}\n{who} pausado até {back}."


def load_collectors(cur) -> tuple[list[dict[str, Any]], int, datetime | None]:
    """Coletores livres (os usados há mais tempo primeiro), total cadastrado e próximo fim de castigo.

    Um coletor com o mesmo @ de uma conta monitorada nunca é usado: o painel já recusa, isto é a
    segunda trava para o robô não entrar logado numa conta que gera dinheiro.
    """
    cur.execute(
        """
        SELECT c.id, c.handle, c.proxy, c.cookies, c.is_active, c.paused_until
        FROM collectors c
        WHERE NOT EXISTS (SELECT 1 FROM x_accounts a WHERE lower(a.username) = lower(c.handle))
        ORDER BY c.last_used_at NULLS FIRST, c.id
        """
    )
    rows = list(cur.fetchall())
    now = datetime.now(timezone.utc)
    free = [r for r in rows if r["is_active"] and (r["paused_until"] is None or r["paused_until"] <= now)]
    paused = [r["paused_until"] for r in rows if r["is_active"] and r["paused_until"] and r["paused_until"] > now]
    return free, len(rows), min(paused) if paused else None


def load_accounts(cur, limit: int) -> list[dict[str, Any]]:
    cur.execute(
        """
        SELECT id, username, display_name, followers_at
        FROM x_accounts
        WHERE is_active IS TRUE
          AND COALESCE(is_archived, FALSE) IS FALSE
        ORDER BY collected_at NULLS FIRST, id
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
    account_id = account["id"]
    display = account.get("display_name") or username
    today = midnight.date()

    if followers_due(account, datetime.now(timezone.utc)):
        followers = fetch_followers(session, username)
        if followers is not None:
            upsert_followers(cur, account_id, today, followers)
            log.info("@%s — %s seguidores", username, followers)
        else:
            log.warning("@%s — seguidores não encontrados no perfil", username)
        cur.execute("UPDATE x_accounts SET followers_at = NOW() WHERE id = %s", (account_id,))
        pause()

    saved = 0
    for tweet in fetch_timeline(session, username):
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
        if views > VIEW_ALERT:
            when = local.strftime("%d/%m/%Y %H:%M")
            link = f"https://x.com/{username}/status/{tid}"
            text = (
                "🚀 Post viralizando!\n"
                f"👤 Conta: {display} (@{username})\n"
                f"👁 Views: {views}\n"
                f"⏰ Horário do post: {when}\n"
                f"❤️ Curtidas: {metrics['like_count']}\n"
                f"🔁 Retweets: {metrics['retweet_count']}\n"
                f"🔗 Link do Post: {link}"
            )
            if alert_once(cur, f"viral:{account_id}:{tid}", "viral", text):
                log.info("alerta views @%s tweet %s (%s)", username, tid, views)
    recompute_today(cur, account_id, today)
    check_views_drop(cur, account, today)
    mark_collected(cur, account_id)
    return saved


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


def collect(conn, anyx: Any, midnight: datetime, manual: bool = False) -> dict[str, Any]:
    """Uma rodada: pares coletor + conta em sequência (um Chrome por vez)."""
    budget = MANUAL_BUDGET if manual else ROUND_BUDGET
    deadline = time.monotonic() + budget
    with conn.cursor() as cur:
        collectors, total, next_free = load_collectors(cur)
        limit = len(collectors) * (MANUAL_PER_COLLECTOR if manual else 1)
        accounts = load_accounts(cur, limit) if collectors else []
        if not collectors:
            no_collector_alert(cur, total, next_free)
    conn.commit()
    log.info("%s coletores livres de %s; %s contas nesta rodada%s", len(collectors), total, len(accounts),
             " (manual)" if manual else "")
    stats: dict[str, Any] = {"coletores": len(collectors), "contas": 0, "tweets": 0, "falhas": 0, "parada_429": False,
                             "manual": manual}
    errors: list[tuple[str, str, str]] = []
    benched: set[int] = set()  # coletores que tomaram castigo nesta rodada
    for index, (collector, account) in enumerate(pairs_for(collectors, accounts, manual)):
        if time.monotonic() > deadline:
            log.warning("rodada passou de %ss; o resto fica para a próxima", budget)
            break
        if collector["id"] in benched:
            continue
        if index:
            pause()
        kind, saved, detail = run_pair(conn, anyx, collector, account, midnight)
        stats["contas"] += 1
        stats["tweets"] += saved
        if kind != "ok":
            stats["falhas"] += 1
        if kind in PAUSE_FOR:
            benched.add(collector["id"])
        if kind in ("conta", "erro"):
            errors.append((f"@{account['username']}", f"lida por @{collector['handle']}", detail or kind))
        if kind == "limite":
            stats["parada_429"] = True
            log.warning("429 no coletor @%s: rodada parada", collector["handle"])
            break
    if errors:
        with conn.cursor() as cur:
            alert_errors(cur, errors)
        conn.commit()
    stats["erros"] = errors
    return stats


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

    with conn:
        try:
            run, manual = round_plan(conn)
        except Exception as exc:
            log.exception("não leu as opções do robô")
            alert_offline("banco", exc)
            return 1
        if not run:
            return 0  # desligado ou dentro do intervalo: sai quieto (o cron chama de novo em 1 min)
        log.info("início%s — tweets depois de %s", " (Atualizar Dados)" if manual else "", midnight.isoformat())
        try:
            import anyx
        except ImportError:
            log.error("Instale anyx no VPS: pip install anyx && playwright install chromium")
            return 1
        tune_anyx()
        try:
            stats = collect(conn, anyx, midnight, manual)
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
        status = "partial" if stats["falhas"] or not stats["coletores"] else "ok"
        with conn.cursor() as cur:
            record_run(cur, started, status, error_summary(errors)[:2000] if errors else None, stats)
        conn.commit()
    log.info("fim — %s contas, %s tweets, %s falhas (%s)", stats["contas"], stats["tweets"], stats["falhas"], status)
    return 0


if __name__ == "__main__":
    sys.exit(main())
