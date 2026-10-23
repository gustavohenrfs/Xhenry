from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Any

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
    avatar_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    logo_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="1")

    folders: Mapped[list["Folder"]] = relationship(
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
    accounts: Mapped[list["XAccount"]] = relationship(
        back_populates="folder", cascade="all, delete-orphan", passive_deletes=True
    )
    daily_metrics: Mapped[list["FolderDailyMetric"]] = relationship(
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
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_health_status: Mapped[HealthStatus | None] = mapped_column(_enum(HealthStatus, "health_status"), nullable=True)
    last_health_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    folder: Mapped[Folder] = relationship(back_populates="accounts")
    health_checks: Mapped[list["HealthCheck"]] = relationship(
        back_populates="account", cascade="all, delete-orphan", passive_deletes=True, order_by="HealthCheck.checked_at.desc()"
    )
    daily_metrics: Mapped[list["DailyMetric"]] = relationship(
        back_populates="account", cascade="all, delete-orphan", passive_deletes=True
    )
    tweet_samples: Mapped[list["TweetSample"]] = relationship(
        back_populates="account", cascade="all, delete-orphan", passive_deletes=True
    )


# (Rest of the models would continue here in a full extraction - HealthCheck, DailyMetric, etc.)
# For brevity in this step, the core models are moved. Full extraction can continue in next steps.
class AppSetting(IntPK, Base):
    __tablename__ = "app_settings"
    telegram_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    telegram_chat_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    impression_goal: Mapped[int] = mapped_column(Integer, nullable=False, default=500, server_default="500")
    xai_api_key: Mapped[str | None] = mapped_column(Text, nullable=True)
