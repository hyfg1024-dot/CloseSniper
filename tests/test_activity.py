import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import pandas as pd

from src.activity import build_activity_frame
from src.validation_store import ValidationStore


class ActivityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = ValidationStore(Path(self.temp.name) / "activity.db")

    def tearDown(self):
        self.temp.cleanup()

    def snapshot(self, day, slot, at, candidates=None):
        self.store.save_strict_scan(
            slot=slot, scanned_at=datetime.fromisoformat(f"{day}T{at}"),
            provider="fixture", candidates=candidates or [],
        )

    def test_zero_candidate_days_are_kept_beyond_last_signal(self):
        candidate = {"code": "002779", "name": "fixture", "price": 10, "score": 80}
        for slot in ("1430", "1445", "1452"):
            at = f"{slot[:2]}:{slot[2:]}:20"
            self.snapshot("2026-10-28", slot, at, [candidate])
            self.snapshot("2026-11-06", slot, at)
        self.store.rebuild_strict_final_signals("2026-10-28")
        frame = build_activity_frame(self.store)
        self.assertEqual(frame["trade_date"].tolist(), ["2026-11-06", "2026-10-28"])
        self.assertEqual(frame.iloc[0]["status"], "complete")
        self.assertEqual(frame.iloc[0]["strict_1452"], 0)
        self.assertEqual(frame.iloc[0]["strict_final_count"], 0)
        self.assertEqual(frame.iloc[1]["strict_final_count"], 1)

    def test_late_full_review_uses_strict_completion_not_fast_timestamp(self):
        self.snapshot("2026-11-04", "1430", "14:39:09")
        self.snapshot("2026-11-04", "1445", "15:07:48")
        self.snapshot("2026-11-04", "1452", "15:15:30")
        self.store.record_scan_run(
            slot="1452", attempted_at=datetime(2026, 11, 4, 14, 52), status="success", attempt_count=1,
            completed_at=datetime(2026, 11, 4, 14, 52, 3),
        )
        row = build_activity_frame(self.store).iloc[0]
        self.assertEqual(row["status"], "delayed")
        self.assertEqual(row["completed_at"], "2026-11-04T15:15:30")
        self.assertIn("14:45", row["delayed_slots"])
        self.assertAlmostEqual(row["max_delay_minutes"], 23.5)

    def test_failed_or_missing_strict_scan_is_not_reported_as_zero(self):
        self.snapshot("2026-11-06", "1430", "14:30:20")
        self.store.record_scan_run(
            slot="1445", attempted_at=datetime(2026, 11, 6, 14, 45),
            status="failed", error_message="行情获取失败", attempt_count=1,
        )
        row = build_activity_frame(self.store).iloc[0]
        self.assertEqual(row["status"], "failed")
        self.assertTrue(pd.isna(row["strict_1445"]))
        self.assertEqual(row["slot_status_1452"], "missing")
        self.assertIn("行情获取失败", row["detail"])

    def test_fast_success_without_strict_snapshot_is_partial(self):
        self.snapshot("2026-11-06", "1430", "14:30:20")
        self.snapshot("2026-11-06", "1445", "14:45:20")
        self.store.record_scan_run(
            slot="1452", attempted_at=datetime(2026, 11, 6, 14, 52), status="success", attempt_count=1,
        )
        with self.store.connect() as db:
            row = build_activity_frame(db).iloc[0]
        self.assertEqual(row["status"], "partial")
        self.assertEqual(row["slot_status_1452"], "incomplete")
        self.assertTrue(pd.isna(row["strict_1452"]))

    def test_success_with_missing_market_data_retains_warning(self):
        for slot in ("1430", "1445", "1452"):
            self.snapshot("2026-11-06", slot, f"{slot[:2]}:{slot[2:]}:20")
        self.store.record_scan_run(
            slot="1430", attempted_at=datetime(2026, 11, 6, 14, 30), status="success", attempt_count=1,
            error_message="2只股票分时缺失",
        )
        self.store.record_review_run(
            trade_date="2026-11-06", started_at=datetime(2026, 11, 6, 14, 52), status="success",
            error_message="1只股票日线缺失",
        )
        row = build_activity_frame(self.store).iloc[0]
        self.assertEqual(row["status"], "data_incomplete")
        self.assertEqual(row["slot_status_1430"], "data_incomplete")
        self.assertIn("分时缺失", row["detail"])
        self.assertIn("日线缺失", row["detail"])


if __name__ == "__main__":
    unittest.main()
