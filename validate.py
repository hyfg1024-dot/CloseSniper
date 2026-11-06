#!/usr/bin/env python3
from __future__ import annotations

import json
from datetime import datetime

from src.validation_source import BoundedValidationSource
from src.validation_service import (
    capture_strict_exit_observation,
    validate_pending,
    validate_strict_three_stage_pending,
)
from src.telegram_service import format_strict_exit_observation_message, load_settings, send_message
from src.validation_store import ValidationStore
from src.trading_calendar import is_trading_day


def send_pending_observations(store, now: datetime, *, settings=None, send_fn=None) -> dict:
    """Retry saved observations independently; never recalculate a frozen price."""
    report = {"sent": [], "already_sent": [], "errors": {}}
    trade_date = now.date().isoformat()
    frozen = store.strict_exit_observation_frame(trade_date)
    if frozen.empty:
        return report
    settings = settings if settings is not None else load_settings()
    if not settings.configured or not settings.enabled:
        report["disabled"] = True
        return report
    send_fn = send_fn or send_message
    stats = store.strict_exit_checkpoint_stats()
    for observation in frozen.to_dict("records"):
        code = str(observation["code"])
        channel = f"telegram-strict-exit-1000:{code}"
        if store.notification_sent(trade_date, channel):
            report["already_sent"].append(code)
            continue
        try:
            send_fn(
                settings, format_strict_exit_observation_message(trade_date, [observation], stats),
                attempts=1, timeout=10,
            )
            store.mark_notification_sent(trade_date, channel, datetime.now())
            report["sent"].append(code)
        except Exception as exc:
            # Keep retry state on disk; do not put a URL/token from an unknown
            # HTTP exception in logs. The captured quote remains unchanged.
            report["errors"][code] = f"{type(exc).__name__}：发送失败，下一轮重试已保存的观察结果"
    return report


def main() -> None:
    now = datetime.now()
    if not is_trading_day(now.date()):
        print(json.dumps({"status": "closed", "date": now.date().isoformat()}))
        return
    store = ValidationStore()
    store.rebuild_all_strict_final_signals()
    source = BoundedValidationSource()
    diagnostics = {}
    observations = capture_strict_exit_observation(store, source, now, diagnostics=diagnostics)
    delivery = send_pending_observations(store, now)
    summary = {
        "improved": validate_pending(store, source, include_history=False),
        "strict_three_stage": validate_strict_three_stage_pending(store, source, include_history=False),
        "strict_exit_observations": len(observations),
        "observation_diagnostics": diagnostics,
        "observation_notifications": delivery,
    }
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
