"""Lógica de negócio compartilhada pelos roteadores."""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import httpx
from fastapi import HTTPException
from sqlalchemy import inspect, select, text
from sqlalchemy.orm import Session

from app.config import LOCAL_EMAIL, STATUS_LABELS, WEEKDAYS, X_API
from app.database import engine
from app.models import (
    DEFAULT_TZ,
    AlertSent,
    AppSetting,
    DailyMetric,
    DayBaseline,
    Folder,
    FolderDailyMetric,
    HealthCheck,
    HealthStatus,
    MetricSource,
    SyncKind,
    SyncRun,
    SyncStatus,
    TweetSample,
    User,
    XAccount,
)

log = logging.getLogger("xhenry")


class TokenMissing(Exception):
    pass


def bearer_token() -> str:
    return os.getenv("X_BEARER_TOKEN", "").strip()


def token_configured() -> bool:
    return bool(bearer_token())


def twitterapi_key() -> str:
    return os.getenv("TWITTERAPI_KEY", "").strip()


def ensure_user_auth_columns() -> None:
    # create_all não altera tabela que já existe.
    insp = inspect(engine)
    if "users" not in insp.get_table_names():
        return
    cols = {col["name"] for col in insp.get_columns("users")}
    statements = []
    if "senha_hash" not in cols:
        statements.append("ALTER TABLE users ADD COLUMN senha_hash VARCHAR(255)")
    if "nome_exibicao" not in cols:
        statements.append("ALTER TABLE users ADD COLUMN nome_exibicao VARCHAR(120)")
    if "avatar_url" not in cols:
        statements.append("ALTER TABLE users ADD COLUMN avatar_url VARCHAR(512)")
    with engine.begin() as conn:
        for statement in statements:
            conn.execute(text(statement))
        if "name" in cols:
            conn.execute(text("UPDATE users SET nome_exibicao = name WHERE nome_exibicao IS NULL"))


def ensure_owner(db: Session) -> User:
    user = db.scalar(select(User).where(User.email == LOCAL_EMAIL))
    if user is None:
        user = User(email=LOCAL_EMAIL, name="XHenry", nome_exibicao="XHenry")
        db.add(user)
        db.commit()
        db.refresh(user)
    return user


def normalize_username(value: str) -> str:
    handle = value.strip().lstrip("@").lower()
    if not handle or len(handle) > 15 or not all(ch.isalnum() or ch == "_" for ch in handle):
        raise HTTPException(400, "Handle inválido. Use 1 a 15 caracteres: letras, números ou _.")
    return handle


def zone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or DEFAULT_TZ)
    except Exception:
        return ZoneInfo(DEFAULT_TZ)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(value: datetime) -> datetime:
    # SQLite devolve datetime sem tz. O valor gravado é UTC.
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def period_window(period: str, today: date | None = None) -> tuple[date, date]:
    period = period.strip().lower()
    br_tz = ZoneInfo("America/Sao_Paulo")
    today = today or datetime.now(br_tz).date()
    if period in {"today", "hoje"}:
        return today, today
    if period in {"yesterday", "ontem"}:
        day = today - timedelta(days=1)
        return day, day
    span = {"7d": 7, "30d": 30, "3m": 90, "1y": 365, "365d": 365, "total": 3650}.get(period)
    if span is None:
        raise HTTPException(400, "Período inválido. Use today, yesterday, 7d, 30d, 3m, 1y ou total.")
    return today - timedelta(days=span - 1), today


def fnum(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)


def metric_value(metrics: dict | None, key: str) -> int | None:
    if not metrics or key not in metrics or metrics[key] is None:
        return None
    return int(metrics[key])


def engagement_of(metrics: dict | None) -> int:
    if not metrics:
        return 0
    return int(metrics.get("like_count") or 0) + int(metrics.get("retweet_count") or 0) + int(
        metrics.get("reply_count") or 0
    ) + int(metrics.get("quote_count") or 0)


def rate_of(engagement: int, impressions: int | None) -> float | None:
    if impressions is None or impressions <= 0:
        return None
    return engagement / impressions


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def error_text(body: Any) -> str:
    if not isinstance(body, dict):
        return ""
    parts: list[str] = []
    detail = body.get("detail")
    if isinstance(detail, str):
        parts.append(detail)
    for item in body.get("errors") or []:
        if isinstance(item, dict):
            parts.append(str(item.get("detail") or item.get("title") or ""))
    return " ".join(part for part in parts if part).strip()


def x_get(path: str, params: dict[str, Any]) -> tuple[int, dict]:
    token = bearer_token()
    if not token:
        raise TokenMissing()
    try:
        with httpx.Client(timeout=20.0, follow_redirects=False) as client:
            query = urlencode(params, safe=",:")
            url = X_API + path + ("?" + query if query else "")
            response = client.get(
                url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "User-Agent": "XHenry/1.0",
                },
            )
    except httpx.HTTPError as exc:
        log.warning("X API rede %s: %s", path, type(exc).__name__)
        return 0, {"detail": "Falha de rede ao falar com a API do X."}
    try:
        body = response.json()
    except ValueError:
        body = {"detail": response.text[:300]}
    if not isinstance(body, dict):
        body = {"data": body}
    return response.status_code, body


def lookup_user(username: str) -> tuple[int, dict]:
    return x_get(
        f"/users/by/username/{username}",
        {"user.fields": "created_at,description,public_metrics,verified,protected,profile_image_url"},
    )


