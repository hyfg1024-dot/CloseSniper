#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import json
from datetime import datetime
from pathlib import Path

from src.data_source import AkshareSource
from src.scan_service import derive_strict_frame, run_market_scan
from src.strategy import StrategyConfig
from src.validation_store import ValidationStore


def main() -> None:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--slot", choices=("1430", "1445", "1452"))
    group.add_argument("--healthcheck", action="store_true")
    args = parser.parse_args()
    if args.healthcheck:
        store = ValidationStore()
        with store.connect() as db:
            db.execute("SELECT 1").fetchone()
        spot = AkshareSource().spot()
        print(json.dumps({
            "status": "ok", "database": str(store.path), "market_rows": len(spot),
        }, ensure_ascii=False))
        return
    now = datetime.now()
    if now.weekday() >= 5:
        print(json.dumps({"status": "skipped", "reason": "weekend"}, ensure_ascii=False))
        return

    lock_path = Path("data/auto-scan.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        cfg = StrategyConfig()
        result = run_market_scan(cfg, mode="rational", now=now)
        candidates = result.candidates.to_dict("records")
        strict_frame = derive_strict_frame(result.result_frame)
        strict_candidates = strict_frame[strict_frame["passed"] == True].to_dict("records")  # noqa: E712
        store = ValidationStore()
        store.save_strict_scan(
            slot=args.slot,
            scanned_at=now,
            provider=result.provider,
            candidates=strict_candidates,
        )
        store.save_staged_scan(
            slot=args.slot,
            scanned_at=now,
            provider=result.provider,
            market_count=result.funnel["全市场"],
            hard_count=result.hard_count,
            config=cfg.as_dict(),
            candidates=candidates,
        )
        finalized = store.finalize_staged_day(now.date().isoformat()) if args.slot == "1452" else False
        print(json.dumps({
            "status": "ok", "slot": args.slot, "rational": len(candidates),
            "strict": len(strict_candidates), "finalized": finalized,
        }, ensure_ascii=False))


if __name__ == "__main__":
    main()
