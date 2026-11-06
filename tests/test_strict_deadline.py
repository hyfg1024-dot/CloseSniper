import unittest
from datetime import datetime

import numpy as np
import pandas as pd

from src.data_source import MarketDataError
from src.strategy import (
    StrategyConfig, analyze_daily, analyze_minute, finalize_candidate,
    hard_filter, merge_live_daily_bar, minute_return_pct, normalize_spot,
)
from src.strict_deadline import evaluate_strict_pool, parse_tencent_minutes


NOW = datetime(2026, 11, 6, 14, 52, 5)


def minute_fixture(price=13.26):
    stamps = pd.date_range("2026-11-06 09:30", "2026-11-06 11:30", freq="min").append(
        pd.date_range("2026-11-06 13:00", "2026-11-06 14:52", freq="min"))
    prices = np.linspace(price * .973, price, len(stamps))
    return pd.DataFrame({"day": stamps, "close": prices, "volume": 100.0, "amount": prices * 100})


def history_fixture():
    prices = np.linspace(8, 12.75, 80)
    return pd.DataFrame({
        "date": pd.bdate_range(end="2026-11-05", periods=80), "open": prices,
        "high": prices, "low": prices, "close": prices, "volume": np.linspace(1000, 20000, 80),
    })


class Source:
    def __init__(self):
        self.quotes = pd.DataFrame([{
            "代码": "002779", "名称": "fixture", "最新价": 13.26, "涨跌幅": 4.,
            "量比": 1.5, "换手率": 7., "流通市值": 100e8, "成交量": 23400.,
            "成交额": 305000., "最高": 13.28, "最低": 12.8, "今开": 12.9,
            "昨收": 12.75, "报价时间": "20261106145205",
        }])
        self.stock = minute_fixture()
        self.index = minute_fixture(3600.)
        self.index["close"] = 3600.
        self.index["amount"] = 360000.

    def fast_spot(self, codes, timeout=5):
        return self.quotes.copy()

    def strict_minute(self, code, now):
        return self.index.copy() if code == "sh000001" else self.stock.copy()