def user_tweets(user_id: str, exclude: str | None = None) -> tuple[int, dict]:
    params: dict[str, Any] = {
        "max_results": 10,
        "tweet.fields": "created_at,public_metrics,lang,author_id",
    }
    if exclude:
        params["exclude"] = exclude
    return x_get(f"/users/{user_id}/tweets", params)


def search_recent(username: str) -> tuple[int, dict]:
    return x_get(
        "/tweets/search/recent",
        {
            "query": f"from:{username}",
            "max_results": 10,
            "tweet.fields": "created_at,public_metrics,lang,author_id",
        },
    )


def tweet_rows(body: dict, author_id: str | None = None) -> list[dict]:
    rows = body.get("data") if isinstance(body, dict) else None
    if not isinstance(rows, list):
        return []
    if author_id:
        rows = [row for row in rows if str(row.get("author_id") or author_id) == str(author_id)]
    return [row for row in rows if isinstance(row, dict) and row.get("id")]


def derive_status(
    *,
    had_known_user_id: bool,
    profile_status: int,
    profile_body: dict,
    protected: bool,
    timeline_status: int | None,
    timeline_ids: list[str],
    newest_tweet: datetime | None,
    search_status: int | None,
    search_ids: set[str],
) -> tuple[HealthStatus, str]:
    text = error_text(profile_body).lower()
    if profile_status == 401:
        return HealthStatus.API_ERROR, "Token rejeitado pela API do X (401)."
    if 429 in {profile_status, timeline_status, search_status}:
        return HealthStatus.RATE_LIMITED, "Limite da API do X. Tente de novo em alguns minutos."
    if profile_status == 0:
        return HealthStatus.API_ERROR, error_text(profile_body) or "Sem resposta da API do X."
    if profile_status == 404 or "not found" in text or "could not find" in text:
        if had_known_user_id:
            return HealthStatus.GHOST_BAN, "Perfil 404 numa conta que já tinha id salvo."
        return HealthStatus.NOT_FOUND, "Handle não encontrado, suspenso ou deletado."
    if "suspend" in text:
        return HealthStatus.SUSPENDED, "A API do X marcou a conta como suspensa."
    if profile_status == 403:
        return HealthStatus.API_ERROR, error_text(profile_body) or "403 no lookup. Não é search ban."
    data = profile_body.get("data") if isinstance(profile_body.get("data"), dict) else None
    if profile_status != 200 or not data:
        return HealthStatus.API_ERROR, error_text(profile_body) or f"Lookup HTTP {profile_status}."
    if protected:
        return HealthStatus.PROTECTED, "Conta protegida. Timeline e busca pública não servem para diagnosticar ban."
    if timeline_status in {403, 404} or search_status in {403, 404}:
        return HealthStatus.SEARCH_BAN, "A timeline ou a busca devolveu 403/404. Restrição real."
    if timeline_status not in {None, 200} or search_status not in {None, 200}:
        failed = timeline_status if timeline_status not in {None, 200} else search_status
        return HealthStatus.API_ERROR, f"Consulta de tweets falhou (HTTP {failed})."
    if not timeline_ids:
        return HealthStatus.HEALTHY, "Perfil existe e não há tweets recentes. Conta inativa, não é search ban."
    return HealthStatus.HEALTHY, "Perfil público sem 403/404 na timeline ou na busca."


def upsert_tweet(db: Session, account: XAccount, raw: dict) -> date | None:
    posted = parse_time(raw.get("created_at")) or now_utc()
    br_tz = ZoneInfo("America/Sao_Paulo")
    local = posted.astimezone(br_tz)
    local_date = local.date()
    # Filtra tweets muito antigos (lixo de 2013 etc.)
    br_today = datetime.now(br_tz).date()
    if (br_today - local_date).days > 7:
        return None
    metrics = raw.get("public_metrics") if isinstance(raw.get("public_metrics"), dict) else {}
    impressions = metric_value(metrics, "impression_count")
    views = metric_value(metrics, "view_count")
    engagement = engagement_of(metrics)
    preview = str(raw.get("text") or "").replace("\n", " ")[:280] or None
    row = db.scalar(
        select(TweetSample).where(TweetSample.account_id == account.id, TweetSample.tweet_id == str(raw["id"]))
    )
    if row is None:
        row = TweetSample(
            account_id=account.id,
            tweet_id=str(raw["id"]),
            posted_at=local,
            timezone_used="America/Sao_Paulo",
            hour_local=local.hour,
            weekday=local.weekday(),
        )
        db.add(row)
    row.text_preview = preview
    row.like_count = int(metrics.get("like_count") or 0)
    row.retweet_count = int(metrics.get("retweet_count") or 0)
    row.reply_count = int(metrics.get("reply_count") or 0)
    row.quote_count = int(metrics.get("quote_count") or 0)
    row.bookmark_count = int(metrics.get("bookmark_count") or 0)
    row.impression_count = impressions
    row.view_count = views
    row.engagement_count = engagement
    row.engagement_rate = rate_of(engagement, impressions)
    row.lang = raw.get("lang")
    row.collected_at = now_utc()
    return local_date


