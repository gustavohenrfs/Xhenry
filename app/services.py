"""Lógica de negócio compartilhada pelos roteadores.

Dashboard lê tweet_samples e daily_metrics.
A API oficial do X entra só no health check (/api/check).
Métricas de tweets vêm do robo.py no VPS.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import date, datetime, time, timedelta, timezone
from itertools import accumulate
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import httpx
from fastapi import HTTPException
from sqlalchemy import func, inspect, select, text
from sqlalchemy.orm import Session

from app.config import DEFAULT_ADMIN_EMAIL, LOCAL_EMAIL, STATUS_LABELS, WEEKDAYS, X_API
from app.database import engine
from app.models import (
    DEFAULT_TZ,
    AlertSent,
    AppSetting,
    DailyMetric,
    Folder,
    FolderDailyMetric,
    HealthCheck,
    HealthStatus,
    MetricSource,
    SyncKind,
    SyncRun,
    SyncStatus,
    PushSubscription,
    TweetSample,
    User,
    UserSetting,
    XAccount,
)

log = logging.getLogger("xhenry")


class TokenMissing(Exception):
    pass


def bearer_token() -> str:
    """Bearer da API do X do .env: só o admin usa (os outros colam o próprio em Configurações)."""
    return os.getenv("X_BEARER_TOKEN", "").strip()


def token_configured() -> bool:
    return bool(bearer_token())


def admin_email() -> str:
    return (os.getenv("ADMIN_EMAIL") or DEFAULT_ADMIN_EMAIL).strip().lower()


def ensure_user_auth_columns() -> None:
    # create_all não altera tabela que já existe.
    insp = inspect(engine)
    tables = insp.get_table_names()
    if "x_accounts" in tables:
        account_cols = {col["name"] for col in insp.get_columns("x_accounts")}
        if "is_archived" not in account_cols:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE x_accounts ADD COLUMN is_archived BOOLEAN NOT NULL DEFAULT FALSE"))
        kind = "TIMESTAMP WITH TIME ZONE" if engine.dialect.name == "postgresql" else "TIMESTAMP"
        for column in ("archived_at", "collected_at", "followers_at"):
            if column not in account_cols:
                with engine.begin() as conn:
                    conn.execute(text(f"ALTER TABLE x_accounts ADD COLUMN {column} {kind}"))
        if "owner_id" not in account_cols:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE x_accounts ADD COLUMN owner_id INTEGER REFERENCES users(id) ON DELETE CASCADE"))
    if "user_settings" in tables:
        user_setting_cols = {col["name"] for col in insp.get_columns("user_settings")}
        added = {
            "apify_auto": "BOOLEAN NOT NULL DEFAULT TRUE",
            "apify_interval_min": "INTEGER NOT NULL DEFAULT 60",
            "notify_prefs": "TEXT",
            "pause_on": "BOOLEAN NOT NULL DEFAULT TRUE",
            "pause_from": "VARCHAR(5) NOT NULL DEFAULT '00:00'",
            "pause_to": "VARCHAR(5) NOT NULL DEFAULT '06:00'",
            "summary_read_on": "DATE",
        }
        for column, ddl in added.items():
            if column not in user_setting_cols:
                with engine.begin() as conn:
                    conn.execute(text(f"ALTER TABLE user_settings ADD COLUMN {column} {ddl}"))
    # Multiusuário: dono do alerta e da inscrição de push (NULL = sistema / ainda sem dono).
    for table in ("alerts_sent", "push_subscriptions"):
        if table in tables and "owner_id" not in {col["name"] for col in insp.get_columns(table)}:
            with engine.begin() as conn:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN owner_id INTEGER REFERENCES users(id) ON DELETE CASCADE"))
    if "app_settings" in tables:
        setting_cols = {col["name"] for col in insp.get_columns("app_settings")}
        kind = "TIMESTAMP WITH TIME ZONE" if engine.dialect.name == "postgresql" else "TIMESTAMP"
        added = {
            "robo_enabled": "BOOLEAN NOT NULL DEFAULT TRUE",
            "robo_interval_min": "INTEGER NOT NULL DEFAULT 5",
            "robo_run_requested_at": kind,
            "vapid_private_key": "TEXT",
            "push_last_alert_id": "INTEGER",
            "apify_interval_min": "INTEGER NOT NULL DEFAULT 30",
        }
        for column, ddl in added.items():
            if column not in setting_cols:
                with engine.begin() as conn:
                    conn.execute(text(f"ALTER TABLE app_settings ADD COLUMN {column} {ddl}"))
    if "collectors" in tables:
        collector_cols = {col["name"] for col in insp.get_columns("collectors")}
        kind = "TIMESTAMP WITH TIME ZONE" if engine.dialect.name == "postgresql" else "TIMESTAMP"
        added = {"test_requested_at": kind, "test_status": "VARCHAR(16)", "test_detail": "TEXT", "test_at": kind}
        for column, ddl in added.items():
            if column not in collector_cols:
                with engine.begin() as conn:
                    conn.execute(text(f"ALTER TABLE collectors ADD COLUMN {column} {ddl}"))
    # Índices novos em tabelas que já existem (create_all também não cria esses).
    indexes = {
        "ix_tweet_samples_collected": "tweet_samples (collected_at)",
        "ix_daily_metrics_collected": "daily_metrics (collected_at)",
        "ix_alerts_sent_kind_id": "alerts_sent (kind, id)",
        "ix_sync_runs_heartbeat": "sync_runs (kind, account_id, finished_at)",
        "ix_x_accounts_owner_id": "x_accounts (owner_id)",
        "ix_alerts_sent_owner_id": "alerts_sent (owner_id)",
        "ix_push_subscriptions_owner_id": "push_subscriptions (owner_id)",
    }
    for name, target in indexes.items():
        if target.split(" ")[0] in tables:
            with engine.begin() as conn:
                conn.execute(text(f"CREATE INDEX IF NOT EXISTS {name} ON {target}"))
    if "users" not in tables:
        return
    cols = {col["name"] for col in insp.get_columns("users")}
    statements = []
    if "senha_hash" not in cols:
        statements.append("ALTER TABLE users ADD COLUMN senha_hash VARCHAR(255)")
    if "nome_exibicao" not in cols:
        statements.append("ALTER TABLE users ADD COLUMN nome_exibicao VARCHAR(120)")
    if "avatar_url" not in cols:
        statements.append("ALTER TABLE users ADD COLUMN avatar_url VARCHAR(512)")
    if "logo_url" not in cols:
        statements.append("ALTER TABLE users ADD COLUMN logo_url VARCHAR(512)")
    if "token_version" not in cols:
        statements.append("ALTER TABLE users ADD COLUMN token_version INTEGER NOT NULL DEFAULT 0")
    if "is_admin" not in cols:
        statements.append("ALTER TABLE users ADD COLUMN is_admin BOOLEAN NOT NULL DEFAULT FALSE")
    if "is_approved" not in cols:
        statements.append("ALTER TABLE users ADD COLUMN is_approved BOOLEAN NOT NULL DEFAULT FALSE")
    if "approved_at" not in cols:
        kind = "TIMESTAMP WITH TIME ZONE" if engine.dialect.name == "postgresql" else "TIMESTAMP"
        statements.append(f"ALTER TABLE users ADD COLUMN approved_at {kind}")
    if "invited_by_id" not in cols:
        statements.append("ALTER TABLE users ADD COLUMN invited_by_id INTEGER REFERENCES users(id) ON DELETE SET NULL")
    with engine.begin() as conn:
        for statement in statements:
            conn.execute(text(statement))
        if "name" in cols:
            conn.execute(text("UPDATE users SET nome_exibicao = name WHERE nome_exibicao IS NULL"))


def ensure_admin(db: Session) -> User:
    """Garante o admin (ADMIN_EMAIL) e passa para ele o que era do dono antigo, local@suzana.

    Roda a cada startup e não muda nada na segunda vez. Sem o usuário do admin, o local@suzana vira ele
    (mesma linha, só troca o email); sem nenhum dos dois, cria o admin sem senha, que entra pelo
    "Esqueceu a senha?" (o link vai para o Telegram do .env, que é dele).
    """
    email = admin_email()
    admin = db.scalar(select(User).where(func.lower(User.email) == email))
    legacy = db.scalar(select(User).where(User.email == LOCAL_EMAIL)) if email != LOCAL_EMAIL else None
    if admin is None and legacy is not None:
        legacy.email = email
        admin, legacy = legacy, None
    elif admin is None:
        admin = User(email=email, name="XHenry", nome_exibicao="XHenry")
        db.add(admin)
        db.flush()
    if legacy is not None:
        taken = set(db.scalars(select(Folder.name).where(Folder.owner_id == admin.id)).all())
        for folder in db.scalars(select(Folder).where(Folder.owner_id == legacy.id)).all():
            if folder.name in taken:
                folder.name = f"{folder.name} (antiga)"[:120]
            folder.owner_id = admin.id
        for model in (AlertSent, PushSubscription):
            db.execute(model.__table__.update().where(model.owner_id == legacy.id).values(owner_id=admin.id))
        db.flush()
    if not admin.is_admin or not admin.is_approved:
        admin.is_admin = admin.is_approved = True
        admin.approved_at = admin.approved_at or now_utc()
    # Um admin só: quem tinha a marca e não é mais o ADMIN_EMAIL perde (troca de email no .env).
    for other in db.scalars(select(User).where(User.is_admin.is_(True), User.id != admin.id)).all():
        other.is_admin = False
    db.commit()
    backfill_owners(db, admin)
    return admin


def backfill_owners(db: Session, admin: User) -> None:
    """Preenche os donos que ficaram vazios ou desencontrados: contas pela pasta, alertas pela conta da
    chave, push e configurações do tempo de um usuário só para o admin."""
    owner = "(SELECT owner_id FROM folders WHERE folders.id = x_accounts.folder_id)"
    db.execute(text(f"UPDATE x_accounts SET owner_id = {owner} WHERE owner_id IS NULL OR owner_id <> {owner}"))
    # Chaves com a conta no 2º pedaço: viral:{conta}:…, queda:{conta}:…, ban:{conta}:…
    for row in db.scalars(
        select(AlertSent).where(AlertSent.owner_id.is_(None), AlertSent.kind.in_(["viral", "queda", "ban"]))
    ).all():
        parts = row.dedupe_key.split(":")
        if len(parts) > 1 and parts[1].isdigit():
            row.owner_id = db.scalar(select(XAccount.owner_id).where(XAccount.id == int(parts[1])))
    db.execute(
        PushSubscription.__table__.update().where(PushSubscription.owner_id.is_(None)).values(owner_id=admin.id)
    )
    if db.scalar(select(UserSetting).where(UserSetting.owner_id == admin.id)) is None:
        legacy = db.get(AppSetting, 1)
        db.add(
            UserSetting(
                owner_id=admin.id,
                impression_goal=legacy.impression_goal if legacy else 500,
                xai_api_key=legacy.xai_api_key if legacy else None,
            )
        )
    db.commit()


def admin_user_id(db: Session) -> int | None:
    return db.scalar(select(User.id).where(User.is_admin.is_(True)).order_by(User.id).limit(1))


def admin_apify_active(db: Session) -> bool:
    """O admin tem token do Apify: as contas dele são lidas pelo Apify e os coletores ficam de reserva."""
    token = db.scalar(
        select(UserSetting.apify_token)
        .join(User, User.id == UserSetting.owner_id)
        .where(User.is_admin.is_(True))
        .order_by(User.id)
        .limit(1)
    )
    return bool((token or "").strip())


def in_window(start: str, end: str, clock: str) -> bool:
    """clock entre start e end (HH:MM), atravessando a meia-noite se start > end."""
    if start == end:
        return False
    return start <= clock < end if start < end else (clock >= start or clock < end)


def pause_payload(row: UserSetting) -> dict[str, Any]:
    return {"on": bool(row.pause_on), "from": row.pause_from or "00:00", "to": row.pause_to or "06:00"}


def apply_pause(row: UserSetting, on: bool | None, start: str | None, end: str | None) -> None:
    """Grava a pausa da madrugada (horários HH:MM); 400 se algum horário não vale."""
    for value in (start, end):
        if value is not None and not HHMM.match(value):
            raise HTTPException(400, "Horário inválido. Use HH:MM.")
    if start is not None:
        row.pause_from = start
    if end is not None:
        row.pause_to = end
    if on is not None:
        row.pause_on = on


def paused_now(row: UserSetting | None, now: datetime | None = None) -> bool:
    if row is None or not row.pause_on:
        return False
    clock = (now or now_utc()).astimezone(zone(DEFAULT_TZ)).strftime("%H:%M")
    return in_window(row.pause_from or "00:00", row.pause_to or "06:00", clock)


def last_pause_end(row: UserSetting | None, now: datetime | None = None) -> datetime | None:
    """Fim da pausa mais recente (já passado): o vigia conta o silêncio a partir dele."""
    if row is None or not row.pause_on:
        return None
    now = now or now_utc()
    local = now.astimezone(zone(DEFAULT_TZ))
    hour, minute = (int(part) for part in (row.pause_to or "06:00").split(":"))
    end = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if end > local:
        end -= timedelta(days=1)
    return end.astimezone(timezone.utc)


def robot_interval(db: Session, settings: AppSetting) -> int:
    """Minutos entre as rodadas do robô nas contas do admin: o do Apify quando ele está ativo."""
    if admin_apify_active(db):
        return settings.apify_interval_min or 30
    return settings.robo_interval_min


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


def x_get(path: str, params: dict[str, Any], token: str = "") -> tuple[int, dict]:
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


def lookup_user(username: str, token: str = "") -> tuple[int, dict]:
    return x_get(
        f"/users/by/username/{username}",
        {"user.fields": "created_at,description,public_metrics,verified,protected,profile_image_url"},
        token=token,
    )


def user_tweets(user_id: str, exclude: str | None = None, token: str = "") -> tuple[int, dict]:
    params: dict[str, Any] = {
        "max_results": 10,
        "tweet.fields": "created_at,public_metrics,lang,author_id",
    }
    if exclude:
        params["exclude"] = exclude
    return x_get(f"/users/{user_id}/tweets", params, token=token)


def search_recent(username: str, token: str = "") -> tuple[int, dict]:
    return x_get(
        "/tweets/search/recent",
        {
            "query": f"from:{username}",
            "max_results": 10,
            "tweet.fields": "created_at,public_metrics,lang,author_id",
        },
        token=token,
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


def collect_health(username: str, token: str = "") -> dict[str, Any]:
    """Health check via API oficial. Não grava métricas de tweets."""
    profile_status, profile_body = lookup_user(username, token)
    data = profile_body.get("data") if isinstance(profile_body.get("data"), dict) else None
    user_id = str(data.get("id")) if data and data.get("id") else None
    protected = bool(data.get("protected")) if data else False
    timeline_status = None
    search_status = None
    timeline: list[dict] = []
    search_ids: set[str] = set()
    if user_id and profile_status == 200 and not protected:
        timeline_status, timeline_body = user_tweets(user_id, exclude="retweets,replies", token=token)
        timeline = tweet_rows(timeline_body, user_id)
        if timeline_status == 200 and not timeline:
            timeline_status, timeline_body = user_tweets(user_id, token=token)
            timeline = tweet_rows(timeline_body, user_id)
        search_status, search_body = search_recent(username, token)
        search_ids = {str(row["id"]) for row in tweet_rows(search_body, user_id)}
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
    metrics = None
    if signals["data"] and isinstance(signals["data"].get("public_metrics"), dict):
        metrics = signals["data"]["public_metrics"]
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


def accounts_for(db: Session, owner_id: int, handle: str, folder_id: int | None) -> list[XAccount]:
    query = owned_accounts(owner_id).where(
        XAccount.username == handle, XAccount.is_active.is_(True), XAccount.is_archived.is_(False)
    )
    if folder_id is not None:
        query = query.where(XAccount.folder_id == folder_id)
    return list(db.scalars(query).all())


NO_X_TOKEN = "Cole o seu Bearer Token da API do X em Configurações para usar o Verificar."


def check_handle(db: Session, owner: User, handle: str, folder_id: int | None) -> dict[str, Any]:
    """Verificação de saúde com o token da API do X de quem pediu; só grava nas contas dele."""
    accounts = accounts_for(db, owner.id, handle, folder_id)
    if folder_id is not None and not accounts:
        raise HTTPException(404, "Essa conta não está na pasta informada.")
    try:
        signals = collect_health(handle, x_token(db, owner))
    except TokenMissing:
        raise HTTPException(503, NO_X_TOKEN) from None
    if signals["profile_status"] == 401:
        raise HTTPException(502, "A API do X rejeitou o token (401). Gere outro Bearer no app do projeto.")
    results = []
    priors = []
    if accounts:
        for account in accounts:
            previous_status = account.last_health_status
            status, detail = persist_account(db, account, signals)
            results.append((account, status, detail))
            priors.append((account, previous_status))
        db.commit()
        for account, previous_status in priors:
            try:
                dispatch_alerts(db, account, previous_status)
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
    }


def account_payload(db: Session, account: XAccount, start: date, end: date) -> dict[str, Any]:
    latest = db.scalar(
        select(DailyMetric)
        .where(DailyMetric.account_id == account.id)
        .order_by(DailyMetric.metric_date.desc())
        .limit(1)
    )
    last_check = db.scalar(
        select(HealthCheck)
        .where(HealthCheck.account_id == account.id)
        .order_by(HealthCheck.checked_at.desc())
        .limit(1)
    )
    samples = [row for _, row, day in load_samples(db, [account]) if start <= day <= end]
    return account_card(account, samples, latest, last_check.detail if last_check else None)


def account_card(account: XAccount, samples: list, latest: DailyMetric | None, detail: str | None) -> dict[str, Any]:
    """Cartão de uma conta a partir dos posts do período já carregados (sem consultar o banco)."""
    impressions = views = likes = retweets = replies = 0
    for row in samples:  # uma passada só
        views += int(row.view_count or 0)
        impressions += int(row.impression_count or row.view_count or 0)
        likes += int(row.like_count or 0)
        retweets += int(row.retweet_count or 0)
        replies += int(row.reply_count or 0)
    status = account.last_health_status.value if account.last_health_status else None
    checked_at = as_utc(account.last_health_checked_at).isoformat() if account.last_health_checked_at else None
    return {
        "id": account.id,
        "folder_id": account.folder_id,
        "is_archived": account.is_archived,
        "folder_name": account.folder.name if account.folder else "",
        "username": account.username,
        "display_name": account.display_name,
        "profile_image_url": account.profile_image_url,
        "status": status,
        "status_label": STATUS_LABELS.get(status or "", "Sem verificação"),
        "detail": detail,
        "checked_at": checked_at,
        "followers": latest.followers_count if latest else None,
        "following": latest.following_count if latest else None,
        "likes": likes,
        "retweets": retweets,
        "replies": replies,
        "impressions": impressions if samples else None,
        "views": views,
        "posts": len(samples),
    }


def suggestion_payload(samples: list[TweetSample], basis: str) -> dict[str, Any]:
    buckets: dict[int, list[TweetSample]] = {}
    for sample in samples:
        buckets.setdefault(sample.hour_local, []).append(sample)
    ranked = []
    for hour, rows in buckets.items():
        avg = sum(int(row.view_count or 0) for row in rows) / len(rows)
        by_day: dict[int, list[int]] = {}
        for row in rows:
            by_day.setdefault(row.weekday, []).append(int(row.view_count or 0))
        best_day = max(by_day, key=lambda key: sum(by_day[key]) / len(by_day[key]))
        ranked.append(
            {
                "hour": hour,
                "label": f"{hour:02d}:00–{(hour + 1) % 24:02d}:00",
                "avg_views": round(avg, 2),
                "samples": len(rows),
                "best_weekday": WEEKDAYS[best_day],
            }
        )
    ranked.sort(key=lambda item: (item["avg_views"], item["samples"]), reverse=True)
    top = ranked[:3]
    if not top:
        return {
            "basis": basis,
            "items": [],
            "note": "Sem posts lidos. Verifique as contas para montar o histórico.",
        }
    if all(item["avg_views"] == 0 for item in top):
        return {
            "basis": basis,
            "items": [],
            "note": "Os posts lidos ainda estão sem views. Sem horário recomendado.",
        }
    note = "Horários com maior média de views nos posts lidos."
    if any(item["samples"] < 3 for item in top):
        note += " Amostra pequena: trate como indício, não como regra."
    if basis == "all":
        note += " Poucos posts no período; a sugestão usa todo o histórico lido."
    return {"basis": basis, "items": top, "note": note}


def heatmap(samples: list[TweetSample]) -> list[list[int | None]]:
    """Média de views por dia da semana (0 = segunda) e hora do post; None onde não houve post."""
    sums = [[0] * 24 for _ in range(7)]
    counts = [[0] * 24 for _ in range(7)]
    for row in samples:
        sums[row.weekday][row.hour_local] += int(row.view_count or 0)
        counts[row.weekday][row.hour_local] += 1
    return [
        [round(sums[day][hour] / counts[day][hour]) if counts[day][hour] else None for hour in range(24)]
        for day in range(7)
    ]


def build_suggestions(db: Session, accounts: list[XAccount], start: date, end: date) -> dict[str, Any]:
    return suggestions_from([row for _, row, _ in load_samples(db, accounts)], start, end)


def suggestions_from(all_rows: list, start: date, end: date, everything=None) -> dict[str, Any]:
    """Melhores horários e mapa de calor; o dia de cada post vem do fuso gravado no próprio post.

    all_rows precisa cobrir o período. Com menos de 3 posts nele a sugestão usa o histórico todo:
    everything() o carrega quando all_rows é só uma janela.
    """
    zones: dict[str | None, ZoneInfo] = {}
    in_period = []
    for row in all_rows:
        day = getattr(row, "used_day", None)
        if day is None:
            tz = zones.get(row.timezone_used) or zones.setdefault(row.timezone_used, zone(row.timezone_used))
            day = as_utc(row.posted_at).astimezone(tz).date()
        if start <= day <= end:
            in_period.append(row)
    if len(in_period) < 3 and everything is not None:
        all_rows = everything()
    rows, basis = (in_period, "period") if len(in_period) >= 3 else (all_rows, "all" if all_rows else "period")
    return suggestion_payload(rows, basis) | {"heatmap": heatmap(rows)}


def owned_accounts(owner_id: int):
    """select das contas de um usuário. O dono vem da pasta, a fonte da verdade."""
    return select(XAccount).join(Folder, Folder.id == XAccount.folder_id).where(Folder.owner_id == owner_id)


def owned_account(db: Session, owner_id: int, account_id: int) -> XAccount:
    """A conta, se for deste usuário; de outro dono é 404, como se não existisse."""
    account = db.scalar(owned_accounts(owner_id).where(XAccount.id == account_id))
    if account is None:
        raise HTTPException(404, "Conta não encontrada.")
    return account


def owned_folder(db: Session, owner_id: int, folder_id: int) -> Folder:
    folder = db.scalar(select(Folder).where(Folder.id == folder_id, Folder.owner_id == owner_id))
    if folder is None:
        raise HTTPException(404, "Pasta não encontrada.")
    return folder


def selected_accounts(db: Session, owner_id: int, folder_id: int | None) -> list[XAccount]:
    """Contas do usuário que entram no dashboard e nas verificações: ativas e não arquivadas."""
    query = owned_accounts(owner_id).where(XAccount.is_active.is_(True), XAccount.is_archived.is_(False))
    if folder_id is not None:
        query = query.where(XAccount.folder_id == folder_id)
    return list(db.scalars(query).all())


# Rótulo da comparação com o período anterior, do mesmo tamanho. "Total" não tem anterior.
COMPARE_LABELS = {
    "today": "vs ontem",
    "hoje": "vs ontem",
    "yesterday": "vs anteontem",
    "ontem": "vs anteontem",
    "7d": "vs 7 dias anteriores",
    "30d": "vs 30 dias anteriores",
    "3m": "vs 3 meses anteriores",
    "1y": "vs ano anterior",
    "365d": "vs ano anterior",
}
SPARK_DAYS = 14
SAMPLE_FIELDS = (
    ("views", "view_count"),
    ("likes", "like_count"),
    ("retweets", "retweet_count"),
    ("replies", "reply_count"),
    ("quotes", "quote_count"),
    ("bookmarks", "bookmark_count"),
)


# Só as colunas que o dashboard usa: linhas leves em vez de objetos do ORM (bem mais rápido com o
# histórico grande).
SAMPLE_COLUMNS = (
    TweetSample.account_id,
    TweetSample.tweet_id,
    TweetSample.posted_at,
    TweetSample.timezone_used,
    TweetSample.hour_local,
    TweetSample.weekday,
    TweetSample.text_preview,
    TweetSample.view_count,
    TweetSample.impression_count,
    TweetSample.like_count,
    TweetSample.retweet_count,
    TweetSample.reply_count,
    TweetSample.quote_count,
    TweetSample.bookmark_count,
)
Loaded = list[tuple[XAccount, Any, date]]


class Sample:
    """Um post já pronto para as contas do dashboard: números em int e a hora local calculada uma vez
    só (antes cada post era convertido de fuso várias vezes por montagem)."""

    __slots__ = (
        "account_id", "tweet_id", "posted_at", "timezone_used", "hour_local", "weekday", "text_preview",
        "view_count", "impression_count", "like_count", "retweet_count", "reply_count", "quote_count",
        "bookmark_count", "local", "used_day",
    )

    def __init__(self, row: tuple, tz: ZoneInfo, tz_name: str, zones: dict[str | None, ZoneInfo]) -> None:
        # Desempacota a tupla na ordem de SAMPLE_COLUMNS (bem mais rápido que ler a linha por nome).
        (self.account_id, self.tweet_id, posted, used_tz, self.hour_local, self.weekday, self.text_preview,
         views, self.impression_count, likes, rts, replies, quotes, bookmarks) = row
        posted = posted if posted.tzinfo is not None else posted.replace(tzinfo=timezone.utc)
        self.posted_at = posted
        self.timezone_used = used_tz
        self.view_count = views or 0
        self.like_count = likes or 0
        self.retweet_count = rts or 0
        self.reply_count = replies or 0
        self.quote_count = quotes or 0
        self.bookmark_count = bookmarks or 0
        local = posted.astimezone(tz)
        self.local = local
        # Dia no fuso gravado no próprio post (sugestões de horário); quase sempre o mesmo da conta.
        if used_tz == tz_name or not used_tz:
            self.used_day = local.date()
        else:
            other = zones.get(used_tz) or zones.setdefault(used_tz, zone(used_tz))
            self.used_day = posted.astimezone(other).date()


def load_samples(db: Session, accounts: list[XAccount], since: date | None = None) -> Loaded:
    """Posts das contas numa consulta só, cada um com a conta e o dia local (fuso da conta).

    Com since, só a partir desse dia local (um dia de folga no UTC cobre qualquer fuso).
    """
    by_id = {account.id: account for account in accounts}
    if not by_id:
        return []
    zones = {account.id: zone(account.timezone) for account in accounts}
    used: dict[str | None, ZoneInfo] = {}
    query = select(*SAMPLE_COLUMNS).where(TweetSample.account_id.in_(by_id)).order_by(TweetSample.id)
    if since is not None:
        query = query.where(TweetSample.posted_at >= datetime.combine(since - timedelta(days=1), time.min, timezone.utc))
    loaded = []
    append = loaded.append
    for row in db.execute(query):  # Row já desempacota como tupla
        account_id = row[0]
        account = by_id[account_id]
        sample = Sample(row, zones[account_id], account.timezone, used)
        append((account, sample, sample.local.date()))
    return loaded


def within(loaded: Loaded, start: date, end: date) -> list[tuple[XAccount, Any]]:
    """Posts publicados entre start e end (dia no fuso de cada conta), com a conta de cada um."""
    return [(account, row) for account, row, day in loaded if start <= day <= end]


def window_totals(pairs: list[tuple[XAccount, TweetSample]]) -> dict[str, Any]:
    """Somas de um período, views por post e taxa de interação (interações ÷ views, em %)."""
    views = likes = retweets = replies = quotes = bookmarks = 0
    for _, row in pairs:  # uma passada só (eram seis)
        views += int(row.view_count or 0)
        likes += int(row.like_count or 0)
        retweets += int(row.retweet_count or 0)
        replies += int(row.reply_count or 0)
        quotes += int(row.quote_count or 0)
        bookmarks += int(row.bookmark_count or 0)
    totals: dict[str, Any] = {
        "views": views, "likes": likes, "retweets": retweets, "replies": replies, "quotes": quotes, "bookmarks": bookmarks,
    }
    posts = len(pairs)
    interactions = sum(totals[key] for key in ("likes", "retweets", "replies", "quotes", "bookmarks"))
    views = totals["views"]
    totals.update(
        {
            "posts": posts,
            "interactions": interactions,
            "views_per_post": round(views / posts) if posts else None,
            "interaction_rate": round(interactions / views * 100, 2) if views else None,
        }
    )
    return totals


def followers_gained(db: Session, accounts: list[XAccount], start: date, end: date) -> int | None:
    """Saldo de seguidores no período: último valor até end menos o último antes de start.

    Sem leitura antes de start, a base é a primeira leitura dentro do período.
    """
    total = None
    if not accounts:
        return None
    by_account: dict[int, list[tuple[date, int]]] = {}
    for account_id, day, count in db.execute(
        select(DailyMetric.account_id, DailyMetric.metric_date, DailyMetric.followers_count)
        .where(
            DailyMetric.account_id.in_([account.id for account in accounts]),
            DailyMetric.followers_count.is_not(None),
            DailyMetric.metric_date <= end,
        )
        .order_by(DailyMetric.account_id, DailyMetric.metric_date)
    ):
        by_account.setdefault(account_id, []).append((day, count))
    for rows in by_account.values():
        inside = [count for day, count in rows if day >= start]
        if not inside:
            continue
        before = [count for day, count in rows if day < start]
        base = before[-1] if before else inside[0]
        total = (total or 0) + int(inside[-1]) - int(base)
    return total


def top_posts(pairs: list[tuple[XAccount, TweetSample]], total_views: int, limit: int = 10) -> list[dict[str, Any]]:
    """Os posts com mais views no período, com o link de cada um no X."""
    ranked = sorted(pairs, key=lambda pair: (int(pair[1].view_count or 0), int(pair[1].like_count or 0)), reverse=True)
    return [post_payload(account, row, total_views) for account, row in ranked[:limit]]


def post_payload(account: XAccount, row: TweetSample, total_views: int) -> dict[str, Any]:
    views = int(row.view_count or 0)
    return {
        "username": account.username,
        "display_name": account.display_name,
        "profile_image_url": account.profile_image_url,
        "tweet_id": row.tweet_id,
        "url": f"https://x.com/{account.username}/status/{row.tweet_id}",
        "text": row.text_preview,
        "posted_at": as_utc(row.posted_at).isoformat(),
        "views": views,
        "likes": int(row.like_count or 0),
        "retweets": int(row.retweet_count or 0),
        "replies": int(row.reply_count or 0),
        "quotes": int(row.quote_count or 0),
        "share": round(views / total_views * 100, 1) if total_views else None,
    }


def local_posted(account: XAccount, row: TweetSample) -> datetime:
    local = getattr(row, "local", None)  # Sample já traz a hora local
    return local if local is not None else as_utc(row.posted_at).astimezone(zone(account.timezone))


def growth_payload(
    pairs: list[tuple[XAccount, TweetSample]],
    prev_pairs: list[tuple[XAccount, TweetSample]] | None,
    start: date,
    end: date,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Cartão "Potencial de Crescimento".

    Um dia só: views acumuladas pela hora do post, com o dia anterior na mesma escala, e o
    ritmo "até agora" (hoje até a hora atual contra o dia anterior até a mesma hora).
    Vários dias: views por dia, com o período anterior alinhado dia a dia.
    """
    now = now or datetime.now(ZoneInfo(DEFAULT_TZ))
    span = (end - start).days + 1
    if span == 1:
        size, labels = 24, [f"{hour:02d}h" for hour in range(24)]

        def slot(account: XAccount, row: TweetSample, base: date) -> int:
            return local_posted(account, row).hour
    else:
        size = span
        labels = [(start + timedelta(days=offset)).isoformat() for offset in range(span)]

        def slot(account: XAccount, row: TweetSample, base: date) -> int:
            return (local_posted(account, row).date() - base).days

    def buckets(rows, base):
        views, posts = [0] * size, [0] * size
        for account, row in rows:
            index = slot(account, row, base)
            if 0 <= index < size:
                views[index] += int(row.view_count or 0)
                posts[index] += 1
        return views, posts

    views, posts = buckets(pairs, start)
    previous = buckets(prev_pairs, start - timedelta(days=span))[0] if prev_pairs is not None else None
    best = max(range(size), key=lambda index: views[index]) if any(views) else None
    payload: dict[str, Any] = {
        "mode": "hour" if span == 1 else "day",
        "labels": labels,
        "posts": posts,
        "best": {"label": labels[best], "views": views[best]} if best is not None else None,
        "pace": None,
    }
    if span == 1:
        last = now.hour if end == now.date() else 23
        current = list(accumulate(views))
        payload["current"] = [value if hour <= last else None for hour, value in enumerate(current)]
        payload["previous"] = list(accumulate(previous)) if previous is not None else None
        payload["avg"] = round(current[last] / (last + 1))
        if previous is not None:
            payload["pace"] = {
                "until": labels[last],
                "current": current[last],
                "previous": payload["previous"][last],
            }
    else:
        elapsed = min(span, (now.date() - start).days + 1) if end == now.date() else span
        payload["current"] = views
        payload["previous"] = previous
        payload["avg"] = round(sum(views) / max(1, elapsed))
    return payload


