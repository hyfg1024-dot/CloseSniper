"""Fast final confirmation of the already-qualified strict two-stage pool.

The caller owns the wall-clock deadline and must reject an incomplete result.
All existing strict predicates are recomputed from cached daily bars and fresh
quotes/minutes; a prior pass is never substituted for a current predicate.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, time, timedelta
import math
from typing import Any

import pandas as pd
import requests

from src.data_source import AkshareSource, MarketDataError, _verified_volume_multiplier
from src.scan_service import ScanResult
from src.strategy import (
    StrategyConfig, _numeric_history, analyze_daily, analyze_minute,
    finalize_candidate, hard_filter, merge_live_daily_bar, minute_return_pct,
    normalize_spot,
)


def _symbol(code: str) -> str:
    return code if code.startswith(("sh", "sz", "bj")) else AkshareSource._market_symbol(code.zfill(6))


def _validated_minutes(raw: pd.DataFrame, now: datetime) -> pd.DataFrame:
    required = {"day", "close", "volume", "amount"}
    if raw is None or not required.issubset(raw.columns):
        raise MarketDataError("分时字段不完整")
    frame = raw[list(required)].copy()
    frame["day"] = pd.to_datetime(frame["day"], errors="coerce")
    if frame["day"].isna().any() or (frame["day"].dt.date != now.date()).any():
        raise MarketDataError("分时数据日期不符")
    cutoff = now
    frame = frame[frame["day"] <= cutoff].sort_values("day").reset_index(drop=True)
    if frame["day"].duplicated().any():
        raise MarketDataError("分时时间重复")
    if len(frame) < 30 or frame["day"].iloc[0].time() > time(9, 31):
        raise MarketDataError("分时数据缺少开盘记录")
    age = (now - frame["day"].iloc[-1].to_pydatetime()).total_seconds()
    minimum_latest = now.replace(second=0, microsecond=0) - timedelta(minutes=1)
    if age < 0 or age > 90 or frame["day"].iloc[-1] < minimum_latest:
        raise MarketDataError(f"分时数据未更新至{minimum_latest:%H:%M}之后")
    # Require complete one-minute coverage up to the returned final timestamp.
    # Missing bars would distort the fraction above VWAP and the last-30-bar high.
    morning = pd.date_range(now.replace(hour=9, minute=30, second=0, microsecond=0),
                            now.replace(hour=11, minute=30, second=0, microsecond=0), freq="min")
    afternoon = pd.date_range(now.replace(hour=13, minute=0, second=0, microsecond=0),
                              frame["day"].iloc[-1], freq="min")
    expected = set(morning).union(afternoon)
    actual = set(frame["day"])
    if not expected.issubset(actual):
        raise MarketDataError("当日分钟线存在缺口")
    for field in ("close", "volume", "amount"):
        frame[field] = pd.to_numeric(frame[field], errors="coerce")
        if not frame[field].map(lambda value: pd.notna(value) and math.isfinite(value)).all():
            raise MarketDataError(f"分时{field}数值无效")
    if (frame["close"] <= 0).any() or (frame[["volume", "amount"]] < 0).any().any():
        raise MarketDataError("分时价格或成交数据无效")
    if frame["volume"].sum() <= 0 or frame["amount"].sum() <= 0:
        raise MarketDataError("分时缺少有效成交量额")
    return frame


def parse_tencent_minutes(payload: dict[str, Any], code: str, now: datetime) -> pd.DataFrame:
    """Decode stock volume into shares; preserve unused index-volume units."""
    symbol = _symbol(code)
    try:
        block = payload["data"][symbol]["data"]
        if str(block["date"]) != now.strftime("%Y%m%d"):
            raise MarketDataError("腾讯分时不是当日数据")
        rows = []
        for line in block["data"]:
            stamp, price, volume, amount = str(line).split()
            if len(stamp) != 4 or not stamp.isdigit():
                raise MarketDataError("腾讯分时时间格式异常")
            rows.append({
                "day": f"{now:%Y-%m-%d} {stamp[:2]}:{stamp[2:]}:00",
                "close": float(price), "volume": float(volume),
                "amount": float(amount),
            })
        frame = pd.DataFrame(rows)
        if frame.empty:
            raise MarketDataError("腾讯分时为空")
        # Difference cumulative values before analyze_minute's cumsum.
        frame = frame.sort_values("day").reset_index(drop=True)
        timestamps = pd.to_datetime(frame["day"], errors="coerce")
        if timestamps.isna().any():
            raise MarketDataError("腾讯分时时间格式异常")
        frame = frame[timestamps <= now].copy()
        if frame.empty:
            raise MarketDataError("腾讯分时没有截止时间前的数据")
        is_index = symbol.startswith(("sh000", "sz399"))
        if not is_index:
            # Tencent STAR minute volumes are already shares (verified 688001
            # and 688256); main-board/ChiNext are lots. Minute closes are not
            # OHLC bounds, so 0.5–2x is only a coarse 1-vs-100 unit check.
            latest = frame.iloc[-1]
            multiplier = _verified_volume_multiplier(
                latest["volume"], latest["amount"], frame["close"].min() * 0.5,
                frame["close"].max() * 2,
            )
            frame["volume"] *= multiplier
        # Index cash/volume describes the constituents, not index points.
        # Never compare that quotient with index levels or invent share units.
        # Index consumers use minute_return_pct(close) only, never VWAP.
        for field in ("volume", "amount"):
            frame[field] = frame[field].diff().fillna(frame[field])
        frame = _validated_minutes(frame, now)
        frame.attrs.update({
            "instrument_type": "index" if is_index else "stock",
            "volume_unit": "provider_raw_index_not_used" if is_index else "shares",
            "volume_amount_usage": "unused_for_index_strength" if is_index else "vwap",
        })
        return frame
    except MarketDataError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise MarketDataError("腾讯分时响应格式异常") from exc


def strict_minute(code: str, now: datetime, *, timeout: float = 5.0) -> pd.DataFrame:
    with requests.Session() as session:
        session.trust_env = False
        for attempt in range(2):
            try:
                response = session.get(
                    "https://web.ifzq.gtimg.cn/appstock/app/minute/query",
                    params={"code": _symbol(code)}, timeout=(3.0, timeout),
                )
                response.raise_for_status()
                return parse_tencent_minutes(response.json(), code, now)
            except requests.RequestException:
                if attempt == 1:
                    raise
    raise MarketDataError("腾讯分时读取失败")


class StrictSource(AkshareSource):
    def strict_minute(self, code: str, now: datetime) -> pd.DataFrame:
        return strict_minute(code, now)


def evaluate_strict_pool(
    cfg: StrategyConfig,
    codes: list[str],
    daily_map: dict[str, pd.DataFrame],
    *,
    now: datetime,
    source=None,
    concepts: dict[str, list[str]] | None = None,
) -> ScanResult:
    """Evaluate every member of a strict 14:30/14:45 intersection, without truncation.

    ``errors`` must be empty before the caller publishes an official complete
    final list. Daily-cache acquisition date/configuration are checked by caller.
    """
    codes = list(dict.fromkeys(str(code).zfill(6) for code in codes))
    empty = pd.DataFrame(columns=["passed", "code", "name", "price", "score"])
    if not codes:
        return ScanResult(pd.DataFrame(), empty, {"全市场": 0}, 0, "严格前两次交集为空", {})
    if not time(14, 52) <= now.time() < time(14, 53):
        raise MarketDataError("严格最终确认只允许在14:52分钟内执行")
    source = source or StrictSource()
    raw_spot = source.fast_spot(codes, timeout=5.0)
    if "报价时间" not in raw_spot.columns:
        raise MarketDataError("缺少报价时间")
    timestamps = pd.to_datetime(raw_spot["报价时间"].astype(str), format="%Y%m%d%H%M%S", errors="coerce")
    age = (pd.Timestamp(now) - timestamps).dt.total_seconds()
    if timestamps.isna().any() or not timestamps.dt.date.eq(now.date()).all() or not age.between(-10, 20).all():
        raise MarketDataError("候选报价不是最新14:52行情")
    if not timestamps.dt.strftime("%H%M").eq("1452").all():
        raise MarketDataError("候选报价不在14:52分钟内")
    spot = normalize_spot(raw_spot)
    if spot["code"].duplicated().any() or set(spot["code"]) != set(codes):
        raise MarketDataError("候选报价不完整或代码重复")
    numeric = ["price", "change_pct", "volume_ratio", "turnover", "float_cap", "volume", "amount", "high", "low", "open", "prev_close"]
    if not spot[numeric].map(lambda value: pd.notna(value) and math.isfinite(value)).all().all():
        raise MarketDataError("候选报价必要字段缺失")
    if (spot[["price", "volume", "amount", "high", "low", "open", "prev_close"]] <= 0).any().any():
        raise MarketDataError("候选报价价格或成交数据无效")
    filtered, funnel = hard_filter(spot, cfg)
    if filtered.empty:
        return ScanResult(raw_spot, empty, funnel, 0, "腾讯实时硬条件复核·严格", {})
    errors: dict[str, str] = {}
    minutes: dict[str, pd.DataFrame] = {}
    targets = ["sh000001", *filtered["code"].tolist()]
    # The public endpoint intermittently times out on many simultaneous TLS
    # connections. Three workers plus one bounded retry was more reliable in
    # live checks; the parent process still enforces the absolute deadline.
    with ThreadPoolExecutor(max_workers=min(3, len(targets))) as pool:
        futures = {pool.submit(source.strict_minute, code, now): code for code in targets}
        for future in as_completed(futures):
            code = futures[future]
            try:
                minutes[code] = _validated_minutes(future.result(), now)
            except Exception as exc:
                errors[code] = str(exc)
    if "sh000001" not in minutes:
        raise MarketDataError(f"指数分时无法确认：{errors.get('sh000001', '无数据')}")
    market_return = minute_return_pct(minutes["sh000001"])
    if market_return is None:
        raise MarketDataError("指数分时无法计算当日相对涨幅")
    result_rows = []
    for row in filtered.to_dict("records"):
        code = row["code"]
        try:
            if code not in daily_map:
                raise MarketDataError("缺少当日准备的日线缓存")
            history = _numeric_history(daily_map[code])
            history = history[history["date"].dt.date < now.date()].copy()
            if len(history) < 64:
                raise MarketDataError("缓存历史日线不足64日")
            if code not in minutes:
                continue
            daily = analyze_daily(merge_live_daily_bar(history, row, now=now), mode="strict")
            minute = analyze_minute(minutes[code], cfg.min_vwap_ratio, market_return)
            result_rows.append(finalize_candidate({
                **row, **daily, **minute, "min_volume_ratio": cfg.min_volume_ratio,
                "apply_improved_risk": False,
                "hot_concepts": [name for name, members in (concepts or {}).items() if code in members],
            }))
        except Exception as exc:
            errors[code] = str(exc)
    frame = pd.DataFrame(result_rows) if result_rows else empty
    if not frame.empty:
        frame = frame.sort_values(["passed", "score"], ascending=[False, False]).reset_index(drop=True)
        frame["strategy_rank"] = range(1, len(frame) + 1)
    return ScanResult(raw_spot, frame, funnel, len(filtered), "腾讯实时分时 + 当日日线缓存·严格重算", errors,
                      daily_history=daily_map, concepts=concepts or {}, minute_history=minutes)