def recompute_days(db: Session, account: XAccount, days: set[date], profile_metrics: dict | None) -> None:
    tz = zone(account.timezone)
    today = datetime.now(tz).date()
    samples = list(db.scalars(select(TweetSample).where(TweetSample.account_id == account.id)).all())
    for day in days:
        day_rows = []
        for sample in samples:
            if as_utc(sample.posted_at).astimezone(tz).date() == day:
                day_rows.append(sample)
        impressions = None
        views = None
        if day_rows and all(row.impression_count is not None for row in day_rows):
            impressions = sum(int(row.impression_count or 0) for row in day_rows)
        if day_rows and all(row.view_count is not None for row in day_rows):
            views = sum(int(row.view_count or 0) for row in day_rows)
        engagement = sum(int(row.engagement_count or 0) for row in day_rows)
        row = db.scalar(
            select(DailyMetric).where(DailyMetric.account_id == account.id, DailyMetric.metric_date == day)
        )
        if row is None:
            row = DailyMetric(account_id=account.id, metric_date=day, source=MetricSource.PUBLIC_API)
            db.add(row)
        row.posts_count = len(day_rows)
        row.likes = sum(int(item.like_count or 0) for item in day_rows)
        row.retweets = sum(int(item.retweet_count or 0) for item in day_rows)
        row.replies = sum(int(item.reply_count or 0) for item in day_rows)
        row.quotes = sum(int(item.quote_count or 0) for item in day_rows)
        row.bookmarks = sum(int(item.bookmark_count or 0) for item in day_rows)
        row.impressions = impressions
        row.views = views
        row.engagement_count = engagement
        row.engagement_rate = rate_of(engagement, impressions)
        row.collected_at = now_utc()
        if day == today and profile_metrics:
            row.followers_count = metric_value(profile_metrics, "followers_count")
            row.following_count = metric_value(profile_metrics, "following_count")
            row.tweet_count = metric_value(profile_metrics, "tweet_count")
            row.listed_count = metric_value(profile_metrics, "listed_count")
            row.raw_public_metrics = profile_metrics
        rebuild_folder_day(db, account.folder_id, day)


def rebuild_folder_day(db: Session, folder_id: int, day: date) -> None:
    accounts = list(db.scalars(select(XAccount).where(XAccount.folder_id == folder_id)).all())
    metrics = []
    for account in accounts:
        row = db.scalar(
            select(DailyMetric).where(DailyMetric.account_id == account.id, DailyMetric.metric_date == day)
        )
        if row is not None:
            metrics.append((account, row))
    rollup = db.scalar(
        select(FolderDailyMetric).where(
            FolderDailyMetric.folder_id == folder_id, FolderDailyMetric.metric_date == day
        )
    )
    if not metrics:
        if rollup is not None:
            db.delete(rollup)
        return
    if rollup is None:
        rollup = FolderDailyMetric(folder_id=folder_id, metric_date=day)
        db.add(rollup)
    statuses = [account.last_health_status for account, _ in metrics]
    rollup.accounts_total = len(accounts)
    rollup.accounts_healthy = sum(1 for status in statuses if status == HealthStatus.HEALTHY)
    rollup.accounts_search_ban = sum(1 for status in statuses if status == HealthStatus.SEARCH_BAN)
    rollup.accounts_ghost_ban = sum(1 for status in statuses if status == HealthStatus.GHOST_BAN)
    rollup.accounts_other = rollup.accounts_total - (
        rollup.accounts_healthy + rollup.accounts_search_ban + rollup.accounts_ghost_ban
    )
    followers = [row.followers_count for _, row in metrics if row.followers_count is not None]
    impressions = [row.impressions for _, row in metrics if row.impressions is not None]
    views = [row.views for _, row in metrics if row.views is not None]
    rollup.followers_sum = sum(followers) if followers else None
    rollup.impressions = sum(impressions) if impressions else None
    rollup.views = sum(views) if views else None
    rollup.likes = sum(int(row.likes or 0) for _, row in metrics)
    rollup.retweets = sum(int(row.retweets or 0) for _, row in metrics)
    rollup.replies = sum(int(row.replies or 0) for _, row in metrics)
    rollup.quotes = sum(int(row.quotes or 0) for _, row in metrics)
    rollup.posts_count = sum(int(row.posts_count or 0) for _, row in metrics)
    rollup.engagement_count = sum(int(row.engagement_count or 0) for _, row in metrics)
    rollup.engagement_rate = rate_of(rollup.engagement_count, rollup.impressions)
    rollup.built_at = now_utc()


def apply_profile(account: XAccount, data: dict | None) -> None:
    if not data:
        return
    account.x_user_id = str(data.get("id") or account.x_user_id or "") or None
    account.display_name = account.display_name or data.get("name")
    account.bio = data.get("description")
    image = data.get("profile_image_url")
    if isinstance(image, str) and image.startswith("https://"):
        account.profile_image_url = image
    account.is_verified = bool(data.get("verified"))
    account.is_protected = bool(data.get("protected"))


def save_snapshot_only(db: Session, account: XAccount, metrics: dict | None) -> None:
    if not metrics:
        return
    day = datetime.now(zone(account.timezone)).date()
    row = db.scalar(select(DailyMetric).where(DailyMetric.account_id == account.id, DailyMetric.metric_date == day))
    if row is None:
        row = DailyMetric(account_id=account.id, metric_date=day, source=MetricSource.PUBLIC_API)
        db.add(row)
    row.followers_count = metric_value(metrics, "followers_count")
    row.following_count = metric_value(metrics, "following_count")
    row.tweet_count = metric_value(metrics, "tweet_count")
    row.listed_count = metric_value(metrics, "listed_count")
    row.raw_public_metrics = metrics
    row.collected_at = now_utc()
    rebuild_folder_day(db, account.folder_id, day)


