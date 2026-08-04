import unittest
from datetime import datetime

from src.data_source import demo_daily, demo_index_minute, demo_minute, demo_spot
from src.strategy import (
    StrategyConfig,
    analyze_daily,
    analyze_minute,
    estimate_volume_ratio,
    hard_filter,
    minute_return_pct,
    normalize_spot,
)


class StrategyTests(unittest.TestCase):
    def test_demo_passes_hard_filter(self):
        spot = normalize_spot(demo_spot())
        result, funnel = hard_filter(spot, StrategyConfig())
        self.assertEqual(len(result), 8)
        self.assertEqual(funnel["全市场"], 8)

    def test_demo_daily_and_minute_confirm(self):
        daily = analyze_daily(demo_daily("600001"))
        market_return = minute_return_pct(demo_index_minute())
        minute = analyze_minute(demo_minute("600001"), market_return_pct=market_return)
        self.assertTrue(daily["ma_bull"])
        self.assertTrue(daily["volume_step"])
        self.assertTrue(minute["vwap_strong"])
        self.assertTrue(minute["relative_strong"])
        self.assertTrue(minute["pullback_ok"])

    def test_st_is_excluded(self):
        raw = demo_spot()
        raw.loc[0, "名称"] = "*ST测试"
        result, _ = hard_filter(normalize_spot(raw), StrategyConfig())
        self.assertNotIn("600001", result["code"].tolist())

    def test_estimated_volume_ratio_uses_elapsed_session(self):
        history = demo_daily("600001")
        history["成交量"] = 240_000
        ratio = estimate_volume_ratio(
            current_volume=150_000,
            raw_history=history,
            now=datetime(2026, 8, 4, 12, 0),
        )
        self.assertAlmostEqual(ratio, 1.25)


if __name__ == "__main__":
    unittest.main()