def dashboard(db: Session, owner_id: int, period: str, folder_id: int | None) -> dict[str, Any]:
    start, end = period_window(period)
    folders = list(
        db.scalars(
            select(Folder).where(Folder.owner_id == owner_id, Folder.is_archived.is_(False)).order_by(Folder.name)
        ).all()
    )
    if folder_id is not None and all(folder.id != folder_id for folder in folders):
        raise HTTPException(404, "Pasta não encontrada.")
    accounts = selected_accounts(db, owner_id, folder_id)
    ids = [account.id for account in accounts]
    # Uma consulta só, da janela que o cartão mais antigo precisa (período anterior ou a linha de
    # 14 dias) até o fim; cada janela abaixo é só um filtro em memória.
    span = (end - start).days + 1
    since = min(start - timedelta(days=span), end - timedelta(days=SPARK_DAYS - 1))
    loaded = load_samples(db, accounts, since)
    pairs = within(loaded, start, end)
    period_rows: dict[int, list] = {}
    for account, row in pairs:
        period_rows.setdefault(account.id, []).append(row)
    latest, details = latest_metrics(db, ids), last_check_details(db, ids)
    cards = [
        account_card(account, period_rows.get(account.id, []), latest.get(account.id), details.get(account.id))
        for account in accounts
    ]
    last_collected = None
    if ids:
        stamps = [
            db.scalar(select(func.max(TweetSample.collected_at)).where(TweetSample.account_id.in_(ids))),
            db.scalar(select(func.max(DailyMetric.collected_at)).where(DailyMetric.account_id.in_(ids))),
        ]
        stamps = [as_utc(item) for item in stamps if item is not None]
        last_collected = max(stamps).isoformat() if stamps else None
    # "Total" começa 10 anos atrás: os gráficos começam no primeiro post (os números não mudam).
    chart_start = start
    if period.strip().lower() == "total" and loaded:
        # pelo menos 14 dias: com um dia só o gráfico viraria o de hora em hora
        chart_start = max(start, min(min(day for _, _, day in loaded), end - timedelta(days=SPARK_DAYS - 1)))
    by_day: dict[date, dict[str, Any]] = {}
    day = chart_start
    while day <= end:
        by_day[day] = {"date": day.isoformat(), "views": 0, "impressions": None, "posts": 0, "_imps": []}
        day += timedelta(days=1)
    for _, sample, local_day in loaded:
        bucket = by_day.get(local_day)
        if bucket is None:
            continue
        bucket["views"] += int(sample.view_count or 0)
        bucket["posts"] += 1
        bucket["_imps"].append(sample.impression_count)
    series = []
    for key in sorted(by_day):
        bucket = by_day[key]
        imps = bucket.pop("_imps")
        if imps and all(item is not None for item in imps):
            bucket["impressions"] = sum(int(item or 0) for item in imps)
        series.append(bucket)
    # Pastas: só contagens pelo status gravado na conta, todas as contas numa consulta.
    statuses: dict[int, list[str | None]] = {folder.id: [] for folder in folders}
    for folder_ref, status in db.execute(
        select(XAccount.folder_id, XAccount.last_health_status).where(
            XAccount.folder_id.in_(list(statuses)), XAccount.is_archived.is_(False)
        )
    ):
        statuses[folder_ref].append(status.value if status else None)
    folder_cards = []
    for folder in folders:
        members = statuses[folder.id]
        folder_cards.append(
            {
                "id": folder.id,
                "name": folder.name,
                "kind": folder.kind.value,
                "color": folder.color,
                "description": folder.description,
                "accounts": len(members),
                "healthy": members.count("healthy"),
                "search_ban": members.count("search_ban"),
                "ghost_ban": members.count("ghost_ban"),
            }
        )
    followers = [card["followers"] for card in cards if card["followers"] is not None]
    views_values = [card.get("views") for card in cards if card.get("views") is not None]
    likes_values = [card.get("likes") for card in cards if card.get("likes") is not None]
    retweets_values = [card.get("retweets") for card in cards if card.get("retweets") is not None]
    replies_values = [card.get("replies") for card in cards if card.get("replies") is not None]
    impression_values = [card["impressions"] for card in cards if card["impressions"] is not None]
    current = window_totals(pairs)
    ranking = top_posts(pairs, current["views"])
    today = datetime.now(ZoneInfo(DEFAULT_TZ)).date()
    today_views = sum(
        int(row.view_count or 0)
        for _, row in (pairs if start <= today <= end and start == end else within(loaded, today, today))
    )
    # Linha do cartão de Views: o período, ou os últimos 14 dias quando o período é curto demais.
    spark_start = min(chart_start, end - timedelta(days=SPARK_DAYS - 1))
    spark = {spark_start + timedelta(days=offset): 0 for offset in range((end - spark_start).days + 1)}
    # Cada conta: mini-linha dos últimos 14 dias.
    mini_start = end - timedelta(days=SPARK_DAYS - 1)
    mini = {account.id: [0] * SPARK_DAYS for account in accounts}
    for account, row in within(loaded, spark_start, end) if spark_start < start else pairs:
        day = local_posted(account, row).date()
        spark[day] += int(row.view_count or 0)
        if day >= mini_start:
            mini[account.id][(day - mini_start).days] += int(row.view_count or 0)
    compare_label = COMPARE_LABELS.get(period.strip().lower())
    previous = prev_pairs = None
    if compare_label:
        span = (end - start).days + 1
        prev_pairs = within(loaded, start - timedelta(days=span), start - timedelta(days=1))
        previous = window_totals(prev_pairs)
    prev_views: dict[int, int] = {}
    for account, row in prev_pairs or []:
        prev_views[account.id] = prev_views.get(account.id, 0) + int(row.view_count or 0)
    for card in cards:
        card["spark"] = mini.get(card["id"], [])
        card["views_prev"] = prev_views.get(card["id"], 0) if prev_pairs is not None else None
    return {
        "period": period,
        "last_collected_at": last_collected,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "token_configured": token_configured(),
        "kpis": {
            "accounts": len(cards),
            "healthy": sum(1 for card in cards if card["status"] == "healthy"),
            "search_ban": sum(1 for card in cards if card["status"] == "search_ban"),
            "ghost_ban": sum(1 for card in cards if card["status"] == "ghost_ban"),
            "followers": sum(followers) if followers else None,
            "views": sum(views_values),
            "likes": sum(likes_values) if likes_values else None,
            "retweets": sum(retweets_values) if retweets_values else None,
            "replies": sum(replies_values) if replies_values else None,
            "impressions": sum(impression_values) if impression_values else None,
            "quotes": current["quotes"],
            # O robô antigo gravava 0 sempre: zero no período vira "sem leitura".
            "bookmarks": current["bookmarks"] or None,
            "posts": current["posts"],
            "views_per_post": current["views_per_post"],
            "interactions": current["interactions"],
            "interaction_rate": current["interaction_rate"],
            "followers_gained": followers_gained(db, accounts, start, end),
        },
        "compare_label": compare_label,
        "spark": [{"date": day.isoformat(), "views": views} for day, views in sorted(spark.items())],
        "previous": previous,
        "top_posts": ranking,
        "top_post": ranking[0] if ranking else None,
        "goal": {"target": user_settings(db, owner_id).impression_goal, "today": today_views},
        "growth": growth_payload(pairs, prev_pairs, chart_start, end),
        "folders": folder_cards,
        "accounts": cards,
        "series": series,
        "by_account": [
            {
                "username": card["username"],
                "views": card["views"],
                "impressions": card["impressions"],
            }
            for card in cards
        ],
        "suggestions": suggestions_from(
            [row for _, row, _ in loaded], start, end, lambda: [row for _, row, _ in load_samples(db, accounts)]
        ),
    }


