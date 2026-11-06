"""Regression coverage for immutable evidence and exclusion of suspect results."""
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import pandas as pd

from src.validation_service import capture_strict_exit_observation, validate_strict_three_stage_pending
from src.validation_store import ValidationStore
from src.validation_ui import usable_validations


def minute_fixture():
    prices = [10 + i * 0.02 for i in range(61)]
    return pd.DataFrame({
        "day": pd.date_range("2026-11-06 09:30", periods=61, freq="min").astype(str),
        "open": prices, "high": [p + 0.03 for p in prices],
        "low": [p - 0.03 for p in prices], "close": prices,
    })


class MinuteSource:
    def minute_recent(self, code):
        return minute_fixture()


class EvidenceStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = ValidationStore(Path(self.temp.name) / "evidence.db")
        candidate = {"code": "600001", "name": "fixture", "price": 10.0, "score": 80}
        for slot in ("1430", "1445", "1452"):
            self.store.save_strict_scan(
                slot=slot, scanned_at=datetime(2026, 11, 5, 14, int(slot[2:])),
                provider="fixture", candidates=[candidate],
            )
        self.store.rebuild_strict_final_signals("2026-11-05")
        self.strict_id = int(self.store.pending_strict_final_signals("2026-11-06")[0]["strict_signal_id"])
        self.store.freeze_scan(scanned_at=datetime(2026, 11, 5, 14, 52), provider="fixture",
                               market_count=1, hard_count=1, config={}, candidates=[candidate])
        self.improved_id = int(self.store.pending_signals("2026-11-06")[0]["signal_id"])

    def tearDown(self):
        self.temp.cleanup()

    def strict_values(self):
        return {
            "validation_date": "2026-11-06", "open_price": 10, "price_1000": 10.6,
            "high_1000": 10.7, "low_1000": 9.9, "open_return": 0, "return_1000": 6,
            "max_return_1000": 7, "max_drawdown_1000": -1,
            "price_0935": 10.1, "price_1030": 11.2,
            "return_0530": (10.6 / 10.1 - 1) * 100,
            "return_3160": (11.2 / 10.6 - 1) * 100,
            "calculated_at": "2026-11-06T10:30:01",
        }

    def improved_values(self):
        return {
            "validation_date": "2026-11-06", "open_price": 10, "price_0945": 10.3,
            "high_0945": 10.4, "low_0945": 9.9, "open_return": 0, "return_0945": 3,
            "max_return": 4, "max_drawdown": -1,
            "price_1030": 11.2, "high_1030": 11.3, "low_1030": 9.9,
            "return_1030": 12, "max_return_1030": 13, "max_drawdown_1030": -1,
            "calculated_at": "2026-11-06T10:30:01",
        }

    def test_first_observation_is_retained_and_service_returns_no_resend(self):
        first = capture_strict_exit_observation(self.store, MinuteSource(), datetime(2026, 11, 6, 10, 1))
        again = capture_strict_exit_observation(self.store, MinuteSource(), datetime(2026, 11, 6, 10, 30))
        self.assertEqual(len(first), 1)
        self.assertEqual(again, [])
        inserted = self.store.save_strict_exit_observation(
            self.strict_id, "1000", datetime(2026, 11, 6, 11), 1.0, -90,
        )
        self.assertFalse(inserted)
        row = self.store.strict_exit_observation_frame("2026-11-06").iloc[0]
        self.assertEqual(row["observed_at"], "2026-11-06T10:01:00")
        self.assertAlmostEqual(row["return_pct"], 6)

    def test_market_evidence_deduplicates_identical_input_and_appends_changed_input(self):
        args = dict(kind="strict", signal_id=self.strict_id, code="600001", trade_date="2026-11-06", provider="fixture")
        first = minute_fixture()
        self.store.save_market_evidence(**args, frame=first)
        self.store.save_market_evidence(**args, frame=first.copy())
        revised = first.copy()
        revised.loc[0, "close"] = 10.01
        self.store.save_market_evidence(**args, frame=revised)
        with self.store.connect() as db:
            rows = db.execute("SELECT * FROM market_evidence ORDER BY id").fetchall()
        self.assertEqual(len(rows), 2)
        self.assertNotEqual(rows[0]["sha256"], rows[1]["sha256"])
        self.assertEqual(json.loads(rows[0]["payload_json"])["data"][0]["close"], 10)
        self.assertEqual(json.loads(rows[1]["payload_json"])["data"][0]["close"], 10.01)

    def test_repair_preserves_prior_json_for_both_validation_kinds(self):
        for kind, signal_id, save, original in (
            ("strict", self.strict_id, self.store.save_strict_validation, self.strict_values()),
            ("improved", self.improved_id, self.store.save_validation, self.improved_values()),
        ):
            with self.subTest(kind=kind):
                corrupt = {**original, "validation_date": "2026-11-09", "open_price": 0, "open_return": -100}
                save(signal_id, corrupt)
                save(signal_id, original)
                with self.store.connect() as db:
                    revisions = db.execute("SELECT prior_json FROM validation_revisions WHERE kind=? AND signal_id=?",
                                           (kind, signal_id)).fetchall()
                self.assertEqual(len(revisions), 1)
                prior = json.loads(revisions[0]["prior_json"])
                self.assertEqual(prior["validation_date"], "2026-11-09")
                self.assertEqual(prior["open_return"], -100)

    def test_omitting_old_optional_values_cannot_erase_without_revision(self):
        original = self.strict_values()
        self.store.save_strict_validation(self.strict_id, original)
        incomplete = {k: v for k, v in original.items() if k not in {"price_1030", "return_3160"}}
        self.store.save_strict_validation(self.strict_id, incomplete)
        with self.store.connect() as db:
            new = db.execute("SELECT price_1030 FROM strict_validations WHERE strict_signal_id=?", (self.strict_id,)).fetchone()
            revisions = db.execute("SELECT prior_json FROM validation_revisions WHERE kind='strict' AND signal_id=?",
                                   (self.strict_id,)).fetchall()
        self.assertIsNone(new["price_1030"])
        self.assertEqual(len(revisions), 1)
        self.assertEqual(json.loads(revisions[0]["prior_json"])["price_1030"], 11.2)

    def test_strict_partial_checkpoint_and_full_completion_have_separate_evidence(self):
        first = validate_strict_three_stage_pending(self.store, MinuteSource(), datetime(2026, 11, 6, 10, 1))
        self.assertEqual(first["completed"], 1)
        partial = self.store.strict_validation_frame().iloc[0]
        self.assertTrue(pd.isna(partial["price_1030"]))
        self.assertEqual(partial["data_quality"], "verified")
        self.assertEqual(len(self.store.pending_live_strict_signals("2026-11-06")), 1)
        second = validate_strict_three_stage_pending(self.store, MinuteSource(), datetime(2026, 11, 6, 10, 31))
        self.assertEqual(second["completed"], 1)
        full = self.store.strict_validation_frame().iloc[0]
        self.assertAlmostEqual(full["price_1030"], 11.2)
        self.assertEqual(full["data_quality"], "verified")
        with self.store.connect() as db:
            evidence = db.execute("SELECT payload_json FROM market_evidence WHERE kind='strict' ORDER BY id").fetchall()
            revision = db.execute("SELECT prior_json FROM validation_revisions WHERE kind='strict'").fetchone()
        self.assertEqual([len(json.loads(r["payload_json"])["data"]) for r in evidence], [31, 61])
        self.assertIsNone(json.loads(revision["prior_json"])["price_1030"])

    def test_invalid_and_unverified_excluded_then_verified_restores_statistics(self):
        self.store.save_strict_validation(self.strict_id, self.strict_values())
        self.store.save_validation(self.improved_id, self.improved_values())
        for kind, signal_id, frame_method in (
            ("strict", self.strict_id, self.store.strict_validation_frame),
            ("improved", self.improved_id, self.store.validation_frame),
        ):
            for quality in ("unverified", "invalid"):
                self.store.mark_validation_quality(kind, signal_id, quality, "fixture audit")
                frame = frame_method()
                self.assertEqual(len(frame), 1)  # suspect result is retained for inspection
                self.assertTrue(usable_validations(frame).empty)
                if kind == "strict":
                    self.assertIsNone(self.store.strict_exit_checkpoint_stats())
            self.store.mark_validation_quality(kind, signal_id, "verified", "exact next-session evidence")
            self.assertEqual(len(usable_validations(frame_method())), 1)
            if kind == "strict":
                stats = self.store.strict_exit_checkpoint_stats()
                self.assertEqual(stats["sample_size"], 1)
                self.assertAlmostEqual(stats["average_return"], 6)


if __name__ == "__main__":
    unittest.main()