def collect(username: str) -> dict[str, Any]:
    profile_status, profile_body = lookup_user(username)
    data = profile_body.get("data") if isinstance(profile_body.get("data"), dict) else None
    user_id = str(data.get("id")) if data and data.get("id") else None
    protected = bool(data.get("protected")) if data else False
    timeline_status = None
    search_status = None
    timeline: list[dict] = []
    search_ids: set[str] = set()
    if user_id and profile_status == 200 and not protected:
        timeline_status, timeline_body = user_tweets(user_id, exclude="retweets,replies")
        timeline = tweet_rows(timeline_body, user_id)
        if timeline_status == 200 and not timeline:
            timeline_status, timeline_body = user_tweets(user_id)
            timeline = tweet_rows(timeline_body, user_id)
        search_status, search_body = search_recent(username)
        search_ids = {str(row["id"]) for row in tweet_rows(search_body, user_id)}

    # TwitterAPI.io para Views, Likes, RTs e Replies (não entregues pela API oficial)
    key = twitterapi_key()
    if key and username:
        try:
            with httpx.Client(timeout=20.0) as client:
                resp = client.get(
                    f"https://api.twitterapi.io/twitter/user/last_tweets?userName={username}",
                    headers={"X-API-Key": key},
                )
                if resp.status_code == 200:
                    tw_data = resp.json()
                    log.info("TwitterAPI.io response for @%s: %s", username, str(tw_data)[:500])
                    tw_tweets = tw_data.get("data", []) if isinstance(tw_data, dict) else []
                    tw_map = {}
                    for t in tw_tweets:
                        tid = str(t.get("id") or t.get("tweet_id") or "") if isinstance(t, dict) else ""
                        if tid:
                            if isinstance(t, dict):
                                v = t.get("viewCount")
                                if isinstance(v, str):
                                    try:
                                        v = int(v)
                                    except ValueError:
                                        v = None
                                l = t.get("likeCount") or t.get("like_count") or t.get("likes")
                                if isinstance(l, str):
                                    try:
                                        l = int(l)
                                    except ValueError:
                                        l = None
                                r = t.get("retweetCount") or t.get("retweet_count") or t.get("retweets")
                                if isinstance(r, str):
                                    try:
                                        r = int(r)
                                    except ValueError:
                                        r = None
                                p = t.get("replyCount") or t.get("reply_count") or t.get("replies")
                                if isinstance(p, str):
                                    try:
                                        p = int(p)
                                    except ValueError:
                                        p = None
                            else:
                                v = l = r = p = None
                            if v is None:
                                log.warning("viewCount nulo para o tweet %s", tid)
                            tw_map[tid] = {
                                "view_count": v,
                                "like_count": l,
                                "retweet_count": r,
                                "reply_count": p,
                            }
                    for t in timeline:
                        tid = str(t.get("id") or "")
                        if tid in tw_map:
                            pm = t.setdefault("public_metrics", {})
                            pm["view_count"] = tw_map[tid].get("view_count")
                            pm["like_count"] = tw_map[tid].get("like_count")
                            pm["retweet_count"] = tw_map[tid].get("retweet_count")
                            pm["reply_count"] = tw_map[tid].get("reply_count")
        except Exception as exc:
            log.warning("TwitterAPI.io error for @%s: %s", username, exc)
            pass
    posted = [parse_time(row.get("created_at")) for row in timeline]
    posted = [item for item in posted if item is not None]
    return {
        "profile_status": profile_status,
        "profile_body": profile_body,
        "data": data,
        "protected": protected,
        "timeline_status": timeline_status,
        "timeline": timeline,
        "timeline_ids": [str(row["id"]) for row in timeline],
        "newest_tweet": max(posted) if posted else None,
        "search_status": search_status,
        "search_ids": search_ids,
    }


