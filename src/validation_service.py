from __future__ import annotations

from datetime import datetime, time
from typing import Any

import pandas as pd

from src.data_source import AkshareSource
from src.validation_store import ValidationStore


def validate_pending(
    store: ValidationStore,
    source: AkshareSource | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now()
    source = source or AkshareSource()
    pending = store.pending_signals(now.date().isoformat())
    summary: dict[str, Any] = {"pending": len(pending), "completed": 0, "skipped": 0, "errors": {}}
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
            result = _calculate_window(rows, float(signal["entry_price"]))
            if result is None:
                summary["skipped"] += 1
                continue
            index_result = _calculate_index_window(index_days.get(validation_date))
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
        except Exception as exc:
            summary["errors"][code] = str(exc)
    return summary


def _standardize_minutes(raw: pd.DataFrame | None) -> pd.DataFrame:
    if raw is None or raw.empty:
        return pd.DataFrame()
    if {"day", "open", "high", "low", "close"}.issubset(raw.columns):
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


def _calculate_window(rows: pd.DataFrame, entry_price: float) -> dict[str, float] | None:
    if rows.empty or entry_price <= 0:
        return None
    window = rows[rows["timestamp"].dt.time <= time(9, 45)].copy()
    if window.empty or window["timestamp"].iloc[-1].time() < time(9, 44):
        return None
    open_price = float(window.iloc[0]["open"])
    price_0945 = float(window.iloc[-1]["close"])
    high_0945 = float(window["high"].max())
    low_0945 = float(window["low"].min())
    pct = lambda price: (price / entry_price - 1) * 100
    return {
        "open_price": open_price,
        "price_0945": price_0945,
        "high_0945": high_0945,
        "low_0945": low_0945,
        "open_return": pct(open_price),
        "return_0945": pct(price_0945),
        "max_return": pct(high_0945),
        "max_drawdown": pct(low_0945),
    }


def _calculate_index_window(rows: pd.DataFrame | None) -> dict[str, float | None]:
    if rows is None or rows.empty:
        return {"index_open_return": None, "index_0945_return": None}
    window = rows[rows["timestamp"].dt.time <= time(9, 45)].copy()
    if window.empty:
        return {"index_open_return": None, "index_0945_return": None}
    base = float(window.iloc[0]["open"])
    if base <= 0:
        return {"index_open_return": None, "index_0945_return": None}
    return {
        "index_open_return": 0.0,
        "index_0945_return": (float(window.iloc[-1]["close"]) / base - 1) * 100,
    }

