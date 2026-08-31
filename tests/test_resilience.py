import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pandas as pd

import auto_scan
from src.data_source import AkshareSource, MarketDataError
from src.scan_service import _cutoff_intraday, run_frozen_candidate_scan
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
    def test_frozen_candidate_scan_reuses_1445_analysis_and_updates_live_fields(self) -> None:
        raw = pd.DataFrame([{
            "代码": "600001", "名称": "测试股份", "最新价": 10.4,
            "涨跌幅": 4.0, "量比": 1.5, "换手率": 7.0,
            "流通市值": 100e8, "成交量": 10_000_000,
            "成交额": 103_000_000, "最高": 10.5, "最低": 9.8,
            "今开": 9.9, "昨收": 10.0, "报价时间": "20260831145205",
        }])
        raw.attrs["provider"] = "测试增量行情"
        prior = pd.DataFrame([{
            "code": "600001", "name": "测试股份", "score": 80.0,
            "change_pct": 3.8, "volume_ratio": 1.4, "turnover": 6.8,
        }])
        with patch("src.scan_service.AkshareSource") as source_class:
            source = source_class.return_value
            source.fast_spot.return_value = raw
            source.fast_index_return.return_value = (1.0, "20260831145205")
            result = run_frozen_candidate_scan(
                auto_scan.StrategyConfig(), prior,
                now=datetime(2026, 8, 31, 14, 52, 5),
            )

        self.assertEqual(len(result.candidates), 1)
        row = result.candidates.iloc[0]
        self.assertEqual(row["code"], "600001")
        self.assertAlmostEqual(row["price"], 10.4)
        self.assertGreater(row["score"], 80.0)

    def test_fast_intraday_data_is_cut_off_at_1452(self) -> None:
        frame = pd.DataFrame({
            "day": ["2026-08-31 14:52:00", "2026-08-31 14:53:00"],
            "close": [10.0, 10.2],
        })

        frozen = _cutoff_intraday(frame, datetime(2026, 8, 31, 14, 52, 59))

        self.assertEqual(frozen["close"].tolist(), [10.0])

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

    def test_fast_final_with_empty_1445_pool_finishes_without_market_request(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ValidationStore(Path(directory) / "scan.db")
            common = dict(provider="测试", market_count=5000, hard_count=10, config={}, candidates=[])
            store.save_staged_scan(
                slot="1430", scanned_at=datetime(2026, 8, 21, 14, 30), **common,
            )
            store.save_staged_scan(
                slot="1445", scanned_at=datetime(2026, 8, 21, 14, 45), **common,
            )
            with patch.object(auto_scan, "run_market_scan") as scanner, patch.object(
                auto_scan, "load_settings",
            ) as settings:
                settings.return_value.enabled = False
                payload = auto_scan.run_fast_final(
                    trade_date="2026-08-21", cfg=auto_scan.StrategyConfig(), store=store,
                    now_fn=lambda: datetime(2026, 8, 21, 14, 52),
                )

            scanner.assert_not_called()
            self.assertEqual(payload["fast"], 0)
            self.assertTrue(payload["finalized"])
            self.assertTrue(store.final_frame("2026-08-21").empty)


if __name__ == "__main__":
    unittest.main()
