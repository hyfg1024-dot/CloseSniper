import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

import pandas as pd

import validate
from src.deadline_worker import DeadlineExceeded, run_with_deadline
from src.telegram_service import TelegramError, TelegramSettings
from src.validation_source import BoundedValidationSource, _fetch_validation_minutes
from src.validation_store import ValidationStore


def _slow_read_only_fixture():
    time.sleep(4)
    return pd.DataFrame({"close": [10.0]})


class ValidationSourceTests(unittest.TestCase):
    def test_all_read_methods_use_picklable_worker_with_deadline(self):
        frame = pd.DataFrame({"close": [10.0]})
        worker = Mock(return_value=frame)
        source = BoundedValidationSource(timeout_seconds=12, runner=worker)
        self.assertIs(source.minute_recent("600001"), frame)
        self.assertIs(source.minute_between("600001", "2026-10-08", "2026-10-08"), frame)
        self.assertIs(source.index_minute_recent(), frame)
        self.assertEqual([c.kwargs["args"] for c in worker.call_args_list], [
            ("minute_recent", ("600001",)),
            ("minute_between", ("600001", "2026-10-08", "2026-10-08")),
            ("index_minute_recent", ()),
        ])
        for call in worker.call_args_list:
            self.assertIs(call.args[0], _fetch_validation_minutes)
            self.assertEqual(call.kwargs["timeout_seconds"], 12)

    def test_slow_read_is_terminated_instead_of_waiting_for_provider(self):
        def redirected_worker(fn, *, args, timeout_seconds):
            return run_with_deadline(_slow_read_only_fixture, timeout_seconds=timeout_seconds)
        source = BoundedValidationSource(timeout_seconds=0.2, runner=redirected_worker)
        started = time.monotonic()
        with self.assertRaises(DeadlineExceeded):
            source.minute_recent("600001")
        self.assertLess(time.monotonic() - started, 2)

    def test_child_dispatch_only_allows_read_methods(self):
        with patch("src.data_source.AkshareSource") as factory:
            sentinel = pd.DataFrame({"close": [10]})
            factory.return_value.minute_between.return_value = sentinel
            self.assertIs(_fetch_validation_minutes("minute_between", ("600001", "2026-10-08", "2026-10-08")), sentinel)
            factory.return_value.minute_between.assert_called_once_with("600001", "2026-10-08", "2026-10-08")
            with self.assertRaises(ValueError):
                _fetch_validation_minutes("save_validation", ())


class FrozenObservationDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = ValidationStore(Path(self.temp.name) / "runtime.db")
        candidates = [{"code": code, "name": f"股票{code}", "price": 10.0, "score": 80}
                      for code in ("600001", "600002")]
        for slot in ("1430", "1445", "1452"):
            self.store.save_strict_scan(slot=slot, scanned_at=datetime(2026, 11, 5, 14, int(slot[2:])),
                                        provider="fixture", candidates=candidates)
        self.store.rebuild_strict_final_signals("2026-11-05")
        self.ids = {r["code"]: r["strict_signal_id"] for r in self.store.pending_live_strict_signals("2026-11-06")}
        self.settings = TelegramSettings("test-only-token", "test-chat", "fixture", True)
        self.at = datetime(2026, 11, 6, 10, 1)

    def tearDown(self):
        self.temp.cleanup()

    def save(self, code, value=6):
        self.store.save_strict_exit_observation(self.ids[code], "1000", self.at, 10 * (1 + value / 100), value)

    def test_failed_delivery_retries_frozen_value_then_stops_after_success(self):
        self.save("600001")
        failing = Mock(side_effect=TelegramError("offline"))
        first = validate.send_pending_observations(self.store, self.at, settings=self.settings, send_fn=failing)
        self.assertIn("600001", first["errors"])
        self.assertFalse(self.store.notification_sent("2026-11-06", "telegram-strict-exit-1000:600001"))
        successful = Mock()
        retry = validate.send_pending_observations(self.store, datetime(2026, 11, 6, 10, 30),
                                                   settings=self.settings, send_fn=successful)
        self.assertEqual(retry["sent"], ["600001"])
        self.assertIn("+6.00%", successful.call_args.args[1])
        self.assertEqual(self.store.strict_exit_observation_frame("2026-11-06").iloc[0]["observed_at"], self.at.isoformat())
        third = validate.send_pending_observations(self.store, self.at, settings=self.settings, send_fn=successful)
        self.assertEqual(third["already_sent"], ["600001"])
        self.assertEqual(successful.call_count, 1)

    def test_later_stock_is_delivered_even_when_first_stock_already_sent(self):
        self.save("600001")
        send = Mock()
        validate.send_pending_observations(self.store, self.at, settings=self.settings, send_fn=send)
        self.save("600002", -2)
        second = validate.send_pending_observations(self.store, self.at, settings=self.settings, send_fn=send)
        self.assertEqual(second["sent"], ["600002"])
        self.assertEqual(second["already_sent"], ["600001"])
        self.assertIn("600002", send.call_args.args[1])
        self.assertNotIn("600001", send.call_args.args[1])

    def test_main_retries_saved_observations_without_any_new_capture(self):
        self.save("600001")
        with patch.object(validate, "datetime") as clock, \
             patch.object(validate, "ValidationStore", return_value=self.store), \
             patch.object(validate, "BoundedValidationSource") as source_factory, \
             patch.object(validate, "capture_strict_exit_observation", return_value=[]), \
             patch.object(validate, "validate_pending", return_value={}), \
             patch.object(validate, "validate_strict_three_stage_pending", return_value={}), \
             patch.object(validate, "load_settings", return_value=self.settings), \
             patch.object(validate, "send_message") as send, patch("builtins.print"):
            clock.now.return_value = self.at
            validate.main()
            source_factory.assert_called_once_with()
            send.assert_called_once()
            self.assertIn("+6.00%", send.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
