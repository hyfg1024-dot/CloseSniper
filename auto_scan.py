#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from src.data_source import AkshareSource, MarketDataError
from src.ai_observer import analyze_watchlist
from src.scan_service import (
    ScanResult,
    derive_strict_frame,
    run_frozen_candidate_scan,
    run_market_scan,
)
from src.strategy import StrategyConfig
from src.telegram_service import (
    TelegramError,
    format_review_message,
    format_scan_issue_message,
    format_strict_watch_message,
    load_settings,
    send_message,
)
from src.validation_store import ValidationStore
from src.deadline_worker import run_with_deadline
from src.strict_schedule import cache_scan, decision_record, run_strict_final
from src.trading_calendar import CalendarUnavailable, is_trading_day


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
    deadline_at: datetime | None = None,
    fast_minutes: bool = False,
    analyze_all: bool = False,
    require_complete: bool = False,
) -> tuple[ScanResult, datetime, int]:
    errors: list[str] = []
    attempts = 0
    attempted_at = now_fn()
    for attempt, delay in enumerate(retry_delays, 1):
        if deadline_at is not None and (deadline_at - now_fn()).total_seconds() <= delay:
            errors.append("已到采样截止时间，未开始迟到重试")
            break
        if delay:
            sleep_fn(delay)
        attempted_at = now_fn()
        attempts = attempt
        if record_status:
            store.record_scan_run(
                slot=slot,
                attempted_at=attempted_at,
                status="running",
                attempt_count=attempt,
            )
        try:
            scan_kwargs = dict(mode="rational", now=attempted_at,
                               restrict_codes=restrict_codes, prefer_tencent=prefer_tencent,
                               cutoff_at=cutoff_at, fast_minutes=fast_minutes,
                               analyze_all=analyze_all)
            if deadline_at is None:
                result = run_market_scan(cfg, **scan_kwargs)
            else:
                remaining = (deadline_at - now_fn()).total_seconds()
                if remaining <= 0:
                    raise MarketDataError("初始化或状态保存已耗尽采样预算")
                result = run_with_deadline(
                    run_market_scan, args=(cfg,), kwargs=scan_kwargs,
                    timeout_seconds=remaining,
                )
            completed_at = now_fn()
            if deadline_at is not None and completed_at >= deadline_at:
                raise MarketDataError("采样计算超过截止时间，未接受迟到结果")
            if require_complete and getattr(result, "errors", {}):
                raise MarketDataError("部分个股数据不完整：" + "; ".join(
                    f"{code}: {error}" for code, error in result.errors.items()
                ))
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
                error_message=(
                    "部分个股数据不完整：" + "; ".join(f"{code}: {error}" for code, error in result.errors.items())
                    if getattr(result, "errors", {}) else None
                ),
            )
        return result, completed_at, attempt
    last_error = errors[-1] if errors else "未配置有效扫描尝试"
    if record_status:
        store.record_scan_run(slot=slot, attempted_at=attempted_at, status="failed", attempt_count=attempts,
                              completed_at=now_fn(), error_message=last_error)
    raise MarketDataError(f"已自动尝试{attempts}次仍失败：{last_error}")


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
        run_with_deadline(send_message, args=(settings, message),
                          kwargs={"attempts": 1, "timeout": 3}, timeout_seconds=5)
        # The timestamp is API acknowledgement, not the earlier scan timestamp
        # and not a claim that the user has already received/read the message.
        store.mark_notification_sent(trade_date, channel, datetime.now())
        return "sent"
    except Exception:
        return "failed"