def persist_account(db: Session, account: XAccount, signals: dict[str, Any]) -> tuple[HealthStatus, str]:
    had_known = bool(account.x_user_id)
    status, detail = derive_status(
        had_known_user_id=had_known,
        profile_status=signals["profile_status"],
        profile_body=signals["profile_body"],
        protected=signals["protected"],
        timeline_status=signals["timeline_status"],
        timeline_ids=signals["timeline_ids"],
        newest_tweet=signals["newest_tweet"],
        search_status=signals["search_status"],
        search_ids=signals["search_ids"],
    )
    apply_profile(account, signals["data"])
    checked = now_utc()
    account.last_health_status = status
    account.last_health_checked_at = checked
    account.last_synced_at = checked
    matched = len([tweet_id for tweet_id in signals["timeline_ids"] if tweet_id in signals["search_ids"]])
    db.add(
        HealthCheck(
            account_id=account.id,
            checked_at=checked,
            status=status,
            profile_http_status=signals["profile_status"] or None,
            timeline_http_status=signals["timeline_status"],
            search_http_status=signals["search_status"],
            profile_found=signals["profile_status"] == 200 and bool(signals["data"]),
            had_known_user_id=had_known,
            timeline_tweet_count=len(signals["timeline_ids"]),
            search_hit_count=len(signals["search_ids"]),
            matched_recent_in_search=matched,
            detail=detail,
            evidence={
                "profile_status": signals["profile_status"],
                "timeline_status": signals["timeline_status"],
                "search_status": signals["search_status"],
                "timeline_tweet_count": len(signals["timeline_ids"]),
                "search_hit_count": len(signals["search_ids"]),
                "matched_recent_in_search": matched,
            },
        )
    )
    days: set[date] = set()
    for raw in signals["timeline"]:
        day = upsert_tweet(db, account, raw)
        if day:
            days.add(day)
    metrics = None
    if signals["data"] and isinstance(signals["data"].get("public_metrics"), dict):
        metrics = signals["data"]["public_metrics"]
    if days:
        recompute_days(db, account, days, metrics)
    else:
        save_snapshot_only(db, account, metrics)
    db.add(
        SyncRun(
            account_id=account.id,
            folder_id=account.folder_id,
            kind=SyncKind.HEALTH,
            status=SyncStatus.OK if status not in {HealthStatus.API_ERROR, HealthStatus.RATE_LIMITED} else SyncStatus.FAILED,
            started_at=checked,
            finished_at=checked,
            error=detail if status in {HealthStatus.API_ERROR, HealthStatus.RATE_LIMITED} else None,
            stats={"status": status.value, "tweets": len(signals["timeline_ids"])},
        )
    )
    return status, detail


def accounts_for(db: Session, handle: str, folder_id: int | None) -> list[XAccount]:
    query = select(XAccount).where(XAccount.username == handle, XAccount.is_active.is_(True))
    if folder_id is not None:
        query = query.where(XAccount.folder_id == folder_id)
    return list(db.scalars(query).all())


def check_handle(db: Session, handle: str, folder_id: int | None) -> dict[str, Any]:
    accounts = accounts_for(db, handle, folder_id)
    if folder_id is not None and not accounts:
        raise HTTPException(404, "Essa conta não está na pasta informada.")
    try:
        signals = collect(handle)
    except TokenMissing:
        raise HTTPException(503, "Defina X_BEARER_TOKEN no ambiente ou no .env ao lado do main.py.") from None
    if signals["profile_status"] == 401:
        raise HTTPException(502, "A API do X rejeitou o token (401). Gere outro Bearer no app do projeto.")
    results = []
    priors = []
    if accounts:
        for account in accounts:
            previous_status = account.last_health_status
            prior_imps = {
                row.tweet_id: row.impression_count
                for row in db.scalars(select(TweetSample).where(TweetSample.account_id == account.id)).all()
            }
            status, detail = persist_account(db, account, signals)
            results.append((account, status, detail))
            priors.append((account, previous_status, prior_imps))
        db.commit()
        for account, previous_status, prior_imps in priors:
            try:
                dispatch_alerts(db, account, signals, previous_status, prior_imps)
            except Exception:
                log.exception("alerta não enviado para @%s", account.username)
                db.rollback()
    else:
        status, detail = derive_status(
            had_known_user_id=False,
            profile_status=signals["profile_status"],
            profile_body=signals["profile_body"],
            protected=signals["protected"],
            timeline_status=signals["timeline_status"],
            timeline_ids=signals["timeline_ids"],
            newest_tweet=signals["newest_tweet"],
            search_status=signals["search_status"],
            search_ids=signals["search_ids"],
        )
        results.append((None, status, detail))
    account, status, detail = results[0]
    data = signals["data"] or {}
    metrics = data.get("public_metrics") if isinstance(data.get("public_metrics"), dict) else {}
    impressions = None
    timeline_metrics = [row.get("public_metrics") or {} for row in signals["timeline"]]
    if timeline_metrics and all("impression_count" in item for item in timeline_metrics):
        impressions = sum(int(item.get("impression_count") or 0) for item in timeline_metrics)
    engagement = sum(engagement_of(item) for item in timeline_metrics)
    log.info("check @%s -> %s", handle, status.value)
    return {
        "handle": handle,
        "status": status.value,
        "status_label": STATUS_LABELS[status.value],
        "detail": detail,
        "persisted": account is not None,
        "checked_at": now_utc().isoformat(),
        "profile": {
            "id": data.get("id"),
            "name": data.get("name"),
            "username": data.get("username") or handle,
            "protected": bool(data.get("protected")),
            "verified": bool(data.get("verified")),
            "followers_count": metric_value(metrics, "followers_count"),
            "following_count": metric_value(metrics, "following_count"),
            "tweet_count": metric_value(metrics, "tweet_count"),
            "profile_image_url": data.get("profile_image_url"),
        },
        "metrics": {
            "likes": sum(int((row.get("public_metrics") or {}).get("like_count") or 0) for row in signals["timeline"]),
            "retweets": sum(int((row.get("public_metrics") or {}).get("retweet_count") or 0) for row in signals["timeline"]),
            "replies": sum(int((row.get("public_metrics") or {}).get("reply_count") or 0) for row in signals["timeline"]),
            "impressions": impressions,
            "views": None,
            "engagement_count": engagement,
            "engagement_rate": rate_of(engagement, impressions),
            "posts_sampled": len(signals["timeline"]),
        },
    }


