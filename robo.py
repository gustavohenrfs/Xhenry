"""Leitor de métricas do X para o VPS, pelo Apify.

Lê as timelines das contas monitoradas (x_accounts) pelo ator do Apify (APIFY_ACTOR), com o token que
cada usuário colou em Configurações (user_settings.apify_token). Nenhuma conta é logada: o Apify lê de
fora. Grava somente tweets de hoje (America/Sao_Paulo) em tweet_samples, refaz o dia em daily_metrics e
guarda os seguidores (vêm junto com os posts).

O cron chama o robô a cada minuto.

Contas do admin (admin_round): quem decide se roda é a aba Configurações do painel (app_settings):
- leitura automática ligada: lê todas as contas quando a última tentativa tem mais que
  app_settings.apify_interval_min (padrão 30);
- desligada: só lê quando alguém clica em "Atualizar Dados";
- "Atualizar Dados" lê na hora pelo painel (read_now, app/reader.py); sem Postgres no painel, ou se a
  thread quebrar, ele grava um pedido (robo_run_requested_at) e o robô apaga o pedido e lê no minuto
  seguinte.
Sem token do Apify nada é lido e o painel mostra "Token do Apify em falta". Cada leitura grava um
batimento em sync_runs (kind=tweets, account_id NULL; status failed quando o Apify falha). O vigia do
painel usa esse batimento para avisar quando o robô fica parado. Ghost/Search Ban é com o painel
(/api/check), não com o robô.

Contas dos outros usuários aprovados (apify_round), em toda passada do cron, independente da leitura
automática do admin: com a leitura automática do usuário ligada (user_settings.apify_auto), cada conta
no máximo a cada user_settings.apify_interval_min (padrão 60); sempre que houver pedido do "Atualizar
dados" (user_settings.apify_run_requested_at). O painel também lê na hora pelo read_now, sem esperar o
cron. Sem token, nada é lido e o painel dele mostra "Token do Apify em falta" (user_settings.apify_status
= sem_token).

Alertas: o robô só grava em alerts_sent (com o dono, owner_id; NULL = sistema, vai para o admin) e o
painel avisa por push e mostra no sininho e no histórico, conforme as Notificações de cada usuário. Nada
vai para o Telegram (lá ficaram só o "Esqueceu a senha?" e o aviso do backup).
- Views de um post: Viralizando, POST BOMBANDO, 100K e 1M (VIEW_LEVELS). Cada nível sai uma vez por
  post; se o post pular níveis, só o maior é avisado.
- Marcos do usuário (user_settings.notify_prefs): a cada post_step views num post e a cada day_step
  views no total do dia; e a meta do dia batida (impression_goal). Só o maior marco alcançado sai.
- Queda de views: a partir de QUEDA_HORA, se as views de hoje ficarem mais de QUEDA_LIMITE abaixo das
  de ontem. Uma vez por conta por dia.
- Falha do Apify (token recusado, sem crédito, rede): no máximo uma por hora (admin) ou por dia (outros).
- Resumo do fim do dia (notify_prefs.kinds.resumo, ligado por padrão): às 23:55 uma leitura de todas as
  contas do usuário (summary_read_on marca o dia) e às 23:59 "Parabéns! Hoje você fechou o dia com X
  views" (summary_round; se o cron perder 23:59, sai até 00:04 com o dia anterior).

Pausa da madrugada (user_settings.pause_on/pause_from/pause_to, padrão 00:00 às 06:00, horário de
Brasília): a leitura automática não roda; "Atualizar Dados" e a leitura do resumo continuam.

Fica na raiz do repositório do painel e lê o mesmo .env (DATABASE_URL). Precisa de psycopg[binary].

    python3 robo.py

Variáveis próprias (ambiente do cron):
- ROBO_ENV_FILE: outro .env, se o usuário do cron não puder ler o do painel.
- ROBO_STATE_DIR: onde ficam a trava e o estado (padrão ~/.xhenry-robo).
- APIFY_ACTOR: ator do Apify que lê as timelines (padrão apidojo~tweet-scraper).
- APIFY_INTERVAL_MIN: intervalo dos usuários sem o próprio (painel antigo; padrão 60).
"""

from __future__ import annotations

import json
import logging
import os
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
BR_TZ = ZoneInfo("America/Sao_Paulo")
# Níveis do alerta de views, do menor para o maior: (sufixo da chave, views acima de, título).
# A chave é viral:{conta}:{tweet} no primeiro nível e viral:{conta}:{tweet}:{sufixo} nos outros;
# o painel (sininho e push) lê o nível pelo sufixo. Todos usam kind=viral.
VIEW_LEVELS = (
    ("", 500, "🚀 Post viralizando!"),
    ("bombando", 10_000, "🔥 POST BOMBANDO!"),
    ("100k", 100_000, "💯 Post passou de 100K views!"),
    ("1m", 1_000_000, "👑 Post passou de 1 MILHÃO de views!"),
)
# Folga para o intervalo: o cron dispara no minuto cheio e a rodada anterior terminou no meio dele.
INTERVAL_SLACK = timedelta(seconds=45)
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


