from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
import json
import math
import re
import time
from typing import Any, Callable

import numpy as np
import pandas as pd
import requests


class MarketDataError(RuntimeError):
    pass


def _request_json(url: str, params: dict, validate: Callable = lambda value: value) -> Any:
    """Bounded read-only requests; malformed/incomplete pages get one retry too."""
    last_error = None
    for attempt in range(2):
        session = requests.Session()
        session.trust_env = False
        try:
            response = session.get(url, params=params, timeout=(3, 8))
            response.raise_for_status()
            return validate(response.json())
        except (requests.RequestException, ValueError, KeyError, TypeError, MarketDataError) as exc:
            last_error = exc
            if attempt == 0:
                time.sleep(0.2)
        finally:
            if hasattr(session, "close"):
                session.close()
    raise MarketDataError(f"行情请求失败（已尝试2次）：{last_error}")


def _positive_total(value: Any, provider: str) -> int:
    try:
        total = int(value)
    except (TypeError, ValueError) as exc:
        raise MarketDataError(f"{provider}缺少有效的全市场总数") from exc
    if isinstance(value, bool) or str(total) != str(value) or not 0 < total < 100_000:
        raise MarketDataError(f"{provider}全市场总数无效")
    return total


def _checked_rows(rows: Any, expected: int, required: set[str], provider: str) -> list[dict]:
    if not isinstance(rows, list) or len(rows) != expected:
        count = len(rows) if isinstance(rows, list) else "格式错误"
        raise MarketDataError(f"{provider}分页不完整：应有{expected}条，实际{count}")
    if any(not isinstance(row, dict) or not required.issubset(row) for row in rows):
        raise MarketDataError(f"{provider}分页缺少必要字段")
    return rows


def _complete_universe(frame: pd.DataFrame, total: int, provider: str, universe: str) -> pd.DataFrame:
    if frame.empty or len(frame) != total or "代码" not in frame:
        raise MarketDataError(f"{provider}全市场数据不完整：{len(frame)}/{total}")
    codes = frame["代码"].astype(str)
    if not codes.str.fullmatch(r"\d{6}").all() or codes.duplicated().any():
        raise MarketDataError(f"{provider}全市场代码重复或格式错误，不能以去重掩盖缺页")
    frame.attrs.update({"provider": provider, "expected_count": total, "received_count": len(frame),
                        "completeness": "provider_count_verified", "universe": universe,
                        "volume_unit": "shares"})
    return frame


def _verified_volume_multiplier(volume: float, amount: float, low: float, high: float) -> float:
    """Resolve raw shares versus lots from this response's own cash/price bounds.

    Verified 2026-11-06: raw Tencent daily/quote volumes are lots for 000001,
    002779, 300001, 600000, but shares for 688001/688256. Do not infer from
    code prefixes or AKShare versions. 2% tolerance covers rounded cash fields;
    it cannot confuse the 100-fold alternatives. Ambiguous units fail closed.
    """
    if not all(math.isfinite(value) for value in (volume, amount, low, high)):
        raise MarketDataError("成交量单位无法核验：量额或高低价缺失")
    if volume <= 0 or amount <= 0 or not 0 < low <= high:
        raise MarketDataError("成交量单位无法核验：量额或价格无效")
    matches = [factor for factor in (1.0, 100.0)
               if low * 0.98 <= amount / (volume * factor) <= high * 1.02]
    if len(matches) != 1:
        raise MarketDataError("成交量单位无法核验：成交均价不在当日高低价范围")
    return matches[0]


