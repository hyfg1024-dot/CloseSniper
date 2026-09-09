#!/usr/bin/env python3
from __future__ import annotations

import json

from src.data_source import AkshareSource
from src.validation_service import validate_pending, validate_strict_three_stage_pending
from src.validation_store import ValidationStore


def main() -> None:
    store = ValidationStore()
    store.rebuild_all_strict_final_signals()
    source = AkshareSource()
    summary = {
        "improved": validate_pending(store, source),
        "strict_three_stage": validate_strict_three_stage_pending(store, source),
    }
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
