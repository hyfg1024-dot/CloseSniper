import json
import tempfile
import unittest
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pandas as pd

import auto_scan
from src.deadline_worker import DeadlineExceeded, run_with_deadline
from src.strategy import StrategyConfig
from src.scan_service import ScanResult
from src.strict_schedule import cache_scan, decision_record, run_strict_final, save_decision
from src.validation_store import ValidationStore
from test_strict_deadline import Source, history_fixture
from src.strict_deadline import evaluate_strict_pool


DAY = "2026-11-06"
NOW = datetime(2026, 11, 6, 14, 52, 1)


def candidate(code="600001", score=80, price=10):
    return {"code": code, "name": "fixture", "score": score, "price": price}


def seed_early(store, cfg, *, second_code="600001", second_time="14:45:20"):
    daily = pd.DataFrame({
        "date": pd.bdate_range(end="2026-11-05", periods=70),
        "open": 10.0, "close": 10.0, "high": 10.1, "low": 9.9, "volume": 1000.0,
    })
    for slot, at, code, score in (
        ("1430", "14:30:20", "600001", 80),
        ("1445", second_time, second_code, 85),
    ):
        completed = datetime.fromisoformat(f"{DAY}T{at}")
        store.save_strict_scan(slot=slot, scanned_at=completed, provider="fixture", candidates=[candidate(code, score)])
        store.record_scan_run(
            slot=slot, attempted_at=completed, completed_at=completed,
            status="success", attempt_count=1, candidate_count=1,
        )
        cache_scan(store, slot, cache_result({code: daily}), cfg, completed)


def cache_result(daily=None):
    return ScanResult(pd.DataFrame(), pd.DataFrame(columns=["passed", "code", "name", "price", "score"]),
                      {"全市场": 1}, 1, "fixture", {}, daily_history=daily or {})


def successful_result():
    result = cache_result()
    result.result_frame = pd.DataFrame([{**candidate(score=90, price=10.5), "passed": True}])
    result.errors = {}
    return result


def synthetic_market_worker(cfg, codes, daily, *, now, concepts):
    source = Source()
    source.quotes["代码"] = "600001"
    source.quotes["报价时间"] = now.strftime("%Y%m%d%H%M%S")
    return evaluate_strict_pool(cfg, codes, daily, now=now, concepts=concepts, source=source)


class StrictScheduleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = ValidationStore(Path(self.temp.name) / "schedule.db")
        self.cfg = StrategyConfig()
        self.publisher = patch("src.strict_schedule.publish_decision", return_value="disabled").start()
        self.addCleanup(patch.stopall)
        self.addCleanup(self.temp.cleanup)

    def run_final(self, store=None, runner=None, now_fn=None):
        return run_strict_final(
            store=store or self.store, cfg=self.cfg, now_fn=now_fn or (lambda: NOW),
            runner=runner or Mock(return_value=successful_result()),
        )

    def assert_failed_without_zero_snapshot(self, store, result):
        self.assertEqual(result["status"], "failed")
        frozen = decision_record(store, DAY)
        self.assertEqual(frozen["status"], "failed")
        self.assertEqual(json.loads(frozen["candidates_json"]), [])
        self.assertNotIn("1452", store.strict_frame(DAY)["slot"].tolist())
        self.assertTrue(store.strict_validation_frame().empty)
        self.assertEqual(store.scan_status_frame(DAY).set_index("slot").loc["1452", "status"], "failed")

    def test_missing_incomplete_or_stale_preparation_is_failure_not_zero(self):
        mutations = {
            "missing_snapshot": "DELETE FROM strict_scans WHERE slot='1445'",
            "missing_run": "DELETE FROM scan_runs WHERE slot='1445'",
            "failed_run": "UPDATE scan_runs SET status='failed' WHERE slot='1445'",
            "partial_data": "UPDATE scan_runs SET error_message='分时缺失' WHERE slot='1445'",
            "wrong_cache_day": "UPDATE strict_scan_cache SET trade_date='2026-11-05' WHERE slot='1445'",
            "wrong_config": "UPDATE strict_scan_cache SET config_json='{}' WHERE slot='1445'",
            "wrong_cache_time": "UPDATE strict_scan_cache SET saved_at='2026-11-06T14:45:21' WHERE slot='1445'",
            "wrong_completed_time": "UPDATE scan_runs SET completed_at='2026-11-06T14:46:20' WHERE slot='1445'",
            "missing_history": "UPDATE strict_scan_cache SET daily_json='{}' WHERE slot='1445'",
        }
        for name, sql in mutations.items():
            with self.subTest(name=name):
                store = ValidationStore(Path(self.temp.name) / f"{name}.db")
                seed_early(store, self.cfg)
                with store.connect() as db:
                    db.execute(sql)
                runner = Mock(return_value=successful_result())
                result = self.run_final(store=store, runner=runner)
                self.assert_failed_without_zero_snapshot(store, result)
                runner.assert_not_called()

    def test_late_early_snapshot_is_not_used(self):
        seed_early(self.store, self.cfg, second_time="15:07:48")
        runner = Mock()
        result = self.run_final(runner=runner)
        self.assert_failed_without_zero_snapshot(self.store, result)
        self.assertIn("迟到", result["reason"])
        runner.assert_not_called()

    def test_complete_empty_intersection_publishes_zero_without_market_runner(self):
        seed_early(self.store, self.cfg, second_code="600002")
        runner = Mock()
        result = self.run_final(runner=runner)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["pool_count"], 0)
        runner.assert_not_called()
        frame = self.store.strict_frame(DAY)
        self.assertEqual(frame["slot"].tolist(), ["1430", "1445", "1452"])
        self.assertTrue(pd.isna(frame[frame["slot"] == "1452"].iloc[0]["code"]))

    def test_success_freezes_weighted_triple_and_all_inputs_against_manual_changes(self):
        seed_early(self.store, self.cfg)
        runner = Mock(return_value=successful_result())
        result = self.run_final(runner=runner)
        self.assertEqual(result["status"], "success")
        final = self.store.strict_validation_frame().iloc[0]
        self.assertEqual(final["code"], "600001")
        self.assertAlmostEqual(final["entry_price"], 10.5)
        self.assertAlmostEqual(final["composite_score"], 86.5)
        saved_rows = json.loads(decision_record(self.store, DAY)["candidates_json"])
        self.assertAlmostEqual(saved_rows[0]["composite_score"], 86.5)
        self.assertEqual(runner.call_args.kwargs["args"][1], ["600001"])
        self.assertLessEqual(runner.call_args.kwargs["timeout_seconds"], 40)
        frozen = self.store.strict_frame(DAY)
        for slot in ("1430", "1445", "1452"):
            self.store.save_strict_scan(
                slot=slot, scanned_at=datetime(2026, 11, 6, 14, 59),
                provider="manual", candidates=[candidate("600009", 100, 50)],
            )
        self.store.rebuild_all_strict_final_signals()
        pd.testing.assert_frame_equal(self.store.strict_frame(DAY), frozen)
        self.assertEqual(self.store.strict_validation_frame()["code"].tolist(), ["600001"])

    def test_real_child_process_computes_and_commits_the_strict_final(self):
        seed_early(self.store, self.cfg)
        for slot, minute in (("1430", 30), ("1445", 45)):
            cache_scan(self.store, slot, cache_result({"600001": history_fixture()}),
                       self.cfg, datetime(2026, 11, 6, 14, minute, 20))

        def process_runner(_fn, **kwargs):
            return run_with_deadline(synthetic_market_worker, **kwargs)

        started = time.monotonic()
        result = self.run_final(runner=process_runner)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["strict_final"], 1)
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(len(self.store.pending_strict_final_signals("2026-11-09")), 1)

    def test_timeout_is_frozen_and_cannot_turn_into_a_late_success(self):
        seed_early(self.store, self.cfg)
        failed_runner = Mock(side_effect=DeadlineExceeded("fixture timeout"))
        self.assert_failed_without_zero_snapshot(self.store, self.run_final(runner=failed_runner))
        later_runner = Mock(return_value=successful_result())
        later = self.run_final(runner=later_runner)
        self.assertTrue(later["already_frozen"])
        self.assertEqual(later["status"], "failed")
        later_runner.assert_not_called()
        self.store.save_strict_scan(slot="1452", scanned_at=NOW, provider="late", candidates=[candidate()])
        self.store.rebuild_all_strict_final_signals()
        self.assertTrue(self.store.strict_validation_frame().empty)

    def test_partial_market_result_is_failure_and_no_partial_recommendation(self):
        seed_early(self.store, self.cfg)
        partial = successful_result()
        partial.errors = {"600002": "分时缺失"}
        self.assert_failed_without_zero_snapshot(self.store, self.run_final(runner=Mock(return_value=partial)))

    def test_after_1453_never_starts_market_scan(self):
        seed_early(self.store, self.cfg)
        runner = Mock()
        result = self.run_final(runner=runner, now_fn=lambda: datetime(2026, 11, 6, 14, 53))
        self.assert_failed_without_zero_snapshot(self.store, result)
        runner.assert_not_called()

    def test_early_invocation_does_not_freeze_or_damage_later_decision(self):
        seed_early(self.store, self.cfg)
        before = self.store.scan_status_frame(DAY)
        runner = Mock()
        response = self.run_final(runner=runner, now_fn=lambda: datetime(2026, 11, 6, 14, 51, 59))
        self.assertEqual(response["status"], "skipped")
        self.assertIsNone(decision_record(self.store, DAY))
        pd.testing.assert_frame_equal(self.store.scan_status_frame(DAY), before)
        self.publisher.assert_not_called()
        runner.assert_not_called()
        self.assertEqual(self.run_final()["status"], "success")

    def test_exact_compute_cutoff_is_failure_without_starting_work(self):
        seed_early(self.store, self.cfg)
        runner = Mock()
        response = self.run_final(runner=runner, now_fn=lambda: datetime(2026, 11, 6, 14, 52, 40))
        self.assert_failed_without_zero_snapshot(self.store, response)
        runner.assert_not_called()

    def test_preparation_time_consumes_the_computation_budget(self):
        seed_early(self.store, self.cfg)
        runner = Mock()
        clock = Mock(side_effect=[NOW, datetime(2026, 11, 6, 14, 52, 40), datetime(2026, 11, 6, 14, 52, 40)])
        self.assert_failed_without_zero_snapshot(self.store, self.run_final(runner=runner, now_fn=clock))
        runner.assert_not_called()

    def test_evidence_write_failure_prevents_success(self):
        seed_early(self.store, self.cfg)
        with patch.object(self.store, "save_scan_evidence", side_effect=OSError("fixture disk failure")):
            self.assert_failed_without_zero_snapshot(self.store, self.run_final())

    def test_result_returned_after_compute_deadline_is_discarded(self):
        seed_early(self.store, self.cfg)
        clock = Mock(side_effect=[NOW, NOW, datetime(2026, 11, 6, 14, 52, 41), datetime(2026, 11, 6, 14, 52, 41)])
        result = self.run_final(now_fn=clock)
        self.assert_failed_without_zero_snapshot(self.store, result)

    def test_failed_decision_blocks_rebuild_from_preexisting_manual_third_snapshot(self):
        seed_early(self.store, self.cfg)
        self.store.save_strict_scan(slot="1452", scanned_at=NOW, provider="manual", candidates=[candidate()])
        result = self.run_final(runner=Mock(side_effect=DeadlineExceeded("fixture timeout")))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(self.store.rebuild_strict_final_signals(DAY), 0)
        self.assertTrue(self.store.strict_validation_frame().empty)
        self.assertEqual(self.store.pending_strict_final_signals("2026-11-09"), [])

    def test_failed_decision_excludes_preexisting_signal_from_pending_validation(self):
        seed_early(self.store, self.cfg)
        self.store.save_strict_scan(slot="1452", scanned_at=NOW, provider="manual", candidates=[candidate()])
        self.store.rebuild_strict_final_signals(DAY)
        self.assertEqual(len(self.store.pending_strict_final_signals("2026-11-09")), 1)
        result = self.run_final(runner=Mock(side_effect=DeadlineExceeded("fixture timeout")))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(self.store.pending_strict_final_signals("2026-11-09"), [])
        self.assertEqual(self.store.pending_live_strict_signals("2026-11-09"), [])

    def test_success_replaces_provisional_signal_price_and_removes_old_manual_final(self):
        seed_early(self.store, self.cfg)
        self.store.save_strict_scan(slot="1452", scanned_at=NOW, provider="manual", candidates=[candidate(price=99)])
        self.store.rebuild_strict_final_signals(DAY)
        with self.store.connect() as db:
            db.execute("""INSERT INTO strict_final_signals
                (signal_date,code,name,entry_price,composite_score,score_1430,score_1445,score_1452)
                VALUES (?, '600009', 'stale manual', 99, 99, 99, 99, 99)""", (DAY,))
        self.assertEqual(self.run_final()["status"], "success")
        frame = self.store.strict_validation_frame()
        self.assertEqual(frame["code"].tolist(), ["600001"])
        self.assertEqual(frame.iloc[0]["entry_price"], 10.5)
        self.assertEqual(frame.iloc[0]["composite_score"], 86.5)
        self.assertEqual(len(self.store.pending_strict_final_signals("2026-11-09")), 1)

    def test_successful_empty_list_removes_provisional_manual_signals(self):
        seed_early(self.store, self.cfg)
        self.store.save_strict_scan(slot="1452", scanned_at=NOW, provider="manual", candidates=[candidate()])
        self.store.rebuild_strict_final_signals(DAY)
        result = successful_result()
        result.result_frame = result.result_frame.iloc[:0]
        self.assertEqual(self.run_final(runner=Mock(return_value=result))["status"], "success")
        self.assertTrue(self.store.strict_validation_frame().empty)
        self.assertEqual(json.loads(decision_record(self.store, DAY)["candidates_json"]), [])

    def test_save_does_not_overwrite_frozen_or_observed_history(self):
        seed_early(self.store, self.cfg)
        self.run_final()
        frozen = self.store.strict_validation_frame()
        self.assertFalse(save_decision(self.store, DAY, NOW, NOW, "failed", [], 0, "late"))
        pd.testing.assert_frame_equal(self.store.strict_validation_frame(), frozen)
        other = ValidationStore(Path(self.temp.name) / "observed.db")
        seed_early(other, self.cfg)
        other.save_strict_scan(slot="1452", scanned_at=NOW, provider="manual", candidates=[candidate()])
        other.rebuild_strict_final_signals(DAY)
        signal_id = other.pending_strict_final_signals("2026-11-09")[0]["strict_signal_id"]
        other.save_strict_exit_observation(signal_id, "1000", NOW, 11, 10)
        original = other.strict_validation_frame()
        with self.assertRaisesRegex(auto_scan.MarketDataError, "拒绝覆盖历史"):
            save_decision(other, DAY, NOW, NOW, "failed", [], 0, "late")
        pd.testing.assert_frame_equal(other.strict_validation_frame(), original)

    def test_changed_early_snapshot_during_calculation_rolls_back_success(self):
        seed_early(self.store, self.cfg)

        def mutate_early(_fn, **_kwargs):
            self.store.save_strict_scan(slot="1430", scanned_at=datetime(2026, 11, 6, 14, 30, 20),
                                       provider="manual", candidates=[candidate(score=99)])
            return successful_result()

        self.assert_failed_without_zero_snapshot(self.store, self.run_final(runner=mutate_early))

    def test_full_review_never_overwrites_timed_final_or_pushes_a_second_list(self):
        seed_early(self.store, self.cfg)
        self.run_final()
        frozen_scans = self.store.strict_frame(DAY)
        frozen_validation = self.store.strict_validation_frame()
        review_time = datetime(2026, 11, 6, 14, 57)
        result = SimpleNamespace(
            candidates=pd.DataFrame(), result_frame=pd.DataFrame(), errors={},
            provider="fixture review", funnel={"全市场": 5500}, hard_count=20,
        )
        review_candidates = pd.DataFrame([{**candidate("600009", 99, 20), "passed": True}])
        with patch("auto_scan.run_scan_with_retries", return_value=(result, review_time, 1)), \
                patch("auto_scan.derive_strict_frame", return_value=review_candidates), \
                patch("auto_scan.send_once") as send, \
                patch("auto_scan.datetime") as clock:
            clock.now.return_value = datetime(2026, 11, 6, 14, 54)
            response = auto_scan.run_full_review(trade_date=DAY, cfg=self.cfg, store=self.store)
        self.assertEqual(response["status"], "ok")
        self.assertEqual(response["telegram"], "archive_only")
        send.assert_not_called()
        pd.testing.assert_frame_equal(self.store.strict_frame(DAY), frozen_scans)
        pd.testing.assert_frame_equal(self.store.strict_validation_frame(), frozen_validation)
        self.assertEqual(self.store.review_frame(DAY, "strict")["code"].tolist(), ["600009"])


if __name__ == "__main__":
    unittest.main()