def account_payload(db: Session, account: XAccount, start: date, end: date) -> dict[str, Any]:
    tz = zone(account.timezone)
    latest = db.scalar(
        select(DailyMetric)
        .where(DailyMetric.account_id == account.id)
        .order_by(DailyMetric.metric_date.desc())
        .limit(1)
    )
    samples = [
        row
        for row in db.scalars(select(TweetSample).where(TweetSample.account_id == account.id)).all()
        if start <= as_utc(row.posted_at).astimezone(tz).date() <= end
    ]
    impressions = None
    views = None
    if samples and all(row.impression_count is not None for row in samples):
        impressions = sum(int(row.impression_count or 0) for row in samples)
    if samples and all(row.view_count is not None for row in samples):
        views = sum(int(row.view_count or 0) for row in samples)
    engagement = sum(int(row.engagement_count or 0) for row in samples)
    status = account.last_health_status.value if account.last_health_status else None
    last_check = db.scalar(
        select(HealthCheck)
        .where(HealthCheck.account_id == account.id)
        .order_by(HealthCheck.checked_at.desc())
        .limit(1)
    )
    checked_at = as_utc(account.last_health_checked_at).isoformat() if account.last_health_checked_at else None
    return {
        "id": account.id,
        "folder_id": account.folder_id,
        "folder_name": account.folder.name if account.folder else "",
        "username": account.username,
        "display_name": account.display_name,
        "profile_image_url": account.profile_image_url,
        "status": status,
        "status_label": STATUS_LABELS.get(status or "", "Sem verificação"),
        "detail": last_check.detail if last_check else None,
        "checked_at": checked_at,
        "followers": latest.followers_count if latest else None,
        "following": latest.following_count if latest else None,
        "likes": sum(int(row.like_count or 0) for row in samples),
        "retweets": sum(int(row.retweet_count or 0) for row in samples),
        "replies": sum(int(row.reply_count or 0) for row in samples),
        "impressions": impressions,
        "views": views,
        "engagement": engagement,
        "engagement_rate": rate_of(engagement, impressions),
        "posts": len(samples),
    }


def suggestion_payload(samples: list[TweetSample], basis: str) -> dict[str, Any]:
    buckets: dict[int, list[TweetSample]] = {}
    for sample in samples:
        buckets.setdefault(sample.hour_local, []).append(sample)
    ranked = []
    for hour, rows in buckets.items():
        avg = sum(int(row.engagement_count or 0) for row in rows) / len(rows)
        by_day: dict[int, list[int]] = {}
        for row in rows:
            by_day.setdefault(row.weekday, []).append(int(row.engagement_count or 0))
        best_day = max(by_day, key=lambda key: sum(by_day[key]) / len(by_day[key]))
        ranked.append(
            {
                "hour": hour,
                "label": f"{hour:02d}:00–{(hour + 1) % 24:02d}:00",
                "avg_engagement": round(avg, 2),
                "samples": len(rows),
                "best_weekday": WEEKDAYS[best_day],
            }
        )
    ranked.sort(key=lambda item: (item["avg_engagement"], item["samples"]), reverse=True)
    top = ranked[:3]
    if not top:
        return {
            "basis": basis,
            "items": [],
            "note": "Sem tweets coletados. Verifique as contas para montar o histórico.",
        }
    if all(item["avg_engagement"] == 0 for item in top):
        return {
            "basis": basis,
            "items": [],
            "note": "Os posts coletados ainda estão com engajamento zerado. Sem horário recomendado.",
        }
    note = "Horários com maior média de curtidas + reposts + replies + quotes nos posts coletados."
    if any(item["samples"] < 3 for item in top):
        note += " Amostra pequena: trate como indício, não como regra."
    if basis == "all":
        note += " Poucos posts no período; a sugestão usa todo o histórico coletado."
    return {"basis": basis, "items": top, "note": note}


def build_suggestions(db: Session, accounts: list[XAccount], start: date, end: date) -> dict[str, Any]:
    if not accounts:
        return suggestion_payload([], "period")
    ids = [account.id for account in accounts]
    all_rows = list(db.scalars(select(TweetSample).where(TweetSample.account_id.in_(ids))).all())
    in_period = []
    for row in all_rows:
        local_day = as_utc(row.posted_at).astimezone(zone(row.timezone_used)).date()
        if start <= local_day <= end:
            in_period.append(row)
    if len(in_period) >= 3:
        return suggestion_payload(in_period, "period")
    return suggestion_payload(all_rows, "all" if all_rows else "period")


def selected_accounts(db: Session, folder_id: int | None) -> list[XAccount]:
    query = select(XAccount).where(XAccount.is_active.is_(True))
    if folder_id is not None:
        query = query.where(XAccount.folder_id == folder_id)
    return list(db.scalars(query).all())


