"""Suzana — painel local de contas do X.

    python -m uvicorn main:app --host 127.0.0.1 --port 8000

X_BEARER_TOKEN vem do ambiente ou de um .env ao lado deste arquivo.
Não cole o token no chat. Sem token o painel abre, mas /api/check não chama a API.
"""

from __future__ import annotations

import logging
import os
import base64
import secrets
from contextlib import asynccontextmanager
from urllib.parse import urlencode
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo

import httpx
from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Index,
    Integer,
    JSON,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    event,
    func,
    select,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    relationship,
    sessionmaker,
)

ROOT = Path(__file__).resolve().parent
INDEX = ROOT / "templates" / "index.html"
DB_FILE = ROOT / "data" / "suzana.db"
X_API = "https://api.x.com/2"
LOCAL_EMAIL = "local@suzana"
DEFAULT_TZ = "America/Sao_Paulo"
log = logging.getLogger("xhenry")

STATUS_LABELS = {
    "healthy": "Saudável",
    "search_ban": "Search Ban",
    "ghost_ban": "Ghost Ban",
    "suspended": "Suspensa",
    "not_found": "Não encontrada",
    "protected": "Protegida",
    "rate_limited": "Limite da API",
    "api_error": "Erro da API",
    "unknown": "Indefinido",
}
WEEKDAYS = ["segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo"]


def load_env_file(path: Path) -> None:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env_file(ROOT / ".env")


def _flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def database_url() -> str:
    return os.getenv("DATABASE_URL") or ("sqlite:///" + DB_FILE.as_posix())


def bearer_token() -> str:
    return os.getenv("X_BEARER_TOKEN", "").strip()


def token_configured() -> bool:
    return bool(bearer_token())


class Base(DeclarativeBase):
    pass


def _is_sqlite(url: str) -> bool:
    return url.startswith("sqlite")


def _ensure_sqlite_parent(url: str) -> None:
    if not _is_sqlite(url) or url.endswith(":memory:"):
        return
    raw = url.split("///", 1)[-1]
    if not raw or raw == ":memory:":
        return
    path = Path(raw)
    if str(path.parent) not in {".", ""}:
        path.parent.mkdir(parents=True, exist_ok=True)


_URL = database_url()
_ensure_sqlite_parent(_URL)
engine = create_engine(
    _URL,
    connect_args={"check_same_thread": False} if _is_sqlite(_URL) else {},
    pool_pre_ping=not _is_sqlite(_URL),
    echo=_flag("ECHO_SQL"),
)


@event.listens_for(engine, "connect")
def _sqlite_foreign_keys(dbapi_conn, _record) -> None:
    if engine.dialect.name == "sqlite":
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=True, autocommit=False, class_=Session)


def _enum(enum_cls: type[Enum], name: str) -> SAEnum:
    return SAEnum(
        enum_cls,
        name=name,
        native_enum=False,
        create_constraint=True,
        length=32,
        values_callable=lambda members: [member.value for member in members],
    )


class FolderKind(str, Enum):
    CLIENT = "client"
    INFLUENCER = "influencer"
    MODEL = "model"
    CUSTOM = "custom"


class HealthStatus(str, Enum):
    HEALTHY = "healthy"
    SEARCH_BAN = "search_ban"
    GHOST_BAN = "ghost_ban"
    SUSPENDED = "suspended"
    NOT_FOUND = "not_found"
    PROTECTED = "protected"
    RATE_LIMITED = "rate_limited"
    API_ERROR = "api_error"
    UNKNOWN = "unknown"


class MetricSource(str, Enum):
    PUBLIC_API = "public_api"
    OWNER_OAUTH = "owner_oauth"
    ROLLUP = "rollup"


class SyncKind(str, Enum):
    HEALTH = "health"
    METRICS = "metrics"
    TWEETS = "tweets"
    ROLLUP = "rollup"