def _parse_sina_minutes(text: str) -> pd.DataFrame:
    """Decode the known Sina assignment wrapper, never execute JavaScript."""
    match = re.fullmatch(
        r"\s*(?:/\*.*?\*/\s*)?(?:(?:var\s+)?[A-Za-z_$][\w$]*\s*)?=\s*\((.*)\)\s*;\s*",
        text, flags=re.DOTALL,
    )
    if match is None:
        raise MarketDataError("新浪分钟线JSONP包装格式异常")
    try:
        rows = json.loads(match.group(1))
    except (json.JSONDecodeError, TypeError) as exc:
        raise MarketDataError("新浪分钟线JSON内容无效") from exc
    fields = ["day", "open", "high", "low", "close", "volume", "amount"]
    if not isinstance(rows, list) or not rows:
        raise MarketDataError("新浪分钟线为空或不是记录列表")
    if any(not isinstance(row, dict) or not set(fields).issubset(row) for row in rows):
        raise MarketDataError("新浪分钟线字段不完整")
    # Date, OHLC and complete-session checks remain in validation_service.
    frame = pd.DataFrame(rows)[fields]
    frame.attrs.update({"provider": "新浪未复权分钟线（直读）", "price_adjustment": "none"})
    return frame


class AkshareSource:
    def __init__(self) -> None:
        self._disable_system_proxy_for_market_data()
        try:
            import akshare as ak
        except ImportError as exc:
            raise MarketDataError("尚未安装 AKShare，请双击桌面的“尾盘狙击 CloseSniper.command”完成安装") from exc
        self.ak = ak

    @staticmethod
    def _disable_system_proxy_for_market_data() -> None:
        """行情站点对部分 macOS 系统代理不兼容，行情请求统一采用直连。"""
        session_cls = requests.sessions.Session
        if getattr(session_cls.__init__, "_tail_radar_direct", False):
            return
        original_init = session_cls.__init__

        def direct_init(session, *args, **kwargs):
            original_init(session, *args, **kwargs)
            session.trust_env = False

        direct_init._tail_radar_direct = True
        session_cls.__init__ = direct_init

    def spot(self) -> pd.DataFrame:
        try:
            return self._sina_spot()
        except Exception as sina_exc:
            try:
                return self._eastmoney_spot()
            except Exception as em_exc:
                try:
                    return self._tencent_spot()
                except Exception as tx_exc:
                    raise MarketDataError(
                        "三个免费实时接口均不可用；"
                        f"新浪：{sina_exc}；东方财富：{em_exc}；腾讯：{tx_exc}"
                    ) from tx_exc

    @staticmethod
    def _tencent_spot() -> pd.DataFrame:
        """Verify every page against provider total; never silently deduplicate.

        Live probing on 2026-11-06 rejected sort_type=code. Price ordering may
        move during pagination, so duplicate/missing rows invalidate the batch.
        """
        url = "https://proxy.finance.qq.com/cgi/cgi-bin/rank/hs/getBoardRankList"
        page_size = 200
        required = {"code", "name", "zxj", "zdf", "lb", "hsl", "ltsz", "volume", "turnover"}

        def fetch_page(offset: int, expected_total: int | None = None) -> dict:
            params = {
                "_appver": "11.17.0",
                "board_code": "aStock",
                "sort_type": "price",
                "direct": "down",
                "offset": str(offset),
                "count": str(page_size),
            }
            def validate(payload):
                data = payload["data"]
                page_total = _positive_total(data["total"], "腾讯")
                if expected_total is not None and page_total != expected_total:
                    raise MarketDataError("腾讯分页过程中全市场总数变化")
                _checked_rows(data["rank_list"], min(page_size, page_total - offset), required, "腾讯")
                return data
            return _request_json(url, params, validate)

        first = fetch_page(0)
        total = _positive_total(first.get("total"), "腾讯")
        rows = list(first["rank_list"])
        offsets = [page * page_size for page in range(1, math.ceil(total / page_size))]
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(fetch_page, offset, total) for offset in offsets]
            for future in as_completed(futures):
                rows.extend(future.result()["rank_list"])
        raw = pd.DataFrame(rows)

        number = lambda name: pd.to_numeric(raw[name], errors="coerce")
        price = number("zxj")
        change = number("zdf")
        previous = price / (1 + change / 100).replace(0, np.nan)
        frame = pd.DataFrame(
            {
                "代码": raw["code"].astype(str).str.replace(r"^(sh|sz|bj)", "", regex=True).str.zfill(6),
                "名称": raw["name"],
                "最新价": price,
                "涨跌幅": change,
                "量比": number("lb"),
                "换手率": number("hsl"),
                # 腾讯 ltsz 单位为亿元。
                "流通市值": number("ltsz") * 1e8,
                # 腾讯榜单 volume 单位为百股，turnover 单位为万元。
                "成交量": number("volume") * 100,
                "成交额": number("turnover") * 10_000,
                # 榜单不提供日内 OHLC；这些字段不参与实时硬筛，日线仅用收盘与成交量。
                "最高": price,
                "最低": price,
                "今开": previous,
                "昨收": previous,
            }
        )
        frame.attrs["pagination_order"] = "price; unique provider count checked"
        return _complete_universe(frame, total, "腾讯财经", "腾讯aStock（沪深京A股）")

    def fast_spot(self, codes: list[str], *, timeout: float = 5.0) -> pd.DataFrame:
        """一次请求读取候选池报价，供14:52冻结版做增量确认。"""
        if not codes:
            return pd.DataFrame()
        symbols = [self._market_symbol(str(code).zfill(6)) for code in codes]
        response = requests.get(
            "https://qt.gtimg.cn/q=" + ",".join(symbols),
            timeout=timeout,
        )
        response.raise_for_status()
        text = response.content.decode("gbk", errors="replace")
        rows: list[dict] = []
        for line in text.splitlines():
            if '="' not in line:
                continue
            payload = line.split('="', 1)[1].rsplit('"', 1)[0].split("~")
            if len(payload) < 58 or not payload[2]:
                continue

            def number(index: int) -> float:
                try:
                    return float(payload[index])
                except (IndexError, TypeError, ValueError):
                    return float("nan")

            price = number(3)
            previous = number(4)
            raw_volume = number(36)
            amount = number(57) * 10_000
            if raw_volume == 0 and amount == 0:
                shares = 0.0
            else:
                shares = raw_volume * _verified_volume_multiplier(raw_volume, amount, number(34), number(33))
            rows.append({
                "代码": str(payload[2]).zfill(6),
                "名称": payload[1],
                "最新价": price,
                "涨跌幅": number(32),
                "量比": number(49),
                "换手率": number(38),
                "流通市值": number(45) * 1e8,
                "成交量": shares,
                "成交额": amount,
                "最高": number(33),
                "最低": number(34),
                "今开": number(5),
                "昨收": previous,
                "报价时间": payload[30],
            })
        frame = pd.DataFrame(rows)
        received = set(frame.get("代码", pd.Series(dtype=str)).astype(str))
        missing = sorted(set(str(code).zfill(6) for code in codes) - received)
        if missing:
            raise MarketDataError(f"腾讯候选报价不完整，缺少：{', '.join(missing)}")
        if frame["代码"].duplicated().any() or received != set(str(code).zfill(6) for code in codes):
            raise MarketDataError("腾讯候选报价包含重复或额外股票")
        frame.attrs["provider"] = "腾讯候选池增量报价"
        frame.attrs["volume_unit"] = "shares"
        return frame

    def fast_index_return(self, *, timeout: float = 5.0) -> tuple[float, str]:
        response = requests.get("https://qt.gtimg.cn/q=sh000001", timeout=timeout)
        response.raise_for_status()
        text = response.content.decode("gbk", errors="replace")
        if '="' not in text:
            raise MarketDataError("腾讯上证指数报价格式异常")
        payload = text.split('="', 1)[1].rsplit('"', 1)[0].split("~")
        try:
            return float(payload[32]), str(payload[30])
        except (IndexError, TypeError, ValueError) as exc:
            raise MarketDataError("腾讯上证指数报价字段不完整") from exc

    @staticmethod
    def _sina_spot() -> pd.DataFrame:
        """读取新浪全市场快照，并保留流通市值与换手率。"""
        count_url = (
            "http://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
            "Market_Center.getHQNodeStockCount"
        )
        data_url = (
            "http://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
            "Market_Center.getHQNodeData"
        )
        total = _request_json(count_url, {"node": "hs_a"}, lambda value: _positive_total(value, "新浪"))
        pages = (total + 99) // 100
        required = {"code", "name", "trade", "changepercent", "turnoverratio", "nmc",
                    "volume", "amount", "high", "low", "open", "settlement"}

        def fetch_page(page: int) -> list[dict]:
            params = {
                "page": str(page),
                "num": "100",
                "sort": "symbol",
                "asc": "1",
                "node": "hs_a",
                "symbol": "",
                "_s_r_a": "page",
            }
            expected = min(100, total - (page - 1) * 100)
            return _request_json(data_url, params, lambda rows: _checked_rows(rows, expected, required, "新浪"))

        rows: list[dict] = []
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(fetch_page, page) for page in range(1, pages + 1)]
            for future in as_completed(futures):
                rows.extend(future.result())
        final_total = _request_json(count_url, {"node": "hs_a"}, lambda value: _positive_total(value, "新浪"))
        if final_total != total:
            raise MarketDataError("新浪分页过程中全市场总数变化")

        raw = pd.DataFrame(rows)
        number = lambda name: pd.to_numeric(raw[name], errors="coerce")
        frame = pd.DataFrame(
            {
                "代码": raw["code"].astype(str).str.zfill(6),
                "名称": raw["name"],
                "最新价": number("trade"),
                "涨跌幅": number("changepercent"),
                "量比": np.nan,
                "换手率": number("turnoverratio"),
                # 新浪 nmc 单位为万元，统一转换为元。
                "流通市值": number("nmc") * 10_000,
                "成交量": number("volume"),
                "成交额": number("amount"),
                "最高": number("high"),
                "最低": number("low"),
                "今开": number("open"),
                "昨收": number("settlement"),
            }
        )
        frame.attrs["pagination_order"] = "symbol ascending"
        return _complete_universe(frame, total, "新浪财经 + 腾讯日线", "新浪hs_a（按接口总数核对）")

    @staticmethod
    def _eastmoney_spot() -> pd.DataFrame:
        """Read server totals directly: the AKShare dataframe drops that evidence."""
        url = "https://82.push2.eastmoney.com/api/qt/clist/get"
        mapping = {"f12": "代码", "f14": "名称", "f2": "最新价", "f3": "涨跌幅",
                   "f10": "量比", "f8": "换手率", "f21": "流通市值", "f5": "成交量",
                   "f6": "成交额", "f15": "最高", "f16": "最低", "f17": "今开", "f18": "昨收"}

        def fetch_page(page, expected_total=None):
            params = {"pn": str(page), "pz": "100", "po": "1", "np": "1", "fltt": "2", "invt": "2",
                      "fid": "f12", "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048",
                      "fields": ",".join(mapping)}
            def validate(payload):
                data = payload["data"]
                total = _positive_total(data["total"], "东方财富")
                if expected_total is not None and total != expected_total:
                    raise MarketDataError("东方财富分页过程中全市场总数变化")
                rows = data["diff"]
                if isinstance(rows, dict):
                    rows = list(rows.values())
                return total, _checked_rows(rows, min(100, total - (page - 1) * 100), set(mapping), "东方财富")
            return _request_json(url, params, validate)

        total, rows = fetch_page(1)
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(fetch_page, page, total) for page in range(2, math.ceil(total / 100) + 1)]
            for future in as_completed(futures):
                rows.extend(future.result()[1])
        frame = pd.DataFrame(rows).rename(columns=mapping)[list(mapping.values())]
        frame["代码"] = frame["代码"].astype(str).str.zfill(6)
        for column in set(mapping.values()) - {"代码", "名称"}:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        # f5 and stock_zh_a_hist 成交量 are lots; checked against cash/price on
        # 600000, 2026-11-06: 642707 lots, 640470012 yuan, range 9.84–10.07.
        frame["成交量"] *= 100
        frame.attrs["pagination_order"] = "f12 code"
        return _complete_universe(frame, total, "东方财富", "东财沪深京A股查询fs（按接口总数核对）")

    def daily(self, code: str) -> pd.DataFrame:
        end = datetime.now().strftime("%Y%m%d")
        start = (datetime.now() - timedelta(days=150)).strftime("%Y%m%d")
        symbol = self._market_symbol(code)
        try:
            # Control raw units here, independent of incompatible AKShare
            # versions (the installed adapter misclassified sz000 as an index).
            params = {"param": f"{symbol},day,{start[:4]}-{start[4:6]}-{start[6:]},{end[:4]}-{end[4:6]}-{end[6:]},180,qfq"}
            payload = _request_json("https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get", params)
            block = payload["data"][symbol]
            rows = block.get("qfqday", block.get("day"))
            if not isinstance(rows, list) or not rows or any(not isinstance(row, list) or len(row) < 9 for row in rows):
                raise MarketDataError("腾讯日线字段不完整，无法验证成交量单位")
            frame = pd.DataFrame([row[:6] + [row[8]] for row in rows],
                                 columns=["date", "open", "close", "high", "low", "volume", "amount"])
            for column in frame.columns.difference(["date"]):
                frame[column] = pd.to_numeric(frame[column], errors="coerce")
            frame["amount"] *= 10_000
            traded = frame[(frame["volume"] > 0) & (frame["amount"] > 0)]
            if traded.empty:
                raise MarketDataError("腾讯日线没有可验证成交量单位的交易日")
            latest = traded.iloc[-1]
            multiplier = _verified_volume_multiplier(latest["volume"], latest["amount"], latest["low"], latest["high"])
            frame["volume"] *= multiplier
            return self._validated_daily(frame, "腾讯前复权日线", start, end)
        except (requests.RequestException, MarketDataError, ValueError, KeyError, TypeError) as tx_exc:
            raw = self.ak.stock_zh_a_hist(
                symbol=code,
                period="daily",
                start_date=start,
                end_date=end,
                adjust="qfq",
                timeout=8,
            )
            if raw is None or raw.empty or not {"日期", "开盘", "收盘", "最高", "最低", "成交量"}.issubset(raw.columns):
                raise MarketDataError(f"腾讯日线失败且东财日线不完整：{tx_exc}")
            frame = raw.rename(columns={"日期": "date", "开盘": "open", "收盘": "close",
                                        "最高": "high", "最低": "low", "成交量": "volume", "成交额": "amount"}).copy()
            frame["volume"] = pd.to_numeric(frame["volume"], errors="coerce") * 100
            return self._validated_daily(frame, "东方财富前复权日线", start, end)

    @staticmethod
    def _validated_daily(frame, provider, start, end):
        frame = frame.copy()
        frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
        if frame["date"].isna().any() or frame["date"].duplicated().any():
            raise MarketDataError(f"{provider}日线日期缺失或重复")
        fields = ["open", "close", "high", "low", "volume"]
        for column in fields:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        if not np.isfinite(frame[fields].to_numpy()).all() or (frame["volume"] < 0).any():
            raise MarketDataError(f"{provider}日线价格或成交量缺失/无效")
        if (frame[["open", "close", "high", "low"]] <= 0).any().any():
            raise MarketDataError(f"{provider}日线含非正价格")
        if ((frame["high"] < frame[["open", "close", "low"]].max(axis=1))
                | (frame["low"] > frame[["open", "close", "high"]].min(axis=1))).any():
            raise MarketDataError(f"{provider}日线高低价范围矛盾")
        frame = frame[frame["date"].between(pd.Timestamp(start), pd.Timestamp(end))].sort_values("date").reset_index(drop=True)
        if frame.empty:
            raise MarketDataError(f"{provider}指定区间无日线")
        frame.attrs.update({"provider": provider, "volume_unit": "shares", "price_adjustment": "qfq"})
        return frame

    def minute(self, code: str) -> pd.DataFrame:
        frame = self.minute_recent(code)
        day = datetime.now().strftime("%Y-%m-%d")
        time_column = "day" if "day" in frame.columns else "时间"
        return frame[frame[time_column].astype(str).str.startswith(day)].copy()

    def minute_fast(self, code: str, cutoff_at: datetime) -> pd.DataFrame:
        """冻结版直接读取东财当日分钟线；接口自带15秒超时，避免新浪请求无限等待。"""
        day = cutoff_at.strftime("%Y-%m-%d")
        return self.ak.stock_zh_a_hist_min_em(
            symbol=code,
            start_date=f"{day} 09:30:00",
            end_date=cutoff_at.strftime("%Y-%m-%d %H:%M:%S"),
            period="1",
            adjust="",
        )

    def minute_recent(self, code: str) -> pd.DataFrame:
        symbol = self._market_symbol(code)
        try:
            return self._sina_minutes(symbol)
        except Exception:
            day = datetime.now().strftime("%Y-%m-%d")
            return self.ak.stock_zh_a_hist_min_em(
                symbol=code,
                start_date=f"{day} 09:30:00",
                end_date=f"{day} 15:00:00",
                period="1",
                adjust="",
            )

    @staticmethod
    def _sina_minutes(symbol: str) -> pd.DataFrame:
        """Same unadjusted Sina endpoint as AKShare, without its unused daily fetch.

        AKShare's stock_zh_a_minute calls stock_zh_a_daily before checking
        adjust='', adding an unrelated unbounded request to every validation.
        """
        session = requests.Session()
        session.trust_env = False
        try:
            response = session.get(
                "https://quotes.sina.cn/cn/api/jsonp_v2.php/=/CN_MarketDataService.getKLineData",
                params={"symbol": symbol, "scale": "1", "ma": "no", "datalen": "1970"},
                timeout=(3, 8),
            )
            response.raise_for_status()
            return _parse_sina_minutes(response.text)
        finally:
            if hasattr(session, "close"):
                session.close()

    def minute_for_date(self, code: str, trade_date: str) -> pd.DataFrame:
        """读取指定交易日的一分钟线，供历史校验补算使用。"""
        return self.ak.stock_zh_a_hist_min_em(
            symbol=str(code).zfill(6),
            start_date=f"{trade_date} 09:30:00",
            end_date=f"{trade_date} 15:00:00",
            period="1",
            adjust="",
        )

    def minute_between(self, code: str, start_date: str, end_date: str) -> pd.DataFrame:
        return self.ak.stock_zh_a_hist_min_em(
            symbol=str(code).zfill(6),
            start_date=f"{start_date} 09:30:00",
            end_date=f"{end_date} 15:00:00",
            period="1",
            adjust="",
        )

    def index_minute(self) -> pd.DataFrame:
        frame = self.index_minute_recent()
        day = datetime.now().strftime("%Y-%m-%d")
        time_column = "day" if "day" in frame.columns else "时间"
        return frame[frame[time_column].astype(str).str.startswith(day)].copy()

    def index_minute_fast(self, cutoff_at: datetime) -> pd.DataFrame:
        day = cutoff_at.strftime("%Y-%m-%d")
        return self.ak.index_zh_a_hist_min_em(
            symbol="000001",
            period="1",
            start_date=f"{day} 09:30:00",
            end_date=cutoff_at.strftime("%Y-%m-%d %H:%M:%S"),
        )

    def index_minute_recent(self) -> pd.DataFrame:
        try:
            return self._sina_minutes("sh000001")
        except Exception:
            day = datetime.now().strftime("%Y-%m-%d")
            return self.ak.index_zh_a_hist_min_em(
                symbol="000001",
                period="1",
                start_date=f"{day} 09:30:00",
                end_date=f"{day} 15:00:00",
            )

    @staticmethod
    def _market_symbol(code: str) -> str:
        if code.startswith(("5", "6", "9")):
            return f"sh{code}"
        if code.startswith(("0", "1", "2", "3")):
            return f"sz{code}"
        return f"bj{code}"

    def hot_concept_members(self, top_n: int = 6) -> dict[str, list[str]]:
        boards = self.ak.stock_board_concept_name_em()
        boards["涨跌幅"] = pd.to_numeric(boards["涨跌幅"], errors="coerce")
        names = boards.sort_values("涨跌幅", ascending=False)["板块名称"].head(top_n).tolist()
        result: dict[str, list[str]] = {}
        for name in names:
            try:
                members = self.ak.stock_board_concept_cons_em(symbol=name)
                result[name] = members["代码"].astype(str).str.zfill(6).tolist()
            except Exception:
                continue
        return result