def dashboard(db: Session, period: str, folder_id: int | None) -> dict[str, Any]:
    start, end = period_window(period)
    folders = list(db.scalars(select(Folder).where(Folder.is_archived.is_(False)).order_by(Folder.name)).all())
    if folder_id is not None and all(folder.id != folder_id for folder in folders):
        raise HTTPException(404, "Pasta não encontrada.")
    accounts = selected_accounts(db, folder_id)
    cards = [account_payload(db, account, start, end) for account in accounts]
    by_day: dict[date, dict[str, Any]] = {}
    day = start
    while day <= end:
        by_day[day] = {"date": day.isoformat(), "engagement": 0, "impressions": None, "posts": 0, "_imps": []}
        day += timedelta(days=1)
    for account in accounts:
        tz = zone(account.timezone)
        for sample in db.scalars(select(TweetSample).where(TweetSample.account_id == account.id)).all():
            local_day = as_utc(sample.posted_at).astimezone(tz).date()
            if local_day not in by_day:
                continue
            bucket = by_day[local_day]
            bucket["engagement"] += int(sample.engagement_count or 0)
            bucket["posts"] += 1
            bucket["_imps"].append(sample.impression_count)
    series = []
    for key in sorted(by_day):
        bucket = by_day[key]
        imps = bucket.pop("_imps")
        if imps and all(item is not None for item in imps):
            bucket["impressions"] = sum(int(item or 0) for item in imps)
        series.append(bucket)
    folder_cards = []
    for folder in folders:
        members = [card for card in [account_payload(db, account, start, end) for account in folder.accounts]]
        folder_cards.append(
            {
                "id": folder.id,
                "name": folder.name,
                "kind": folder.kind.value,
                "color": folder.color,
                "description": folder.description,
                "accounts": len(folder.accounts),
                "healthy": sum(1 for card in members if card["status"] == "healthy"),
                "search_ban": sum(1 for card in members if card["status"] == "search_ban"),
                "ghost_ban": sum(1 for card in members if card["status"] == "ghost_ban"),
            }
        )
    followers = [card["followers"] for card in cards if card["followers"] is not None]
    views_values = [card.get("views") for card in cards if card.get("views") is not None]
    likes_values = [card.get("likes") for card in cards if card.get("likes") is not None]
    retweets_values = [card.get("retweets") for card in cards if card.get("retweets") is not None]
    replies_values = [card.get("replies") for card in cards if card.get("replies") is not None]
    impression_values = [card["impressions"] for card in cards if card["impressions"] is not None]
    return {
        "period": period,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "token_configured": token_configured(),
        "kpis": {
            "accounts": len(cards),
            "healthy": sum(1 for card in cards if card["status"] == "healthy"),
            "search_ban": sum(1 for card in cards if card["status"] == "search_ban"),
            "ghost_ban": sum(1 for card in cards if card["status"] == "ghost_ban"),
            "followers": sum(followers) if followers else None,
            "views": sum(views_values) if views_values else None,
            "likes": sum(likes_values) if likes_values else None,
            "retweets": sum(retweets_values) if retweets_values else None,
            "replies": sum(replies_values) if replies_values else None,
            "engagement": sum(int(card["engagement"] or 0) for card in cards),
            "impressions": sum(impression_values) if impression_values else None,
        },
        "folders": folder_cards,
        "accounts": cards,
        "series": series,
        "by_account": [
            {
                "username": card["username"],
                "engagement": card["engagement"],
                "impressions": card["impressions"],
            }
            for card in cards
        ],
        "suggestions": build_suggestions(db, accounts, start, end),
    }


def get_settings(db: Session) -> AppSetting:
    row = db.get(AppSetting, 1)
    if row is None:
        row = AppSetting(id=1, impression_goal=500)
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


def telegram_creds(db: Session) -> tuple[str, str]:
    row = get_settings(db)
    token = (row.telegram_token or os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    chat = (row.telegram_chat_id or os.getenv("TELEGRAM_CHAT_ID") or "").strip()
    return token, chat


def xai_key(db: Session) -> str:
    row = get_settings(db)
    return (row.xai_api_key or os.getenv("XAI_API_KEY") or "").strip()


def send_telegram(token: str, chat_id: str, text: str) -> None:
    if not token or not chat_id:
        raise HTTPException(503, "Configure o token do bot e o Chat ID em Configurações.")
    try:
        with httpx.Client(timeout=20.0, follow_redirects=False) as client:
            response = client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text},
            )
    except httpx.HTTPError as exc:
        raise HTTPException(502, "Falha de rede ao falar com o Telegram.") from exc
    if response.status_code != 200:
        detail = ""
        try:
            detail = str(response.json().get("description") or "")
        except ValueError:
            detail = ""
        raise HTTPException(502, detail or "O Telegram recusou a mensagem.")


def viral_message(name: str, username: str, gain: int, when: str, likes: int, reposts: int, link: str) -> str:
    return (
        "🚀 Post viralizando!\n"
        f"👤 Conta: {name} (@{username})\n"
        f"👁 Ganho de impressao no post: {gain}\n"
        f"⏰ Horário do post: {when}\n"
        f"💬 Engajamento: {likes + reposts}\n"
        f"🔗 Link do Post: {link}"
    )


def try_send(db: Session, key: str, kind: str, text: str) -> bool:
    token, chat = telegram_creds(db)
    if not token or not chat:
        return False
    if db.scalar(select(AlertSent).where(AlertSent.dedupe_key == key)):
        return False
    try:
        send_telegram(token, chat, text)
    except HTTPException as exc:
        log.warning("telegram %s não enviado: %s", kind, exc.detail)
        return False
    db.add(AlertSent(dedupe_key=key, kind=kind, detail=text[:500], sent_at=now_utc()))
    db.commit()
    return True


