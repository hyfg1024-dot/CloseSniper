import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

import pandas as pd

import auto_scan
from src.data_source import AkshareSource, MarketDataError, demo_spot
from src.scan_service import _cutoff_intraday, run_frozen_candidate_scan, run_market_scan
from src.validation_store import ValidationStore


class _Response:
    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {
            "data": {
                "total": 1,
                "rank_list": [{
                    "code": "sh600000", "name": "浦发银行", "zxj": "9.05",
                    "zdf": "-0.66", "lb": "0.83", "hsl": "0.40",
                    "ltsz": "3014.18", "volume": "503206", "turnover": "45657",
                }],
            }
        }


class _Session:
    def get(self, *args, **kwargs) -> _Response:
        return _Response()


class _ScanResult:
    provider = "测试行情"
    funnel = {"全市场": 1}
    candidates = pd.DataFrame()
    raw_spot = pd.DataFrame()
    result_frame = pd.DataFrame(columns=["passed", "code", "name", "price", "score"])
    daily_history = {}
    minute_history = {}
    concepts = {}
    errors = {}
    hard_count = 0


class ResilienceTests(unittest.TestCase):
    def test_no_attempt_budget_and_empty_retry_list_record_failure(self):
        for delays, expired in (((0, 3), True), ((), False)):
            with self.subTest(delays=delays), tempfile.TemporaryDirectory() as directory:
                store = ValidationStore(Path(directory) / "scan.db")
                now = datetime(2026, 11, 6, 14, 32, 30)
                with patch.object(auto_scan, "run_with_deadline") as worker:
                    with self.assertRaises(MarketDataError):
                        auto_scan.run_scan_with_retries(
                            cfg=auto_scan.StrategyConfig(), slot="1430", store=store,
                            now_fn=lambda: now, retry_delays=delays,
                            deadline_at=now if expired else now + timedelta(seconds=1),
                        )
                worker.assert_not_called()
                status = store.scan_status_frame("2026-11-06").iloc[0]
                self.assertEqual(status["status"], "failed")
                self.assertEqual(status["attempt_count"], 0)

    def test_timed_out_retry_does_not_sleep_past_deadline_or_claim_success(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ValidationStore(Path(directory) / "scan.db")
            clock = [datetime(2026, 11, 6, 14, 30)]
            deadline = clock[0] + timedelta(seconds=150)
            sleep = Mock()

            def slow_result(*_args, **_kwargs):
                clock[0] = deadline
                return _ScanResult()

            with patch.object(auto_scan, "run_with_deadline", side_effect=slow_result) as worker:
                with self.assertRaisesRegex(MarketDataError, "截止"):
                    auto_scan.run_scan_with_retries(
                        cfg=auto_scan.StrategyConfig(), slot="1430", store=store,
                        now_fn=lambda: clock[0], retry_delays=(0, 3), sleep_fn=sleep,
                        deadline_at=deadline,
                    )
            self.assertEqual(worker.call_count, 1)
            sleep.assert_not_called()
            status = store.scan_status_frame("2026-11-06").iloc[0]
            self.assertEqual(status["status"], "failed")
            self.assertEqual(status["attempt_count"], 1)
            self.assertTrue(store.strict_frame("2026-11-06").empty)

    def test_early_slot_uses_absolute_budget_after_init_and_retries(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ValidationStore(Path(directory) / "scan.db")
            clock = [datetime(2026, 11, 6, 14, 30, 10)]
            budgets = []

            def worker(*_args, **kwargs):
                budgets.append(kwargs["timeout_seconds"])
                self.assertTrue(kwargs["kwargs"]["analyze_all"])
                if len(budgets) == 1:
                    clock[0] += timedelta(seconds=2)
                    raise MarketDataError("transient")
                return _ScanResult()

            def sleep(seconds):
                clock[0] += timedelta(seconds=seconds)

            # Call the retry layer directly so its clock and sleep are fully
            # controlled without touching the production provider or Telegram.
            with patch.object(auto_scan, "run_with_deadline", side_effect=worker):
                auto_scan.run_scan_with_retries(
                    cfg=auto_scan.StrategyConfig(), slot="1430", store=store,
                    now_fn=lambda: clock[0], sleep_fn=sleep, retry_delays=(0, 3),
                    deadline_at=datetime(2026, 11, 6, 14, 32, 30), analyze_all=True,
                )
            self.assertEqual(budgets, [140, 135])

    def test_automatic_early_partial_data_is_failed_not_empty_success(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ValidationStore(Path(directory) / "scan.db")
            result = _ScanResult()
            result.errors = {"600001": "missing minute"}
            with patch.object(auto_scan, "run_with_deadline", return_value=result):
                with self.assertRaisesRegex(MarketDataError, "600001"):
                    auto_scan.run_scan_with_retries(
                        cfg=auto_scan.StrategyConfig(), slot="1430", store=store,
                        now_fn=lambda: datetime(2026, 11, 6, 14, 30), retry_delays=(0,),
                        deadline_at=datetime(2026, 11, 6, 14, 32, 30), require_complete=True,
                    )
            self.assertEqual(store.scan_status_frame("2026-11-06").iloc[0]["status"], "failed")
            self.assertTrue(store.strict_frame("2026-11-06").empty)

    def test_early_call_is_harmless_and_late_call_is_explicit_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ValidationStore(Path(directory) / "scan.db")
            with patch.object(auto_scan, "run_scan_with_retries") as scan:
                response = auto_scan.run_regular_slot(
                    slot="1430", cfg=auto_scan.StrategyConfig(), store=store,
                    now_fn=lambda: datetime(2026, 11, 6, 14, 29, 59),
                )
                self.assertEqual(response["status"], "skipped")
                self.assertTrue(store.scan_status_frame("2026-11-06").empty)
                with self.assertRaises(MarketDataError):
                    auto_scan.run_regular_slot(
                        slot="1430", cfg=auto_scan.StrategyConfig(), store=store,
                        now_fn=lambda: datetime(2026, 11, 6, 14, 32, 30),
                    )
                scan.assert_not_called()
            self.assertEqual(store.scan_status_frame("2026-11-06").iloc[0]["status"], "failed")

    def test_early_cache_failure_invalidates_previously_successful_scan(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ValidationStore(Path(directory) / "scan.db")
            at = datetime(2026, 11, 6, 14, 30, 10)
            with patch.object(auto_scan, "run_with_deadline", return_value=_ScanResult()), \
                    patch.object(auto_scan, "derive_strict_frame", return_value=_ScanResult.result_frame), \
                    patch.object(auto_scan, "cache_scan", side_effect=OSError("disk failure")):
                with self.assertRaisesRegex(MarketDataError, "disk failure"):
                    auto_scan.run_regular_slot(slot="1430", cfg=auto_scan.StrategyConfig(), store=store,
                                               now_fn=lambda: at)
            self.assertEqual(store.scan_status_frame("2026-11-06").iloc[0]["status"], "failed")
            self.assertTrue(store.strict_frame("2026-11-06").empty)

    def test_holiday_main_skips_without_database_or_network(self):
        with patch("sys.argv", ["auto_scan.py", "--slot", "1452"]), \
                patch.object(auto_scan, "datetime") as clock, \
                patch.object(auto_scan, "ValidationStore") as store, \
                patch.object(auto_scan, "run_strict_final") as scanner:
            clock.now.return_value = datetime(2026, 10, 1, 14, 52)
            auto_scan.main()
        store.assert_not_called()
        scanner.assert_not_called()

    def test_busy_node_lock_is_nonblocking_and_skips_without_database(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch("sys.argv", ["auto_scan.py", "--slot", "1452"]), \
                    patch.object(auto_scan, "datetime") as clock, \
                    patch.object(auto_scan, "Path", return_value=Path(directory) / "test.lock") as path, \
                    patch.object(auto_scan.fcntl, "flock", side_effect=BlockingIOError) as flock, \
                    patch.object(auto_scan, "ValidationStore") as store:
                clock.now.return_value = datetime(2026, 11, 6, 14, 52)
                auto_scan.main()
            store.assert_not_called()
            path.assert_called_with("data/auto-scan-1452.lock")
            self.assertTrue(flock.call_args.args[1] & auto_scan.fcntl.LOCK_NB)

    def test_main_initialization_does_not_extend_final_deadline(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ValidationStore(Path(directory) / "scan.db")
            at = [datetime(2026, 11, 6, 14, 52)]

            def delayed_initialization(**_kwargs):
                at[0] = datetime(2026, 11, 6, 14, 52, 41)
                return store

            with patch("sys.argv", ["auto_scan.py", "--slot", "1452"]), \
                    patch.object(auto_scan, "datetime") as clock, \
                    patch.object(auto_scan, "Path", return_value=Path(directory) / "test.lock"), \
                    patch.object(auto_scan, "ValidationStore", side_effect=delayed_initialization), \
                    patch("src.strict_schedule.publish_decision", return_value="disabled"), \
                    patch("src.strict_schedule.prepared_inputs") as prepare:
                clock.now.side_effect = lambda: at[0]
                auto_scan.main()
            prepare.assert_not_called()
            self.assertEqual(store.scan_status_frame("2026-11-06").iloc[0]["status"], "failed")

    def test_main_before_planned_time_skips_without_initialization(self):
        with patch("sys.argv", ["auto_scan.py", "--slot", "1452"]), \
                patch.object(auto_scan, "datetime") as clock, \
                patch.object(auto_scan, "ValidationStore") as store:
            clock.now.return_value = datetime(2026, 11, 6, 14, 51, 59)
            auto_scan.main()
        store.assert_not_called()

    def test_frozen_candidate_scan_reuses_1445_analysis_and_updates_live_fields(self) -> None:
        raw = pd.DataFrame([{
            "代码": "600001", "名称": "测试股份", "最新价": 10.4,
            "涨跌幅": 4.0, "量比": 1.5, "换手率": 7.0,
            "流通市值": 100e8, "成交量": 10_000_000,
            "成交额": 103_000_000, "最高": 10.5, "最低": 9.8,
            "今开": 9.9, "昨收": 10.0, "报价时间": "20260831145205",
        }])
        raw.attrs["provider"] = "测试增量行情"
        prior = pd.DataFrame([{
            "code": "600001", "name": "测试股份", "score": 80.0,
            "change_pct": 3.8, "volume_ratio": 1.4, "turnover": 6.8,
        }])
        with patch("src.scan_service.AkshareSource") as source_class:
            source = source_class.return_value
            source.fast_spot.return_value = raw
            source.fast_index_return.return_value = (1.0, "20260831145205")
            result = run_frozen_candidate_scan(
                auto_scan.StrategyConfig(), prior,
                now=datetime(2026, 8, 31, 14, 52, 5),
            )

        self.assertEqual(len(result.candidates), 1)
        row = result.candidates.iloc[0]
        self.assertEqual(row["code"], "600001")
        self.assertAlmostEqual(row["price"], 10.4)
        self.assertGreater(row["score"], 80.0)

    def test_fast_intraday_data_is_cut_off_at_1452(self) -> None:
        frame = pd.DataFrame({
            "day": ["2026-08-31 14:52:00", "2026-08-31 14:53:00"],
            "close": [10.0, 10.2],
        })

        frozen = _cutoff_intraday(frame, datetime(2026, 8, 31, 14, 52, 59))

        self.assertEqual(frozen["close"].tolist(), [10.0])

    def test_tencent_spot_maps_required_strategy_fields(self) -> None:
        with patch("src.data_source.requests.Session", return_value=_Session()):
            frame = AkshareSource._tencent_spot()

        row = frame.iloc[0]
        self.assertEqual(row["代码"], "600000")
        self.assertAlmostEqual(row["最新价"], 9.05)
        self.assertAlmostEqual(row["换手率"], 0.40)
        self.assertAlmostEqual(row["流通市值"], 3014.18e8)
        self.assertAlmostEqual(row["成交量"], 50_320_600)
        self.assertEqual(frame.attrs["provider"], "腾讯财经")

    def test_background_scan_retries_then_records_success(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ValidationStore(Path(directory) / "scan.db")
            with patch.object(
                auto_scan,
                "run_market_scan",
                side_effect=[MarketDataError("临时失败"), _ScanResult()],
            ) as scanner:
                result, _, attempts = auto_scan.run_scan_with_retries(
                    cfg=auto_scan.StrategyConfig(),
                    slot="1430",
                    store=store,
                    sleep_fn=lambda _seconds: None,
                    now_fn=lambda: datetime(2026, 8, 21, 14, 30),
                )

            self.assertEqual(result.provider, "测试行情")
            self.assertEqual(scanner.call_count, 2)
            self.assertEqual(attempts, 2)
            status = store.scan_status_frame("2026-08-21").iloc[0]
            self.assertEqual(status["status"], "success")
            self.assertEqual(status["attempt_count"], 2)

    def test_background_scan_records_failure_after_all_attempts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ValidationStore(Path(directory) / "scan.db")
            with patch.object(auto_scan, "run_market_scan", side_effect=MarketDataError("持续失败")):
                with self.assertRaises(MarketDataError):
                    auto_scan.run_scan_with_retries(
                        cfg=auto_scan.StrategyConfig(),
                        slot="1445",
                        store=store,
                        sleep_fn=lambda _seconds: None,
                        now_fn=lambda: datetime(2026, 8, 21, 14, 45),
                    )

            status = store.scan_status_frame("2026-08-21").iloc[0]
            self.assertEqual(status["status"], "failed")
            self.assertEqual(status["attempt_count"], 4)
            self.assertIn("持续失败", status["error_message"])

    def test_partial_data_errors_survive_success_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ValidationStore(Path(directory) / "scan.db")
            partial_result = _ScanResult()
            partial_result.errors = {"600001": "分时读取失败", "600002": "日线读取失败"}
            with patch.object(auto_scan, "run_market_scan", return_value=partial_result):
                auto_scan.run_scan_with_retries(
                    cfg=auto_scan.StrategyConfig(), slot="1430", store=store,
                    now_fn=lambda: datetime(2026, 11, 6, 14, 30),
                )
            status = store.scan_status_frame("2026-11-06").iloc[0]
            self.assertEqual(status["status"], "success")
            self.assertEqual(status["candidate_count"], 0)
            self.assertIn("600001: 分时读取失败", status["error_message"])
            self.assertIn("600002: 日线读取失败", status["error_message"])

    def test_total_candidate_data_failure_never_becomes_clean_empty_result(self) -> None:
        available = {"600001": pd.DataFrame({"close": [10.0]})}
        unavailable = {"600001": "行情读取失败"}
        for daily, minutes in (({}, available), (available, {}), ({}, {})):
            with self.subTest(daily_missing=not daily, minutes_missing=not minutes):
                with patch("src.scan_service.AkshareSource") as source_class, patch(
                    "src.scan_service.parallel_fetch",
                    side_effect=[(daily, unavailable if not daily else {}),
                                 (minutes, unavailable if not minutes else {})],
                ):
                    source = source_class.return_value
                    source.spot.return_value = demo_spot()
                    with self.assertRaisesRegex(MarketDataError, "不能将缺失数据记为无符合条件股票"):
                        run_market_scan(auto_scan.StrategyConfig(), now=datetime(2026, 11, 6, 14, 30))
                    source.index_minute.assert_not_called()

    def test_fast_final_with_empty_1445_pool_finishes_without_market_request(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ValidationStore(Path(directory) / "scan.db")
            common = dict(provider="测试", market_count=5000, hard_count=10, config={}, candidates=[])
            store.save_staged_scan(
                slot="1430", scanned_at=datetime(2026, 8, 21, 14, 30), **common,
            )
            store.save_staged_scan(
                slot="1445", scanned_at=datetime(2026, 8, 21, 14, 45), **common,
            )
            with patch.object(auto_scan, "run_market_scan") as scanner, patch.object(
                auto_scan, "load_settings",
            ) as settings:
                settings.return_value.enabled = False
                payload = auto_scan.run_fast_final(
                    trade_date="2026-08-21", cfg=auto_scan.StrategyConfig(), store=store,
                    now_fn=lambda: datetime(2026, 8, 21, 14, 52),
                )

            scanner.assert_not_called()
            self.assertEqual(payload["fast"], 0)
            self.assertTrue(payload["finalized"])
            self.assertTrue(store.final_frame("2026-08-21").empty)
            self.assertEqual(payload["telegram"], "suppressed_risk_only")


if __name__ == "__main__":
    unittest.main()
