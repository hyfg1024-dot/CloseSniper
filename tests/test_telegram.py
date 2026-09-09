import stat
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import pandas as pd

from src.telegram_service import (
    TelegramSettings,
    format_final_message,
    format_fast_decision_message,
    format_review_message,
    format_scan_issue_message,
    format_strict_watch_message,
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
            "code": "600002", "name": "改进股", "composite_score": 86.5,
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
        self.assertIn("改进流程｜三时点加权", message)
        self.assertIn("严格股（600001）", message)
        self.assertIn("改进股（600002）", message)
        self.assertIn("综合86.5", message)
        self.assertIn("三次稳定", message)

    def test_scan_issue_message_explains_missing_result(self) -> None:
        message = format_scan_issue_message(
            trade_date="2026-08-21",
            failed_slots=["1430", "1445"],
            reason="免费行情读取失败",
            generated_at=datetime(2026, 8, 21, 14, 52),
        )

        self.assertIn("未形成最终结果", message)
        self.assertIn("14:30、14:45", message)
        self.assertIn("请勿把“无结果”理解为“扫描成功且无候选”", message)

    def test_dual_messages_are_clearly_labeled(self) -> None:
        fast = format_fast_decision_message(
            trade_date="2026-08-31", candidates=[],
            generated_at=datetime(2026, 8, 31, 14, 52, 30),
        )
        review = format_review_message(
            trade_date="2026-08-31", strict_candidates=[], improved_candidates=[],
            generated_at=datetime(2026, 8, 31, 14, 57),
        )

        self.assertIn("14:52冻结决策版", fast)
        self.assertIn("完整重扫版将在后台完成后另行发送", fast)
        self.assertIn("完整重扫复核版", review)
        self.assertIn("完整重扫｜严格标准", review)

    def test_strict_watch_message_is_explicitly_not_final(self) -> None:
        message = format_strict_watch_message(
            trade_date="2026-09-09",
            candidates=[{
                "code": "600001", "name": "观察股", "entry_price": 12.3,
                "watch_score": 82.2, "score_1430": 80, "score_1445": 83,
            }],
        )

        self.assertIn("连续两次", message)
        self.assertIn("不是最终买入名单", message)
        self.assertIn("观察股（600001）", message)

    def test_strict_watch_message_can_include_ai_observation(self) -> None:
        message = format_strict_watch_message(
            trade_date="2026-09-09",
            candidates=[{"code": "600001", "name": "观察股", "watch_score": 80}],
            analyses={"600001": {
                "verdict": "继续观察", "strengths": ["量比 1.8"],
                "risks": ["接近日内高点"], "confirm_before_1452": ["不跌破均价线"],
            }},
        )
        self.assertIn("AI观察：继续观察", message)
        self.assertIn("14:52确认：不跌破均价线", message)


if __name__ == "__main__":
    unittest.main()