def run_regular_slot(
    *,
    slot: str,
    cfg: StrategyConfig,
    store: ValidationStore,
    now_fn: Callable[[], datetime] = datetime.now,
) -> dict:
    started = now_fn()
    planned = started.replace(hour=14, minute=int(slot[2:]), second=0, microsecond=0)
    deadline = planned + timedelta(seconds=150)
    if started < planned:
        return {"status": "skipped", "reason": "尚未到采样时间，未修改当天记录"}
    if decision_record(store, started.date().isoformat()):
        return {"status": "skipped", "reason": "当天决策已冻结，未修改采样记录"}
    if started >= deadline:
        store.record_scan_run(slot=slot, attempted_at=started, status="failed", attempt_count=0,
                              completed_at=started, error_message="错过采样窗口，未用迟到行情补写原始快照")
        raise MarketDataError("错过采样窗口，未用迟到行情补写原始快照")
    result, scan_now, attempt_count = run_scan_with_retries(
        cfg=cfg, slot=slot, store=store, retry_delays=(0, 3), deadline_at=deadline, fast_minutes=True,
        now_fn=now_fn, analyze_all=True, require_complete=True,
    )
    try:
        if scan_now >= deadline or now_fn() >= deadline:
            raise MarketDataError("采样计算超时，未写入迟到快照")
        candidates = result.candidates.to_dict("records")
        strict_frame = derive_strict_frame(result.result_frame)
        strict_candidates = strict_frame[strict_frame["passed"] == True].to_dict("records")  # noqa: E712
        # Cache/evidence must be durable before snapshots can qualify for the
        # official final. A failed or late write invalidates this slot below.
        cache_scan(store, slot, result, cfg, scan_now)
        if now_fn() >= deadline:
            raise MarketDataError("预备数据保存超时，未写入迟到快照")
        store.save_strict_scan(
            slot=slot, scanned_at=scan_now, provider=result.provider,
            candidates=strict_candidates,
        )
        store.save_staged_scan(
            slot=slot, scanned_at=scan_now, provider=result.provider,
            market_count=result.funnel["全市场"], hard_count=result.hard_count,
            config=cfg.as_dict(), candidates=candidates,
        )
        if now_fn() >= deadline:
            raise MarketDataError("快照保存超时，本节点不能用于准时终判")
    except Exception as exc:
        store.record_scan_run(slot=slot, attempted_at=started, status="failed", attempt_count=attempt_count,
                              completed_at=now_fn(), error_message=str(exc)[:500])
        raise MarketDataError(str(exc)) from exc
    telegram = "not_due"
    if slot == "1445":
        watchlist = store.strict_two_stage_frame(scan_now.date().isoformat())
        remaining = (deadline - now_fn()).total_seconds() - 8
        analyses = {}
        if remaining > 0 and not watchlist.empty:
            try:
                analyses = run_with_deadline(
                    analyze_watchlist, args=(watchlist, strict_frame.to_dict("records")),
                    timeout_seconds=min(15, remaining),
                )
            except Exception:
                pass  # Optional AI never invalidates a complete strict sample.
        store.save_ai_observations(scan_now.date().isoformat(), watchlist, analyses)
        telegram = send_once(
            store=store,
            trade_date=scan_now.date().isoformat(),
            channel="telegram-strict-watch-1445",
            message=format_strict_watch_message(
                trade_date=scan_now.date().isoformat(),
                candidates=watchlist,
                analyses=analyses,
                generated_at=scan_now,
            ),
            sent_at=scan_now,
        ) if (deadline - now_fn()).total_seconds() >= 8 else "skipped_time_budget"
    return {
        "status": "ok", "slot": slot, "rational": len(candidates),
        "strict": len(strict_candidates), "attempts": attempt_count,
        "telegram": telegram,
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
        # The improved flow is a risk/reference screen only.  It must not be
        # delivered as a 14:52 recommendation, including when it is empty.
        telegram = "suppressed_risk_only"
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
            retry_delays=(0, 5), deadline_at=started_at + timedelta(minutes=5),
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
        error_message=(
            "部分个股数据不完整：" + "; ".join(f"{code}: {error}" for code, error in result.errors.items())
            if result.errors else None
        ),
    )
    return {
        "status": "ok", "strict": len(strict),
        "improved": len(improved),
        "attempts": attempts, "telegram": "archive_only",
        "completed_at": completed_at.isoformat(timespec="seconds"),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--slot", choices=("1430", "1445", "1452"))
    group.add_argument("--healthcheck", action="store_true")
    group.add_argument("--review", action="store_true")
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
    try:
        trading_day = is_trading_day(now.date())
    except CalendarUnavailable as exc:
        print(json.dumps({"status": "failed", "reason": str(exc)}, ensure_ascii=False))
        return
    if not trading_day:
        print(json.dumps({"status": "skipped", "reason": "交易所休市日"}, ensure_ascii=False))
        return
    if args.slot and now < now.replace(hour=14, minute=int(args.slot[2:]), second=0, microsecond=0):
        print(json.dumps({"status": "skipped", "reason": "尚未到采样时间，未修改当天记录"}, ensure_ascii=False))
        return

    # Independent locks: a slow 14:45 scan or archival review never queues 14:52.
    lock_path = Path(f"data/auto-scan-{args.slot or 'review'}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({"status": "skipped", "reason": "同节点任务已运行"}, ensure_ascii=False))
            return
        cfg = StrategyConfig()
        store = ValidationStore(connection_timeout=1)
        trade_date = now.date().isoformat()
        try:
            if args.review:
                payload = run_full_review(trade_date=trade_date, cfg=cfg, store=store)
            elif args.slot != "1452":
                payload = run_regular_slot(slot=args.slot, cfg=cfg, store=store, now_fn=datetime.now)
            else:
                payload = run_strict_final(store=store, cfg=cfg, now_fn=datetime.now)
        except MarketDataError as exc:
            payload = {"status": "failed", "slot": args.slot, "reason": str(exc)}
            print(json.dumps(payload, ensure_ascii=False))
            raise SystemExit(1) from exc
        print(json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    main()