DETAIL_CHECKS = 10
DETAIL_POSTS = 5


def account_detail(db: Session, owner_id: int, account_id: int, period: str) -> dict[str, Any]:
    """Gaveta da conta: o cartão dela, views e seguidores por dia, os melhores posts e as últimas
    verificações de saúde. Períodos curtos mostram os últimos 14 dias no gráfico."""
    account = owned_account(db, owner_id, account_id)
    start, end = period_window(period)
    chart_start = min(start, end - timedelta(days=SPARK_DAYS - 1))
    is_total = period.strip().lower() == "total"
    loaded = load_samples(db, [account], None if is_total else chart_start)
    if is_total and loaded:
        chart_start = max(start, min(min(day for _, _, day in loaded), end - timedelta(days=SPARK_DAYS - 1)))
    rows = [row for _, row, day in loaded if start <= day <= end]
    card = account_card(
        account, rows, latest_metrics(db, [account.id]).get(account.id), last_check_details(db, [account.id]).get(account.id)
    )
    views = {chart_start + timedelta(days=offset): 0 for offset in range((end - chart_start).days + 1)}
    for _, row, day in loaded:
        if day in views:
            views[day] += int(row.view_count or 0)
    followers = dict(
        db.execute(
            select(DailyMetric.metric_date, DailyMetric.followers_count).where(
                DailyMetric.account_id == account.id,
                DailyMetric.metric_date >= chart_start,
                DailyMetric.metric_date <= end,
                DailyMetric.followers_count.is_not(None),
            )
        ).all()
    )
    ranked = sorted(rows, key=lambda row: (int(row.view_count or 0), int(row.like_count or 0)), reverse=True)
    checks = db.scalars(
        select(HealthCheck)
        .where(HealthCheck.account_id == account.id)
        .order_by(HealthCheck.checked_at.desc(), HealthCheck.id.desc())
        .limit(DETAIL_CHECKS)
    ).all()
    return {
        "account": card,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "series": [
            {"date": day.isoformat(), "views": count, "followers": followers.get(day)}
            for day, count in sorted(views.items())
        ],
        "top_posts": [post_payload(account, row, card["views"]) for row in ranked[:DETAIL_POSTS]],
        "checks": [
            {
                "status": check.status.value,
                "label": STATUS_LABELS.get(check.status.value, check.status.value),
                "checked_at": as_utc(check.checked_at).isoformat(),
                "detail": check.detail,
            }
            for check in checks
        ],
    }