def env_int(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int((os.getenv(name) or "").strip() or default)
    except ValueError:
        value = default
    return max(low, min(high, value))


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


def alert_offline(step: str, exc: BaseException) -> None:
    """Banco fora: sem alerts_sent não há como avisar o painel; fica no log do cron (uma vez por hora)
    e o vigia do painel avisa "Robô parado" quando o batimento atrasar."""
    hour = datetime.now(BR_TZ).strftime("%Y-%m-%dT%H")
    try:
        if OFFLINE_STAMP.read_text(encoding="utf-8").strip() == hour:
            return
    except OSError:
        pass
    log.error("❌ Erro na leitura — robô, %s: %s", step, f"{type(exc).__name__}: {exc}"[:160])
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


def load_accounts(cur, limit: int) -> list[dict[str, Any]]:
    """Contas do admin, as lidas há mais tempo primeiro."""
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


def mark_collected(cur, account_id: int) -> None:
    cur.execute("UPDATE x_accounts SET collected_at = NOW() WHERE id = %s", (account_id,))


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


# ---- Apify ----

APIFY_API = "https://api.apify.com/v2"
APIFY_ACTOR_DEFAULT = "apidojo~tweet-scraper"
APIFY_INTERVAL_DEFAULT = 60
APIFY_TIMEOUT = 150  # o run-sync do Apify espera até 300 s; aqui bem menos, para não segurar o cron
APIFY_PER_ACCOUNT = 20  # posts pedidos por conta
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


SUMMARY_READ_FROM, SUMMARY_AT, SUMMARY_LATE = "23:55", "23:59", "00:05"


def br_clock(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).astimezone(BR_TZ).strftime("%H:%M")


def in_window(start: str, end: str, clock: str) -> bool:
    """clock entre start e end (HH:MM), atravessando a meia-noite se start > end."""
    if start == end:
        return False
    return start <= clock < end if start < end else (clock >= start or clock < end)


def paused(row: dict[str, Any], now: datetime | None = None) -> bool:
    """Pausa da madrugada do usuário: a leitura automática espera; o botão e o resumo não."""
    if row.get("pause_on") is False:
        return False
    return in_window(row.get("pause_from") or "00:00", row.get("pause_to") or "06:00", br_clock(now))


def summary_wanted(raw_prefs: str | None) -> bool:
    try:
        kinds = (json.loads(raw_prefs or "{}") or {}).get("kinds") or {}
    except ValueError:
        kinds = {}
    return kinds.get("resumo", True) is not False


def summary_read_due(row: dict[str, Any], now: datetime | None = None) -> bool:
    """Leitura das 23:55 para o resumo: uma vez por dia, só de quem quer o resumo."""
    now = now or datetime.now(timezone.utc)
    today = now.astimezone(BR_TZ).date()
    return (summary_wanted(row.get("notify_prefs")) and SUMMARY_READ_FROM <= br_clock(now) < SUMMARY_AT
            and row.get("summary_read_on") != today)


def mark_summary_read(cur, owner_id: int, now: datetime | None = None) -> None:
    today = (now or datetime.now(timezone.utc)).astimezone(BR_TZ).date()
    cur.execute("UPDATE user_settings SET summary_read_on = %s WHERE owner_id = %s", (today, owner_id))


def apify_users(cur) -> list[dict[str, Any]]:
    """Usuários aprovados que não são o admin e têm alguma conta ativa."""
    cur.execute(
        """
        SELECT u.id, s.apify_token, s.apify_run_requested_at,
               COALESCE(s.apify_auto, TRUE) AS apify_auto,
               COALESCE(s.apify_interval_min, %s) AS apify_interval_min,
               s.pause_on, s.pause_from, s.pause_to, s.notify_prefs, s.summary_read_on
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
    """Lê as contas pelo Apify (lotes de APIFY_MAX_ACCOUNTS) e grava: posts de hoje, alertas de views,
    o dia refeito e seguidores (em toda leitura: vêm junto com os posts).

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
    summary = summary_read_due(user)
    if summary:
        # Leitura das 23:55 para o resumo: marca antes, para um erro não repetir a cada minuto.
        with conn.cursor() as cur:
            mark_summary_read(cur, owner_id)
        conn.commit()
    requested = user["apify_run_requested_at"] is not None or summary
    if not requested and (user.get("apify_auto") is False or paused(user)):
        return {"owner": owner_id, "contas": 0}  # automática desligada ou pausa da madrugada: só pelo botão
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


def summary_day(now: datetime | None = None) -> date | None:
    """Dia do resumo: hoje a partir das 23:59; o de ontem até 00:04 (o cron pode ter perdido o minuto)."""
    local = (now or datetime.now(timezone.utc)).astimezone(BR_TZ)
    clock = local.strftime("%H:%M")
    if clock >= SUMMARY_AT:
        return local.date()
    if clock < SUMMARY_LATE:
        return local.date() - timedelta(days=1)
    return None


def summary_text(total: int, top: dict[str, Any] | None) -> str:
    if total <= 0:
        return "🌙 Dia encerrado\nHoje as suas contas não tiveram views lidas."
    text = f"🎉 Parabéns! Hoje você fechou o dia com {fmt_int(total)} views"
    if top and top["views"]:
        who = top["display_name"] or top["username"]
        text += f"\n🏆 Melhor conta: {who} (@{top['username']}) com {fmt_int(as_int(top['views']))} views"
    return text


def summary_round(connect, now: datetime | None = None) -> int:
    """23:59: o total de views do dia de cada usuário que quer o resumo (só uma vez por dia)."""
    day = summary_day(now)
    if day is None:
        return 0
    sent = 0
    with connect() as conn:
        with conn.cursor() as cur:
            try:
                cur.execute(
                    """
                    SELECT u.id, s.notify_prefs
                    FROM users u LEFT JOIN user_settings s ON s.owner_id = u.id
                    WHERE u.is_approved IS TRUE
                      AND EXISTS (SELECT 1 FROM x_accounts a WHERE a.owner_id = u.id AND a.is_active IS TRUE
                                  AND COALESCE(a.is_archived, FALSE) IS FALSE)
                    """
                )
                users = [row for row in cur.fetchall() if summary_wanted(row["notify_prefs"])]
            except Exception as exc:
                conn.rollback()
                log.warning("resumo do dia: sem as tabelas novas (%s)", type(exc).__name__)
                return 0
            for user in users:
                if already_alerted(cur, f"resumo:{user['id']}:{day.isoformat()}"):
                    continue
                total = day_views(cur, user["id"], day)
                cur.execute(
                    """
                    SELECT a.username, a.display_name, d.views
                    FROM daily_metrics d JOIN x_accounts a ON a.id = d.account_id
                    WHERE a.owner_id = %s AND d.metric_date = %s AND COALESCE(a.is_archived, FALSE) IS FALSE
                    ORDER BY d.views DESC NULLS LAST LIMIT 1
                    """,
                    (user["id"], day),
                )
                if alert_once(cur, f"resumo:{user['id']}:{day.isoformat()}", "resumo",
                              summary_text(total, cur.fetchone()), user["id"]):
                    sent += 1
        conn.commit()
    if sent:
        log.info("resumo do dia %s: %s usuários", day.isoformat(), sent)
    return sent


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

    # Contas dos outros usuários: em paralelo com as do admin, toda passada do cron (cada conta respeita
    # o próprio intervalo) e sem depender da leitura automática do admin.
    connect = lambda: psycopg.connect(database_url(), row_factory=dict_row, connect_timeout=10)  # noqa: E731
    apify = ThreadPoolExecutor(max_workers=1, thread_name_prefix="apify")
    # Depois das leituras do Apify (inclusive a das 23:55), o resumo das 23:59.
    apify_job = apify.submit(lambda: (apify_round(connect, midnight), summary_round(connect)))
    try:
        return admin_round(conn, started, midnight)
    finally:
        try:
            apify_job.result()
        except Exception:
            log.exception("leitura pelo Apify falhou")
        apify.shutdown()


APIFY_ADMIN_INTERVAL_DEFAULT = 30


def admin_schedule(conn) -> dict[str, Any] | None:
    """Pausa da madrugada e leitura do resumo do admin (valem com ou sem Apify). None em painel antigo."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT u.id, s.pause_on, s.pause_from, s.pause_to, s.notify_prefs, s.summary_read_on
                FROM users u LEFT JOIN user_settings s ON s.owner_id = u.id
                WHERE u.is_admin IS TRUE ORDER BY u.id LIMIT 1
                """
            )
            row = cur.fetchone()
        conn.commit()
    except Exception as exc:
        conn.rollback()
        log.warning("sem a pausa da madrugada (%s); atualize o painel", type(exc).__name__)
        return None
    if row is None:
        return None
    return {"id": row["id"], "paused": paused(row), "summary_due": summary_read_due(row)}


def take_request(conn) -> bool:
    """Pedido do "Atualizar Dados" deixado para o cron (sem Postgres no painel, leitura na hora que
    quebrou, token novo ou leitura religada). Apaga o pedido ao aceitar. Painel antigo: nenhum."""
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT robo_run_requested_at FROM app_settings WHERE id = 1")
            row = cur.fetchone()
    except Exception as exc:
        conn.rollback()
        log.warning("app_settings sem as opções do robô (%s); atualize o painel", type(exc).__name__)
        return False
    if row is None or row["robo_run_requested_at"] is None:
        conn.commit()
        return False
    with conn.cursor() as cur:
        cur.execute("UPDATE app_settings SET robo_run_requested_at = NULL WHERE id = 1")
    conn.commit()
    return True


def admin_apify(conn) -> dict[str, Any] | None:
    """Token do Apify do admin ("" sem token) e se a leitura venceu (leitura ligada e a última tentativa
    mais velha que app_settings.apify_interval_min). None sem admin ou em painel antigo."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT u.id, s.apify_token, s.apify_checked_at, a.robo_enabled,
                       COALESCE(a.apify_interval_min, %s) AS interval_min
                FROM users u
                LEFT JOIN user_settings s ON s.owner_id = u.id
                LEFT JOIN app_settings a ON a.id = 1
                WHERE u.is_admin IS TRUE
                ORDER BY u.id
                LIMIT 1
                """,
                (APIFY_ADMIN_INTERVAL_DEFAULT,),
            )
            row = cur.fetchone()
        conn.commit()
    except Exception as exc:  # painel antigo, sem as colunas
        conn.rollback()
        log.warning("sem as opções do Apify do admin (%s); atualize o painel", type(exc).__name__)
        return None
    if row is None:
        return None
    interval = timedelta(minutes=max(1, int(row["interval_min"] or APIFY_ADMIN_INTERVAL_DEFAULT)))
    last = row["apify_checked_at"]
    now = datetime.now(timezone.utc)
    due = row["robo_enabled"] is not False and (last is None or now - last >= interval - INTERVAL_SLACK)
    return {"id": row["id"], "token": (row["apify_token"] or "").strip(), "due": due}