class SyncStatus(str, Enum):
    RUNNING = "running"
    OK = "ok"
    PARTIAL = "partial"
    FAILED = "failed"


class IntPK:
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class User(IntPK, TimestampMixin, Base):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    senha_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    nome_exibicao: Mapped[str | None] = mapped_column(String(120), nullable=True)
    avatar_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="1")
    folders: Mapped[list[Folder]] = relationship(
        back_populates="owner", cascade="all, delete-orphan", passive_deletes=True
    )


class Folder(IntPK, TimestampMixin, Base):
    __tablename__ = "folders"
    __table_args__ = (UniqueConstraint("owner_id", "name", name="uq_folders_owner_name"),)

    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    kind: Mapped[FolderKind] = mapped_column(_enum(FolderKind, "folder_kind"), nullable=False, default=FolderKind.CUSTOM)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    color: Mapped[str] = mapped_column(String(7), nullable=False, default="#E0B15A", server_default="#E0B15A")
    is_archived: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    owner: Mapped[User] = relationship(back_populates="folders")
    accounts: Mapped[list[XAccount]] = relationship(
        back_populates="folder", cascade="all, delete-orphan", passive_deletes=True
    )
    daily_metrics: Mapped[list[FolderDailyMetric]] = relationship(
        back_populates="folder", cascade="all, delete-orphan", passive_deletes=True
    )


class XAccount(IntPK, TimestampMixin, Base):
    __tablename__ = "x_accounts"
    __table_args__ = (
        UniqueConstraint("folder_id", "username", name="uq_x_accounts_folder_username"),
        CheckConstraint("length(username) > 0", name="ck_x_accounts_username_nonempty"),
    )

    folder_id: Mapped[int] = mapped_column(ForeignKey("folders.id", ondelete="CASCADE"), nullable=False, index=True)
    username: Mapped[str] = mapped_column(String(15), nullable=False, index=True)
    x_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    display_name: Mapped[str | None] = mapped_column(String(80), nullable=True)
    profile_image_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    bio: Mapped[str | None] = mapped_column(Text, nullable=True)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default=DEFAULT_TZ, server_default=DEFAULT_TZ)
    is_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    is_protected: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="1")
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_health_status: Mapped[HealthStatus | None] = mapped_column(_enum(HealthStatus, "health_status"), nullable=True)
    last_health_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    folder: Mapped[Folder] = relationship(back_populates="accounts")
    health_checks: Mapped[list[HealthCheck]] = relationship(
        back_populates="account",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="HealthCheck.checked_at.desc()",
    )
    daily_metrics: Mapped[list[DailyMetric]] = relationship(
        back_populates="account", cascade="all, delete-orphan", passive_deletes=True
    )
    tweet_samples: Mapped[list[TweetSample]] = relationship(
        back_populates="account", cascade="all, delete-orphan", passive_deletes=True
    )


class HealthCheck(IntPK, Base):
    __tablename__ = "health_checks"
    __table_args__ = (
        CheckConstraint("timeline_tweet_count >= 0", name="ck_health_timeline_count"),
        CheckConstraint("search_hit_count >= 0", name="ck_health_search_count"),
        CheckConstraint("matched_recent_in_search >= 0", name="ck_health_matched_recent"),
        Index("ix_health_checks_account_checked", "account_id", "checked_at"),
    )

    account_id: Mapped[int] = mapped_column(ForeignKey("x_accounts.id", ondelete="CASCADE"), nullable=False)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    status: Mapped[HealthStatus] = mapped_column(_enum(HealthStatus, "health_check_status"), nullable=False)
    profile_http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    timeline_http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    search_http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    profile_found: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    had_known_user_id: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    timeline_tweet_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    search_hit_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    matched_recent_in_search: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    account: Mapped[XAccount] = relationship(back_populates="health_checks")