def latest_metrics(db: Session, ids: list[int]) -> dict[int, DailyMetric]:
    """Leitura mais recente de cada conta (seguidores), todas numa consulta."""
    if not ids:
        return {}
    newest = (
        select(DailyMetric.account_id, func.max(DailyMetric.metric_date).label("day"))
        .where(DailyMetric.account_id.in_(ids))
        .group_by(DailyMetric.account_id)
        .subquery()
    )
    rows = db.scalars(
        select(DailyMetric).join(
            newest, (DailyMetric.account_id == newest.c.account_id) & (DailyMetric.metric_date == newest.c.day)
        )
    )
    return {row.account_id: row for row in rows}


def last_check_details(db: Session, ids: list[int]) -> dict[int, str | None]:
    """Texto da última verificação de saúde de cada conta, todas numa consulta."""
    if not ids:
        return {}
    newest = (
        select(HealthCheck.account_id, func.max(HealthCheck.checked_at).label("at"))
        .where(HealthCheck.account_id.in_(ids))
        .group_by(HealthCheck.account_id)
        .subquery()
    )
    rows = db.execute(
        select(HealthCheck.account_id, HealthCheck.detail)
        .join(newest, (HealthCheck.account_id == newest.c.account_id) & (HealthCheck.checked_at == newest.c.at))
        .order_by(HealthCheck.id)
    )
    return {account_id: detail for account_id, detail in rows}


