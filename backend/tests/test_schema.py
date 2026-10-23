"""O schema sobe e as janelas de relatório batem com o contrato do produto."""

from datetime import date

from sqlalchemy import create_engine

from app.database import Base
from app.models import (  # noqa: F401
    DailyMetric,
    Folder,
    FolderDailyMetric,
    HealthCheck,
    SyncRun,
    TweetSample,
    User,
    XAccount,
)
from app.periods import ReportPeriod, window


EXPECTED_TABLES = {
    "users",
    "folders",
    "x_accounts",
    "health_checks",
    "daily_metrics",
    "folder_daily_metrics",
    "tweet_samples",
    "sync_runs",
}


def test_expected_tables_registered() -> None:
    assert EXPECTED_TABLES == set(Base.metadata.tables)


def test_report_windows() -> None:
    today = date(2026, 10, 23)
    assert window(ReportPeriod.TODAY, today) == (today, today)
    assert window("yesterday", today) == (date(2026, 10, 22), date(2026, 10, 22))
    assert window(ReportPeriod.LAST_7_DAYS, today) == (date(2026, 10, 17), today)
    assert window(ReportPeriod.LAST_30_DAYS, today) == (date(2026, 9, 24), today)
    assert window(ReportPeriod.LAST_YEAR, today) == (date(2025, 10, 24), today)


def test_create_all_sqlite() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    names = set(Base.metadata.tables)
    assert EXPECTED_TABLES <= names
    with engine.connect() as conn:
        rows = conn.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).all()
    created = {row[0] for row in rows}
    assert EXPECTED_TABLES <= created
