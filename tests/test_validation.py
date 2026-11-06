import tempfile
import unittest
from datetime import datetime, time
from pathlib import Path

import pandas as pd

from src.validation_service import (
    _calculate_half_hour,
    _calculate_strict_windows,
    _calculate_windows,
    _complete_morning_window,
    _minutes_for_signal,
    capture_strict_exit_observation,
    capture_open_pending,
    validate_pending,
    validate_strict_three_stage_pending,
)
from src.validation_store import ValidationStore
from src.validation_ui import _format_return, _payoff_ratio, _return_color


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


class RecentOnlySource:
    """Simulates a fallback endpoint that no longer retains the requested next day."""
    def minute_recent(self, code: str) -> pd.DataFrame:
        return minute_frame("2026-09-03", 10.0)


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

    def test_payoff_ratio_uses_average_profit_over_average_loss(self):
        self.assertAlmostEqual(_payoff_ratio(pd.Series([2.0, 4.0, -1.0, -3.0])), 1.5)
        self.assertIsNone(_payoff_ratio(pd.Series([1.0, 2.0])))

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
        self.assertNotIn("600003", final.index)
        self.assertAlmostEqual(final.loc["600001", "composite_score"], 86.5)
        self.assertAlmostEqual(final.loc["600002", "composite_score"], 67.4)
        self.assertEqual(final.loc["600001", "persistence"], "三次稳定")
        self.assertEqual(final.loc["600002", "persistence"], "连续两次")
        self.assertEqual(len(self.store.validation_frame()), 2)

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

    def test_notification_delivery_is_recorded_once(self):
        self.assertFalse(self.store.notification_sent("2026-08-03", "telegram"))

        self.store.mark_notification_sent(
            "2026-08-03", "telegram", datetime(2026, 8, 3, 14, 53),
        )
        self.store.mark_notification_sent(
            "2026-08-03", "telegram", datetime(2026, 8, 3, 14, 54),
        )

        self.assertTrue(self.store.notification_sent("2026-08-03", "telegram"))
        with self.store.connect() as db:
            count = db.execute(
                "SELECT COUNT(*) FROM notification_deliveries WHERE trade_date=? AND channel=?",
                ("2026-08-03", "telegram"),
            ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_full_review_is_stored_without_overwriting_fast_official_result(self):
        def candidate(code: str, score: float, price: float = 10.0):
            return {
                "code": code, "name": f"股票{code}", "price": price, "score": score,
                "change_pct": 4.0, "volume_ratio": 1.5, "turnover": 7.0,
                "float_cap_yi": 100,
            }

        common = dict(provider="测试", market_count=5000, hard_count=20, config={})
        for slot, minute, score in (("1430", 30, 80), ("1445", 45, 85), ("1452", 52, 90)):
            self.store.save_staged_scan(
                slot=slot, scanned_at=datetime(2026, 8, 3, 14, minute),
                candidates=[candidate("600001", score)], **common,
            )
        self.assertTrue(self.store.finalize_staged_day("2026-08-03"))

        self.store.save_review_results(
            trade_date="2026-08-03",
            started_at=datetime(2026, 8, 3, 14, 52),
            completed_at=datetime(2026, 8, 3, 14, 57),
            provider="完整重扫", market_count=5000, hard_count=30,
            strict_candidates=[candidate("600009", 92)],
            improved_candidates=[candidate("600001", 88), candidate("600008", 95)],
        )

        official = self.store.final_frame("2026-08-03")
        self.assertEqual(official["code"].tolist(), ["600001"])
        self.assertAlmostEqual(official.iloc[0]["composite_score"], 86.5)
        self.assertEqual(self.store.review_frame("2026-08-03", "strict")["code"].tolist(), ["600009"])
        reviewed = self.store.review_frame("2026-08-03", "improved")
        self.assertEqual(reviewed["code"].tolist(), ["600001"])
        self.assertAlmostEqual(reviewed.iloc[0]["score"], 85.5)

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

    def test_strict_three_stage_signal_is_validated_separately_to_1000(self):
        candidate = {"code": "600001", "name": "严格测试", "price": 10.0, "score": 80.0}
        for slot, score in (("1430", 80.0), ("1445", 85.0), ("1452", 90.0)):
            row = {**candidate, "score": score}
            self.store.save_strict_scan(
                slot=slot, scanned_at=datetime(2026, 8, 2, 14, int(slot[2:])),
                provider="测试", candidates=[row],
            )
        self.assertEqual(self.store.rebuild_strict_final_signals("2026-08-02"), 1)
        strict = self.store.strict_validation_frame()
        self.assertEqual(strict.iloc[0]["persistence"], "三次稳定")
        self.assertAlmostEqual(strict.iloc[0]["composite_score"], 86.5)

        summary = validate_strict_three_stage_pending(
            self.store, FakeSource(), now=datetime(2026, 8, 3, 10, 31),
        )
        self.assertEqual(summary["completed"], 1)
        result = self.store.strict_validation_frame().iloc[0]
        self.assertAlmostEqual(result["open_return"], 0.0)
        self.assertAlmostEqual(result["return_1000"], 6.0)
        self.assertGreater(result["max_return_1000"], result["return_1000"])
        self.assertAlmostEqual(result["return_0530"], (10.6 / 10.1 - 1) * 100)
        self.assertAlmostEqual(result["return_3160"], (11.2 / 10.6 - 1) * 100)

    def test_strict_backfill_never_replaces_an_old_validation_with_recent_fallback(self):
        candidate = {"code": "600001", "name": "严格测试", "price": 10.0, "score": 80.0}
        for slot, score in (("1430", 80.0), ("1445", 85.0), ("1452", 90.0)):
            self.store.save_strict_scan(
                slot=slot, scanned_at=datetime(2026, 8, 24, 14, int(slot[2:])),
                provider="测试", candidates=[{**candidate, "score": score}],
            )
        self.store.rebuild_strict_final_signals("2026-08-24")
        strict_id = self.store.pending_strict_final_signals("2026-09-09")[0]["strict_signal_id"]
        self.store.save_strict_validation(int(strict_id), {
            "validation_date": "2026-08-25", "open_price": 10.1, "price_1000": 10.2,
            "high_1000": 10.3, "low_1000": 10.0, "open_return": 1.0, "return_1000": 2.0,
            "max_return_1000": 3.0, "max_drawdown_1000": 0.0,
            "price_0935": None, "price_1030": None, "return_0530": None, "return_3160": None,
            "calculated_at": "2026-08-25T10:30:00",
        })

        summary = validate_strict_three_stage_pending(
            self.store, RecentOnlySource(), now=datetime(2026, 9, 9, 10, 30),
        )
        row = self.store.strict_validation_frame().iloc[0]
        self.assertEqual(summary["completed"], 0)
        self.assertEqual(row["validation_date"], "2026-08-25")
        self.assertAlmostEqual(row["return_1000"], 2.0)

        class NoNetwork:
            def minute_recent(self, code):
                raise AssertionError("自动校验不应反复读取数月前的缺失分钟数据")

        recent = validate_strict_three_stage_pending(
            self.store, NoNetwork(), now=datetime(2026, 11, 6, 10, 30), include_history=False,
        )
        self.assertEqual(recent["pending"], 0)
        self.assertEqual(recent["errors"], {})

    def test_1000_observation_only_captures_latest_live_strict_signal(self):
        candidate = {"code": "600001", "name": "严格测试", "price": 10.0, "score": 80.0}
        for slot, score in (("1430", 80.0), ("1445", 85.0), ("1452", 90.0)):
            self.store.save_strict_scan(
                slot=slot, scanned_at=datetime(2026, 8, 2, 14, int(slot[2:])),
                provider="测试", candidates=[{**candidate, "score": score}],
            )
        self.store.rebuild_strict_final_signals("2026-08-02")

        rows = capture_strict_exit_observation(
            self.store, FakeSource(), now=datetime(2026, 8, 3, 10, 1),
        )
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]["return_pct"], 6.0)
        stored = self.store.strict_exit_observation_frame("2026-08-03")
        self.assertEqual(stored.iloc[0]["observation_slot"], "1000")
        self.assertAlmostEqual(stored.iloc[0]["return_pct"], 6.0)

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

    def test_minute_windows_require_exact_endpoint_and_no_internal_gaps(self):
        complete = minute_frame("2026-08-03", 10)
        self.assertIsNotNone(_calculate_half_hour(complete, 10))
        self.assertIsNotNone(_complete_morning_window(complete.iloc[1:], time(10, 30)))
        self.assertIsNone(_complete_morning_window(complete.iloc[2:], time(10, 30)))
        for index in (5, 15, 30):
            self.assertIsNone(_calculate_half_hour(complete.drop(index=index), 10))
        self.assertIsNone(_calculate_windows(complete.iloc[:-1], 10, include_1030=True))
        self.assertIsNone(_calculate_windows(complete.drop(index=15), 10, include_1030=False))
        duplicate = pd.concat([complete, complete.iloc[[5]]], ignore_index=True)
        self.assertIsNone(_calculate_half_hour(duplicate, 10))

    def test_minute_windows_reject_zero_negative_nan_and_inconsistent_prices(self):
        for column, value in (("open", 0), ("close", -1), ("close", float("nan")),
                              ("high", float("inf")), ("high", 1)):
            frame = minute_frame("2026-08-03", 10)
            frame.loc[0, column] = value
            self.assertIsNone(_calculate_half_hour(frame, 10), (column, value))
        self.assertIsNone(_calculate_half_hour(minute_frame("2026-08-03", 10), float("nan")))

    def test_next_day_fetch_never_substitutes_later_available_day(self):
        class Source:
            def minute_between(self, code, start_date, end_date):
                self.requested = (start_date, end_date)
                raise RuntimeError("historical unavailable")

            def minute_recent(self, code):
                return minute_frame("2026-10-09", 10)

        source = Source()
        frame = _minutes_for_signal(source, "600001", "2026-09-30", datetime(2026, 10, 12))
        self.assertEqual(source.requested, ("2026-10-08", "2026-10-08"))
        self.assertTrue(frame.empty)

    def test_ordinary_and_open_validation_do_not_drift_to_later_session(self):
        self.store.freeze_scan(
            scanned_at=datetime(2026, 8, 24, 14, 52), provider="fixture", market_count=1,
            hard_count=1, config={}, candidates=[{"code": "600001", "name": "fixture", "price": 10, "score": 80}],
        )
        class Source(RecentOnlySource):
            def index_minute_recent(self):
                return minute_frame("2026-09-03", 3800)
        summary = validate_pending(self.store, Source(), now=datetime(2026, 9, 3, 10, 31))
        self.assertEqual(summary["completed"], 0)
        self.assertIn("2026-08-25", summary["skipped_reasons"]["600001"])
        captured = capture_open_pending(self.store, Source(), now=datetime(2026, 9, 3, 9, 35))
        self.assertEqual(captured["captured"], 0)
        self.assertIn("2026-08-25", captured["skipped_reasons"]["600001"])

    def test_strict_1000_can_be_saved_then_enriched_at_exact_1030(self):
        candidate = {"code": "600001", "name": "fixture", "price": 10.0, "score": 80}
        for slot in ("1430", "1445", "1452"):
            self.store.save_strict_scan(slot=slot, scanned_at=datetime(2026, 7, 31, 14, int(slot[2:])),
                                        provider="fixture", candidates=[candidate])
        self.store.rebuild_strict_final_signals("2026-07-31")
        early = validate_strict_three_stage_pending(self.store, FakeSource(), now=datetime(2026, 8, 3, 10, 1))
        self.assertEqual(early["completed"], 1)
        row = self.store.strict_validation_frame().iloc[0]
        self.assertAlmostEqual(row["return_1000"], 6)
        self.assertTrue(pd.isna(row["price_1030"]))
        later = validate_strict_three_stage_pending(self.store, FakeSource(), now=datetime(2026, 8, 3, 10, 31))
        self.assertEqual(later["completed"], 1)
        self.assertAlmostEqual(self.store.strict_validation_frame().iloc[0]["price_1030"], 11.2)

    def test_live_observation_is_frozen_and_not_replaced_at_1030(self):
        candidate = {"code": "600001", "name": "fixture", "price": 10.0, "score": 80}
        for slot in ("1430", "1445", "1452"):
            self.store.save_strict_scan(slot=slot, scanned_at=datetime(2026, 7, 31, 14, int(slot[2:])),
                                        provider="fixture", candidates=[candidate])
        self.store.rebuild_strict_final_signals("2026-07-31")
        first = capture_strict_exit_observation(self.store, FakeSource(), datetime(2026, 8, 3, 10, 1))
        second = capture_strict_exit_observation(self.store, FakeSource(), datetime(2026, 8, 3, 10, 30))
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])
        self.assertEqual(self.store.strict_exit_observation_frame("2026-08-03").iloc[0]["observed_at"],
                         "2026-08-03T10:01:00")

    def test_exact_long_holiday_session_is_used_for_validation(self):
        self.store.freeze_scan(
            scanned_at=datetime(2026, 2, 13, 14, 52), provider="fixture", market_count=1,
            hard_count=1, config={}, candidates=[{"code": "600001", "name": "fixture", "price": 10, "score": 80}],
        )
        class Source:
            def minute_recent(self, code):
                return minute_frame("2026-02-24", 10)
            def index_minute_recent(self):
                return minute_frame("2026-02-24", 3800)
        result = validate_pending(self.store, Source(), datetime(2026, 2, 24, 10, 31), include_history=False)
        self.assertEqual(result["completed"], 1)
        self.assertEqual(self.store.validation_frame().iloc[0]["validation_date"], "2026-02-24")

    def test_ordinary_automatic_validation_ignores_old_unverified_signals(self):
        self.store.freeze_scan(
            scanned_at=datetime(2026, 7, 31, 14, 52), provider="fixture", market_count=1,
            hard_count=1, config={}, candidates=[{"code": "600001", "name": "fixture", "price": 10, "score": 80}],
        )
        class NoNetwork:
            def index_minute_recent(self):
                raise AssertionError("old signal should not fetch")
        result = validate_pending(self.store, NoNetwork(), datetime(2026, 11, 6, 10, 31), include_history=False)
        self.assertEqual(result["pending"], 0)
        self.assertEqual(result["errors"], {})


if __name__ == "__main__":
    unittest.main()