def archived_payload(db: Session, owner_id: int) -> list[dict[str, Any]]:
    """Contas arquivadas do usuário com o histórico inteiro: totais, datas e views/seguidores por dia."""
    start, end = period_window("total")
    accounts = db.scalars(
        owned_accounts(owner_id)
        .where(XAccount.is_archived.is_(True))
        .order_by(XAccount.archived_at.desc(), XAccount.id)
    ).all()
    items = []
    for account in accounts:
        card = account_payload(db, account, start, end)
        first, last = db.execute(
            select(func.min(TweetSample.posted_at), func.max(TweetSample.posted_at)).where(
                TweetSample.account_id == account.id
            )
        ).one()
        days = db.scalars(
            select(DailyMetric).where(DailyMetric.account_id == account.id).order_by(DailyMetric.metric_date)
        ).all()
        card.update(
            {
                "archived_at": as_utc(account.archived_at).isoformat() if account.archived_at else None,
                "first_post_at": as_utc(first).isoformat() if first else None,
                "last_post_at": as_utc(last).isoformat() if last else None,
                "history": [
                    {"date": row.metric_date.isoformat(), "views": row.views, "followers": row.followers_count}
                    for row in days
                ],
            }
        )
        items.append(card)
    return items


def get_settings(db: Session) -> AppSetting:
    row = db.get(AppSetting, 1)
    if row is None:
        row = AppSetting(id=1, impression_goal=500)
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


