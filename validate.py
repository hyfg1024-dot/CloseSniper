#!/usr/bin/env python3
from __future__ import annotations

import json
from datetime import datetime

from src.data_source import AkshareSource
from src.validation_service import (
    capture_strict_exit_observation,
    validate_pending,
    validate_strict_three_stage_pending,
)
from src.telegram_service import format_strict_exit_observation_message, load_settings, send_message
from src.validation_store import ValidationStore


def main() -> None:
    store = ValidationStore()
    store.rebuild_all_strict_final_signals()
    source = AkshareSource()
    now = datetime.now()
    observations = capture_strict_exit_observation(store, source, now)
    if observations:
        settings = load_settings()
        channel = "telegram-strict-exit-1000"
        if settings.configured and settings.enabled and not store.notification_sent(now.date().isoformat(), channel):
            try:
                send_message(
                    settings,
                    format_strict_exit_observation_message(
                        now.date().isoformat(), observations, store.strict_exit_checkpoint_stats(),
                    ),
                )
                store.mark_notification_sent(now.date().isoformat(), channel, now)
            except Exception:
                pass
    summary = {
        "improved": validate_pending(store, source),
        "strict_three_stage": validate_strict_three_stage_pending(store, source),
        "strict_exit_observations": len(observations),
    }
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