class DailyMetric(IntPK, Base):
    __tablename__ = "daily_metrics"
    __table_args__ = (
        UniqueConstraint("account_id", "metric_date", name="uq_daily_metrics_account_date"),
        CheckConstraint("posts_count >= 0", name="ck_daily_metrics_posts"),
        Index("ix_daily_metrics_date", "metric_date"),
    )

    account_id: Mapped[int] = mapped_column(ForeignKey("x_accounts.id", ondelete="CASCADE"), nullable=False)
    metric_date: Mapped[date] = mapped_column(Date, nullable=False)
    source: Mapped[MetricSource] = mapped_column(
        _enum(MetricSource, "metric_source"), nullable=False, default=MetricSource.PUBLIC_API
    )
    followers_count: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    following_count: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    tweet_count: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    listed_count: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    posts_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    likes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    retweets: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    replies: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    quotes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    bookmarks: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    impressions: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    views: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    engagement_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    engagement_rate: Mapped[float | None] = mapped_column(Numeric(12, 6), nullable=True)
    raw_public_metrics: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    account: Mapped[XAccount] = relationship(back_populates="daily_metrics")


class FolderDailyMetric(IntPK, Base):
    __tablename__ = "folder_daily_metrics"
    __table_args__ = (
        UniqueConstraint("folder_id", "metric_date", name="uq_folder_daily_folder_date"),
        Index("ix_folder_daily_date", "metric_date"),
    )

    folder_id: Mapped[int] = mapped_column(ForeignKey("folders.id", ondelete="CASCADE"), nullable=False)
    metric_date: Mapped[date] = mapped_column(Date, nullable=False)
    accounts_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    accounts_healthy: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    accounts_search_ban: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    accounts_ghost_ban: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    accounts_other: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    followers_sum: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    likes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    retweets: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    replies: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    quotes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    impressions: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    views: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    engagement_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    engagement_rate: Mapped[float | None] = mapped_column(Numeric(12, 6), nullable=True)
    posts_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    built_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    folder: Mapped[Folder] = relationship(back_populates="daily_metrics")


class TweetSample(IntPK, Base):
    __tablename__ = "tweet_samples"
    __table_args__ = (
        UniqueConstraint("account_id", "tweet_id", name="uq_tweet_samples_account_tweet"),
        CheckConstraint("hour_local >= 0 AND hour_local <= 23", name="ck_tweet_hour"),
        CheckConstraint("weekday >= 0 AND weekday <= 6", name="ck_tweet_weekday"),
        Index("ix_tweet_samples_account_posted", "account_id", "posted_at"),
        Index("ix_tweet_samples_account_hour", "account_id", "hour_local"),
    )

    account_id: Mapped[int] = mapped_column(ForeignKey("x_accounts.id", ondelete="CASCADE"), nullable=False)
    tweet_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    posted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    timezone_used: Mapped[str] = mapped_column(String(64), nullable=False)
    hour_local: Mapped[int] = mapped_column(Integer, nullable=False)
    weekday: Mapped[int] = mapped_column(Integer, nullable=False)
    text_preview: Mapped[str | None] = mapped_column(String(280), nullable=True)
    like_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    retweet_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    reply_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    quote_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    bookmark_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    impression_count: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    view_count: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    engagement_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    engagement_rate: Mapped[float | None] = mapped_column(Numeric(12, 6), nullable=True)
    lang: Mapped[str | None] = mapped_column(String(16), nullable=True)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    account: Mapped[XAccount] = relationship(back_populates="tweet_samples")