def telegram_creds(db: Session) -> tuple[str, str]:
    """Bot do .env, o mesmo do robo.py (que aceita TELEGRAM_TOKEN). O que ficou salvo no banco é ignorado."""
    token = (os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_TOKEN") or "").strip()
    chat = (os.getenv("TELEGRAM_CHAT_ID") or "").strip()
    return token, chat


def user_settings(db: Session, owner_id: int) -> UserSetting:
    row = db.scalar(select(UserSetting).where(UserSetting.owner_id == owner_id))
    if row is None:
        row = UserSetting(owner_id=owner_id, impression_goal=500)
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


def xai_key(db: Session, user: User) -> str:
    """Chave da xAI do usuário; a do .env (paga pelo admin) só vale para o admin."""
    own = (user_settings(db, user.id).xai_api_key or "").strip()
    return own or ((os.getenv("XAI_API_KEY") or "").strip() if user.is_admin else "")


def x_token(db: Session, user: User) -> str:
    """Bearer da API do X do usuário; o X_BEARER_TOKEN do .env só vale para o admin."""
    own = (user_settings(db, user.id).x_bearer_token or "").strip()
    return own or (bearer_token() if user.is_admin else "")


def send_telegram(token: str, chat_id: str, text: str) -> None:
    if not token or not chat_id:
        raise HTTPException(503, "Telegram não configurado: defina TELEGRAM_BOT_TOKEN e TELEGRAM_CHAT_ID no .env do VPS.")
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
        f"❤️ Curtidas: {likes}\n"
        f"🔁 Retweets: {reposts}\n"
        f"🔗 Link do Post: {link}"
    )


