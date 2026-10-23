"""Janelas de relatório. Datas inclusive, no fuso do relatório (default: hoje local do processo)."""

from __future__ import annotations

from datetime import date, timedelta
from enum import Enum


class ReportPeriod(str, Enum):
    TODAY = "today"
    YESTERDAY = "yesterday"
    LAST_7_DAYS = "7d"
    LAST_30_DAYS = "30d"
    LAST_YEAR = "1y"


_SPAN_DAYS = {
    ReportPeriod.LAST_7_DAYS: 7,
    ReportPeriod.LAST_30_DAYS: 30,
    ReportPeriod.LAST_YEAR: 365,
}


def window(period: ReportPeriod | str, today: date | None = None) -> tuple[date, date]:
    """Retorna (início, fim) inclusive. '30d' cobre hoje e os 29 dias anteriores."""
    period = ReportPeriod(period)
    today = today or date.today()
    if period is ReportPeriod.TODAY:
        return today, today
    if period is ReportPeriod.YESTERDAY:
        yesterday = today - timedelta(days=1)
        return yesterday, yesterday
    span = _SPAN_DAYS[period]
    return today - timedelta(days=span - 1), today
