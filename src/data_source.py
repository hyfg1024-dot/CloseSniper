from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Callable

import numpy as np
import pandas as pd
import requests


class MarketDataError(RuntimeError):
    pass


class AkshareSource:
    def __init__(self) -> None:
        self._disable_system_proxy_for_market_data()
        try:
            import akshare as ak
        except ImportError as exc:
            raise MarketDataError("尚未安装 AKShare，请双击“启动尾盘雷达.command”完成安装") from exc
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
                frame = self.ak.stock_zh_a_spot_em()
                frame.attrs["provider"] = "东方财富"
                return frame
            except Exception as em_exc:
                raise MarketDataError(
                    f"新浪与东方财富实时接口均不可用；新浪：{sina_exc}；东方财富：{em_exc}"
                ) from em_exc

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
        session = requests.Session()
        count_response = session.get(count_url, params={"node": "hs_a"}, timeout=12)
        count_response.raise_for_status()
        total = int(count_response.json())
        pages = (total + 99) // 100

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
            local_session = requests.Session()
            response = local_session.get(data_url, params=params, timeout=15)
            response.raise_for_status()
            return response.json()

        rows: list[dict] = []
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(fetch_page, page) for page in range(1, pages + 1)]
            for future in as_completed(futures):
                rows.extend(future.result())
        if not rows:
            raise MarketDataError("新浪实时行情返回空数据")

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
        frame.attrs["provider"] = "新浪财经 + 腾讯日线"
        return frame

    def daily(self, code: str) -> pd.DataFrame:
        end = datetime.now().strftime("%Y%m%d")
        start = (datetime.now() - timedelta(days=150)).strftime("%Y%m%d")
        symbol = self._market_symbol(code)
        try:
            return self.ak.stock_zh_a_hist_tx(
                symbol=symbol,
                start_date=start,
                end_date=end,
                adjust="qfq",
                timeout=15,
            )
        except Exception:
            return self.ak.stock_zh_a_hist(
                symbol=code,
                period="daily",
                start_date=start,
                end_date=end,
                adjust="qfq",
            )

    def minute(self, code: str) -> pd.DataFrame:
        frame = self.minute_recent(code)
        day = datetime.now().strftime("%Y-%m-%d")
        time_column = "day" if "day" in frame.columns else "时间"
        return frame[frame[time_column].astype(str).str.startswith(day)].copy()

    def minute_recent(self, code: str) -> pd.DataFrame:
        symbol = self._market_symbol(code)
        try:
            return self.ak.stock_zh_a_minute(symbol=symbol, period="1", adjust="")
        except Exception:
            day = datetime.now().strftime("%Y-%m-%d")
            return self.ak.stock_zh_a_hist_min_em(
                symbol=code,
                start_date=f"{day} 09:30:00",
                end_date=f"{day} 15:00:00",
                period="1",
                adjust="",
            )

    def index_minute(self) -> pd.DataFrame:
        frame = self.index_minute_recent()
        day = datetime.now().strftime("%Y-%m-%d")
        time_column = "day" if "day" in frame.columns else "时间"
        return frame[frame[time_column].astype(str).str.startswith(day)].copy()

    def index_minute_recent(self) -> pd.DataFrame:
        try:
            return self.ak.stock_zh_a_minute(symbol="sh000001", period="1", adjust="")
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
    close = 8 + np.cumsum(rng.normal(0.08, 0.06, len(days)))
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
    base = 10 + np.linspace(0, 0.45, len(times)) + rng.normal(0, 0.018, len(times))
    volume = rng.integers(900, 2200, len(times))
    amount = base * volume * 100
    return pd.DataFrame({"时间": times, "收盘": base, "成交量": volume, "成交额": amount})


def demo_index_minute() -> pd.DataFrame:
    times = pd.date_range(pd.Timestamp.today().normalize() + pd.Timedelta(hours=9, minutes=30), periods=240, freq="min")
    close = 3500 + np.linspace(0, 7, len(times))
    return pd.DataFrame({"时间": times, "收盘": close})