def fmt_int(value: int) -> str:
    """12345 -> 12.345."""
    return f"{value:,}".replace(",", ".")


# Níveis do alerta de views do robo.py (VIEW_LEVELS lá): sufixo da chave → (emoji, título).
VIRAL_LEVELS = {
    "": ("🚀", "Post viralizando"),
    "bombando": ("🔥", "POST BOMBANDO"),
    "100k": ("💯", "Passou de 100K views"),
    "1m": ("👑", "Passou de 1 MILHÃO de views"),
}


def viral_item(db: Session, row: AlertSent) -> dict[str, Any]:
    """Um alerta de views do robô com o nível, a conta, o link e as views de agora.

    A chave do robô é viral:{conta}:{tweet}[:{nível}]; o texto do alerta só entra se a conta foi apagada.
    """
    text = row.detail or ""
    _, account_id, tweet_id, level = (row.dedupe_key.split(":", 3) + ["", "", ""])[:4]
    if level not in VIRAL_LEVELS:
        level = ""
    account = db.get(XAccount, int(account_id)) if account_id.isdigit() else None
    sample = None
    if account is not None and tweet_id:
        sample = db.scalar(
            select(TweetSample).where(TweetSample.account_id == account.id, TweetSample.tweet_id == tweet_id)
        )
    found = re.search(r"Conta:\s*(.*?)\s*\(@(\w+)\)", text)
    username = account.username if account else (found.group(2) if found else "")
    link = re.search(r"https://x\.com/\S+", text)
    views = re.search(r"Views:\s*([\d.]+)", text)
    emoji, title = VIRAL_LEVELS[level]
    return {
        "id": row.id,
        "level": level or "viral",
        "emoji": emoji,
        "title": title,
        "sent_at": as_utc(row.sent_at).isoformat(),
        "username": username,
        "display_name": (account.display_name if account else (found.group(1) if found else None)) or None,
        "profile_image_url": account.profile_image_url if account else None,
        "url": f"https://x.com/{username}/status/{tweet_id}" if username and tweet_id.isdigit() else (link.group(0) if link else None),
        "text": sample.text_preview if sample else None,
        "views": int(sample.view_count or 0) if sample else (int(views.group(1).replace(".", "")) if views else None),
        "likes": int(sample.like_count or 0) if sample else None,
    }


def record_alert(db: Session, key: str, kind: str, text: str, owner_id: int | None = None) -> bool:
    """Registra o alerta (uma vez por chave). O vigia manda por push (app/push.py) e ele aparece no
    sininho e no histórico, conforme as Notificações do dono; owner_id None = sistema, vai para o admin.
    Telegram ficou só para o "Esqueceu a senha?" e o aviso do backup."""
    if db.scalar(select(AlertSent).where(AlertSent.dedupe_key == key)):
        return False
    db.add(AlertSent(owner_id=owner_id, dedupe_key=key, kind=kind, detail=text[:500], sent_at=now_utc()))
    db.commit()
    return True