def parallel_fetch(
    codes: list[str],
    fetcher: Callable[[str], pd.DataFrame],
    max_workers: int = 6,
) -> tuple[dict[str, pd.DataFrame], dict[str, str]]:
    data: dict[str, pd.DataFrame] = {}
    errors: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(fetcher, code): code for code in codes}
        for future in as_completed(futures):
            code = futures[future]
            try:
                data[code] = future.result()
            except Exception as exc:
                errors[code] = str(exc)
    return data, errors


def demo_spot() -> pd.DataFrame:
    rng = np.random.default_rng(42)
    names = ["云启科技", "南岭新材", "海岳电子", "北辰智造", "星河能源", "远帆数据", "中州装备", "青禾生物"]
    rows = []
    for i, name in enumerate(names):
        price = 8 + i * 2.7
        rows.append(
            {
                "代码": f"60{i + 1:04d}",
                "名称": name,
                "最新价": price,
                "涨跌幅": 3.2 + i * 0.23,
                "量比": 1.15 + i * 0.12,
                "换手率": 5.3 + i * 0.55,
                "流通市值": (62 + i * 15) * 1e8,
                "成交量": int(rng.integers(100_000, 500_000)),
                "成交额": int(rng.integers(80_000_000, 500_000_000)),
                "最高": price * 1.01,
                "最低": price * 0.96,
                "今开": price * 0.97,
                "昨收": price / (1 + (3.2 + i * 0.23) / 100),
            }
        )
    return pd.DataFrame(rows)


