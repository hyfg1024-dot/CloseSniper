from __future__ import annotations

from datetime import datetime, time, timedelta
import math
from typing import Any

import pandas as pd
import numpy as np

from src.data_source import AkshareSource
from src.validation_source import BoundedValidationSource
from src.validation_store import ValidationStore
from src.trading_calendar import next_trading_day


def _skip(summary: dict[str, Any], code: str, reason: str) -> None:
    summary["skipped"] += 1
    summary.setdefault("skipped_reasons", {})[code] = reason


def _expected_session(signal: Any, store: ValidationStore | None = None, kind: str = "strict") -> str:
    expected = next_trading_day(str(signal["signal_date"])).isoformat()
    existing = signal["validation_date"] if "validation_date" in signal.keys() else None
    if existing and str(existing) != expected and store is not None:
        signal_id = signal["strict_signal_id"] if kind == "strict" else signal["signal_id"]
        store.mark_validation_quality(kind, int(signal_id), "invalid", f"历史校验日 {existing} 与下一交易日 {expected} 不符")
    return expected


def _save_evidence(store: ValidationStore, kind: str, signal_id: int, code: str,
                   validation_date: str, rows: pd.DataFrame) -> None:
    store.save_market_evidence(
        kind=kind, signal_id=signal_id, code=code, trade_date=validation_date,
        frame=rows, provider=str(rows.attrs.get("provider") or "分钟行情校验"),
    )
    store.mark_validation_quality(kind, signal_id, "verified", "下一交易日、完整分钟与有效价格已校验")


