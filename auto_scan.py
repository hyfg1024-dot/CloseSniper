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
from src.scan_service import (
    ScanResult,
    derive_strict_frame,
    run_frozen_candidate_scan,
    run_market_scan,
)
from src.strategy import StrategyConfig
from src.telegram_service import (
    TelegramError,
    format_fast_decision_message,
    format_review_message,
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
    retry_delays: tuple[int, ...] = RETRY_DELAYS_SECONDS,
    restrict_codes: set[str] | None = None,
    prefer_tencent: bool = False,
    record_status: bool = True,
    cutoff_at: datetime | None = None,
) -> tuple[ScanResult, datetime, int]:
    errors: list[str] = []
    for attempt, delay in enumerate(retry_delays, 1):
        if delay:
            sleep_fn(delay)
        attempted_at = now_fn()
        if record_status:
            store.record_scan_run(
                slot=slot,
                attempted_at=attempted_at,
                status="running",
                attempt_count=attempt,
            )
        try:
            result = run_market_scan(
                cfg, mode="rational", now=attempted_at,
                restrict_codes=restrict_codes, prefer_tencent=prefer_tencent,
                cutoff_at=cutoff_at,
            )
        except Exception as exc:
            message = str(exc)
            errors.append(message)
            if record_status:
                store.record_scan_run(
                    slot=slot,
                    attempted_at=attempted_at,
                    status="retrying" if attempt < len(retry_delays) else "failed",
                    attempt_count=attempt,
                    completed_at=now_fn() if attempt == len(retry_delays) else None,
                    error_message=message,
                )
            continue
        completed_at = now_fn()
        if record_status:
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
    raise MarketDataError(f"已自动尝试{len(retry_delays)}次仍失败：{last_error}")


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


def send_once(
    *,
    store: ValidationStore,
    trade_date: str,
    channel: str,
    message: str,
    sent_at: datetime,
) -> str:
    settings = load_settings()
    if not settings.enabled or not settings.configured:
        return "disabled"
    if store.notification_sent(trade_date, channel):
        return "already_sent"
    try:
        send_message(settings, message)
        store.mark_notification_sent(trade_date, channel, sent_at)
        return "sent"
    except TelegramError:
        return "failed"


def run_regular_slot(
    *,
    slot: str,
    cfg: StrategyConfig,
    store: ValidationStore,
) -> dict:
    result, scan_now, attempt_count = run_scan_with_retries(
        cfg=cfg, slot=slot, store=store,
    )
    candidates = result.candidates.to_dict("records")
    strict_frame = derive_strict_frame(result.result_frame)
    strict_candidates = strict_frame[strict_frame["passed"] == True].to_dict("records")  # noqa: E712
    store.save_strict_scan(
        slot=slot, scanned_at=scan_now, provider=result.provider,
        candidates=strict_candidates,
    )
    store.save_staged_scan(
        slot=slot, scanned_at=scan_now, provider=result.provider,
        market_count=result.funnel["全市场"], hard_count=result.hard_count,
        config=cfg.as_dict(), candidates=candidates,
    )
    return {
        "status": "ok", "slot": slot, "rational": len(candidates),
        "strict": len(strict_candidates), "attempts": attempt_count,
        "telegram": "not_due",
    }


