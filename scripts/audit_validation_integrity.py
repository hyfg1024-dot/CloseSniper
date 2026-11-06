"""Quarantine questionable legacy summaries without deleting or inventing prices.

Default is read-only. Run --apply after backing up the database. New verified
minute evidence may later repair a quarantined record through normal backfill.
"""
from __future__ import annotations

import argparse
from datetime import date
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.trading_calendar import CalendarUnavailable, next_trading_day
from src.validation_store import DEFAULT_DB_PATH, ValidationStore


def audit(path: Path) -> list[dict]:
    issues = []
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        for kind, query in (
            ("strict", """SELECT s.id, s.signal_date, s.code, v.validation_date,
                v.price_0935, v.price_1030 FROM strict_final_signals s
                JOIN strict_validations v ON v.strict_signal_id=s.id"""),
            ("improved", """SELECT s.id, sc.trade_date AS signal_date, s.code, v.validation_date,
                v.price_1030 FROM signals s JOIN scans sc ON sc.id=s.scan_id
                JOIN validations v ON v.signal_id=s.id"""),
        ):
            for row in db.execute(query):
                try:
                    expected = next_trading_day(row["signal_date"]).isoformat()
                except CalendarUnavailable as exc:
                    issues.append(dict(kind=kind, signal_id=row["id"], code=row["code"],
                                       status="unverified", reason=str(exc)))
                    continue
                if row["validation_date"] != expected:
                    issues.append(dict(kind=kind, signal_id=row["id"], code=row["code"], status="invalid",
                                       reason=f"原校验日{row['validation_date']}，应为下一交易日{expected}"))
                elif (kind == "strict" and expected < date.today().isoformat()
                      and (row["price_0935"] is None or row["price_1030"] is None)):
                    issues.append(dict(kind=kind, signal_id=row["id"], code=row["code"], status="unverified",
                                       reason="旧严格校验缺少完整分钟时点依据；保留原数值，待取得原始行情重验后恢复统计"))
    return issues


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    issues = audit(args.db)
    if args.apply:
        store = ValidationStore(args.db)
        for issue in issues:
            store.mark_validation_quality(issue["kind"], issue["signal_id"], issue["status"], issue["reason"])
    print(json.dumps({"applied": args.apply, "issues": issues}, ensure_ascii=False))


if __name__ == "__main__":
    main()