def demo_daily(code: str) -> pd.DataFrame:
    seed = int(code[-3:])
    rng = np.random.default_rng(seed)
    days = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=90)
    index = max(seed - 1, 0)
    target = 8 + index * 2.7
    trend = np.linspace(target * 0.68, target, len(days))
    close = trend + rng.normal(0, target * 0.0015, len(days))
    close[-1] = target
    volume = np.linspace(100_000, 360_000, len(days)) * rng.uniform(0.94, 1.06, len(days))
    volume[-5:] = [200_000, 220_000, 250_000, 290_000, 340_000]
    return pd.DataFrame(
        {
            "日期": days,
            "开盘": close * 0.99,
            "收盘": close,
            "最高": close * 1.015,
            "最低": close * 0.98,
            "成交量": volume,
        }
    )


def demo_minute(code: str) -> pd.DataFrame:
    seed = int(code[-3:])
    rng = np.random.default_rng(seed)
    times = pd.date_range(pd.Timestamp.today().normalize() + pd.Timedelta(hours=9, minutes=30), periods=240, freq="min")
    index = max(seed - 1, 0)
    target = 8 + index * 2.7
    base = np.linspace(target * 0.97, target, len(times)) + rng.normal(0, target * 0.0012, len(times))
    base[-1] = target
    volume = rng.integers(900, 2200, len(times))
    amount = base * volume * 100
    return pd.DataFrame({"时间": times, "收盘": base, "成交量": volume, "成交额": amount})


def demo_index_minute() -> pd.DataFrame:
    times = pd.date_range(pd.Timestamp.today().normalize() + pd.Timedelta(hours=9, minutes=30), periods=240, freq="min")
    close = 3500 + np.linspace(0, 7, len(times))
    return pd.DataFrame({"时间": times, "收盘": close})