def run_fast_final(
    *,
    trade_date: str,
    cfg: StrategyConfig,
    store: ValidationStore,
    now_fn: Callable[[], datetime] = datetime.now,
) -> dict:
    if not store.staged_slot_exists(trade_date, "1445"):
        now = now_fn()
        telegram = notify_scan_issue(
            store=store, trade_date=trade_date, failed_slots=["1445"],
            reason="14:45没有有效快照，无法生成14:52冻结决策版", now=now,
        )
        return {"status": "incomplete", "telegram": telegram, "fast": 0}

    eligible_codes = set(store.staged_candidate_codes(trade_date, "1445"))
    if eligible_codes:
        prior = store.staged_frame(trade_date)
        prior = prior[(prior["slot"] == "1445") & prior["code"].notna()].copy()
        errors: list[str] = []
        result = None
        attempts = 0
        for attempts, delay in enumerate((0, 3), 1):
            if delay:
                time.sleep(delay)
            attempted_at = now_fn()
            store.record_scan_run(
                slot="1452", attempted_at=attempted_at, status="running",
                attempt_count=attempts,
            )
            try:
                result = run_frozen_candidate_scan(cfg, prior, now=attempted_at)
                break
            except Exception as exc:
                errors.append(str(exc))
                store.record_scan_run(
                    slot="1452", attempted_at=attempted_at,
                    status="retrying" if attempts == 1 else "failed",
                    attempt_count=attempts,
                    completed_at=now_fn() if attempts == 2 else None,
                    error_message=str(exc),
                )
        if result is None:
            raise MarketDataError(f"冻结报价已尝试2次仍失败：{errors[-1]}")
        scan_now = now_fn()
        store.record_scan_run(
            slot="1452", attempted_at=attempted_at, status="success",
            attempt_count=attempts, completed_at=scan_now, provider=result.provider,
            market_count=result.funnel.get("全市场"), candidate_count=len(result.candidates),
        )
        candidates = result.candidates.to_dict("records")
        provider = result.provider
        market_count = result.funnel["全市场"]
        hard_count = result.hard_count
    else:
        scan_now = now_fn()
        attempts = 0
        candidates = []
        provider = "14:45候选池为空"
        market_count = 0
        hard_count = 0
        store.record_scan_run(
            slot="1452", attempted_at=scan_now, status="success", attempt_count=0,
            completed_at=scan_now, provider=provider, market_count=0, candidate_count=0,
        )

    store.save_staged_scan(
        slot="1452", scanned_at=scan_now, provider=provider,
        market_count=market_count, hard_count=hard_count,
        config=cfg.as_dict(), candidates=candidates,
    )
    finalized = store.finalize_staged_day(trade_date)
    completed_slots = set(store.staged_frame(trade_date)["slot"].astype(str))
    if completed_slots != {"1430", "1445", "1452"}:
        missing = [slot for slot in ("1430", "1445", "1452") if slot not in completed_slots]
        telegram = notify_scan_issue(
            store=store, trade_date=trade_date, failed_slots=missing,
            reason="缺少三时点快照，无法生成冻结决策版", now=scan_now,
        )
    else:
        final = store.final_frame(trade_date)
        telegram = send_once(
            store=store, trade_date=trade_date, channel="telegram-fast",
            message=format_fast_decision_message(
                trade_date=trade_date, candidates=final, generated_at=scan_now,
            ),
            sent_at=scan_now,
        )
    return {
        "status": "ok", "fast": len(candidates), "eligible": len(eligible_codes),
        "finalized": finalized, "attempts": attempts, "telegram": telegram,
        "completed_at": scan_now.isoformat(timespec="seconds"),
    }


def run_full_review(
    *,
    trade_date: str,
    cfg: StrategyConfig,
    store: ValidationStore,
) -> dict:
    started_at = datetime.now()
    store.record_review_run(
        trade_date=trade_date, status="running", started_at=started_at,
    )
    try:
        result, completed_at, attempts = run_scan_with_retries(
            cfg=cfg, slot="1452", store=store, record_status=False,
        )
    except MarketDataError as exc:
        store.record_review_run(
            trade_date=trade_date, status="failed", started_at=started_at,
            completed_at=datetime.now(), error_message=str(exc),
        )
        return {"status": "failed", "reason": str(exc), "telegram": "not_sent"}

    improved = result.candidates.to_dict("records")
    strict_frame = derive_strict_frame(result.result_frame)
    strict = strict_frame[strict_frame["passed"] == True].to_dict("records")  # noqa: E712
    store.save_review_results(
        trade_date=trade_date, started_at=started_at, completed_at=completed_at,
        provider=result.provider, market_count=result.funnel["全市场"],
        hard_count=result.hard_count, strict_candidates=strict,
        improved_candidates=improved,
    )
    store.save_strict_scan(
        slot="1452", scanned_at=completed_at, provider=result.provider,
        candidates=strict,
    )
    strict_three_stage = store.rebuild_strict_final_signals(trade_date)
    telegram = send_once(
        store=store, trade_date=trade_date, channel="telegram-review",
        message=format_review_message(
            trade_date=trade_date,
            strict_candidates=store.review_frame(trade_date, "strict"),
            improved_candidates=store.review_frame(trade_date, "improved"),
            generated_at=completed_at,
        ),
        sent_at=completed_at,
    )
    return {
        "status": "ok", "strict": len(strict), "strict_three_stage": strict_three_stage,
        "improved": len(improved),
        "attempts": attempts, "telegram": telegram,
        "completed_at": completed_at.isoformat(timespec="seconds"),
    }


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
            if args.slot != "1452":
                payload = run_regular_slot(slot=args.slot, cfg=cfg, store=store)
            else:
                try:
                    fast_payload = run_fast_final(
                        trade_date=trade_date, cfg=cfg, store=store,
                    )
                except MarketDataError as exc:
                    failed_at = datetime.now()
                    telegram = notify_scan_issue(
                        store=store, trade_date=trade_date, failed_slots=["1452"],
                        reason=f"冻结决策版失败：{exc}", now=failed_at,
                    )
                    fast_payload = {
                        "status": "failed", "reason": str(exc), "telegram": telegram,
                    }
                payload = {
                    "status": "ok", "slot": "1452",
                    "fast_decision": fast_payload,
                    "full_review": run_full_review(
                        trade_date=trade_date, cfg=cfg, store=store,
                    ),
                }
        except MarketDataError as exc:
            payload = {"status": "failed", "slot": args.slot, "reason": str(exc)}
            print(json.dumps(payload, ensure_ascii=False))
            raise SystemExit(1) from exc
        print(json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    main()
