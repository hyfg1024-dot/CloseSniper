from __future__ import annotations

from datetime import datetime, time
from typing import Any

import pandas as pd

from src.data_source import AkshareSource
from src.validation_store import ValidationStore


def capture_open_pending(
    store: ValidationStore,
    source: AkshareSource | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """在次日早盘窗口保存开盘价与点击时快照，不提前生成9:45历史结论。"""
    now = now or datetime.now()
    source = source or AkshareSource()
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
            stock_days = _split_days(source.minute_recent(code))
            validation_date = _first_day_after(stock_days, str(signal["signal_date"]))
            if validation_date is None:
                summary["skipped"] += 1
                continue
            if validation_date == now.date().isoformat() and now.time() < time(9, 30):
                summary["skipped"] += 1
                continue
            result = _calculate_open_snapshot(
                stock_days[validation_date],
                float(signal["entry_price"]),
                cutoff=now.time() if validation_date == now.date().isoformat() else time(9, 50),
            )
            if result is None:
                summary["skipped"] += 1
                continue
            store.save_open_snapshot(
                int(signal["signal_id"]),
                {
                    "validation_date": validation_date,
                    **result,
                    "captured_at": now.isoformat(timespec="seconds"),
                },
            )
            summary["captured"] += 1
        except Exception as exc:
            summary["errors"][code] = str(exc)
    return summary


def validate_pending(
    store: ValidationStore,
    source: AkshareSource | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now()
    source = source or AkshareSource()
    pending = store.pending_signals(now.date().isoformat())
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

    index_days = _split_days(source.index_minute_recent())
    for signal in pending:
        code = str(signal["code"])
        try:
            stock_days = _split_days(source.minute_recent(code))
            validation_date = _first_day_after(stock_days, str(signal["signal_date"]))
            if validation_date is None:
                summary["skipped"] += 1
                continue
            if validation_date == now.date().isoformat() and now.time() < time(9, 45):
                summary["skipped"] += 1
                continue
            rows = stock_days[validation_date]
            include_1030 = (
                validation_date < now.date().isoformat()
                or now.time() >= time(10, 30)
            )
            if signal["price_0945"] is not None and not include_1030:
                summary["skipped"] += 1
                continue
            result = _calculate_windows(
                rows,
                float(signal["entry_price"]),
                include_1030=include_1030,
            )
            if result is None:
                summary["skipped"] += 1
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
) -> dict[str, Any]:
    """校验严格标准且14:30、14:45、14:52均入选的股票至10:00。"""
    now = now or datetime.now()
    source = source or AkshareSource()
    pending = store.pending_strict_final_signals(now.date().isoformat())
    summary: dict[str, Any] = {"pending": len(pending), "completed": 0, "skipped": 0, "errors": {}}
    for signal in pending:
        code = str(signal["code"])
        try:
            signal_date = str(signal["signal_date"])
            existing_date = str(signal["validation_date"] or "")
            stock_days = _split_days(_minutes_for_signal(
                source, code, signal_date, now, validation_date=existing_date or None,
            ))
            validation_date = existing_date or _first_day_after(stock_days, signal_date)
            if validation_date is None:
                summary["skipped"] += 1
                continue
            if validation_date not in stock_days:
                # Historical fallback may contain only much later sessions. Never overwrite a
                # prior validation with a date that is unrelated to this signal.
                summary["skipped"] += 1
                continue
            if validation_date == now.date().isoformat() and now.time() < time(10, 0):
                summary["skipped"] += 1
                continue
            result = _calculate_strict_windows(
                stock_days[validation_date], float(signal["entry_price"]),
            )
            if result is None:
                summary["skipped"] += 1
                continue
            store.save_strict_validation(
                int(signal["strict_signal_id"]),
                {
                    "validation_date": validation_date,
                    **result,
                    "calculated_at": now.isoformat(timespec="seconds"),
                },
            )
            summary["completed"] += 1
        except Exception as exc:
            summary["errors"][code] = str(exc)
    return summary


def capture_strict_exit_observation(
    store: ValidationStore,
    source: AkshareSource | None = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """At 10:01, capture the completed 10:00 checkpoint for live strict signals only."""
    now = now or datetime.now()
    if now.time() < time(10, 0):
        return []
    source = source or AkshareSource()
    rows: list[dict[str, Any]] = []
    for signal in store.pending_live_strict_signals(now.date().isoformat()):
        try:
            minutes = _split_days(source.minute_recent(str(signal["code"])))
            current = minutes.get(now.date().isoformat())
            if current is None:
                continue
            window = current[current["timestamp"].dt.time <= time(10, 0)].copy()
            if window.empty or window["timestamp"].iloc[-1].time() < time(9, 59):
                continue
            price = float(window.iloc[-1]["close"])
            value = (price / float(signal["entry_price"]) - 1) * 100
            store.save_strict_exit_observation(int(signal["strict_signal_id"]), "1000", now, price, value)
            rows.append({"code": str(signal["code"]), "name": str(signal["name"]), "return_pct": value})
        except Exception:
            continue
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
    raw: pd.DataFrame
    if signal_date < now.date().isoformat() and hasattr(source, "minute_between"):
        start = pd.Timestamp(validation_date) if validation_date else pd.Timestamp(signal_date) + pd.Timedelta(days=1)
        # 留出周末和节假日；已有校验日时则只允许该日期，防止补算覆盖历史结果。
        end = pd.Timestamp(validation_date) if validation_date else pd.Timestamp(signal_date) + pd.Timedelta(days=7)
        try:
            raw = source.minute_between(code, start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))
        except Exception:
            # The recent endpoint is an availability fallback only. It is bounded below so
            # an unrelated later day cannot become a synthetic “next session”.
            raw = source.minute_recent(code)
    else:
        raw = source.minute_recent(code)
    standardized = _standardize_minutes(raw)
    if standardized.empty:
        return raw
    start = pd.Timestamp(validation_date) if validation_date else pd.Timestamp(signal_date) + pd.Timedelta(days=1)
    end = pd.Timestamp(validation_date) if validation_date else pd.Timestamp(signal_date) + pd.Timedelta(days=7)
    return standardized.loc[
        (standardized["timestamp"].dt.normalize() >= start.normalize())
        & (standardized["timestamp"].dt.normalize() <= end.normalize())
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
    return df.dropna().sort_values("timestamp")


def _split_days(raw: pd.DataFrame | None) -> dict[str, pd.DataFrame]:
    df = _standardize_minutes(raw)
    if df.empty:
        return {}
    df["trade_date"] = df["timestamp"].dt.date.astype(str)
    return {date: group.copy() for date, group in df.groupby("trade_date")}


def _first_day_after(days: dict[str, pd.DataFrame], signal_date: str) -> str | None:
    later = sorted(date for date in days if date > signal_date)
    return later[0] if later else None


def _calculate_open_snapshot(
    rows: pd.DataFrame,
    entry_price: float,
    *,
    cutoff: time,
) -> dict[str, float] | None:
    if rows.empty or entry_price <= 0:
        return None
    effective_cutoff = min(cutoff, time(9, 50))
    window = rows[rows["timestamp"].dt.time <= effective_cutoff].copy()
    if window.empty:
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
    if rows.empty or entry_price <= 0:
        return None
    window = rows[rows["timestamp"].dt.time <= time(10, 0)].copy()
    if window.empty or window["timestamp"].iloc[-1].time() < time(9, 59):
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


def _calculate_strict_windows(rows: pd.DataFrame, entry_price: float) -> dict[str, float] | None:
    """严格三次稳定的开盘、5–30分钟与31–60分钟三个固定时段。"""
    half_hour = _calculate_half_hour(rows, entry_price)
    if half_hour is None:
        return None
    first_5 = rows[rows["timestamp"].dt.time <= time(9, 35)].copy()
    first_hour = rows[rows["timestamp"].dt.time <= time(10, 30)].copy()
    if (
        first_5.empty
        or first_hour.empty
        or first_5["timestamp"].iloc[-1].time() < time(9, 35)
        or first_hour["timestamp"].iloc[-1].time() < time(10, 29)
    ):
        return None
    price_0935 = float(first_5.iloc[-1]["close"])
    price_1030 = float(first_hour.iloc[-1]["close"])
    price_1000 = float(half_hour["price_1000"])
    half_hour.update(
        {
            "price_0935": price_0935,
            "price_1030": price_1030,
            "return_0530": (price_1000 / price_0935 - 1) * 100,
            "return_3160": (price_1030 / price_1000 - 1) * 100,
        }
    )
    return half_hour


def _calculate_windows(
    rows: pd.DataFrame,
    entry_price: float,
    *,
    include_1030: bool,
) -> dict[str, float | None] | None:
    if rows.empty or entry_price <= 0:
        return None
    window_0945 = rows[rows["timestamp"].dt.time <= time(9, 45)].copy()
    if window_0945.empty or window_0945["timestamp"].iloc[-1].time() < time(9, 44):
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
    window_1030 = rows[rows["timestamp"].dt.time <= time(10, 30)].copy()
    if window_1030.empty or window_1030["timestamp"].iloc[-1].time() < time(10, 29):
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
    if rows is None or rows.empty:
        return empty_result
    window_0945 = rows[rows["timestamp"].dt.time <= time(9, 45)].copy()
    if window_0945.empty:
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
        window_1030 = rows[rows["timestamp"].dt.time <= time(10, 30)].copy()
        if not window_1030.empty and window_1030["timestamp"].iloc[-1].time() >= time(10, 29):
            result["index_1030_return"] = (
                float(window_1030.iloc[-1]["close"]) / base - 1
            ) * 100
    return result
