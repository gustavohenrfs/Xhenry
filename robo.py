"""Coletor de métricas do X para o VPS.

Lê contas em x_accounts, puxa a timeline com anyx (sessão henryyzog) e grava
somente tweets de hoje (America/Sao_Paulo) em tweet_samples.

    python3 robo.py
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
BR_TZ = ZoneInfo("America/Sao_Paulo")
SESSION_NAME = "henryyzog"
TWEET_LIMIT = 20
VIEW_ALERT = 500
SLEEP_BETWEEN_ACCOUNTS = 0.8

log = logging.getLogger("robo")


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
        "engagement_count": likes + rts + replies + quotes,
    }


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


def mark_alerted(cur, key: str, text: str) -> bool:
    cur.execute(
        """
        INSERT INTO alerts_sent (dedupe_key, kind, detail, sent_at)
        VALUES (%s, 'viral', %s, NOW())
        ON CONFLICT (dedupe_key) DO NOTHING
        """,
        (key, text[:500]),
    )


def upsert_sample(cur, account_id: int, tweet_id: str, posted_local: datetime, metrics: dict[str, int], preview: str | None) -> None:
    engagement = metrics["engagement_count"]
    views = metrics["view_count"]
    cur.execute(
        """
        INSERT INTO tweet_samples (
            account_id, tweet_id, posted_at, timezone_used, hour_local, weekday,
            text_preview, like_count, retweet_count, reply_count, quote_count,
            bookmark_count, impression_count, view_count, engagement_count,
            engagement_rate, collected_at
        ) VALUES (
            %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s,
            0, %s, %s, %s,
            %s, NOW()
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
            engagement_count = EXCLUDED.engagement_count,
            engagement_rate = EXCLUDED.engagement_rate,
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
            engagement,
            (engagement / views) if views else None,
        ),
    )


def recompute_today(cur, account_id: int, day) -> None:
    cur.execute(
        """
        INSERT INTO daily_metrics (
            account_id, metric_date, source, posts_count, likes, retweets, replies,
            quotes, bookmarks, views, impressions, engagement_count, collected_at
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
            COALESCE(SUM(engagement_count), 0),
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
            engagement_count = EXCLUDED.engagement_count,
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
        except Exception:
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
            quotes, bookmarks, views, impressions, engagement_count,
            followers_count, collected_at
        ) VALUES (%s, %s, 'public_api', 0, 0, 0, 0, 0, 0, 0, 0, 0, %s, NOW())
        ON CONFLICT (account_id, metric_date) DO UPDATE SET
            followers_count = EXCLUDED.followers_count,
            collected_at = NOW()
        """,
        (account_id, day, followers),
    )


def process_account(cur, session: Any, account: dict[str, Any], midnight: datetime) -> int:
    username = account["username"]
    account_id = account["id"]
    display = account.get("display_name") or username
    saved = 0
    today = midnight.date()

    # Seguidores primeiro: salva mesmo que a conta não tenha tweets hoje.
    try:
        followers = fetch_followers(session, username)
        if followers is not None:
            with cur.connection.transaction():  # savepoint: erro aqui não derruba o resto
                upsert_followers(cur, account_id, today, followers)
            log.info("@%s — %s seguidores", username, followers)
        else:
            log.warning("@%s — seguidores não encontrados no perfil", username)
    except Exception:
        log.exception("@%s seguidores falhou", username)

    try:
        tweets = iter_timeline(session, username)
    except Exception:
        log.exception("@%s timeline falhou", username)
        return 0
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
        if views > VIEW_ALERT:
            key = f"viral:{account_id}:{tid}"
            if not already_alerted(cur, key):
                when = local.strftime("%d/%m/%Y %H:%M")
                link = f"https://x.com/{username}/status/{tid}"
                text = (
                    "🚀 Post viralizando!\n"
                    f"👤 Conta: {display} (@{username})\n"
                    f"👁 Views: {views}\n"
                    f"⏰ Horário do post: {when}\n"
                    f"💬 Engajamento: {metrics['engagement_count']}\n"
                    f"🔗 Link do Post: {link}"
                )
                if send_telegram(text):
                    mark_alerted(cur, key, text)
                    log.info("alerta views @%s tweet %s (%s)", username, tid, views)
    recompute_today(cur, account_id, today)
    return saved


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    load_env(ROOT / ".env")
    midnight = today_start_br()
    log.info("início — tweets depois de %s", midnight.isoformat())

    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError:
        log.error("Instale psycopg: pip install 'psycopg[binary]'")
        return 1
    try:
        import anyx
    except ImportError:
        log.error("Instale anyx no VPS e confirme a sessão %s", SESSION_NAME)
        return 1

    saved_total = 0
    try:
        with anyx.Session(SESSION_NAME) as session:
            with psycopg.connect(database_url(), row_factory=dict_row) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT id, username, display_name
                        FROM x_accounts
                        WHERE is_active IS TRUE
                          AND COALESCE(is_archived, FALSE) IS FALSE
                        ORDER BY id
                        """
                    )
                    accounts = list(cur.fetchall())
                log.info("%s contas ativas", len(accounts))
                for index, account in enumerate(accounts):
                    with conn.cursor() as cur:
                        n = process_account(cur, session, account, midnight)
                        conn.commit()
                        saved_total += n
                        log.info("@%s — %s tweets de hoje", account["username"], n)
                    if index < len(accounts) - 1:
                        time.sleep(SLEEP_BETWEEN_ACCOUNTS)
    except Exception:
        log.exception("anyx.Session(%s) falhou — cookies da sessão no VPS?", SESSION_NAME)
        return 1
    log.info("fim — %s tweets gravados/atualizados", saved_total)
    return 0


if __name__ == "__main__":
    sys.exit(main())