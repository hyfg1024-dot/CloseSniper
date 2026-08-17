import stat
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import pandas as pd

from src.telegram_service import (
    TelegramSettings,
    format_final_message,
    load_settings,
    save_settings,
)


class TelegramTests(unittest.TestCase):
    def test_settings_are_saved_with_private_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "telegram.json"
            settings = TelegramSettings("token", "123456", "close_sniper_bot", True)

            save_settings(settings, path)

            self.assertEqual(load_settings(path), settings)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_final_message_contains_both_result_modes(self) -> None:
        strict = [{"code": "600001", "name": "严格股", "score": 88, "price": 10.5}]
        rational = pd.DataFrame([{
            "code": "600002", "name": "理性股", "composite_score": 86.5,
            "score_1430": 80, "score_1445": 85, "score_1452": 90,
            "persistence": "三次稳定",
        }])

        message = format_final_message(
            trade_date="2026-08-18",
            strict_candidates=strict,
            rational_candidates=rational,
            generated_at=datetime(2026, 8, 18, 14, 52),
        )

        self.assertIn("严格标准｜14:52", message)
        self.assertIn("理性流程｜三时点加权", message)
        self.assertIn("严格股（600001）", message)
        self.assertIn("理性股（600002）", message)
        self.assertIn("综合86.5", message)
        self.assertIn("三次稳定", message)


if __name__ == "__main__":
    unittest.main()
