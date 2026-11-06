"""Offline mainland exchange calendar, verified for 2026 only.

Sources (verified 2026-11-06):
SSE announcement 〔2025〕45, published 2025-12-22:
https://www.sse.com.cn/disclosure/announcement/general/c/c_20251222_10802507.shtml
SZSE announcement 〔2025〕481, published 2025-12-22:
https://www.szse.cn/disclosure/notice/general/t20251222_618087.html

Government make-up working weekends are NOT exchange sessions. Unknown years
fail closed: callers must surface the missing calendar, never assume weekdays.
"""
from datetime import date, datetime, timedelta


SUPPORTED_YEARS = frozenset({2026})
HOLIDAY_RANGES_2026 = (
    ("2026-01-01", "2026-01-03"),
    ("2026-02-15", "2026-02-23"),
    ("2026-04-04", "2026-04-06"),
    ("2026-05-01", "2026-05-05"),
    ("2026-06-19", "2026-06-21"),
    ("2026-09-25", "2026-09-27"),
    ("2026-10-01", "2026-10-07"),
)


class CalendarUnavailable(ValueError):
    pass


def _date(value: str | date) -> date:
    if isinstance(value, datetime):
        return value.date()
    return date.fromisoformat(value) if isinstance(value, str) else value


def is_trading_day(value: str | date) -> bool:
    day = _date(value)
    if day.year not in SUPPORTED_YEARS:
        raise CalendarUnavailable(f"尚未核验 {day.year} 年交易日历，停止推断交易日")
    return day.weekday() < 5 and not any(start <= day.isoformat() <= end for start, end in HOLIDAY_RANGES_2026)


def next_trading_day(value: str | date) -> date:
    day = _date(value)
    is_trading_day(day)  # Verify input year even if the next date crosses years.
    while True:
        day += timedelta(days=1)
        if is_trading_day(day):
            return day
