"""Run potentially hanging market reads in a disposable spawned process."""
from __future__ import annotations

import multiprocessing
import math
import threading
import time
from typing import Any, Callable


class DeadlineExceeded(TimeoutError):
    """The calculation did not finish inside its allotted wall-clock budget."""


class DeadlineWorkerError(RuntimeError):
    """A spawned calculation failed, crashed, or could not return a result."""


def _run_child(connection, task_connection) -> None:
    try:
        try:
            fn, args, kwargs = task_connection.recv()
            message = ("ok", fn(*args, **kwargs))
        except BaseException as exc:
            message = ("error", type(exc).__name__, str(exc))
        connection.send(message)
    finally:
        task_connection.close()
        connection.close()


def _stop_process(process) -> None:
    # A provider may have left non-daemon threads blocked in network reads even
    # after returning a result.  Do not let those threads hold the parent open.
    process.join(timeout=0.1)
    if process.is_alive():
        process.terminate()
        process.join(timeout=0.2)
    if process.is_alive():
        process.kill()
        process.join(timeout=0.2)
    if process.is_alive():
        raise DeadlineWorkerError("行情子进程未能停止，禁止继续接受其结果")
    process.close()


def run_with_deadline(
    fn: Callable,
    args: tuple = (),
    kwargs: dict[str, Any] | None = None,
    *,
    timeout_seconds: float,
) -> Any:
    """Return ``fn(*args, **kwargs)`` or stop its process at the deadline.

    ``fn`` must be a module-level, spawn-picklable callable.  It must not write
    production state: only the parent can accept and persist a timely result.
    The budget includes process startup and result transfer; process cleanup
    adds at most 0.7 seconds of bounded joins.  Unlike a thread timeout, this
    stops a stalled request and prevents it from later publishing a result.
    """
    timeout_seconds = float(timeout_seconds)
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise DeadlineExceeded("已超过计算截止时间")
    deadline = time.monotonic() + timeout_seconds
    context = multiprocessing.get_context("spawn")
    receive, send = context.Pipe(duplex=False)
    task_receive, task_send = context.Pipe(duplex=False)
    process = context.Process(
        # Only tiny connection handles are passed through process.start().
        # Sending cached histories there could otherwise block startup on a
        # full pipe before the caller is able to enforce its timeout.
        target=_run_child, args=(send, task_receive), daemon=True,
    )
    complete = threading.Event()
    result: list[tuple] = []

    def read_result() -> None:
        try:
            result.append(receive.recv())
        except Exception as exc:
            result.append(("error", "WorkerDisconnected", str(exc) or "行情子进程提前退出"))
        finally:
            complete.set()

    def send_task() -> None:
        try:
            task_send.send((fn, tuple(args), dict(kwargs or {})))
        except Exception as exc:
            result.append(("error", "WorkerInputError", str(exc)))
            complete.set()
        finally:
            task_send.close()

    started = False
    reader: threading.Thread | None = None
    writer: threading.Thread | None = None
    try:
        try:
            process.start()
            started = True
        except Exception as exc:
            raise DeadlineWorkerError(f"无法启动行情子进程：{exc}") from exc
        send.close()
        task_receive.close()
        writer = threading.Thread(target=send_task, name="deadline-input", daemon=True)
        writer.start()
        # recv() itself may block on a large payload after its header arrives.
        # Reading on a daemon thread keeps the deadline effective during IPC.
        reader = threading.Thread(target=read_result, name="deadline-result", daemon=True)
        reader.start()
        remaining = max(0.0, deadline - time.monotonic())
        if not complete.wait(remaining) or time.monotonic() >= deadline:
            raise DeadlineExceeded(f"行情计算超过 {timeout_seconds:g} 秒预算")
        message = result[0]
        if message[0] != "ok":
            raise DeadlineWorkerError(f"{message[1]}: {message[2]}")
        return message[1]
    finally:
        send.close()
        task_receive.close()
        if started:
            _stop_process(process)
        else:
            process.close()
        receive.close()
        if writer is not None:
            writer.join(timeout=0.1)
        else:
            task_send.close()
        if reader is not None:
            reader.join(timeout=0.1)