def admin_accounts(cur) -> list[dict[str, Any]]:
    """Todas as contas ativas do admin."""
    return load_accounts(cur, 100_000)


def apify_admin_read(conn, admin: dict[str, Any], started: datetime, midnight: datetime, manual: bool) -> str | None:
    """Lê as contas do admin pelo Apify e grava o batimento. None = deu certo; texto = motivo da falha."""
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
            set_apify_status(cur, admin["id"], "erro", reason)
            alert_once(cur, f"apify:{admin['id']}:erro:{hour}", "apify", f"⚠️ Apify falhou: {reason}", admin["id"])
            record_run(cur, started, "failed", reason[:500], {"via": "apify", "contas": 0, "tweets": 0,
                                                               "falhas": len(accounts), "manual": manual})
        conn.commit()
        log.warning("apify do admin falhou (%s)", reason)
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
    esperar o cron. Devolve "ok", "erro" (o motivo fica em user_settings) ou "sem_apify". Postgres e
    DATABASE_URL vêm do mesmo .env do painel.
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
            return "ok" if error is None else "erro"
        result = apify_user_round(conn, {
            "id": owner_id, "apify_token": user["apify_token"], "apify_run_requested_at": started,
            "apify_auto": True, "apify_interval_min": user["apify_interval_min"],
        }, midnight)
        return "erro" if result.get("erro") else "ok"


