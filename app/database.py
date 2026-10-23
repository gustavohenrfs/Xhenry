from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

ROOT = Path(__file__).resolve().parent.parent
DB_FILE = ROOT / "data" / "suzana.db"


def _flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def database_url() -> str:
    url = os.getenv("DATABASE_URL")
    if url:
        if url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql+psycopg://", 1)
        elif url.startswith("postgresql://"):
            url = url.replace("postgresql://", "postgresql+psycopg://", 1)
        return url
    return "sqlite:///" + DB_FILE.as_posix()


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


class Base(DeclarativeBase):
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