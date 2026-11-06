"""Read-only scan activity, including days that produced no stock signals."""
from __future__ import annotations

from contextlib import closing, nullcontext
from datetime import datetime
import sqlite3
from typing import Any

import pandas as pd


SLOTS = ("1430", "1445", "1452")
ACTIVITY_COLUMNS = [
    "trade_date", "status", "strict_1430", "strict_1445", "strict_1452",
    "slot_status_1430", "slot_status_1445", "slot_status_1452",
    "strict_final_count", "validated_count", "completed_at", "last_validation_at",
    "delayed_slots", "max_delay_minutes", "detail",
]


def build_activity_frame(store: Any, limit: int = 20) -> pd.DataFrame:
    """Accept a ValidationStore or an open SQLite connection; never mutate either.

    Counts are null when a strict snapshot is absent, and zero only when the
    snapshot exists and has no candidates.  A fast-flow success is deliberately
    insufficient to call the strict 14:52 scan complete.
    """
    manager = nullcontext(store) if isinstance(store, sqlite3.Connection) else closing(store.connect())
    with manager as db:
        days = pd.read_sql_query(
            """SELECT trade_date FROM (
                SELECT trade_date FROM strict_scans UNION SELECT trade_date FROM scan_runs
                UNION SELECT trade_date FROM review_scans
                UNION SELECT signal_date AS trade_date FROM strict_final_signals
            ) ORDER BY trade_date DESC LIMIT ?""", db, params=(max(0, int(limit)),),
        )["trade_date"].tolist()
        if not days:
            return pd.DataFrame(columns=ACTIVITY_COLUMNS)
        lower = days[-1]
        snapshots = pd.read_sql_query(
            """SELECT s.trade_date, s.slot, s.scanned_at, COUNT(c.id) AS count
            FROM strict_scans s LEFT JOIN strict_candidates c ON c.strict_scan_id=s.id
            WHERE s.trade_date>=? GROUP BY s.id""", db, params=(lower,),
        )
        runs = pd.read_sql_query("SELECT * FROM scan_runs WHERE trade_date>=?", db, params=(lower,))
        reviews = pd.read_sql_query("SELECT * FROM review_scans WHERE trade_date>=?", db, params=(lower,))
        finals = pd.read_sql_query(
            """SELECT s.signal_date AS trade_date, COUNT(s.id) AS final_count,
                COUNT(v.id) AS validated_count, MAX(v.calculated_at) AS validated_at
            FROM strict_final_signals s LEFT JOIN strict_validations v ON v.strict_signal_id=s.id
            WHERE s.signal_date>=? GROUP BY s.signal_date""", db, params=(lower,),
        )

    snapshot_map = {(r["trade_date"], r["slot"]): r for r in snapshots.to_dict("records")}
    run_map = {(r["trade_date"], r["slot"]): r for r in runs.to_dict("records")}
    review_map = {r["trade_date"]: r for r in reviews.to_dict("records")}
    final_map = {r["trade_date"]: r for r in finals.to_dict("records")}
    rows: list[dict[str, Any]] = []
    for day in days:
        row: dict[str, Any] = {"trade_date": day}
        finished: list[str] = []
        delayed: list[str] = []
        delays: list[float] = []
        issues: list[str] = []
        states: list[str] = []
        for slot in SLOTS:
            snapshot = snapshot_map.get((day, slot))
            run = run_map.get((day, slot), {})
            review = review_map.get(day, {}) if slot == "1452" else {}
            slot_issues = [str(value) for value in (run.get("error_message"), review.get("error_message"))
                           if value is not None and pd.notna(value) and str(value).strip()]
            if slot_issues:
                issues.append(f"{slot[:2]}:{slot[2:]} " + "；".join(dict.fromkeys(slot_issues)))
            if snapshot is not None:
                state = "data_incomplete" if slot_issues else "success"
                row[f"strict_{slot}"] = int(snapshot["count"])
                completed = snapshot["scanned_at"]
                finished.append(completed)
                scheduled = datetime.fromisoformat(f"{day}T{slot[:2]}:{slot[2:]}:00")
                delay = (datetime.fromisoformat(completed) - scheduled).total_seconds() / 60
                delays.append(max(0.0, delay))
                if delay > 3:
                    delayed.append(f"{slot[:2]}:{slot[2:]}")
            else:
                row[f"strict_{slot}"] = None
                state = str(review.get("status") or run.get("status") or "missing")
                if state == "success":
                    # A completed fast scan, without a strict snapshot, is not
                    # proof that the strict full review has finished.
                    state = "incomplete"
            row[f"slot_status_{slot}"] = state
            states.append(state)
        if "failed" in states:
            status = "failed"
        elif any(s in {"running", "retrying"} for s in states):
            status = "running"
        elif "data_incomplete" in states:
            status = "data_incomplete"
        elif all(s == "success" for s in states):
            status = "delayed" if delayed else "complete"
        elif all(s == "missing" for s in states):
            status = "missing"
        else:
            status = "partial"
        final = final_map.get(day, {})
        row.update(
            status=status,
            strict_final_count=int(final.get("final_count", 0)),
            validated_count=int(final.get("validated_count", 0)),
            completed_at=max(finished) if finished else None,
            last_validation_at=final.get("validated_at"),
            delayed_slots="、".join(delayed),
            max_delay_minutes=round(max(delays), 1) if delays else None,
            detail="；".join(issues),
        )
        rows.append(row)
    frame = pd.DataFrame(rows, columns=ACTIVITY_COLUMNS)
    for slot in SLOTS:
        frame[f"strict_{slot}"] = frame[f"strict_{slot}"].astype("Int64")
    return frame