def admin_round(conn, started: datetime, midnight: datetime) -> int:
    """As contas do admin pelo Apify: no intervalo (fora da pausa da madrugada), com o pedido do botão ou
    às 23:55 para o resumo."""
    with conn:
        try:
            manual = take_request(conn)
            admin = admin_apify(conn)
            schedule = admin_schedule(conn)
        except Exception as exc:
            log.exception("não leu as opções do robô")
            alert_offline("banco", exc)
            return 1
        if admin is None:
            return 0
        if schedule and schedule["summary_due"]:
            # 23:55: lê tudo para o resumo das 23:59, como o "Atualizar Dados".
            with conn.cursor() as cur:
                mark_summary_read(cur, schedule["id"])
            conn.commit()
            manual = True
        night = bool(schedule and schedule["paused"])
        if not manual and (not admin["due"] or night):
            return 0  # desligado, dentro do intervalo ou na pausa: sai quieto (o cron chama de novo em 1 min)
        if not admin["token"]:
            day = midnight.date().isoformat()
            with conn.cursor() as cur:
                set_apify_status(cur, admin["id"], "sem_token", NO_APIFY_TOKEN)
                alert_once(cur, f"apify:{admin['id']}:sem_token:{day}", "apify", f"🔑 {NO_APIFY_TOKEN}", admin["id"])
            conn.commit()
            log.info("apify: admin sem token; nada lido")
            return 0
        apify_admin_read(conn, admin, started, midnight, manual)  # falha já fica no batimento e no aviso
        return 0


if __name__ == "__main__":
    sys.exit(main())