def capture_open_pending(
    store: ValidationStore,
    source: AkshareSource | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """在次日早盘窗口保存开盘价与点击时快照，不提前生成9:45历史结论。"""
    now = now or datetime.now()
    source = source or BoundedValidationSource()
    pending = store.pending_signals(now.date().isoformat())
    summary: dict[str, Any] = {
        "pending": len(pending),
        "captured": 0,
        "skipped": 0,
        "errors": {},
    }
    for signal in pending:
        code = str(signal["code"])
        try:
            validation_date = _expected_session(signal, store, "improved")
            if validation_date > now.date().isoformat():
                _skip(summary, code, f"等待下一交易日 {validation_date}")
                continue
            stock_days = _split_days(_minutes_for_signal(
                source, code, str(signal["signal_date"]), now, validation_date=validation_date,
            ))
            if validation_date not in stock_days:
                _skip(summary, code, f"缺少下一交易日 {validation_date} 分钟行情")
                continue
            if validation_date == now.date().isoformat() and now.time() < time(9, 30):
                _skip(summary, code, "尚未开盘")
                continue
            result = _calculate_open_snapshot(
                stock_days[validation_date],
                float(signal["entry_price"]),
                cutoff=now.time() if validation_date == now.date().isoformat() else time(9, 50),
            )
            if result is None:
                _skip(summary, code, "开盘快照缺少连续分钟行情或含无效价格")
                continue
            store.save_open_snapshot(
                int(signal["signal_id"]),
                {
                    "validation_date": validation_date,
                    **result,
                    "captured_at": now.isoformat(timespec="seconds"),
                },
            )
            cutoff = now.time() if validation_date == now.date().isoformat() else time(9, 50)
            evidence = _complete_morning_window(
                stock_days[validation_date], min(cutoff.replace(second=0, microsecond=0), time(9, 50)),
            )
            store.save_market_evidence(
                kind="open_snapshot", signal_id=int(signal["signal_id"]), code=code,
                trade_date=validation_date, frame=evidence,
                provider=str(evidence.attrs.get("provider") or "分钟行情校验"),
            )
            summary["captured"] += 1
        except Exception as exc:
            summary["errors"][code] = str(exc)
    return summary


def validate_pending(
    store: ValidationStore,
    source: AkshareSource | None = None,
    now: datetime | None = None,
    *,
    include_history: bool = True,
) -> dict[str, Any]:
    now = now or datetime.now()
    source = source or BoundedValidationSource()
    pending = store.pending_signals(now.date().isoformat())
    if not include_history:
        cutoff = (now.date() - timedelta(days=14)).isoformat()
        pending = [row for row in pending if str(row["validation_date"] or row["signal_date"]) >= cutoff]
    summary: dict[str, Any] = {
        "pending": len(pending),
        "completed": 0,
        "completed_0945": 0,
        "completed_1030": 0,
        "skipped": 0,
        "errors": {},
    }
    if not pending:
        return summary

    try:
        index_days = _split_days(source.index_minute_recent())
    except Exception as exc:
        index_days = {}
        summary["errors"]["index"] = str(exc)
    for signal in pending:
        code = str(signal["code"])
        try:
            validation_date = _expected_session(signal, store, "improved")
            if validation_date > now.date().isoformat():
                _skip(summary, code, f"等待下一交易日 {validation_date}")
                continue
            stock_days = _split_days(_minutes_for_signal(
                source, code, str(signal["signal_date"]), now, validation_date=validation_date,
            ))
            if validation_date not in stock_days:
                _skip(summary, code, f"缺少下一交易日 {validation_date} 分钟行情")
                continue
            if validation_date == now.date().isoformat() and now.time() < time(9, 45):
                _skip(summary, code, "尚未到 09:45")
                continue
            rows = stock_days[validation_date]
            include_1030 = (
                validation_date < now.date().isoformat()
                or now.time() >= time(10, 30)
            )
            if signal["price_0945"] is not None and not include_1030:
                _skip(summary, code, "09:45 已有记录，等待 10:30")
                continue
            result = _calculate_windows(
                rows,
                float(signal["entry_price"]),
                include_1030=include_1030,
            )
            if result is None:
                _skip(summary, code, "固定时点分钟行情缺失、不连续或含无效价格")
                continue
            index_result = _calculate_index_windows(
                index_days.get(validation_date),
                include_1030=include_1030,
            )
            store.save_validation(
                int(signal["signal_id"]),
                {
                    "validation_date": validation_date,
                    **result,
                    **index_result,
                    "calculated_at": now.isoformat(timespec="seconds"),
                },
            )
            evidence = _complete_morning_window(rows, time(10, 30) if include_1030 else time(9, 45))
            _save_evidence(store, "improved", int(signal["signal_id"]), code, validation_date, evidence)
            summary["completed"] += 1
            if result["price_1030"] is None:
                summary["completed_0945"] += 1
            else:
                summary["completed_1030"] += 1
        except Exception as exc:
            summary["errors"][code] = str(exc)
    return summary


def validate_strict_three_stage_pending(
    store: ValidationStore,
    source: AkshareSource | None = None,
    now: datetime | None = None,
    *,
    include_history: bool = True,
) -> dict[str, Any]:
    """校验严格标准且14:30、14:45、14:52均入选的股票至10:00。"""
    now = now or datetime.now()
    source = source or BoundedValidationSource()
    pending = store.pending_strict_final_signals(now.date().isoformat())
    if not include_history:
        cutoff = (now.date() - timedelta(days=14)).isoformat()
        pending = [row for row in pending if str(row["validation_date"] or row["signal_date"]) >= cutoff]
    summary: dict[str, Any] = {"pending": len(pending), "completed": 0, "skipped": 0, "errors": {}}
    for signal in pending:
        code = str(signal["code"])
        try:
            signal_date = str(signal["signal_date"])
            validation_date = _expected_session(signal, store, "strict")
            if validation_date > now.date().isoformat():
                _skip(summary, code, f"等待下一交易日 {validation_date}")
                continue
            stock_days = _split_days(_minutes_for_signal(
                source, code, signal_date, now, validation_date=validation_date,
            ))
            if validation_date not in stock_days:
                _skip(summary, code, f"缺少下一交易日 {validation_date} 分钟行情")
                continue
            if validation_date == now.date().isoformat() and now.time() < time(10, 0):
                _skip(summary, code, "尚未到 10:00")
                continue
            include_1030 = validation_date < now.date().isoformat() or now.time() >= time(10, 30)
            result = _calculate_strict_windows(
                stock_days[validation_date], float(signal["entry_price"]),
                include_1030=include_1030,
            )
            if result is None:
                _skip(summary, code, "严格校验固定时点行情缺失、不连续或含无效价格")
                continue
            store.save_strict_validation(
                int(signal["strict_signal_id"]),
                {
                    "validation_date": validation_date,
                    **result,
                    "calculated_at": now.isoformat(timespec="seconds"),
                },
            )
            evidence = _complete_morning_window(stock_days[validation_date], time(10, 30) if include_1030 else time(10, 0))
            _save_evidence(store, "strict", int(signal["strict_signal_id"]), code, validation_date, evidence)
            summary["completed"] += 1
        except Exception as exc:
            summary["errors"][code] = str(exc)
    return summary


def capture_strict_exit_observation(
    store: ValidationStore,
    source: AkshareSource | None = None,
    now: datetime | None = None,
    *,
    diagnostics: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """At 10:01, capture the completed 10:00 checkpoint for live strict signals only."""
    now = now or datetime.now()
    if now.time() < time(10, 0):
        return []
    source = source or BoundedValidationSource()
    rows: list[dict[str, Any]] = []
    for signal in store.pending_live_strict_signals(now.date().isoformat()):
        code = str(signal["code"])
        try:
            expected = _expected_session(signal)
            if expected != now.date().isoformat():
                if diagnostics is not None:
                    diagnostics[code] = f"该信号下一交易日为 {expected}，不能以今日替代"
                continue
            minutes = _split_days(_minutes_for_signal(
                source, code, str(signal["signal_date"]), now, validation_date=expected,
            ))
            current = minutes.get(expected)
            window = _complete_morning_window(current, time(10, 0))
            if window is None or not _valid_entry(float(signal["entry_price"])):
                if diagnostics is not None:
                    diagnostics[code] = "缺少当天连续至 10:00 的有效分钟行情"
                continue
            price = float(window.iloc[-1]["close"])
            value = (price / float(signal["entry_price"]) - 1) * 100
            store.save_market_evidence(
                kind="strict_observation", signal_id=int(signal["strict_signal_id"]), code=code,
                trade_date=expected, frame=window, provider=str(window.attrs.get("provider") or "分钟行情校验"),
            )
            inserted = store.save_strict_exit_observation(int(signal["strict_signal_id"]), "1000", now, price, value)
            if inserted:
                rows.append({"code": code, "name": str(signal["name"]), "return_pct": value})
        except Exception as exc:
            if diagnostics is not None:
                diagnostics[code] = str(exc)
    return rows


def _minutes_for_signal(
    source: AkshareSource,
    code: str,
    signal_date: str,
    now: datetime,
    *,
    validation_date: str | None = None,
) -> pd.DataFrame:
    """Fetch only the next-session window; a recent-data fallback must never drift forward."""
    expected = next_trading_day(signal_date).isoformat()
    if validation_date and validation_date != expected:
        raise ValueError(f"请求校验日 {validation_date} 不是下一交易日 {expected}")
    raw: pd.DataFrame
    if expected != now.date().isoformat() and hasattr(source, "minute_between"):
        try:
            raw = source.minute_between(code, expected, expected)
        except Exception:
            # The recent endpoint is an availability fallback only. It is bounded below so
            # an unrelated later day cannot become a synthetic “next session”.
            raw = source.minute_recent(code)
    else:
        raw = source.minute_recent(code)
    standardized = _standardize_minutes(raw)
    if standardized.empty:
        return raw
    return standardized.loc[
        standardized["timestamp"].dt.date.astype(str) == expected
    ].copy()


def _standardize_minutes(raw: pd.DataFrame | None) -> pd.DataFrame:
    if raw is None or raw.empty:
        return pd.DataFrame()
    if {"timestamp", "open", "high", "low", "close"}.issubset(raw.columns):
        df = raw[["timestamp", "open", "high", "low", "close"]].copy()
    elif {"day", "open", "high", "low", "close"}.issubset(raw.columns):
        df = raw[["day", "open", "high", "low", "close"]].copy()
        df = df.rename(columns={"day": "timestamp"})
    elif {"时间", "开盘", "最高", "最低", "收盘"}.issubset(raw.columns):
        df = raw[["时间", "开盘", "最高", "最低", "收盘"]].copy()
        df.columns = ["timestamp", "open", "high", "low", "close"]
    else:
        return pd.DataFrame()
    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
    for column in ("open", "high", "low", "close"):
        df[column] = pd.to_numeric(df[column], errors="coerce")
    if df["timestamp"].isna().any():
        raise ValueError("分钟行情包含无法识别的时间戳")
    df.attrs.update(raw.attrs)
    return df.sort_values("timestamp")


def _split_days(raw: pd.DataFrame | None) -> dict[str, pd.DataFrame]:
    df = _standardize_minutes(raw)
    if df.empty:
        return {}
    df["trade_date"] = df["timestamp"].dt.date.astype(str)
    return {date: group.copy() for date, group in df.groupby("trade_date")}


def _first_day_after(days: dict[str, pd.DataFrame], signal_date: str) -> str | None:
    expected = next_trading_day(signal_date).isoformat()
    return expected if expected in days else None


def _valid_entry(entry_price: float) -> bool:
    return math.isfinite(entry_price) and entry_price > 0


def _complete_morning_window(rows: pd.DataFrame | None, endpoint: time) -> pd.DataFrame | None:
    """Validate a complete provider-minute series through an exact fixed endpoint.

    Both common timestamps are supported: first bar at 09:30, or first closed
    one-minute bar at 09:31. Its open is the opening-price proxy in either case.
    Every successive minute through the endpoint must exist exactly once. The
    endpoint close must be timestamped at the endpoint, never the prior minute.
    """
    if rows is None or rows.empty or not time(9, 30) <= endpoint <= time(11, 30):
        return None
    rows = _standardize_minutes(rows)
    window = rows[(rows["timestamp"].dt.time >= time(9, 30)) &
                  (rows["timestamp"].dt.time <= endpoint)].copy()
    if window.empty or window["timestamp"].dt.date.nunique() != 1:
        return None
    timestamps = window["timestamp"]
    if timestamps.iloc[0].time() not in (time(9, 30), time(9, 31)):
        return None
    if timestamps.iloc[-1].time() != endpoint or timestamps.duplicated().any():
        return None
    expected = pd.date_range(timestamps.iloc[0], timestamps.iloc[-1], freq="min")
    if list(timestamps) != list(expected):
        return None
    prices = window[["open", "high", "low", "close"]]
    if not np.isfinite(prices.to_numpy()).all() or not (prices > 0).all().all():
        return None
    if not ((window["high"] >= prices[["open", "close", "low"]].max(axis=1)) &
            (window["low"] <= prices[["open", "close", "high"]].min(axis=1))).all():
        return None
    return window


def _calculate_open_snapshot(
    rows: pd.DataFrame,
    entry_price: float,
    *,
    cutoff: time,
) -> dict[str, float] | None:
    if not _valid_entry(entry_price):
        return None
    effective_cutoff = min(cutoff.replace(second=0, microsecond=0), time(9, 50))
    window = _complete_morning_window(rows, effective_cutoff)
    if window is None:
        return None
    open_price = float(window.iloc[0]["open"])
    captured_price = float(window.iloc[-1]["close"])
    return {
        "open_price": open_price,
        "captured_price": captured_price,
        "open_return": (open_price / entry_price - 1) * 100,
        "captured_return": (captured_price / entry_price - 1) * 100,
    }


def _calculate_half_hour(rows: pd.DataFrame, entry_price: float) -> dict[str, float] | None:
    """固定用9:30开盘至10:00收盘，衡量开盘半小时盈亏。"""
    if not _valid_entry(entry_price):
        return None
    window = _complete_morning_window(rows, time(10, 0))
    if window is None:
        return None
    open_price = float(window.iloc[0]["open"])
    price_1000 = float(window.iloc[-1]["close"])
    high = float(window["high"].max())
    low = float(window["low"].min())
    pct = lambda price: (price / entry_price - 1) * 100
    return {
        "open_price": open_price,
        "price_1000": price_1000,
        "high_1000": high,
        "low_1000": low,
        "open_return": pct(open_price),
        "return_1000": pct(price_1000),
        "max_return_1000": pct(high),
        "max_drawdown_1000": pct(low),
    }


def _calculate_strict_windows(
    rows: pd.DataFrame, entry_price: float, *, include_1030: bool = True,
) -> dict[str, float | None] | None:
    """严格三次稳定的开盘、5–30分钟与31–60分钟三个固定时段。"""
    half_hour = _calculate_half_hour(rows, entry_price)
    if half_hour is None:
        return None
    first_5 = _complete_morning_window(rows, time(9, 35))
    if first_5 is None:
        return None
    price_0935 = float(first_5.iloc[-1]["close"])
    price_1030 = None
    if include_1030:
        first_hour = _complete_morning_window(rows, time(10, 30))
        if first_hour is None:
            return None
        price_1030 = float(first_hour.iloc[-1]["close"])
    price_1000 = float(half_hour["price_1000"])
    half_hour.update(
        {
            "price_0935": price_0935,
            "price_1030": price_1030,
            "return_0530": (price_1000 / price_0935 - 1) * 100,
            "return_3160": (price_1030 / price_1000 - 1) * 100 if price_1030 is not None else None,
        }
    )
    return half_hour


def _calculate_windows(
    rows: pd.DataFrame,
    entry_price: float,
    *,
    include_1030: bool,
) -> dict[str, float | None] | None:
    if not _valid_entry(entry_price):
        return None
    window_0945 = _complete_morning_window(rows, time(9, 45))
    if window_0945 is None:
        return None
    open_price = float(window_0945.iloc[0]["open"])
    price_0945 = float(window_0945.iloc[-1]["close"])
    high_0945 = float(window_0945["high"].max())
    low_0945 = float(window_0945["low"].min())
    pct = lambda price: (price / entry_price - 1) * 100
    result: dict[str, float | None] = {
        "open_price": open_price,
        "price_0945": price_0945,
        "high_0945": high_0945,
        "low_0945": low_0945,
        "open_return": pct(open_price),
        "return_0945": pct(price_0945),
        "max_return": pct(high_0945),
        "max_drawdown": pct(low_0945),
        "price_1030": None,
        "high_1030": None,
        "low_1030": None,
        "return_1030": None,
        "max_return_1030": None,
        "max_drawdown_1030": None,
    }
    if not include_1030:
        return result
    window_1030 = _complete_morning_window(rows, time(10, 30))
    if window_1030 is None:
        return None
    price_1030 = float(window_1030.iloc[-1]["close"])
    high_1030 = float(window_1030["high"].max())
    low_1030 = float(window_1030["low"].min())
    result.update(
        {
            "price_1030": price_1030,
            "high_1030": high_1030,
            "low_1030": low_1030,
            "return_1030": pct(price_1030),
            "max_return_1030": pct(high_1030),
            "max_drawdown_1030": pct(low_1030),
        }
    )
    return result


def _calculate_index_windows(
    rows: pd.DataFrame | None,
    *,
    include_1030: bool,
) -> dict[str, float | None]:
    empty_result = {
        "index_open_return": None,
        "index_0945_return": None,
        "index_1030_return": None,
    }
    window_0945 = _complete_morning_window(rows, time(9, 45))
    if window_0945 is None:
        return empty_result
    base = float(window_0945.iloc[0]["open"])
    if base <= 0:
        return empty_result
    result = {
        "index_open_return": 0.0,
        "index_0945_return": (float(window_0945.iloc[-1]["close"]) / base - 1) * 100,
        "index_1030_return": None,
    }
    if include_1030:
        window_1030 = _complete_morning_window(rows, time(10, 30))
        if window_1030 is not None:
            result["index_1030_return"] = (
                float(window_1030.iloc[-1]["close"]) / base - 1
            ) * 100
    return result
