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
- APIFY_ACTOR: ator do Apify que lê as timelines (padrão kaitoeasyapi~twitter-x-data-tweet-scraper-pay-per-result-
  cheapest, que roda no plano grátis; apidojo~tweet-scraper só no plano pago; eiv~twitter-scraper lê pela linha do
  tempo do perfil sem login, que não mostra contas novas).
- APIFY_FALLBACK_ACTOR: quem relê as contas que a busca não trouxe (padrão apidojo~twitter-scraper-lite; com
  eiv~twitter-scraper relê todas, não só 5).
- APIFY_PROFILE_TWEETS: posts mais novos por conta no ator de linha do tempo (padrão 10).
- APIFY_INTERVAL_MIN: intervalo dos usuários sem o próprio (painel antigo; padrão 60).
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
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
    raw = pick(tweet, "id", "tweet_id", "tweetId", "rest_id", "id_str")
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
            -- no X as views só sobem: ator sem o campo (ou leitura atrasada) não derruba o número
            impression_count = GREATEST(tweet_samples.impression_count, EXCLUDED.impression_count),
            view_count = GREATEST(tweet_samples.view_count, EXCLUDED.view_count),
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


def load_accounts(cur, limit: int, folder_id: int | None = None) -> list[dict[str, Any]]:
    """Contas do admin, as lidas há mais tempo primeiro (só as da pasta, com folder_id)."""
    cur.execute(
        """
        SELECT a.id, a.owner_id, a.username, a.display_name, a.followers_at
        FROM x_accounts a
        JOIN users u ON u.id = a.owner_id
        WHERE a.is_active IS TRUE
          AND COALESCE(a.is_archived, FALSE) IS FALSE
          AND u.is_admin IS TRUE
          AND (%s::int IS NULL OR a.folder_id = %s::int)
        ORDER BY a.collected_at NULLS FIRST, a.id
        LIMIT %s
        """,
        (folder_id, folder_id, limit),
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
# Padrão: o Tweet Scraper da KaitoEasyAPI. Testado nas contas reais em 2026-11-04: roda pela API no plano grátis do
# Apify (o V2 do apidojo não: no grátis devolve só {"noResults": true}), traz views, e cobra por post
# (US$0,25/mil, mínimo de 15 por execução). Não entende "since:dia" (devolve itens falsos), só since_time.
APIFY_V2 = "apidojo~tweet-scraper"
APIFY_KAITO = "kaitoeasyapi~twitter-x-data-tweet-scraper-pay-per-result-cheapest"
# Padrão: Tweet Scraper V2 pelo @ (twitterHandles = a página de cada perfil, não a busca do X, que esconde parte
# das contas). 2026-11-04, 47 contas reais no plano Starter: 46 de 46 existentes em 15 s, lotes de 8 em paralelo.
APIFY_ACTOR_DEFAULT = APIFY_V2
KAITO_OWNERS = ("kaitoeasyapi",)
APIFY_INTERVAL_DEFAULT = 60
APIFY_TIMEOUT = 150  # tempo máximo de uma execução (inicia, espera e baixa os itens); depois é abortada
APIFY_RETRIES = 3  # tentativas de cada chamada à API do Apify quando a rede falha ou ele responde 429/5xx
APIFY_RETRY_WAIT = 2.0  # segundos entre tentativas (dobra a cada uma)
APIFY_POLL = 20  # o GET da execução espera até isso por ela terminar (waitForFinish)
# Pelo @ (twitterHandles): o V2 lê a página de cada perfil (2 a 20 itens, reposts inclusos) e para no maxItems
# da execução. Lotes de 8 contas com 12 itens cada trouxeram todas; com 5 cada, contas ficaram de fora.
APIFY_HANDLES_PER_RUN = 8
APIFY_HANDLE_ITEMS = 12
APIFY_HANDLES_PARALLEL = 8  # execuções ao mesmo tempo (o plano pago aceita 32)
# Regras do Tweet Scraper V2 (apidojo/tweet-scraper, documentação do ator): no máximo 1 execução por vez, com
# até 5 buscas; cada busca precisa trazer pelo menos 50 tweets; não repetir a mesma busca em intervalo curto.
APIFY_TODAY_PER_ACCOUNT = 60  # teto de posts de hoje por conta na leitura (só se paga o que vier)
APIFY_MIN_PER_QUERY = 50  # o mínimo que o ator pede por busca
APIFY_QUERY_ACCOUNTS = 8  # contas juntas numa busca ("(from:a OR from:b …) since:dia")
APIFY_MAX_QUERIES = 5  # buscas por execução
APIFY_MAX_ACCOUNTS = APIFY_QUERY_ACCOUNTS * APIFY_MAX_QUERIES  # contas por execução; mais que isso vira outra, em seguida
APIFY_PARALLEL = 1  # o ator só aceita uma execução por vez
# Kaito: quando o X responde "Internal server error" a uma busca, ele não tenta de novo e devolve só itens falsos
# (2026-11-04, no log do ator; mais comum com várias buscas ao mesmo tempo). Então uma busca por execução, poucas
# ao mesmo tempo, e a busca que voltou só com itens falsos é repetida (até KAITO_RETRIES vezes, US$0,004 cada).
KAITO_PARALLEL = 2
KAITO_RETRIES = 2
KAITO_RETRY_WAIT = 3  # segundos antes de repetir
APIFY_PROFILE_ITEMS = 20  # reserva pelo perfil: os posts mais novos de cada conta (o Unlimited inclui ~40 por busca)
APIFY_PROFILE_MAX = 5  # contas lidas pelo perfil numa leitura (uma execução, até 5 perfis: a faixa mais barata)
# Twitter Scraper Unlimited: sem o mínimo de 50 tweets por busca do V2. É o ator da reserva (conta pequena ou que a
# busca do V2 não trouxe); APIFY_FALLBACK_ACTOR no .env troca.
APIFY_UNLIMITED = "apidojo~twitter-scraper-lite"
APIFY_ROUND_ACCOUNTS = 200  # contas de um usuário numa passada
NO_APIFY_TOKEN = "Token do Apify em falta. Cole o seu em Configurações para o robô ler as suas contas."


class ApifyError(Exception):
    """Falha ao chamar o Apify; a mensagem vai para o painel do dono."""


def apify_actor() -> str:
    # Na URL da API o ator vai como "dono~nome"; aceita também "dono/nome".
    return (os.getenv("APIFY_ACTOR") or APIFY_ACTOR_DEFAULT).strip().replace("/", "~")


def is_kaito(actor: str | None = None) -> bool:
    return (actor or apify_actor()).split("~", 1)[0].lower() in KAITO_OWNERS


def is_fake_item(item: dict[str, Any]) -> bool:
    """Aviso que o kaito põe no dataset quando a busca vem vazia (type mock_tweet, id -1): não é post."""
    return item.get("type") == "mock_tweet" or str(item.get("id")) == "-1"


def by_handle(actor: str | None = None) -> bool:
    """O ator lê pelo @ da conta (twitterHandles): o V2."""
    return (actor or apify_actor()) == APIFY_V2


def batch_size() -> int:
    """Contas por execução: 8 pelo @ (V2), uma busca só no kaito, até 5 buscas nos outros."""
    if by_handle():
        return APIFY_HANDLES_PER_RUN
    return APIFY_QUERY_ACCOUNTS if is_kaito() else APIFY_MAX_ACCOUNTS


def parallel_runs() -> int:
    if by_handle():
        return APIFY_HANDLES_PARALLEL
    return KAITO_PARALLEL if is_kaito() else APIFY_PARALLEL


def handles_input(handles: list[str], per: int = APIFY_HANDLE_ITEMS) -> dict[str, Any]:
    """Pelo @: os itens mais novos da página de cada conta (o robô guarda só os de hoje)."""
    return {"twitterHandles": list(handles), "maxItems": per * len(handles), "sort": "Latest"}


def apify_interval() -> timedelta:
    return timedelta(minutes=env_int("APIFY_INTERVAL_MIN", APIFY_INTERVAL_DEFAULT, 5, 24 * 60))


def apify_input(handles: list[str], since: datetime | None = None) -> dict[str, Any]:
    """Os posts de hoje das contas: até APIFY_QUERY_ACCOUNTS contas juntas por busca
    ("(from:a OR from:b) since:2026-11-02"), dentro das regras do ator (até 5 buscas por execução, buscas
    maiores). since: é a data de Brasília; o X lê em UTC, então vem um pouco de ontem, que o robô descarta."""
    midnight = since or today_start_br()
    kaito = is_kaito()
    # O kaito só entende since_time (segundos); "since:dia" volta com itens falsos.
    limit = f" since_time:{int(midnight.timestamp())}" if kaito else f" since:{midnight.date().isoformat()}"
    groups = [handles[i:i + APIFY_QUERY_ACCOUNTS] for i in range(0, len(handles), APIFY_QUERY_ACCOUNTS)]
    terms = [(f"from:{g[0]}" if len(g) == 1 else "(" + " OR ".join(f"from:{h}" for h in g) + ")") + limit
             for g in groups]
    body = {"searchTerms": terms, "maxItems": max(APIFY_MIN_PER_QUERY * len(terms), APIFY_TODAY_PER_ACCOUNT * len(handles)),
            "sort": "Latest"}
    if kaito:
        body["queryType"] = "Latest"
    return body


def apify_profile_input(handles: list[str], actor: str | None = None) -> dict[str, Any]:
    """Pedido pelo perfil, não pela busca. No ator de linha do tempo (eiv) é o formato dele; no V2, twitterHandles
    (ele lê a página de cada perfil, ~20 posts por conta, todas ao mesmo tempo)."""
    actor = actor or apify_fallback_actor()
    if is_timeline_actor(actor):
        return timeline_input(handles)
    if by_handle(actor):
        return handles_input(handles, APIFY_PROFILE_ITEMS)
    return {"startUrls": [f"https://x.com/{handle}" for handle in handles],
            "maxItems": APIFY_PROFILE_ITEMS * len(handles), "sort": "Latest"}


# Ator que lê a linha do tempo do perfil, não a busca do X (eiv/twitter-scraper, "Twitter / X Scraper - Tweets,
# Profiles & Engagement"). A busca do X esconde contas +18 das contas do Apify; a linha do tempo não passa por
# ela. Entrada: profiles + maxTweetsPerProfile; saída achatada (authorHandle, authorFollowers, tweetId).
TIMELINE_OWNERS = ("eiv",)


def is_timeline_actor(actor: str) -> bool:
    return actor.split("~", 1)[0].lower() in TIMELINE_OWNERS


def timeline_input(handles: list[str]) -> dict[str, Any]:
    per = env_int("APIFY_PROFILE_TWEETS", 10, 3, 100)  # os mais novos de cada conta; paga-se por post lido
    return {"profiles": list(handles), "maxTweetsPerProfile": per, "maxTotalTweets": per * len(handles),
            "includeRetweets": False, "includeReplies": False}


def apify_fallback_actor() -> str:
    # Com o kaito, a reserva é o V2 pelo @ da conta (2026-11-04, contas reais: o V2 por twitterHandles achou 46 de
    # 47 contas, o kaito 26 a 36, porque o X devolve "Internal server error" a parte das buscas dele).
    # Com o V2 pelo @, a reserva é ele mesmo com mais itens por conta (a conta que o maxItems do lote cortou).
    default = APIFY_V2 if is_kaito() or by_handle() else APIFY_UNLIMITED
    return (os.getenv("APIFY_FALLBACK_ACTOR") or default).strip().replace("/", "~")


def apify_fetch(token: str, handles: list[str]) -> list[dict[str, Any]]:
    """Leitura normal: pelo @ no V2 (handles_input), a linha do tempo no eiv, a busca de hoje nos outros."""
    actor = apify_actor()
    if by_handle(actor):
        return apify_call(token, handles_input(handles))
    return apify_call(token, timeline_input(handles) if is_timeline_actor(actor) else apify_input(handles))


def apify_profile(token: str, handles: list[str]) -> list[dict[str, Any]]:
    """Reserva: os posts mais novos dessas contas pelo perfil, no Twitter Scraper Unlimited (o V2 pede pelo menos
    50 tweets por busca; conta pequena, com 1 post no dia, não sai por ele)."""
    return apify_call(token, apify_profile_input(handles), apify_fallback_actor())


APIFY_HTTP_ERRORS = {
    401: "o Apify recusou o token (401). Confira o token em Configurações.",
    402: "a conta do Apify está sem crédito (402).",
    403: "o Apify negou o acesso (403). O plano gratuito não roda esse ator pela API.",
    408: "o Apify demorou demais (408). Tenta de novo na próxima passada.",
    429: "limite de chamadas do Apify (429). Tenta de novo na próxima passada.",
}
RETRY_CODES = (429, 500, 502, 503, 504)
RUN_ACTIVE = ("READY", "RUNNING")


def apify_api(token: str, method: str, path: str, body: Any = None, timeout: float = 60) -> Any:
    """Uma chamada curta à API do Apify, com APIFY_RETRIES tentativas quando a rede cai (URLError, conexão
    fechada, tempo esgotado) ou ele responde 429/5xx. Erros viram ApifyError legível, com o motivo da rede."""
    data = json.dumps(body).encode() if body is not None else None
    wait = APIFY_RETRY_WAIT
    for attempt in range(1, APIFY_RETRIES + 1):
        req = urllib.request.Request(f"{APIFY_API}{path}", data=data, method=method,
                                     headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8") or "null")
        except urllib.error.HTTPError as exc:
            if exc.code not in RETRY_CODES or attempt == APIFY_RETRIES:
                if exc.code == 404 and path.startswith("/acts/"):
                    raise ApifyError(f"ator {path.split('/')[2]} não encontrado no Apify (404).") from exc
                raise ApifyError(APIFY_HTTP_ERRORS.get(exc.code, f"o Apify respondeu {exc.code}.")) from exc
            problem = f"resposta {exc.code}"
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", None) or exc
            problem = f"{type(exc).__name__}: {reason}"
            if attempt == APIFY_RETRIES:
                raise ApifyError(f"sem resposta do Apify ({problem})."[:200]) from exc
        except ValueError as exc:
            raise ApifyError("o Apify devolveu algo que não é JSON.") from exc
        log.info("Apify %s %s: tentativa %d falhou (%s); de novo em %.0f s", method, path.split("?")[0], attempt,
                 problem, wait)
        time.sleep(wait)
        wait *= 2
    raise ApifyError("sem resposta do Apify.")  # não chega aqui


def apify_call(token: str, payload: dict[str, Any], actor: str | None = None) -> list[dict[str, Any]]:
    """Roda o ator (o do .env, ou outro com actor) e devolve os itens do dataset. Em vez do run-sync (uma
    conexão presa por minutos, que caía como URLError), inicia a execução, espera com GETs curtos
    (waitForFinish) e baixa os itens; cada passo tenta de novo se a rede falhar. Passou de APIFY_TIMEOUT:
    aborta e usa o que já veio. Erros viram ApifyError."""
    actor = actor or apify_actor()
    deadline = time.monotonic() + APIFY_TIMEOUT
    started = apify_api(token, "POST", f"/acts/{actor}/runs?timeout={APIFY_TIMEOUT}", payload)
    run = (started or {}).get("data") or {}
    if not run.get("id"):
        raise ApifyError("o Apify não iniciou a execução.")
    while run.get("status") in RUN_ACTIVE:
        left = deadline - time.monotonic()
        if left <= 0:
            try:
                apify_api(token, "POST", f"/actor-runs/{run['id']}/abort", {})
            except ApifyError:
                pass
            log.warning("Apify: execução %s passou de %d s; abortada, uso o que já veio", run["id"], APIFY_TIMEOUT)
            break
        answer = apify_api(token, "GET", f"/actor-runs/{run['id']}?waitForFinish={max(1, min(APIFY_POLL, int(left)))}",
                           timeout=APIFY_POLL + 30)
        run = (answer or {}).get("data") or run
    if run.get("status") == "FAILED":
        raise ApifyError(f"a execução do ator falhou no Apify ({(run.get('statusMessage') or 'sem detalhe')[:120]}).")
    dataset = run.get("defaultDatasetId")
    if not dataset:
        raise ApifyError("o Apify não devolveu a lista de posts.")
    body = apify_api(token, "GET", f"/datasets/{dataset}/items?clean=1&format=json", timeout=90)
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
    # eiv/twitter-scraper devolve o autor achatado no item (authorHandle, authorFollowers).
    handle = (pick(author, "userName", "username", "screen_name", default="")
              or pick(item, "authorHandle", "authorUsername", default="") or "")
    raw = pick(author, "followers", "followers_count", "followersCount")
    if raw is None:
        raw = pick(item, "authorFollowers")
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


def apify_accounts(cur, owner_id: int, everything: bool, interval_min: int | None = None,
                   folder_id: int | None = None) -> list[dict[str, Any]]:
    """Contas do usuário para ler agora: todas com o botão, senão as que passaram do intervalo (só as da
    pasta, com folder_id)."""
    cur.execute(
        """
        SELECT id, owner_id, username, display_name, followers_at
        FROM x_accounts
        WHERE owner_id = %s
          AND is_active IS TRUE
          AND COALESCE(is_archived, FALSE) IS FALSE
          AND (%s::int IS NULL OR folder_id = %s::int)
          AND (%s OR collected_at IS NULL OR collected_at < NOW() - make_interval(mins => %s))
        ORDER BY collected_at NULLS FIRST, id
        LIMIT %s
        """,
        (owner_id, folder_id, folder_id, everything, interval_min or int(apify_interval().total_seconds() // 60),
         APIFY_ROUND_ACCOUNTS),
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


def fetch_batch(token: str, handles: list[str]) -> list[dict[str, Any]]:
    """apify_fetch; no kaito, repete a busca que voltou só com itens falsos (erro do X que o ator engole).
    Conta que de fato não postou também volta assim: custa as repetições, mas não perde post."""
    items = apify_fetch(token, handles)
    if not is_kaito():
        return items
    for _ in range(KAITO_RETRIES):
        if any(not is_fake_item(item) for item in items):
            break
        time.sleep(KAITO_RETRY_WAIT)
        log.info(f"Apify: busca de {', '.join(handles[:3])}… voltou vazia; repetindo")
        items = apify_fetch(token, handles)
    return items


def apify_read(conn, token: str, accounts: list[dict[str, Any]], midnight: datetime,
               report: dict[str, Any] | None = None) -> int:
    """Lê as contas pelo Apify (lotes de APIFY_MAX_ACCOUNTS) e grava: posts de hoje, alertas de views,
    o dia refeito e seguidores (em toda leitura: vêm junto com os posts).

    Devolve os posts de hoje que vieram nesta leitura. Falha da chamada sobe como ApifyError antes de gravar
    o lote; conta sem post nenhum não é falha. A conta que a busca devolveu vazia é lida de novo pelo perfil
    (apify_profile, só ela; erro aí não derruba a leitura). Com report, anota em report["perfil"] as contas
    lidas pelo perfil e em report["sem_post"] as que nem assim trouxeram nada (o que já estava guardado fica).
    """
    size = batch_size()
    batches = [accounts[start:start + size] for start in range(0, len(accounts), size)]
    errors: list[ApifyError | None] = [None] * len(batches)

    def one(index: int) -> list[dict[str, Any]]:
        # Um lote que falha não derruba os outros: as contas dele ficam em report["falha"].
        try:
            return fetch_batch(token, [a["username"] for a in batches[index]])
        except ApifyError as exc:
            errors[index] = exc
            return []

    if len(batches) > 1:
        # Vários lotes: as chamadas vão juntas (cada uma é um run do ator); gravar fica numa conexão só.
        with ThreadPoolExecutor(max_workers=min(parallel_runs(), len(batches)), thread_name_prefix="apify-lote") as pool:
            answers = list(pool.map(one, range(len(batches))))
    else:
        answers = [one(0)] if batches else []
    if batches and all(errors):
        raise errors[0]  # nenhum lote veio: a leitura falhou
    failed = {a["username"] for batch, error in zip(batches, errors) if error for a in batch}
    for error in {str(e) for e in errors if e}:
        log.warning("um lote do Apify falhou: %s", error)
    # Reserva pelo perfil: contas sem item nenhum na busca (nem post antigo, nem repost). Pelo @ (V2), só nos lotes
    # que bateram no maxItems: num lote que não encheu, a conta sem item não existe ou não tem post nenhum.
    per = APIFY_HANDLE_ITEMS if by_handle() else None
    missing = [account["username"] for batch, items, error in zip(batches, answers, errors) if not error
               if per is None or len(items) >= per * len(batch)
               for account in batch if account["username"].lower() not in {apify_author(i)[0] for i in items}]
    by_profile: dict[str, list[dict[str, Any]]] = {}

    # Com o ator de linha do tempo como principal, quem faltou não tem para onde ir (já foi pelo perfil).
    # Com o kaito, quem faltou vai para o V2 pelo perfil (a busca dele falha com erro do X em parte das contas).
    if missing and not is_timeline_actor(apify_actor()):
        # Linha do tempo e V2 cobram por post, não por perfil: lê todas as que faltaram, não só 5.
        fallback = apify_fallback_actor()
        wanted = missing if is_timeline_actor(fallback) or fallback == APIFY_V2 else missing[:APIFY_PROFILE_MAX]
        try:
            for item in apify_profile(token, wanted):  # uma execução só, com todas as que faltaram
                author = apify_author(item)[0]
                for handle in wanted:
                    if handle.lower() == author:
                        by_profile.setdefault(handle, []).append(item)
        except ApifyError as exc:
            log.warning("reserva pelo perfil (%s) falhou: %s", ", ".join(wanted), exc)
    answers = [items + [i for account in batch for i in by_profile.get(account["username"], [])]
               for batch, items in zip(batches, answers)]
    if report is not None:
        # Diagnóstico de leitura vazia: o Apify não trouxe nada, só posts antigos, ou posts de outras contas?
        ours = {a["username"].lower() for a in accounts}
        everything = [i for items in answers for i in items if not is_fake_item(i)]
        authors = [apify_author(i)[0] for i in everything if tweet_id_of(i)]
        report["diag"] = diag = {}
        diag["itens"] = len(everything)
        diag["outros"] = sorted({a for a in authors if a and a not in ours})[:3]
        diag["sem_autor"] = sum(1 for i in everything if tweet_id_of(i) and not apify_author(i)[0])
        diag["antigos"] = sum(
            1 for i in everything if apify_author(i)[0] in ours and not is_repost(i)
            and (parse_created_at(pick(i, "created_at", "createdAt", "date", "posted_at")) or midnight) < midnight)
        diag["vazios"] = [i for i in everything if not tweet_id_of(i)][:1]
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
                today = save_tweets(cur, account, by_author.get(handle, []), midnight)
                saved += today
                if report is not None and account["username"] in failed:
                    report.setdefault("falha", []).append(account["username"])
                    report["falha_motivo"] = str(next(e for e in errors if e))
                elif report is not None:
                    # Lida pelo perfil e, à parte, sem post de hoje nesta leitura (o perfil traz os mais
                    # novos, que podem ser de ontem: a conta não pode passar como "com post").
                    if by_profile.get(account["username"]):
                        report.setdefault("perfil", []).append(account["username"])
                    if not today:
                        report.setdefault("sem_post", []).append(account["username"])
        conn.commit()
    return saved


READ_LOCK_NS = 5_817_001  # namespace do advisory lock "lendo as contas deste dono"


@contextmanager
def reading_lock(conn, owner_id: int) -> Iterator[bool]:
    """Uma leitura por dono de cada vez: o "Atualizar dados" (thread do painel) e o cron usam conexões
    diferentes e, sem isto, liam o mesmo dono juntos (duas execuções do ator, que só aceita uma, e crédito
    em dobro). Advisory lock de sessão do Postgres: sobrevive aos commits e some se a conexão cair.
    False = outra leitura deste dono está rodando agora."""
    with conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(%s, %s) AS ok", (READ_LOCK_NS, owner_id))
        row = cur.fetchone()
    conn.commit()
    got = bool(row and (row["ok"] if isinstance(row, dict) else row[0]))
    try:
        yield got
    finally:
        if got:
            try:
                conn.rollback()  # se a leitura quebrou no meio, a transação precisa sair do erro antes
                with conn.cursor() as cur:
                    cur.execute("SELECT pg_advisory_unlock(%s, %s)", (READ_LOCK_NS, owner_id))
                conn.commit()
            except Exception:  # conexão caída: o Postgres solta o lock sozinho
                log.warning("não soltou a trava de leitura do dono %s", owner_id)


OTHER_READ_WAIT = 12 * 60  # o painel desiste de esperar em 12 min (ROBO_RUN_MS no app.js)


def wait_other_read(conn, owner_id: int, timeout: float | None = None, step: float = 2.0) -> bool:
    """"Atualizar dados" com o cron já lendo este dono: espera essa leitura acabar em vez de pagar outra.
    True = havia outra leitura (e ela acabou ou o prazo passou); False = ninguém lendo, pode ler."""
    with reading_lock(conn, owner_id) as alone:
        if alone:
            return False
    deadline = time.monotonic() + (OTHER_READ_WAIT if timeout is None else timeout)
    while time.monotonic() < deadline:
        time.sleep(step)
        with reading_lock(conn, owner_id) as alone:
            if alone:
                break
    return True


def stored_today(cur, account_ids: list[int], midnight: datetime) -> int:
    """Posts de hoje guardados dessas contas (o que o painel mostra), não só os que vieram agora."""
    if not account_ids:
        return 0
    cur.execute("SELECT COUNT(*) AS n FROM tweet_samples WHERE account_id = ANY(%s) AND posted_at >= %s",
                (list(account_ids), midnight))
    row = cur.fetchone()
    return int((row["n"] if isinstance(row, dict) else row[0]) or 0)


def why_empty(report: dict[str, Any]) -> str:
    """O que o Apify devolveu, quando faltou post: é o que separa "a busca não acha" de "o robô descarta"."""
    total = report.get("itens") or 0
    if not total:
        return "o Apify devolveu 0 itens"
    parts = [f"o Apify devolveu {total} itens"]
    if report.get("antigos"):
        parts.append(f"{report['antigos']} posts de antes de hoje")
    if report.get("sem_autor"):
        parts.append(f"{report['sem_autor']} sem a @ do autor")
    if report.get("outros"):
        parts.append("de outras contas: " + ", ".join("@" + h for h in report["outros"]))
    empty = report.get("vazios") or []
    if empty and total == len(empty):
        parts.append("nenhum é post (" + ", ".join(sorted(empty[0])[:4]) + ")")
    return "; ".join(parts)


def read_detail(accounts: int, stored: int, report: dict[str, Any], folder_name: str | None = None) -> str:
    """Texto do fim da leitura: contas lidas, posts de hoje guardados e as contas sem post de hoje."""
    text = f"{accounts} contas lidas, {stored} posts de hoje"
    empty = len(report.get("sem_post") or [])
    if empty:
        text += f" ({empty} {'conta' if empty == 1 else 'contas'} sem post hoje)"
    failed = len(report.get("falha") or [])
    if failed:
        text += (f" · {failed} {'conta não lida' if failed == 1 else 'contas não lidas'} agora"
                 f" ({report.get('falha_motivo') or 'erro no Apify'}); vão na próxima leitura")
    profile = len(report.get("perfil") or [])
    if profile:
        text += f" · {profile} {'lida' if profile == 1 else 'lidas'} pelo perfil"
    if empty and "diag" in report:
        text += " · " + why_empty(report["diag"])
    return f"Pasta {folder_name}: {text}" if folder_name else text


def apify_user_round(conn, user: dict[str, Any], midnight: datetime) -> dict[str, Any]:
    """Lê pelo Apify as contas vencidas de um usuário, numa chamada só. Devolve o resumo. Se outra leitura
    desse usuário já está rodando (o botão ou o cron), sai sem ler: ela já traz os números."""
    with reading_lock(conn, user["id"]) as alone:
        if not alone:
            log.info("apify: usuário %s já está sendo lido; esta passada espera", user["id"])
            return {"owner": user["id"], "contas": 0, "ocupado": True}
        return _apify_user_round(conn, user, midnight)


def _apify_user_round(conn, user: dict[str, Any], midnight: datetime) -> dict[str, Any]:
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
        accounts = apify_accounts(cur, owner_id, requested, user.get("apify_interval_min"), user.get("folder_id"))
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
    report: dict[str, Any] = {}
    try:
        saved = apify_read(conn, token, accounts, midnight, report)
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
        stored = stored_today(cur, [a["id"] for a in accounts], midnight)
        set_apify_status(cur, owner_id, "ok", read_detail(len(accounts), stored, report, user.get("folder_name")))
    conn.commit()
    log.info("apify: usuário %s — %s contas, %s posts novos, %s de hoje guardados, sem item: %s", owner_id,
             len(accounts), saved, stored, report.get("sem_post"))
    return {"owner": owner_id, "contas": len(accounts), "tweets": stored, "novos": saved}


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


def admin_accounts(cur, folder_id: int | None = None) -> list[dict[str, Any]]:
    """Todas as contas ativas do admin (só as da pasta, com folder_id)."""
    return load_accounts(cur, 100_000, folder_id)


def apify_admin_read(conn, admin: dict[str, Any], started: datetime, midnight: datetime, manual: bool,
                     folder: dict[str, Any] | None = None) -> str | None:
    """Lê as contas do admin pelo Apify e grava o batimento. None = deu certo; texto = motivo da falha.
    Com folder ({"id", "name"}): só as contas dessa pasta ("Atualizar dados" com a pasta selecionada).
    Com outra leitura do admin rodando (botão e cron juntos), não lê de novo: o batimento dela vem já já."""
    with reading_lock(conn, admin["id"]) as alone:
        if not alone:
            log.info("apify do admin já está lendo; esta passada espera")
            return None
        return _apify_admin_read(conn, admin, started, midnight, manual, folder)


def _apify_admin_read(conn, admin: dict[str, Any], started: datetime, midnight: datetime, manual: bool,
                      folder: dict[str, Any] | None = None) -> str | None:
    with conn.cursor() as cur:
        accounts = admin_accounts(cur, folder["id"] if folder else None)
    conn.commit()
    folder_name = folder["name"] if folder else None
    log.info("início pelo Apify%s — %s contas, tweets depois de %s", " (Atualizar Dados)" if manual else "",
             len(accounts), midnight.isoformat())
    report: dict[str, Any] = {}
    try:
        saved = apify_read(conn, admin["token"], accounts, midnight, report)
    except ApifyError as exc:
        conn.rollback()
        reason = str(exc)
        hour = datetime.now(BR_TZ).strftime("%Y-%m-%dT%H")
        with conn.cursor() as cur:
            set_apify_status(cur, admin["id"], "erro", reason)
            alert_once(cur, f"apify:{admin['id']}:erro:{hour}", "apify", f"⚠️ Apify falhou: {reason}", admin["id"])
            record_run(cur, started, "failed", reason[:500], {"via": "apify", "contas": 0, "tweets": 0,
                                                               "falhas": len(accounts), "manual": manual,
                                                               "pasta": folder_name})
        conn.commit()
        log.warning("apify do admin falhou (%s)", reason)
        return reason
    with conn.cursor() as cur:
        # "tweets" é o total de hoje guardado dessas contas: uma leitura em que o Apify não trouxe um post não
        # faz o aviso dizer 0 (o post continua guardado). "novos" = o que veio nesta leitura.
        stored = stored_today(cur, [a["id"] for a in accounts], midnight)
        empty = report.get("sem_post") or []
        set_apify_status(cur, admin["id"], "ok", read_detail(len(accounts), stored, report, folder_name))
        record_run(cur, started, "ok", None, {"via": "apify", "contas": len(accounts), "tweets": stored,
                                               "novos": saved, "sem_post": len(empty),
                                               "perfil": len(report.get("perfil") or []), "falhas": 0,
                                               "manual": manual, "pasta": folder_name})
    conn.commit()
    log.info("fim pelo Apify — %s contas, %s posts novos, %s de hoje guardados, sem item: %s",
             len(accounts), saved, stored, empty)
    return None


def read_now(owner_id: int, folder_id: int | None = None) -> str:
    """"Atualizar dados" do painel (app/reader.py): lê as contas do usuário pelo Apify na hora, sem
    esperar o cron. Com folder_id (a pasta selecionada no painel), só as contas dessa pasta. Devolve "ok",
    "erro" (o motivo fica em user_settings), "sem_apify" ou "sem_pasta" (pasta que não é do usuário).
    Postgres e DATABASE_URL vêm do mesmo .env do painel.
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
        folder = None
        if folder_id is not None:
            with conn.cursor() as cur:
                cur.execute("SELECT id, name FROM folders WHERE id = %s AND owner_id = %s", (folder_id, owner_id))
                folder = cur.fetchone()
            conn.commit()
            if folder is None:
                return "sem_pasta"
        if wait_other_read(conn, owner_id):
            log.info("leitura na hora do usuário %s: o cron já estava lendo; usa a leitura dele", owner_id)
            return "ok"
        if user["is_admin"]:
            error = apify_admin_read(conn, {"id": owner_id, "token": user["apify_token"].strip()}, started, midnight,
                                     True, folder)
            return "ok" if error is None else "erro"
        result = apify_user_round(conn, {
            "id": owner_id, "apify_token": user["apify_token"], "apify_run_requested_at": started,
            "apify_auto": True, "apify_interval_min": user["apify_interval_min"],
            "folder_id": folder["id"] if folder else None, "folder_name": folder["name"] if folder else None,
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