class StrictDeadlineTests(unittest.TestCase):
    def test_strict_predicates_and_score_match_existing_algorithm(self):
        cfg, source, history = StrategyConfig(), Source(), history_fixture()
        row = hard_filter(normalize_spot(source.quotes), cfg)[0].iloc[0].to_dict()
        expected = finalize_candidate({
            **row,
            **analyze_daily(merge_live_daily_bar(history, row, NOW), mode="strict"),
            **analyze_minute(source.stock, cfg.min_vwap_ratio, minute_return_pct(source.index)),
            "min_volume_ratio": cfg.min_volume_ratio, "apply_improved_risk": False,
            "hot_concepts": [],
        })
        actual = evaluate_strict_pool(cfg, ["002779"], {"002779": history}, now=NOW, source=source)
        self.assertEqual(actual.errors, {})
        result = actual.result_frame.iloc[0]
        self.assertEqual(result["checks"], expected["checks"])
        self.assertEqual(result["score"], expected["score"])
        self.assertEqual(result["passed"], expected["passed"])
        self.assertTrue(result["passed"])

    def test_prior_pass_cannot_survive_current_minute_failure(self):
        source = Source()
        source.stock.loc[source.stock.index[-1], "close"] = 11.
        result = evaluate_strict_pool(StrategyConfig(), ["002779"], {"002779": history_fixture()}, now=NOW, source=source)
        self.assertFalse(result.result_frame.iloc[0]["passed"])
        self.assertIn("回踩有效", result.result_frame.iloc[0]["failed_reasons"])

    def test_quote_wrong_date_or_outside_final_minute_is_fatal(self):
        for stamp in ("20261105145205", "20261106145150", "20261106145300", "20261106145000"):
            with self.subTest(stamp=stamp):
                source = Source()
                source.quotes["报价时间"] = stamp
                with self.assertRaises(MarketDataError):
                    evaluate_strict_pool(StrategyConfig(), ["002779"], {"002779": history_fixture()}, now=NOW, source=source)

    def test_missing_history_or_minute_is_not_normal_empty_result(self):
        result = evaluate_strict_pool(StrategyConfig(), ["002779"], {}, now=NOW, source=Source())
        self.assertIn("002779", result.errors)
        source = Source()
        source.stock = source.stock.iloc[:-5]
        result = evaluate_strict_pool(StrategyConfig(), ["002779"], {"002779": history_fixture()}, now=NOW, source=source)
        self.assertIn("002779", result.errors)
        self.assertTrue(result.candidates.empty)

    def test_cumulative_tencent_units_are_differenced_before_vwap(self):
        frame = minute_fixture()
        rows = [f"{row.day:%H%M} {row.close:.6f} {volume:.6f} {amount:.6f}"
                for row, volume, amount in zip(frame.itertuples(), frame.volume.cumsum() / 100, frame.amount.cumsum())]
        payload = {"data": {"sz002779": {"data": {"date": "20261106", "data": rows}}}}
        actual = parse_tencent_minutes(payload, "002779", NOW)
        self.assertAlmostEqual(actual.volume.sum(), frame.volume.sum())
        self.assertAlmostEqual(actual.amount.sum(), frame.amount.sum(), places=4)
        self.assertAlmostEqual(analyze_minute(actual, .7, 0)["vwap"], frame.amount.sum() / frame.volume.sum())
        payload["data"]["sz002779"]["data"]["date"] = "20261105"
        with self.assertRaises(MarketDataError):
            parse_tencent_minutes(payload, "002779", NOW)

    def test_gap_in_early_minutes_is_rejected(self):
        source = Source()
        source.stock = source.stock.drop(index=[5])
        result = evaluate_strict_pool(StrategyConfig(), ["002779"], {"002779": history_fixture()}, now=NOW, source=source)
        self.assertIn("分钟线存在缺口", result.errors["002779"])

    def test_star_minute_volume_already_in_shares_is_not_multiplied_again(self):
        frame = minute_fixture()
        rows = [f"{row.day:%H%M} {row.close:.6f} {volume:.6f} {amount:.6f}"
                for row, volume, amount in zip(frame.itertuples(), frame.volume.cumsum(), frame.amount.cumsum())]
        payload = {"data": {"sh688001": {"data": {"date": "20261106", "data": rows}}}}
        actual = parse_tencent_minutes(payload, "688001", NOW)
        self.assertAlmostEqual(actual.volume.sum(), frame.volume.sum())
        self.assertAlmostEqual(analyze_minute(actual, .7, 0)["vwap"], frame.amount.sum() / frame.volume.sum())

    def test_unverifiable_minute_volume_unit_is_rejected(self):
        frame = minute_fixture()
        rows = [f"{row.day:%H%M} {row.close:.6f} {volume:.6f} {amount:.6f}"
                for row, volume, amount in zip(frame.itertuples(), frame.volume.cumsum(), frame.amount.cumsum() * 10000)]
        payload = {"data": {"sh688001": {"data": {"date": "20261106", "data": rows}}}}
        with self.assertRaisesRegex(MarketDataError, "成交量单位无法核验"):
            parse_tencent_minutes(payload, "688001", NOW)

    def test_index_points_are_not_compared_with_constituent_cash_per_volume(self):
        frame = minute_fixture()
        frame["close"] *= 500  # Index levels; cash/volume still a stock price.
        rows = [f"{row.day:%H%M} {row.close:.6f} {volume:.6f} {amount:.6f}"
                for row, volume, amount in zip(frame.itertuples(), frame.volume.cumsum(), frame.amount.cumsum())]
        payload = {"data": {"sh000001": {"data": {"date": "20261106", "data": rows}}}}
        actual = parse_tencent_minutes(payload, "sh000001", NOW)
        self.assertAlmostEqual(actual.volume.sum(), frame.volume.sum())
        self.assertEqual(actual.attrs["instrument_type"], "index")
        self.assertEqual(actual.attrs["volume_unit"], "provider_raw_index_not_used")
        self.assertEqual(actual.attrs["volume_amount_usage"], "unused_for_index_strength")
        self.assertAlmostEqual(minute_return_pct(actual), minute_return_pct(frame), places=5)
        payload["data"]["sh000001"]["data"]["data"].pop(4)
        with self.assertRaisesRegex(MarketDataError, "缺口"):
            parse_tencent_minutes(payload, "sh000001", NOW)

    def test_early_slot_parser_uses_its_own_cutoff(self):
        frame = minute_fixture()
        rows = [f"{row.day:%H%M} {row.close:.6f} {volume:.6f} {amount:.6f}"
                for row, volume, amount in zip(frame.itertuples(), frame.volume.cumsum() / 100, frame.amount.cumsum())]
        payload = {"data": {"sz002779": {"data": {"date": "20261106", "data": rows}}}}
        for minute, count in ((30, 212), (45, 227)):
            with self.subTest(minute=minute):
                now = NOW.replace(minute=minute)
                actual = parse_tencent_minutes(payload, "002779", now)
                self.assertEqual(len(actual), count)
                self.assertEqual(actual.day.iloc[-1], pd.Timestamp(now.replace(second=0)))
                with self.assertRaises(MarketDataError):
                    evaluate_strict_pool(StrategyConfig(), ["002779"], {"002779": history_fixture()}, now=now, source=Source())

    def test_empty_pool_needs_no_requests(self):
        result = evaluate_strict_pool(StrategyConfig(), [], {}, now=NOW, source=object())
        self.assertTrue(result.candidates.empty)
        self.assertEqual(result.errors, {})


if __name__ == "__main__":
    unittest.main()
