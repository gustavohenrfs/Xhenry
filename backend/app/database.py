"""Engine síncrono. SQLite no dev, Postgres pela mesma URL. Sem greenlet — Python 3.14."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings


class Base(DeclarativeBase):
    pass


def _is_sqlite(url: str) -> bool:
    return url.startswith("sqlite")


def _ensure_sqlite_parent(url: str) -> None:
    if not _is_sqlite(url) or url.endswith(":memory:") or url.endswith(":memory:?cache=shared"):
        return
    raw = url.split("///", 1)[-1]
    if not raw or raw == ":memory:":
        return
    path = Path(raw)
    if path.parent and str(path.parent) not in {".", ""}:
        path.parent.mkdir(parents=True, exist_ok=True)


_ensure_sqlite_parent(settings.database_url)

engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False} if _is_sqlite(settings.database_url) else {},
    pool_pre_ping=not _is_sqlite(settings.database_url),
    echo=settings.echo_sql,
)

@event.listens_for(engine, "connect")
def _enable_sqlite_foreign_keys(dbapi_conn, _connection_record) -> None:
    # SQLite ignora ON DELETE CASCADE se isso não estiver ligado na conexão.
    if engine.dialect.name == "sqlite":
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, class_=Session)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