class SyncRun(IntPK, Base):
    __tablename__ = "sync_runs"
    __table_args__ = (Index("ix_sync_runs_started", "started_at"),)

    account_id: Mapped[int | None] = mapped_column(
        ForeignKey("x_accounts.id", ondelete="SET NULL"), nullable=True, index=True
    )
    folder_id: Mapped[int | None] = mapped_column(
        ForeignKey("folders.id", ondelete="SET NULL"), nullable=True, index=True
    )
    kind: Mapped[SyncKind] = mapped_column(_enum(SyncKind, "sync_kind"), nullable=False)
    status: Mapped[SyncStatus] = mapped_column(
        _enum(SyncStatus, "sync_status"), nullable=False, default=SyncStatus.RUNNING
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    stats: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class AppSetting(IntPK, Base):
    __tablename__ = "app_settings"

    telegram_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    telegram_chat_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    impression_goal: Mapped[int] = mapped_column(Integer, nullable=False, default=500, server_default="500")
    xai_api_key: Mapped[str | None] = mapped_column(Text, nullable=True)


class AlertSent(IntPK, Base):
    __tablename__ = "alerts_sent"
    __table_args__ = (UniqueConstraint("dedupe_key", name="uq_alerts_dedupe"),)

    dedupe_key: Mapped[str] = mapped_column(String(160), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)


class DayBaseline(IntPK, Base):
    __tablename__ = "day_baselines"
    __table_args__ = (UniqueConstraint("account_id", "metric_date", name="uq_day_baseline"),)

    account_id: Mapped[int] = mapped_column(ForeignKey("x_accounts.id", ondelete="CASCADE"), nullable=False)
    metric_date: Mapped[date] = mapped_column(Date, nullable=False)
    day_avg: Mapped[int | None] = mapped_column(Integer, nullable=True)
    drop_alerted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")


class FolderIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    kind: FolderKind = FolderKind.CUSTOM
    description: str | None = Field(default=None, max_length=2000)
    color: str = "#E0B15A"


class AccountIn(BaseModel):
    folder_id: int
    username: str = Field(min_length=1, max_length=16)
    display_name: str | None = Field(default=None, max_length=80)


class SettingsIn(BaseModel):
    telegram_token: str | None = None
    telegram_chat_id: str | None = None
    impression_goal: int | None = Field(default=None, ge=1, le=10_000_000)
    xai_api_key: str | None = None


class TokenMissing(Exception):
    pass


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def ensure_user_auth_columns() -> None:
    # create_all não altera tabela que já existe.
    from sqlalchemy import inspect, text

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
    today = today or datetime.now(zone(DEFAULT_TZ)).date()
    if period in {"today", "hoje"}:
        return today, today
    if period in {"yesterday", "ontem"}:
        day = today - timedelta(days=1)
        return day, day
    span = {"7d": 7, "30d": 30, "1y": 365, "365d": 365}.get(period)
    if span is None:
        raise HTTPException(400, "Período inválido. Use today, yesterday, 7d, 30d ou 1y.")
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
    tz = zone(account.timezone)
    local = posted.astimezone(tz)
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
            posted_at=posted,
            timezone_used=account.timezone,
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
    return local.date()


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


@asynccontextmanager
async def lifespan(_app: FastAPI):
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    Base.metadata.create_all(bind=engine)
    ensure_user_auth_columns()
    with SessionLocal() as db:
        ensure_owner(db)
    log.info("token X: %s", "configurado" if token_configured() else "ausente")
    yield


_docs = None if os.getenv("APP_ENV") == "production" else "/docs"
app = FastAPI(title="XHenry", version="1.0.0", lifespan=lifespan, docs_url=_docs, redoc_url=None)


@app.get("/avatars/{filename}")
def avatar_file(filename: str) -> FileResponse:
    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(404, "Avatar não encontrado.")
    stem, dot, ext = filename.partition(".")
    if not stem.isdigit() or ext not in {"jpg", "png"} or dot != ".":
        raise HTTPException(404, "Avatar não encontrado.")
    path = ROOT / "data" / "avatars" / filename
    if not path.is_file():
        raise HTTPException(404, "Avatar não encontrado.")
    media = "image/png" if ext == "png" else "image/jpeg"
    return FileResponse(path, media_type=media, headers={"Cache-Control": "no-store"})


@app.get("/")
def root() -> FileResponse:
    if not INDEX.is_file():
        raise HTTPException(500, "index.html não está ao lado do main.py.")
    return FileResponse(INDEX, media_type="text/html; charset=utf-8", headers={"Cache-Control": "no-store"})


@app.get("/favicon.ico")
def favicon() -> Response:
    return Response(status_code=204)


@app.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "service": "xhenry", "token_configured": token_configured()}


