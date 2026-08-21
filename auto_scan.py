#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

from src.data_source import AkshareSource, MarketDataError
from src.scan_service import ScanResult, derive_strict_frame, run_market_scan
from src.strategy import StrategyConfig
from src.telegram_service import (
    TelegramError,
    format_final_message,
    format_scan_issue_message,
    load_settings,
    send_message,
)
from src.validation_store import ValidationStore


RETRY_DELAYS_SECONDS = (0, 20, 40, 60)


def run_scan_with_retries(
    *,
    cfg: StrategyConfig,
    slot: str,
    store: ValidationStore,
    sleep_fn: Callable[[float], None] = time.sleep,
    now_fn: Callable[[], datetime] = datetime.now,
) -> tuple[ScanResult, datetime, int]:
    errors: list[str] = []
    for attempt, delay in enumerate(RETRY_DELAYS_SECONDS, 1):
        if delay:
            sleep_fn(delay)
        attempted_at = now_fn()
        store.record_scan_run(
            slot=slot,
            attempted_at=attempted_at,
            status="running",
            attempt_count=attempt,
        )
        try:
            result = run_market_scan(cfg, mode="rational", now=attempted_at)
        except Exception as exc:
            message = str(exc)
            errors.append(message)
            store.record_scan_run(
                slot=slot,
                attempted_at=attempted_at,
                status="retrying" if attempt < len(RETRY_DELAYS_SECONDS) else "failed",
                attempt_count=attempt,
                completed_at=now_fn() if attempt == len(RETRY_DELAYS_SECONDS) else None,
                error_message=message,
            )
            continue
        completed_at = now_fn()
        store.record_scan_run(
            slot=slot,
            attempted_at=attempted_at,
            status="success",
            attempt_count=attempt,
            completed_at=completed_at,
            provider=result.provider,
            market_count=result.funnel.get("全市场"),
            candidate_count=len(result.candidates),
        )
        return result, completed_at, attempt
    last_error = errors[-1] if errors else "未知行情错误"
    raise MarketDataError(f"已自动尝试{len(RETRY_DELAYS_SECONDS)}次仍失败：{last_error}")


def notify_scan_issue(
    *,
    store: ValidationStore,
    trade_date: str,
    failed_slots: list[str],
    reason: str,
    now: datetime,
) -> str:
    settings = load_settings()
    channel = "telegram-scan-issue"
    if not settings.enabled or not settings.configured:
        return "disabled"
    if store.notification_sent(trade_date, channel):
        return "already_sent"
    try:
        send_message(settings, format_scan_issue_message(
            trade_date=trade_date,
            failed_slots=failed_slots,
            reason=reason,
            generated_at=now,
        ))
        store.mark_notification_sent(trade_date, channel, now)
        return "issue_sent"
    except TelegramError:
        return "issue_failed"


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
        store = ValidationStore()
        trade_date = now.date().isoformat()
        try:
            result, scan_now, attempt_count = run_scan_with_retries(
                cfg=cfg,
                slot=args.slot,
                store=store,
            )
        except MarketDataError as exc:
            telegram_status = "not_due"
            if args.slot == "1452":
                statuses = store.scan_status_frame(trade_date)
                failed = statuses.loc[statuses["status"] == "failed", "slot"].astype(str).tolist()
                telegram_status = notify_scan_issue(
                    store=store,
                    trade_date=trade_date,
                    failed_slots=failed or [args.slot],
                    reason=str(exc),
                    now=datetime.now(),
                )
            print(json.dumps({
                "status": "failed", "slot": args.slot,
                "attempts": len(RETRY_DELAYS_SECONDS),
                "telegram": telegram_status, "reason": str(exc),
            }, ensure_ascii=False))
            raise SystemExit(1) from exc
        candidates = result.candidates.to_dict("records")
        strict_frame = derive_strict_frame(result.result_frame)
        strict_candidates = strict_frame[strict_frame["passed"] == True].to_dict("records")  # noqa: E712
        store.save_strict_scan(
            slot=args.slot,
            scanned_at=scan_now,
            provider=result.provider,
            candidates=strict_candidates,
        )
        store.save_staged_scan(
            slot=args.slot,
            scanned_at=scan_now,
            provider=result.provider,
            market_count=result.funnel["全市场"],
            hard_count=result.hard_count,
            config=cfg.as_dict(),
            candidates=candidates,
        )
        finalized = store.finalize_staged_day(trade_date) if args.slot == "1452" else False
        telegram_status = "not_due"
        if args.slot == "1452":
            staged = store.staged_frame(trade_date)
            completed_slots = set(staged["slot"].astype(str)) if not staged.empty else set()
            settings = load_settings()
            if completed_slots != {"1430", "1445", "1452"}:
                missing = [slot for slot in ("1430", "1445", "1452") if slot not in completed_slots]
                telegram_status = notify_scan_issue(
                    store=store,
                    trade_date=trade_date,
                    failed_slots=missing,
                    reason="部分时点未取得有效行情快照，无法计算三时点稳定性",
                    now=scan_now,
                )
            elif not settings.enabled or not settings.configured:
                telegram_status = "disabled"
            elif store.notification_sent(trade_date, "telegram"):
                telegram_status = "already_sent"
            else:
                try:
                    message = format_final_message(
                        trade_date=trade_date,
                        strict_candidates=strict_candidates,
                        rational_candidates=store.final_frame(trade_date),
                        generated_at=scan_now,
                    )
                    send_message(settings, message)
                    store.mark_notification_sent(trade_date, "telegram", scan_now)
                    telegram_status = "sent"
                except TelegramError as exc:
                    print(json.dumps({
                        "status": "error", "slot": args.slot,
                        "telegram": "failed", "reason": str(exc),
                    }, ensure_ascii=False))
                    raise SystemExit(1) from exc
        print(json.dumps({
            "status": "ok", "slot": args.slot, "rational": len(candidates),
            "strict": len(strict_candidates), "finalized": finalized,
            "attempts": attempt_count, "telegram": telegram_status,
        }, ensure_ascii=False))


if __name__ == "__main__":
    main()
