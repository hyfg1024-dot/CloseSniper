import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import pandas as pd

from src.validation_service import validate_pending
from src.validation_store import ValidationStore


def minute_frame(day: str, start: float) -> pd.DataFrame:
    times = pd.date_range(f"{day} 09:30:00", periods=16, freq="min")
    close = [start + i * 0.02 for i in range(16)]
    return pd.DataFrame(
        {
            "day": times.astype(str),
            "open": [start, *close[1:]],
            "high": [value + 0.03 for value in close],
            "low": [value - 0.04 for value in close],
            "close": close,
            "volume": 1000,
            "amount": 100_000,
        }
    )


class FakeSource:
    def minute_recent(self, code: str) -> pd.DataFrame:
        return minute_frame("2026-08-03", 10.0)

    def index_minute_recent(self) -> pd.DataFrame:
        return minute_frame("2026-08-03", 3800.0)


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = ValidationStore(Path(self.temp.name) / "test.db")

    def tearDown(self):
        self.temp.cleanup()

    def test_first_scan_is_frozen(self):
        candidate = {
            "code": "600001", "name": "测试", "price": 10.0, "score": 88,
            "change_pct": 4.0, "volume_ratio": 1.5, "turnover": 7.0,
            "float_cap_yi": 100,
        }
        first = self.store.freeze_scan(
            scanned_at=datetime(2026, 7, 31, 14, 35),
            provider="测试", market_count=5000, hard_count=12,
            config={}, candidates=[candidate],
        )
        second = self.store.freeze_scan(
            scanned_at=datetime(2026, 7, 31, 14, 50),
            provider="测试", market_count=5000, hard_count=20,
            config={}, candidates=[],
        )
        self.assertTrue(first)
        self.assertFalse(second)
        self.assertEqual(len(self.store.validation_frame()), 1)

    def test_validation_separates_open_and_0945(self):
        self.store.freeze_scan(
            scanned_at=datetime(2026, 7, 31, 14, 35),
            provider="测试", market_count=5000, hard_count=12,
            config={},
            candidates=[{
                "code": "600001", "name": "测试", "price": 10.0, "score": 88,
                "change_pct": 4.0, "volume_ratio": 1.5, "turnover": 7.0,
                "float_cap_yi": 100,
            }],
        )
        summary = validate_pending(
            self.store,
            FakeSource(),
            now=datetime(2026, 8, 3, 10, 0),
        )
        self.assertEqual(summary["completed"], 1)
        row = self.store.validation_frame().iloc[0]
        self.assertAlmostEqual(row["open_return"], 0.0)
        self.assertAlmostEqual(row["return_0945"], 3.0)
        self.assertGreater(row["max_return"], row["return_0945"])


if __name__ == "__main__":
    unittest.main()