# ---------- Notificações (push e sininho) ----------
# Cada tipo liga/desliga em Configurações → Notificações. Os alertas continuam todos no histórico.
NOTIFY_TYPES = [
    # chave, rótulo, emoji, só admin
    ("viral", "Post viralizando (500 views)", "🚀", False),
    ("bombando", "Post bombando (10K views)", "🔥", False),
    ("100k", "Post passou de 100K views", "💯", False),
    ("1m", "Post passou de 1M de views", "👑", False),
    ("marco_post", "Marcos de views de um post", "📈", False),
    ("marco_dia", "Marcos de views do dia", "📊", False),
    ("meta", "Meta do dia batida", "🎯", False),
    ("resumo", "Resumo do fim do dia (23:59)", "🎉", False),
    ("queda", "Queda de views", "📉", False),
    ("ban", "Possível restrição da conta", "⚠️", False),
    ("falha", "Falhas de leitura (Apify, coletores, robô)", "🛠️", False),
    ("acesso", "Novo pedido de acesso", "👤", True),
]
POST_STEPS = (0, 500, 1_000, 5_000, 10_000, 50_000, 100_000, 1_000_000)
DAY_STEPS = (0, 10_000, 50_000, 100_000, 500_000, 1_000_000)
NOTIFY_DEFAULTS: dict[str, Any] = {
    "kinds": {key: True for key, *_ in NOTIFY_TYPES},
    "post_step": 0,
    "day_step": 0,
    "quiet": {"on": False, "from": "00:00", "to": "08:00"},
}
# alerts_sent.kind → grupo das preferências (o nível do viral vem do fim da chave).
FAILURE_KINDS = {"coletor", "erro", "silencio", "apify"}
HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def notify_prefs(row: UserSetting | None) -> dict[str, Any]:
    """Preferências salvas por cima dos padrões (tipo novo nasce ligado)."""
    import json

    saved: dict[str, Any] = {}
    if row is not None and row.notify_prefs:
        try:
            saved = json.loads(row.notify_prefs)
        except ValueError:
            saved = {}
    kinds = dict(NOTIFY_DEFAULTS["kinds"])
    kinds.update({k: bool(v) for k, v in (saved.get("kinds") or {}).items() if k in kinds})
    quiet = dict(NOTIFY_DEFAULTS["quiet"])
    quiet.update({k: v for k, v in (saved.get("quiet") or {}).items() if k in quiet})
    post_step = saved.get("post_step", 0)
    day_step = saved.get("day_step", 0)
    return {
        "kinds": kinds,
        "post_step": post_step if post_step in POST_STEPS else 0,
        "day_step": day_step if day_step in DAY_STEPS else 0,
        "quiet": quiet,
    }


def alert_group(row: AlertSent) -> str:
    if row.kind == "viral":
        level = (row.dedupe_key.split(":", 3) + ["", "", ""])[3]
        return level if level in {"bombando", "100k", "1m"} else "viral"
    if row.kind in FAILURE_KINDS:
        return "falha"
    return row.kind


def in_quiet_hours(prefs: dict[str, Any], now: datetime | None = None) -> bool:
    """Não perturbe (horário de Brasília): de "from" até "to", atravessando a meia-noite se precisar."""
    quiet = prefs["quiet"]
    if not quiet.get("on") or not HHMM.match(str(quiet.get("from"))) or not HHMM.match(str(quiet.get("to"))):
        return False
    local = (now or now_utc()).astimezone(zone(DEFAULT_TZ)).strftime("%H:%M")
    start, end = quiet["from"], quiet["to"]
    if start == end:
        return False
    return start <= local < end if start < end else (local >= start or local < end)


def wants(prefs: dict[str, Any], row: AlertSent) -> bool:
    return bool(prefs["kinds"].get(alert_group(row), True))


ALERT_TITLES = {
    "queda": ("📉", "Queda de views"),
    "ban": ("⚠️", "Possível restrição"),
    "marco_post": ("📈", "Marco de views do post"),
    "marco_dia": ("📊", "Marco de views do dia"),
    "meta": ("🎯", "Meta do dia batida"),
    "resumo": ("🎉", "Resumo do dia"),
    "acesso": ("👤", "Novo pedido de acesso"),
    "coletor": ("🛠️", "Coletor com problema"),
    "erro": ("❌", "Erro na leitura"),
    "silencio": ("🔇", "Robô parado"),
    "apify": ("🔑", "Leitura pelo Apify"),
}


def alert_item(db: Session, row: AlertSent) -> dict[str, Any]:
    """Item do sininho para qualquer alerta: os de views com conta, link e views de agora (viral_item);
    os outros com o título do tipo e o texto do alerta."""
    if row.kind == "viral":
        return viral_item(db, row)
    lines = [line.strip() for line in (row.detail or "").splitlines() if line.strip()]
    emoji, title = ALERT_TITLES.get(row.kind, ("🔔", "Alerta"))
    if lines and row.kind in {"marco_post", "marco_dia", "meta", "resumo"}:
        title = re.sub(r"^\W+\s*", "", lines[0]) or title  # "Post passou de 10.000 views"
    found = re.search(r"Conta:\s*(.*?)\s*\(@(\w+)\)", row.detail or "")
    link = re.search(r"https://x\.com/\S+", row.detail or "")
    views = re.search(r"Views(?: hoje)?:\s*([\d.]+)", row.detail or "")
    rest = [line for line in lines[1:] if "Conta:" not in line and "https://" not in line]
    return {
        "id": row.id,
        "level": row.kind,
        "emoji": emoji,
        "title": title,
        "sent_at": as_utc(row.sent_at).isoformat(),
        "username": found.group(2) if found else "",
        "display_name": found.group(1) if found else "XHenry",
        "profile_image_url": None,
        "url": link.group(0) if link else None,
        "text": " · ".join(rest)[:240] or None,
        "views": int(views.group(1).replace(".", "")) if views else None,
        "likes": None,
    }


def alerts_query(user: User):
    """Alertas que o usuário vê: os das contas dele e, se for o admin, os do sistema."""
    if user.is_admin:
        return select(AlertSent).where((AlertSent.owner_id == user.id) | AlertSent.owner_id.is_(None))
    return select(AlertSent).where(AlertSent.owner_id == user.id)


def dispatch_alerts(db: Session, account: XAccount, previous_status: HealthStatus | None) -> None:
    name = account.display_name or account.username
    tz = zone(account.timezone)
    local_now = datetime.now(tz)
    status = account.last_health_status
    if status in {HealthStatus.SEARCH_BAN, HealthStatus.GHOST_BAN} and status != previous_status:
        label = STATUS_LABELS[status.value]
        record_alert(
            db,
            f"ban:{account.id}:{status.value}:{local_now.date().isoformat()}",
            "ban",
            f"⚠️ Possível restrição\n👤 Conta: {name} (@{account.username})\nStatus: {label}\nO alcance pode ter sido cortado.",
            account.owner_id,
        )


def settings_payload(db: Session, user: User) -> dict[str, Any]:
    """Configurações do usuário. Chaves nunca voltam, só se estão definidas."""
    row = user_settings(db, user.id)
    checked = row.apify_checked_at
    return {
        "telegram_ready": all(telegram_creds(None)) if user.is_admin else False,
        "impression_goal": row.impression_goal,
        "xai_key_set": bool(xai_key(db, user)),
        "x_token_set": bool(x_token(db, user)),
        "apify_token_set": bool((row.apify_token or "").strip()),
        "apify": {
            "status": row.apify_status,
            "detail": row.apify_detail,
            "checked_at": as_utc(checked).isoformat() if checked else None,
            "requested_at": as_utc(row.apify_run_requested_at).isoformat() if row.apify_run_requested_at else None,
            "auto": bool(row.apify_auto),
            "interval_min": row.apify_interval_min,
        },
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
