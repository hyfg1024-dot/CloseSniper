import unittest
from unittest.mock import patch

from src.data_source import MarketDataError, demo_spot
from src.scan_service import run_market_scan
from src.strategy import StrategyConfig


class ScanCoverageTests(unittest.TestCase):
    def test_automatic_full_coverage_does_not_silently_apply_preview_cap(self):
        cfg = StrategyConfig(max_candidates=1)
        full = run_market_scan(cfg, use_demo=True, analyze_all=True)
        preview = run_market_scan(cfg, use_demo=True)
        self.assertGreater(full.hard_count, 1)
        self.assertEqual(len(full.result_frame), full.hard_count)
        self.assertEqual(len(preview.result_frame), 1)

    def test_full_coverage_requires_provider_count_proof(self):
        with patch("src.scan_service.AkshareSource") as source:
            source.return_value.spot.return_value = demo_spot()
            with self.assertRaisesRegex(MarketDataError, "总数未经核验"):
                run_market_scan(StrategyConfig(), analyze_all=True)


if __name__ == "__main__":
    unittest.main()
