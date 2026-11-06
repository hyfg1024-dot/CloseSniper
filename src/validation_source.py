"""Bound each read-only minute-data fetch in a disposable process."""
from __future__ import annotations

from typing import Callable

import pandas as pd

from src.deadline_worker import run_with_deadline


def _fetch_validation_minutes(method: str, args: tuple[str, ...]) -> pd.DataFrame:
    # A module-level callable is spawn-picklable on macOS. Do not import stores,
    # run validators, send notifications, or write production state in here.
    if method not in {"minute_recent", "minute_between", "index_minute_recent"}:
        raise ValueError("不支持的校验行情读取方法")
    from src.data_source import AkshareSource
    source = AkshareSource()
    return getattr(source, method)(*args)


class BoundedValidationSource:
    def __init__(self, timeout_seconds: float = 15.0, *, runner: Callable | None = None):
        self.timeout_seconds = timeout_seconds
        self._runner = runner or run_with_deadline

    def _read(self, method: str, *args: str) -> pd.DataFrame:
        return self._runner(
            _fetch_validation_minutes, args=(method, tuple(args)),
            timeout_seconds=self.timeout_seconds,
        )

    def minute_recent(self, code: str) -> pd.DataFrame:
        return self._read("minute_recent", code)

    def minute_between(self, code: str, start_date: str, end_date: str) -> pd.DataFrame:
        return self._read("minute_between", code, start_date, end_date)

    def index_minute_recent(self) -> pd.DataFrame:
        return self._read("index_minute_recent")
