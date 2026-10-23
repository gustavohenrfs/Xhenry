"""Settings lidas do ambiente. Sem pydantic-settings aqui para o schema subir só com SQLAlchemy."""

from __future__ import annotations

import os
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
PROJECT_DIR = BACKEND_DIR.parent
DEFAULT_DB_FILE = BACKEND_DIR / "data" / "suzana.db"
DEFAULT_DATABASE_URL = "sqlite:///" + DEFAULT_DB_FILE.as_posix()


def _flag(name: str, default: str = "") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


class Settings:
    def __init__(self) -> None:
        self.database_url = os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL)
        self.app_env = os.getenv("APP_ENV", "development")
        self.x_bearer_token = os.getenv("X_BEARER_TOKEN", "")
        self.echo_sql = _flag("ECHO_SQL")


settings = Settings()
