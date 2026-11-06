import unittest
import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pandas as pd
import requests

from src.data_source import (
    AkshareSource, MarketDataError, _request_json, _verified_volume_multiplier,
    _parse_sina_minutes,
)


def sina_row(index):
    return {"code": f"{600000 + index:06d}", "name": "测试", "trade": "10.4",
            "changepercent": "4", "turnoverratio": "7", "nmc": "1000000",
            "volume": "1000000", "amount": "10400000", "high": "10.5",
            "low": "10", "open": "10", "settlement": "10"}


def tencent_row(index):
    return {"code": f"sh{600000 + index:06d}", "name": "测试", "zxj": "10.4",
            "zdf": "4", "lb": "1.4", "hsl": "7", "ltsz": "100",
            "volume": "10000", "turnover": "1040"}


def em_row(index):
    return {"f12": f"{600000 + index:06d}", "f14": "测试", "f2": 10.4,
            "f3": 4, "f10": 1.4, "f8": 7, "f21": 100e8, "f5": 10000,
            "f6": 10400000, "f15": 10.5, "f16": 10, "f17": 10, "f18": 10}


class Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


class DataIntegrityTests(unittest.TestCase):
    def source(self):
        source = object.__new__(AkshareSource)
        source.ak = SimpleNamespace(stock_zh_a_hist=Mock())
        return source

    def sina_session(self, mutation=None):
        pages = {1: [sina_row(i) for i in range(100)],
                 2: [sina_row(i) for i in range(100, 200)], 3: [sina_row(200)]}
        if mutation:
            mutation(pages)
        def get(url, params, timeout):
            self.assertEqual(timeout, (3, 8))
            return Response("201" if "Count" in url else pages[int(params["page"])])
        return SimpleNamespace(get=get)

    def test_sina_all_pages_and_provider_count_are_verified(self):
        with patch("src.data_source.requests.Session", return_value=self.sina_session()):
            frame = AkshareSource._sina_spot()
        self.assertEqual(len(frame), 201)
        self.assertEqual(frame.attrs["completeness"], "provider_count_verified")
        self.assertEqual(frame.attrs["expected_count"], 201)
        self.assertEqual(frame.attrs["volume_unit"], "shares")

    def test_sina_missing_duplicate_malformed_pages_cannot_succeed(self):
        cases = [lambda pages: pages[2].pop(),
                 lambda pages: pages[3].__setitem__(0, pages[1][0]),
                 lambda pages: pages[2][0].pop("volume"),
                 lambda pages: pages.__setitem__(2, {"error": "busy"})]
        for mutate in cases:
            with self.subTest(mutation=mutate), patch("src.data_source.time.sleep"), \
                    patch("src.data_source.requests.Session", return_value=self.sina_session(mutate)):
                with self.assertRaises(MarketDataError):
                    AkshareSource._sina_spot()

    def test_sina_total_changing_during_scan_is_rejected(self):
        count = 0
        def get(url, params, timeout):
            nonlocal count
            if "Count" in url:
                count += 1
                return Response("1" if count == 1 else "2")
            return Response([sina_row(0)])
        with patch("src.data_source.requests.Session", return_value=SimpleNamespace(get=get)):
            with self.assertRaisesRegex(MarketDataError, "总数变化"):
                AkshareSource._sina_spot()

    def test_request_retry_is_finite_and_also_retries_invalid_json(self):
        session = SimpleNamespace(get=Mock(side_effect=[requests.Timeout("busy"), Response({"ok": True})]))
        with patch("src.data_source.time.sleep"), patch("src.data_source.requests.Session", return_value=session):
            self.assertEqual(_request_json("https://example.invalid", {}), {"ok": True})
        self.assertEqual(session.get.call_count, 2)
        session.get = Mock(return_value=Response(None))
        with patch("src.data_source.time.sleep"), patch("src.data_source.requests.Session", return_value=session):
            with self.assertRaises(MarketDataError):
                _request_json("https://example.invalid", {}, lambda payload: payload["data"])
        self.assertEqual(session.get.call_count, 2)

    def test_tencent_does_not_hide_duplicate_codes_by_deduplication(self):
        for mode in ("duplicate", "missing", "total_changed"):
            def get(url, params, timeout):
                offset = int(params["offset"])
                rows = [tencent_row(i) for i in range(200)] if offset == 0 else [tencent_row(200)]
                if offset and mode == "duplicate":
                    rows = [tencent_row(0)]
                if offset and mode == "missing":
                    rows = []
                total = 202 if offset and mode == "total_changed" else 201
                return Response({"data": {"total": total, "rank_list": rows}})
            with self.subTest(mode=mode), patch("src.data_source.time.sleep"), \
                    patch("src.data_source.requests.Session", return_value=SimpleNamespace(get=get)):
                with self.assertRaises(MarketDataError):
                    AkshareSource._tencent_spot()

    def test_eastmoney_verifies_totals_fields_duplicates_and_lot_units(self):
        for mode in ("valid", "duplicate", "missing_field", "missing_total", "short_page"):
            def get(url, params, timeout):
                page = int(params["pn"])
                self.assertEqual(params["fid"], "f12")
                rows = [em_row(i) for i in range(100)] if page == 1 else [em_row(100)]
                if page == 2 and mode == "duplicate":
                    rows = [em_row(0)]
                if mode == "missing_field":
                    rows[0].pop("f21")
                if page == 2 and mode == "short_page":
                    rows = []
                data = {"total": 101, "diff": rows}
                if mode == "missing_total":
                    data.pop("total")
                return Response({"data": data})
            with self.subTest(mode=mode), patch("src.data_source.time.sleep"), \
                    patch("src.data_source.requests.Session", return_value=SimpleNamespace(get=get)):
                if mode == "valid":
                    frame = AkshareSource._eastmoney_spot()
                    self.assertEqual(len(frame), 101)
                    self.assertEqual(frame.iloc[0]["成交量"], 1000000)
                    self.assertEqual(frame.attrs["expected_count"], 101)
                else:
                    with self.assertRaises(MarketDataError):
                        AkshareSource._eastmoney_spot()

    def test_bad_primary_market_response_switches_provider(self):
        source = self.source()
        good = pd.DataFrame([sina_row(0)])
        with patch.object(source, "_sina_spot", side_effect=MarketDataError("缺页")), \
                patch.object(source, "_eastmoney_spot", return_value=good):
            self.assertIs(source.spot(), good)

    def test_live_verified_board_volume_units_use_cash_and_price_not_prefix(self):
        # Read-only observations on 2026-11-06, Tencent daily and quote field36
        # matched raw volume for all six securities. Cash is yuan, prices qfq
        # latest-day OHLC; the rank-list endpoint separately uses lots for STAR.
        observations = [
            ("000001", 1640276, 1927307600, 11.57, 11.85, 100),
            ("002779", 31980, 263456200, 80.90, 84.50, 100),
            ("300001", 113761, 359233300, 30.66, 32.21, 100),
            ("600000", 642707, 640470000, 9.84, 10.07, 100),
            ("688001", 8630559, 312867300, 35.10, 37.66, 1),
            ("688256", 11135886, 8917277500, 776.36, 830.00, 1),
        ]
        for code, volume, amount, low, high, factor in observations:
            with self.subTest(code=code):
                self.assertEqual(_verified_volume_multiplier(volume, amount, low, high), factor)
                self.assertLessEqual(low, amount / (volume * factor))
                self.assertLessEqual(amount / (volume * factor), high)
        for args in ((100, 10_000_000, 10, 11), (0, 100, 10, 11), (100, float("nan"), 10, 11)):
            with self.assertRaises(MarketDataError):
                _verified_volume_multiplier(*args)

    def test_tencent_daily_normalizes_raw_lots_and_shares_once(self):
        today = datetime.now().strftime("%Y-%m-%d")
        for code, raw_volume, amount_wan, low, high, expected in (
            ("000001", 1640276, 192730.76, 11.57, 11.85, 164027600),
            ("688001", 8630559, 31286.73, 35.10, 37.66, 8630559),
        ):
            price = (low + high) / 2
            symbol = AkshareSource._market_symbol(code)
            payload = {"data": {symbol: {"qfqday": [[today, price, price, high, low, raw_volume, {}, "1", amount_wan]]}}}
            source = self.source()
            with self.subTest(code=code), patch("src.data_source._request_json", return_value=payload):
                frame = source.daily(code)
            self.assertEqual(frame.iloc[0]["volume"], expected)
            self.assertEqual(frame.attrs["volume_unit"], "shares")
            source.ak.stock_zh_a_hist.assert_not_called()

    def test_tencent_fast_quotes_normalize_each_stock_units_and_reject_duplicates(self):
        def line(code, volume, amount, low, high):
            fields = ["0"] * 58
            fields[1], fields[2] = "测试", code
            fields[3] = fields[4] = fields[5] = str((low + high) / 2)
            fields[30] = "20261106145203"
            fields[32], fields[33], fields[34] = "4", str(high), str(low)
            fields[36], fields[38], fields[45], fields[49] = str(volume), "7", "100", "1.4"
            fields[57] = str(amount / 10000)
            return 'v_quote="' + "~".join(fields) + '";'
        raw_lines = [line("000001", 1640276, 1927307591, 11.57, 11.85),
                     line("688001", 8630559, 312867299, 35.10, 37.66)]
        response = Mock(content="\n".join(raw_lines).encode("gbk"))
        with patch("src.data_source.requests.get", return_value=response):
            frame = self.source().fast_spot(["000001", "688001"])
        self.assertEqual(frame["成交量"].tolist(), [164027600, 8630559])
        response.content = "\n".join(raw_lines + [raw_lines[0]]).encode("gbk")
        with patch("src.data_source.requests.get", return_value=response):
            with self.assertRaisesRegex(MarketDataError, "重复"):
                self.source().fast_spot(["000001", "688001"])

    def test_eastmoney_daily_fallback_converts_confirmed_lots_and_has_timeout(self):
        source = self.source()
        source.ak.stock_zh_a_hist.return_value = pd.DataFrame([{
            "日期": datetime.now().date(), "开盘": 9.91, "收盘": 10.03,
            "最高": 10.07, "最低": 9.84, "成交量": 642707, "成交额": 640470012,
        }])
        with patch("src.data_source._request_json", side_effect=MarketDataError("unavailable")):
            frame = source.daily("600000")
        self.assertEqual(frame.iloc[0]["volume"], 64270700)
        self.assertEqual(source.ak.stock_zh_a_hist.call_args.kwargs["timeout"], 8)

    def test_daily_duplicate_dates_or_missing_numbers_fail(self):
        source = self.source()
        for rows in ([{"日期": datetime.now().date(), "开盘": 10, "收盘": 10,
                       "最高": 10, "最低": 10, "成交量": 100}] * 2,
                     [{"日期": datetime.now().date(), "开盘": 10, "收盘": None,
                       "最高": 10, "最低": 10, "成交量": 100}]):
            source.ak.stock_zh_a_hist.return_value = pd.DataFrame(rows)
            with patch("src.data_source._request_json", side_effect=MarketDataError("unavailable")):
                with self.assertRaises(MarketDataError):
                    source.daily("600000")

    def test_sina_jsonp_parser_accepts_only_json_records(self):
        row = {"day": "2026-11-06 09:31:00", "open": "10.1", "high": "10.2",
               "low": "10", "close": "10.1", "volume": "1000", "amount": "10100"}
        for prefix in ("=", "/* provider comment */ =", "var _sample="):
            frame = _parse_sina_minutes(prefix + "(" + json.dumps([row]) + ");")
            self.assertEqual(frame.iloc[0].to_dict(), row)
        for raw in ("=(null);", "=([]);", "=({});", "=([{}]);", "=([{'day': 'bad'}]);",
                    "alert('not json');", "=(" + json.dumps([row]) + "); trailing-code"):
            with self.subTest(raw=raw), self.assertRaises(MarketDataError):
                _parse_sina_minutes(raw)

    def test_sina_minute_direct_read_never_requests_daily_or_akshare_minute(self):
        row = {"day": "2026-11-06 09:31:00", "open": "10.1", "high": "10.2",
               "low": "10", "close": "10.1", "volume": "1000", "amount": "10100"}
        response = Mock(text="=(" + json.dumps([row]) + ");")
        session = SimpleNamespace(get=Mock(return_value=response))
        source = self.source()
        source.ak.stock_zh_a_minute = Mock(side_effect=AssertionError("must not fetch daily through AKShare"))
        source.ak.stock_zh_a_hist_min_em = Mock(side_effect=AssertionError("fallback not needed"))
        with patch("src.data_source.requests.Session", return_value=session):
            self.assertEqual(len(source.minute_recent("600000")), 1)
            self.assertEqual(len(source.index_minute_recent()), 1)
        self.assertEqual(session.get.call_count, 2)
        self.assertEqual(session.get.call_args.kwargs["timeout"], (3, 8))
        self.assertEqual(session.get.call_args.kwargs["params"]["symbol"], "sh000001")
        source.ak.stock_zh_a_minute.assert_not_called()
        source.ak.stock_zh_a_hist.assert_not_called()
        source.ak.stock_zh_a_hist_min_em.assert_not_called()

    def test_sina_minute_timeout_and_parse_errors_keep_eastmoney_fallback(self):
        source = self.source()
        fallback = pd.DataFrame({"时间": ["2026-11-06 09:31:00"]})
        source.ak.stock_zh_a_hist_min_em = Mock(return_value=fallback)
        source.ak.index_zh_a_hist_min_em = Mock(return_value=fallback)
        for error in (requests.Timeout("bounded timeout"), MarketDataError("bad wrapper")):
            with patch.object(source, "_sina_minutes", side_effect=error):
                self.assertIs(source.minute_recent("600000"), fallback)
                self.assertIs(source.index_minute_recent(), fallback)


if __name__ == "__main__":
    unittest.main()
