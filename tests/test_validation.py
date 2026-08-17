import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import pandas as pd

from src.validation_service import capture_open_pending, validate_pending
from src.validation_store import ValidationStore
from src.validation_ui import _format_return, _return_color


def minute_frame(day: str, start: float, periods: int = 61) -> pd.DataFrame:
    times = pd.date_range(f"{day} 09:30:00", periods=periods, freq="min")
    close = [start + i * 0.02 for i in range(periods)]
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

    def test_return_color_uses_a_share_red_up_green_down(self):
        self.assertIn("#c92f2f", _return_color(1.2))
        self.assertIn("#16835d", _return_color(-0.8))
        self.assertIn("#656b66", _return_color(0))
        self.assertEqual(_return_color(float("nan")), "")

    def test_return_format_includes_direction_and_unit(self):
        self.assertEqual(_format_return(1.234), "+1.23%")
        self.assertEqual(_format_return(-0.8), "-0.80%")
        self.assertEqual(_format_return(0), "0.00%")
        self.assertEqual(_format_return(float("nan")), "—")

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

    def test_three_stage_result_uses_1452_gate_and_weighted_score(self):
        def candidate(code: str, score: float, price: float = 10.0):
            return {
                "code": code, "name": f"股票{code}", "price": price, "score": score,
                "change_pct": 4.0, "volume_ratio": 1.5, "turnover": 7.0,
                "float_cap_yi": 100,
            }

        common = dict(provider="测试", market_count=5000, hard_count=20, config={})
        self.store.save_staged_scan(
            slot="1430", scanned_at=datetime(2026, 8, 3, 14, 30),
            candidates=[candidate("600001", 80), candidate("600009", 95)], **common,
        )
        self.store.save_staged_scan(
            slot="1445", scanned_at=datetime(2026, 8, 3, 14, 45),
            candidates=[candidate("600001", 85), candidate("600002", 78)], **common,
        )
        self.store.save_staged_scan(
            slot="1452", scanned_at=datetime(2026, 8, 3, 14, 52),
            candidates=[candidate("600001", 90, 10.5), candidate("600002", 88, 20.0), candidate("600003", 92, 30.0)],
            **common,
        )

        self.assertTrue(self.store.finalize_staged_day("2026-08-03"))
        self.assertFalse(self.store.finalize_staged_day("2026-08-03"))
        final = self.store.final_frame("2026-08-03").set_index("code")
        self.assertNotIn("600009", final.index)
        self.assertAlmostEqual(final.loc["600001", "composite_score"], 86.5)
        self.assertAlmostEqual(final.loc["600002", "composite_score"], 67.4)
        self.assertAlmostEqual(final.loc["600003", "composite_score"], 46.0)
        self.assertEqual(final.loc["600001", "persistence"], "三次稳定")
        self.assertEqual(final.loc["600002", "persistence"], "连续两次")
        self.assertEqual(final.loc["600003", "persistence"], "14:52新进入")
        self.assertEqual(len(self.store.validation_frame()), 3)

    def test_final_result_requires_all_three_slots(self):
        candidate = {
            "code": "600001", "name": "测试", "price": 10.0, "score": 90,
            "change_pct": 4.0, "volume_ratio": 1.5, "turnover": 7.0,
            "float_cap_yi": 100,
        }
        self.store.save_staged_scan(
            slot="1452", scanned_at=datetime(2026, 8, 3, 14, 52),
            provider="测试", market_count=5000, hard_count=20,
            config={}, candidates=[candidate],
        )

        self.assertFalse(self.store.finalize_staged_day("2026-08-03"))
        self.assertTrue(self.store.final_frame("2026-08-03").empty)

    def test_strict_daily_result_keeps_latest_slot_under_its_own_mode(self):
        common = dict(provider="测试")
        self.store.save_strict_scan(
            slot="1430", scanned_at=datetime(2026, 8, 3, 14, 30),
            candidates=[{"code": "600001", "name": "早段", "price": 10, "score": 80}], **common,
        )
        self.store.save_strict_scan(
            slot="1452", scanned_at=datetime(2026, 8, 3, 14, 52),
            candidates=[{"code": "600002", "name": "终段", "price": 20, "score": 90}], **common,
        )
        latest = self.store.latest_strict_frame("2026-08-03")
        self.assertEqual(latest.iloc[0]["slot"], "1452")
        self.assertEqual(latest.iloc[0]["code"], "600002")

        all_slots = self.store.strict_frame("2026-08-03")
        self.assertEqual(all_slots["slot"].tolist(), ["1430", "1452"])
        self.assertEqual(all_slots["code"].tolist(), ["600001", "600002"])

    def test_validation_separates_open_0945_and_1030(self):
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
            now=datetime(2026, 8, 3, 10, 31),
        )
        self.assertEqual(summary["completed"], 1)
        self.assertEqual(summary["completed_1030"], 1)
        row = self.store.validation_frame().iloc[0]
        self.assertAlmostEqual(row["open_return"], 0.0)
        self.assertAlmostEqual(row["return_0945"], 3.0)
        self.assertAlmostEqual(row["return_1030"], 12.0)
        self.assertGreater(row["max_return"], row["return_0945"])
        self.assertGreater(row["max_return_1030"], row["max_return"])

    def test_0945_snapshot_is_enriched_at_1030(self):
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

        first = validate_pending(
            self.store,
            FakeSource(),
            now=datetime(2026, 8, 3, 9, 46),
        )
        early = self.store.validation_frame().iloc[0]
        self.assertEqual(first["completed_0945"], 1)
        self.assertAlmostEqual(early["return_0945"], 3.0)
        self.assertTrue(pd.isna(early["return_1030"]))
        self.assertEqual(self.store.pending_count("2026-08-03"), 1)

        second = validate_pending(
            self.store,
            FakeSource(),
            now=datetime(2026, 8, 3, 10, 31),
        )
        completed = self.store.validation_frame().iloc[0]
        self.assertEqual(second["completed_1030"], 1)
        self.assertAlmostEqual(completed["return_1030"], 12.0)
        self.assertEqual(self.store.pending_count("2026-08-03"), 0)

    def test_open_button_captures_provisional_result_before_0945(self):
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

        summary = capture_open_pending(
            self.store,
            FakeSource(),
            now=datetime(2026, 8, 3, 9, 35),
        )
        row = self.store.validation_frame().iloc[0]

        self.assertEqual(summary["captured"], 1)
        self.assertAlmostEqual(row["open_return"], 0.0)
        self.assertAlmostEqual(row["captured_return"], 1.0)
        self.assertTrue(pd.isna(row["return_0945"]))
        self.assertEqual(self.store.pending_count("2026-08-03"), 1)


if __name__ == "__main__":
    unittest.main()
