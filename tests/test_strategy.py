import unittest
from datetime import datetime

import numpy as np
import pandas as pd

from src.data_source import demo_daily, demo_index_minute, demo_minute, demo_spot
from src.strategy import (
    StrategyConfig,
    analyze_daily,
    analyze_minute,
    estimate_volume_ratio,
    finalize_candidate,
    hard_filter,
    merge_live_daily_bar,
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

    def test_live_snapshot_is_merged_as_provisional_daily_bar(self):
        days = pd.bdate_range(end="2026-08-04", periods=70)
        close = np.linspace(8.0, 10.0, len(days))
        history = pd.DataFrame(
            {
                "date": days,
                "open": close,
                "close": close,
                "high": close,
                "low": close,
                "volume": 100_000,
            }
        )
        merged = merge_live_daily_bar(
            history,
            {
                "prev_close": 10.0,
                "price": 10.4,
                "open": 10.1,
                "high": 10.5,
                "low": 10.0,
                "volume": 180_000,
            },
            now=datetime(2026, 8, 5, 14, 50),
        )
        self.assertEqual(str(merged.iloc[-1]["date"])[:10], "2026-08-05")
        self.assertAlmostEqual(float(merged.iloc[-1]["close"]), 10.4)
        self.assertEqual(float(merged.iloc[-1]["volume"]), 180_000)

    def test_rational_mode_relaxes_only_long_term_ma_gate(self):
        close = np.r_[np.linspace(20, 10, 50), np.linspace(9, 15, 20)]
        history = pd.DataFrame(
            {
                "date": pd.bdate_range("2026-01-01", periods=len(close)),
                "open": close,
                "close": close,
                "high": close,
                "low": close,
                "volume": np.linspace(100_000, 300_000, len(close)),
            }
        )
        strict = analyze_daily(history, mode="strict")
        rational = analyze_daily(history, mode="rational")
        self.assertFalse(strict["ma_bull"])
        self.assertTrue(rational["ma_bull"])
        self.assertFalse(rational["ma60_rising"])

    def test_strategy_match_score_ranks_stronger_candidate_first(self):
        strong = {
            "change_pct": 4.0,
            "volume_ratio": 2.0,
            "turnover": 7.5,
            "float_cap_yi": 125.0,
            "volume_step": True,
            "volume_slope": 0.14,
            "ma_bull": True,
            "ma5": 12.0,
            "ma60": 11.2,
            "vwap_strong": True,
            "vwap_ratio": 0.95,
            "relative_strong": True,
            "stock_intraday_pct": 3.0,
            "market_intraday_pct": 0.5,
            "pullback_ok": True,
            "pullback_pct": 0.4,
            "hot_concepts": ["人工智能"],
            "daily_ok": True,
            "minute_ok": True,
            "min_volume_ratio": 1.0,
        }
        weaker = {
            **strong,
            "change_pct": 3.05,
            "volume_ratio": 1.05,
            "turnover": 5.1,
            "float_cap_yi": 52.0,
            "volume_slope": 0.01,
            "ma5": 11.3,
            "vwap_ratio": 0.71,
            "stock_intraday_pct": 0.6,
            "pullback_pct": 1.1,
            "hot_concepts": [],
        }

        strong_result = finalize_candidate(strong)
        weaker_result = finalize_candidate(weaker)

        self.assertTrue(strong_result["passed"])
        self.assertTrue(weaker_result["passed"])
        self.assertGreater(strong_result["score"], weaker_result["score"])
        self.assertLessEqual(strong_result["score"], 100)
        self.assertAlmostEqual(
            sum(strong_result["score_breakdown"].values()),
            strong_result["score"],
            places=1,
        )
        self.assertTrue(strong_result["rank_reason"])


if __name__ == "__main__":
    unittest.main()
