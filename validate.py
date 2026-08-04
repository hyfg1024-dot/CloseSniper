#!/usr/bin/env python3
from __future__ import annotations

import json

from src.data_source import AkshareSource
from src.validation_service import validate_pending
from src.validation_store import ValidationStore


def main() -> None:
    summary = validate_pending(ValidationStore(), AkshareSource())
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()