@app.get("/api/folders")
def list_folders(db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    rows = db.scalars(select(Folder).where(Folder.is_archived.is_(False)).order_by(Folder.name)).all()
    return [
        {
            "id": row.id,
            "name": row.name,
            "kind": row.kind.value,
            "color": row.color,
            "description": row.description,
            "accounts": len(row.accounts),
        }
        for row in rows
    ]


@app.post("/api/folders", status_code=201)
def create_folder(payload: FolderIn, db: Session = Depends(get_db)) -> dict[str, Any]:
    if not payload.color.startswith("#") or len(payload.color) != 7:
        raise HTTPException(400, "Cor inválida. Use #RRGGBB.")
    owner = ensure_owner(db)
    name = payload.name.strip()
    exists = db.scalar(select(Folder).where(Folder.owner_id == owner.id, Folder.name == name))
    if exists:
        raise HTTPException(409, "Já existe uma pasta com esse nome.")
    folder = Folder(
        owner_id=owner.id,
        name=name,
        kind=payload.kind,
        description=payload.description.strip() if payload.description else None,
        color=payload.color.upper(),
    )
    db.add(folder)
    db.commit()
    db.refresh(folder)
    return {"id": folder.id, "name": folder.name, "kind": folder.kind.value, "color": folder.color}


@app.patch("/api/folders/{folder_id}")
def update_folder(folder_id: int, payload: FolderIn, db: Session = Depends(get_db)) -> dict[str, Any]:
    folder = db.get(Folder, folder_id)
    if folder is None:
        raise HTTPException(404, "Pasta não encontrada.")
    name = payload.name.strip()
    exists = db.scalar(select(Folder).where(Folder.owner_id == folder.owner_id, Folder.name == name, Folder.id != folder.id))
    if exists:
        raise HTTPException(409, "Já existe uma pasta com esse nome.")
    folder.name = name
    db.commit()
    return {"id": folder.id, "name": folder.name}


@app.delete("/api/folders/{folder_id}", status_code=204)
def delete_folder(folder_id: int, db: Session = Depends(get_db)) -> Response:
    folder = db.get(Folder, folder_id)
    if folder is None:
        raise HTTPException(404, "Pasta não encontrada.")
    db.delete(folder)
    db.commit()
    return Response(status_code=204)


@app.post("/api/accounts", status_code=201)
def create_account(payload: AccountIn, db: Session = Depends(get_db)) -> dict[str, Any]:
    folder = db.get(Folder, payload.folder_id)
    if folder is None or folder.is_archived:
        raise HTTPException(404, "Pasta não encontrada.")
    username = normalize_username(payload.username)
    exists = db.scalar(
        select(XAccount).where(XAccount.folder_id == folder.id, XAccount.username == username)
    )
    if exists:
        raise HTTPException(409, "Essa conta já está na pasta.")
    account = XAccount(
        folder_id=folder.id,
        username=username,
        display_name=(payload.display_name or "").strip() or None,
        timezone=DEFAULT_TZ,
    )
    db.add(account)
    db.commit()
    db.refresh(account)
    warning = None
    checked = None
    try:
        checked = check_handle(db, username, folder.id)
    except HTTPException as exc:
        if exc.status_code == 503:
            warning = str(exc.detail)
        else:
            warning = str(exc.detail)
    return {"id": account.id, "folder_id": folder.id, "username": username, "warning": warning, "check": checked}


@app.delete("/api/accounts/{account_id}", status_code=204)
def delete_account(account_id: int, db: Session = Depends(get_db)) -> Response:
    account = db.get(XAccount, account_id)
    if account is None:
        raise HTTPException(404, "Conta não encontrada.")
    db.delete(account)
    db.commit()
    return Response(status_code=204)


@app.get("/api/check/{handle}")
def check_account(
    handle: str,
    folder_id: int | None = Query(default=None),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return check_handle(db, normalize_username(handle), folder_id)


@app.post("/api/check-all")
def check_all(folder_id: int | None = Query(default=None), db: Session = Depends(get_db)) -> dict[str, Any]:
    if not token_configured():
        raise HTTPException(503, "Defina X_BEARER_TOKEN no ambiente ou no .env ao lado do main.py.")
    accounts = selected_accounts(db, folder_id)
    if len(accounts) > 30:
        raise HTTPException(400, "Máximo de 30 contas por verificação. Selecione uma pasta.")
    results = []
    seen: set[str] = set()
    for account in accounts:
        if account.username in seen:
            continue
        seen.add(account.username)
        results.append(check_handle(db, account.username, folder_id))
    return {"checked": len(results), "results": results}


@app.get("/api/dashboard")
def get_dashboard(
    period: str = Query(default="7d"),
    folder_id: int | None = Query(default=None),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return dashboard(db, period, folder_id)


@app.get("/api/suggestions")
def get_suggestions(
    period: str = Query(default="7d"),
    folder_id: int | None = Query(default=None),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    start, end = period_window(period)
    return build_suggestions(db, selected_accounts(db, folder_id), start, end)


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


@app.get("/api/settings")
def read_settings(db: Session = Depends(get_db)) -> dict[str, Any]:
    return settings_payload(get_settings(db))


@app.put("/api/settings")
def write_settings(payload: SettingsIn, db: Session = Depends(get_db)) -> dict[str, Any]:
    row = get_settings(db)
    if payload.telegram_token:
        row.telegram_token = payload.telegram_token.strip()
    if payload.telegram_chat_id is not None:
        row.telegram_chat_id = payload.telegram_chat_id.strip() or None
    if payload.impression_goal is not None:
        row.impression_goal = payload.impression_goal
    if payload.xai_api_key:
        row.xai_api_key = payload.xai_api_key.strip()
    db.commit()
    return settings_payload(row)


@app.post("/api/telegram/test")
def test_telegram(db: Session = Depends(get_db)) -> dict[str, bool]:
    row = get_settings(db)
    when = datetime.now(zone(DEFAULT_TZ)).strftime("%d/%m/%Y %H:%M")
    send_telegram(
        *telegram_creds(db),
        viral_message("Teste", "xhenry", row.impression_goal, when, 1, 1, "https://x.com/xhenry"),
    )
    return {"ok": True}


@app.post("/api/posts/review")
async def review_post(
    caption: str = Form(...),
    media: UploadFile | None = File(default=None),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    text = caption.strip()
    if not text:
        raise HTTPException(400, "A legenda é obrigatória.")
    if len(text) > 2000:
        raise HTTPException(400, "Legenda acima de 2000 caracteres.")
    key = xai_key(db)
    if not key:
        raise HTTPException(503, "Defina a chave da xAI em Configurações ou em XAI_API_KEY.")
    image_uri = None
    tmp_path = None
    if media is not None and media.filename:
        raw = await media.read()
        if len(raw) > 8_000_000:
            raise HTTPException(400, "Imagem acima de 8 MB.")
        kind = (media.content_type or "").lower()
        if kind not in {"image/jpeg", "image/jpg", "image/png"}:
            raise HTTPException(400, "Use JPG ou PNG. A API de visão não aceita outro formato.")
        suffix = "png" if kind == "image/png" else "jpeg"
        folder = ROOT / "data" / "tmp"
        folder.mkdir(parents=True, exist_ok=True)
        tmp_path = folder / f"{secrets.token_hex(8)}.{suffix}"
        tmp_path.write_bytes(raw)
        image_uri = f"data:image/{suffix};base64,{base64.b64encode(raw).decode()}"
    try:
        verdict = ask_grok(key, text, image_uri)
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
    return {"verdict": verdict}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=False)
