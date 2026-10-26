"""Modelos SQLAlchemy do banco."""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum

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
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

DEFAULT_TZ = "America/Sao_Paulo"


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
    avatar_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    logo_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
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
    is_archived: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
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
    # Legado: o app não lê nem grava mais. Fica para não quebrar o robo.py nem exigir migração.
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
    # Legado: o app não lê nem grava mais. Fica para não quebrar o robo.py nem exigir migração.
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
    # Legado: o app não lê nem grava mais. Fica para não quebrar o robo.py nem exigir migração.
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