def dispatch_alerts(
    db: Session,
    account: XAccount,
    signals: dict[str, Any],
    previous_status: HealthStatus | None,
    prior_imps: dict[str, int | None],
) -> None:
    settings = get_settings(db)
    goal = int(settings.impression_goal or 500)
    name = account.display_name or account.username
    tz = zone(account.timezone)
    local_now = datetime.now(tz)
    for raw in signals["timeline"]:
        metrics = raw.get("public_metrics") if isinstance(raw.get("public_metrics"), dict) else {}
        current = metric_value(metrics, "impression_count")
        if current is None or current < goal:
            continue
        previous = prior_imps.get(str(raw["id"]))
        gain = current - int(previous or 0)
        posted = parse_time(raw.get("created_at")) or now_utc()
        when = as_utc(posted).astimezone(tz).strftime("%d/%m/%Y %H:%M")
        likes = int(metrics.get("like_count") or 0)
        reposts = int(metrics.get("retweet_count") or 0)
        link = f"https://x.com/{account.username}/status/{raw['id']}"
        try_send(
            db,
            f"viral:{account.id}:{raw['id']}",
            "viral",
            viral_message(name, account.username, gain, when, likes, reposts, link),
        )
    status = account.last_health_status
    if status in {HealthStatus.SEARCH_BAN, HealthStatus.GHOST_BAN} and status != previous_status:
        label = STATUS_LABELS[status.value]
        try_send(
            db,
            f"ban:{account.id}:{status.value}:{local_now.date().isoformat()}",
            "ban",
            f"⚠️ Possível restrição\n👤 Conta: {name} (@{account.username})\nStatus: {label}\nO alcance pode ter sido cortado.",
        )
    sampled = signals["timeline"]
    if not sampled:
        return
    avg = round(sum(engagement_of(row.get("public_metrics") or {}) for row in sampled) / len(sampled))
    baseline = db.scalar(
        select(DayBaseline).where(DayBaseline.account_id == account.id, DayBaseline.metric_date == local_now.date())
    )
    if baseline is None:
        baseline = DayBaseline(account_id=account.id, metric_date=local_now.date())
        db.add(baseline)
    if local_now.hour < 18:
        baseline.day_avg = avg
        db.commit()
        return
    if baseline.day_avg and avg < baseline.day_avg * 0.7 and not baseline.drop_alerted:
        drop = round((1 - avg / baseline.day_avg) * 100)
        sent = try_send(
            db,
            f"drop:{account.id}:{local_now.date().isoformat()}",
            "drop",
            f"⚠️ Queda de engajamento\n👤 Conta: {name} (@{account.username})\n📉 Do dia para a noite: -{drop}%\nPode ser sombra no alcance. Confira o status da conta.",
        )
        baseline.drop_alerted = sent or baseline.drop_alerted
    db.commit()


def settings_payload(row: AppSetting) -> dict[str, Any]:
    return {
        "telegram_token_set": bool(row.telegram_token or os.getenv("TELEGRAM_BOT_TOKEN")),
        "telegram_chat_id": row.telegram_chat_id or os.getenv("TELEGRAM_CHAT_ID") or "",
        "impression_goal": row.impression_goal,
        "xai_key_set": bool(row.xai_api_key or os.getenv("XAI_API_KEY")),
    }


def grok_text(body: dict) -> str:
    parts: list[str] = []
    for item in body.get("output") or []:
        if not isinstance(item, dict):
            continue
        if isinstance(item.get("content"), str):
            parts.append(item["content"])
        for chunk in item.get("content") or []:
            if isinstance(chunk, dict) and chunk.get("text"):
                parts.append(str(chunk["text"]))
    if not parts and body.get("choices"):
        message = body["choices"][0].get("message") or {}
        content = message.get("content")
        if isinstance(content, str):
            parts.append(content)
    return "\n".join(part.strip() for part in parts if part and part.strip())


def ask_grok(key: str, caption: str, image_uri: str | None) -> str:
    prompt = (
        "Avalie se este post do X corre risco de restrição da plataforma.\n"
        f"Legenda:\n{caption}\n\n"
        "Se houver imagem, diga se ela sugere nudez, violência gráfica, sangue, ameaça "
        "ou outro motivo comum de limite no X. Responda em português, em um parágrafo curto: "
        "veredito (liberado, atenção ou risco alto) e o motivo em uma frase. "
        "Não descreva conteúdo sexual de forma gráfica e não ensine a burlar a moderação."
    )
    content: list[dict[str, Any]] = []
    if image_uri:
        content.append({"type": "input_image", "image_url": image_uri, "detail": "high"})
    content.append({"type": "input_text", "text": prompt})
    model = os.getenv("XAI_MODEL", "grok-4.6")
    with httpx.Client(timeout=60.0, follow_redirects=False) as client:
        response = client.post(
            "https://api.x.ai/v1/responses",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"model": model, "input": [{"role": "user", "content": content}]},
        )
        if response.status_code >= 400:
            parts: list[dict[str, Any]] = []
            if image_uri:
                parts.append({"type": "image_url", "image_url": {"url": image_uri}})
            parts.append({"type": "text", "text": prompt})
            response = client.post(
                "https://api.x.ai/v1/chat/completions",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={"model": model, "messages": [{"role": "user", "content": parts}]},
            )
    if response.status_code >= 400:
        raise HTTPException(502, "A xAI recusou a análise. Confira a chave e o modelo.")
    try:
        text = grok_text(response.json())
    except ValueError as exc:
        raise HTTPException(502, "A xAI não devolveu um veredito legível.") from exc
    return text or "A análise não devolveu texto."
