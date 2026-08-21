import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pandas as pd

import auto_scan
from src.data_source import AkshareSource, MarketDataError
from src.validation_store import ValidationStore


class _Response:
    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {
            "data": {
                "total": 1,
                "rank_list": [{
                    "code": "sh600000", "name": "浦发银行", "zxj": "9.05",
                    "zdf": "-0.66", "lb": "0.83", "hsl": "0.40",
                    "ltsz": "3014.18", "volume": "503206", "turnover": "45657",
                }],
            }
        }


class _Session:
    def get(self, *args, **kwargs) -> _Response:
        return _Response()


class _ScanResult:
    provider = "测试行情"
    funnel = {"全市场": 1}
    candidates = pd.DataFrame()


class ResilienceTests(unittest.TestCase):
    def test_tencent_spot_maps_required_strategy_fields(self) -> None:
        with patch("src.data_source.requests.Session", return_value=_Session()):
            frame = AkshareSource._tencent_spot()

        row = frame.iloc[0]
        self.assertEqual(row["代码"], "600000")
        self.assertAlmostEqual(row["最新价"], 9.05)
        self.assertAlmostEqual(row["换手率"], 0.40)
        self.assertAlmostEqual(row["流通市值"], 3014.18e8)
        self.assertAlmostEqual(row["成交量"], 50_320_600)
        self.assertEqual(frame.attrs["provider"], "腾讯财经")

    def test_background_scan_retries_then_records_success(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ValidationStore(Path(directory) / "scan.db")
            with patch.object(
                auto_scan,
                "run_market_scan",
                side_effect=[MarketDataError("临时失败"), _ScanResult()],
            ) as scanner:
                result, _, attempts = auto_scan.run_scan_with_retries(
                    cfg=auto_scan.StrategyConfig(),
                    slot="1430",
                    store=store,
                    sleep_fn=lambda _seconds: None,
                    now_fn=lambda: datetime(2026, 8, 21, 14, 30),
                )

            self.assertEqual(result.provider, "测试行情")
            self.assertEqual(scanner.call_count, 2)
            self.assertEqual(attempts, 2)
            status = store.scan_status_frame("2026-08-21").iloc[0]
            self.assertEqual(status["status"], "success")
            self.assertEqual(status["attempt_count"], 2)

    def test_background_scan_records_failure_after_all_attempts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ValidationStore(Path(directory) / "scan.db")
            with patch.object(auto_scan, "run_market_scan", side_effect=MarketDataError("持续失败")):
                with self.assertRaises(MarketDataError):
                    auto_scan.run_scan_with_retries(
                        cfg=auto_scan.StrategyConfig(),
                        slot="1445",
                        store=store,
                        sleep_fn=lambda _seconds: None,
                        now_fn=lambda: datetime(2026, 8, 21, 14, 45),
                    )

            status = store.scan_status_frame("2026-08-21").iloc[0]
            self.assertEqual(status["status"], "failed")
            self.assertEqual(status["attempt_count"], 4)
            self.assertIn("持续失败", status["error_message"])


if __name__ == "__main__":
    unittest.main()
