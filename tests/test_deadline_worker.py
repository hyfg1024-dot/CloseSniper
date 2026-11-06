import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

from src.deadline_worker import DeadlineExceeded, DeadlineWorkerError, run_with_deadline


def _add(a, *, b):
    return {"sum": a + b}


def _fail():
    raise ValueError("fixture failure")


def _crash():
    os._exit(3)


def _late_write(path):
    time.sleep(1.0)
    Path(path).write_text("too late", encoding="utf-8")
    return "too late"


def _large_result():
    return b"x" * 2_000_000


class _SlowArgument:
    def __reduce__(self):
        time.sleep(1.0)
        return str, ("argument",)


def _identity(value):
    return value


def _leave_background_thread(path):
    threading.Thread(target=_late_write, args=(path,)).start()
    return "complete"


class DeadlineWorkerTests(unittest.TestCase):
    def test_spawn_returns_result_and_kwargs(self):
        self.assertEqual(run_with_deadline(_add, args=(2,), kwargs={"b": 3}, timeout_seconds=3), {"sum": 5})

    def test_error_and_process_crash_are_explicit_failures(self):
        with self.assertRaisesRegex(DeadlineWorkerError, "ValueError: fixture failure"):
            run_with_deadline(_fail, timeout_seconds=3)
        with self.assertRaisesRegex(DeadlineWorkerError, "WorkerDisconnected"):
            run_with_deadline(_crash, timeout_seconds=3)

    def test_timed_out_process_cannot_write_a_late_result(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "late.txt"
            started = time.monotonic()
            with self.assertRaises(DeadlineExceeded):
                run_with_deadline(_late_write, args=(str(marker),), timeout_seconds=0.2)
            self.assertLess(time.monotonic() - started, 0.9)
            time.sleep(1.1)
            self.assertFalse(marker.exists())

    def test_large_result_does_not_deadlock_on_join(self):
        self.assertEqual(len(run_with_deadline(_large_result, timeout_seconds=3)), 2_000_000)

    def test_input_serialization_is_inside_budget(self):
        started = time.monotonic()
        with self.assertRaises(DeadlineExceeded):
            run_with_deadline(_identity, args=(_SlowArgument(),), timeout_seconds=0.2)
        self.assertLess(time.monotonic() - started, 0.9)

    def test_large_input_does_not_block_process_start(self):
        payload = b"x" * 2_000_000
        self.assertEqual(len(run_with_deadline(_identity, args=(payload,), timeout_seconds=3)), len(payload))

    def test_returning_worker_cannot_leave_network_threads_alive(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "late-thread.txt"
            self.assertEqual(run_with_deadline(_leave_background_thread, args=(str(marker),), timeout_seconds=3), "complete")
            time.sleep(1.1)
            self.assertFalse(marker.exists())

    def test_expired_budget_never_starts_work(self):
        for budget in (0, -1, float("nan"), float("inf")):
            with self.subTest(budget=budget), self.assertRaises(DeadlineExceeded):
                run_with_deadline(_add, args=(2,), kwargs={"b": 3}, timeout_seconds=budget)


if __name__ == "__main__":
    unittest.main()
