import unittest
from datetime import date

from src.trading_calendar import CalendarUnavailable, is_trading_day, next_trading_day


class TradingCalendarTests(unittest.TestCase):
    def test_long_holidays_use_next_exchange_session(self):
        self.assertEqual(next_trading_day("2026-02-13"), date(2026, 2, 24))
        self.assertEqual(next_trading_day("2026-09-30"), date(2026, 10, 8))
        self.assertEqual(next_trading_day("2026-09-24"), date(2026, 9, 28))
        self.assertEqual(next_trading_day("2026-04-30"), date(2026, 5, 6))

    def test_makeup_working_weekends_are_still_closed(self):
        for day in ("2026-01-04", "2026-02-14", "2026-02-28", "2026-05-09", "2026-09-20", "2026-10-10"):
            self.assertFalse(is_trading_day(day), day)
        self.assertTrue(is_trading_day("2026-10-09"))
        self.assertEqual(next_trading_day("2026-10-09"), date(2026, 10, 12))

    def test_unknown_calendar_and_year_crossing_fail_closed(self):
        for day in ("2025-12-31", "2027-01-04"):
            with self.assertRaises(CalendarUnavailable):
                is_trading_day(day)
        with self.assertRaises(CalendarUnavailable):
            next_trading_day("2026-12-31")


if __name__ == "__main__":
    unittest.main()
