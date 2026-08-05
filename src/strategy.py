from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, time
from typing import Any

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class StrategyConfig:
    min_change: float = 3.0
    max_change: float = 5.0
    min_volume_ratio: float = 1.0
    min_turnover: float = 5.0
    max_turnover: float = 10.0
    min_float_cap_yi: float = 50.0
    max_float_cap_yi: float = 200.0
    max_candidates: int = 30
    min_vwap_ratio: float = 0.70

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


SPOT_RENAME = {
    "代码": "code",
    "名称": "name",
    "最新价": "price",
    "涨跌幅": "change_pct",
    "量比": "volume_ratio",
    "换手率": "turnover",
    "流通市值": "float_cap",
    "成交量": "volume",
    "成交额": "amount",
    "最高": "high",
    "最低": "low",
    "今开": "open",
    "昨收": "prev_close",
}


def normalize_spot(raw: pd.DataFrame) -> pd.DataFrame:
    missing = [c for c in SPOT_RENAME if c not in raw.columns]
    if missing:
        raise ValueError(f"实时行情缺少字段：{', '.join(missing)}")
    df = raw.rename(columns=SPOT_RENAME)[list(SPOT_RENAME.values())].copy()
    for col in set(SPOT_RENAME.values()) - {"code", "name"}:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["code"] = df["code"].astype(str).str.zfill(6)
    df["float_cap_yi"] = df["float_cap"] / 1e8
    df.attrs.update(raw.attrs)
    return df


def hard_filter(spot: pd.DataFrame, cfg: StrategyConfig) -> tuple[pd.DataFrame, dict[str, int]]:
    """执行可从实时快照直接计算的硬条件，并返回漏斗计数。"""
    df = spot.copy()
    counts = {"全市场": len(df)}
    name = df["name"].fillna("").astype(str)
    code = df["code"].astype(str)
    excluded = (
        name.str.contains(r"ST|退", regex=True)
        | name.str.startswith(("N", "C"))
        | code.str.startswith(("4", "8", "92"))
    )
    df = df.loc[~excluded].copy()
    counts["排除风险标的"] = len(df)

    conditions = [
        ("涨幅 3%–5%", df["change_pct"].between(cfg.min_change, cfg.max_change, inclusive="both")),
        ("换手率 5%–10%", df["turnover"].between(cfg.min_turnover, cfg.max_turnover, inclusive="both")),
        (
            "流通市值 50–200亿",
            df["float_cap_yi"].between(cfg.min_float_cap_yi, cfg.max_float_cap_yi, inclusive="both"),
        ),
    ]
    for label, mask in conditions:
        df = df.loc[mask.reindex(df.index).fillna(False)].copy()
        counts[label] = len(df)

    if df["volume_ratio"].notna().any():
        df = df.loc[df["volume_ratio"] >= cfg.min_volume_ratio].copy()
        counts["量比 ≥ 1"] = len(df)
    else:
        counts["量比待日线计算"] = len(df)

    safe_ratio = df["volume_ratio"].fillna(1.0)
    df["base_score"] = (
        25
        - (df["change_pct"] - 4).abs() * 4
        + (safe_ratio.clip(upper=3) - 1) * 5
        + (1 - (df["turnover"] - 7.5).abs() / 2.5).clip(lower=0) * 5
    )
    return df.sort_values("base_score", ascending=False), counts


def _numeric_history(raw: pd.DataFrame) -> pd.DataFrame:
    chinese = {"日期": "date", "开盘": "open", "收盘": "close", "最高": "high", "最低": "low", "成交量": "volume"}
    english = {"date": "date", "open": "open", "close": "close", "high": "high", "low": "low", "volume": "volume"}
    rename = chinese if set(chinese).issubset(raw.columns) else english
    if not set(rename).issubset(raw.columns):
        raise ValueError("历史行情字段不完整")
    df = raw.rename(columns=rename)[list(rename.values())].copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    for col in ("open", "close", "high", "low", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.dropna().sort_values("date")


def merge_live_daily_bar(
    raw: pd.DataFrame,
    spot_row: dict[str, Any],
    now: datetime | None = None,
) -> pd.DataFrame:
    """将实时快照合成当日临时日K。

    历史日线使用前复权口径时，以昨收与历史末日收盘的比例
    将实时价折算到同一口径，避免除权日直接拼接造成均线跳变。
    """
    now = now or datetime.now()
    df = _numeric_history(raw)
    if df.empty:
        return df
    dates = pd.to_datetime(df["date"], errors="coerce")
    today = now.date()
    has_today = bool(len(dates) and dates.iloc[-1].date() == today)
    reference_index = -2 if has_today and len(df) >= 2 else -1
    historical_prev_close = float(df.iloc[reference_index]["close"])
    spot_prev_close = _number(spot_row.get("prev_close"))
    scale = historical_prev_close / spot_prev_close if spot_prev_close > 0 else 1.0
    bar = {
        "date": pd.Timestamp(today),
        "open": _number(spot_row.get("open")) * scale,
        "close": _number(spot_row.get("price")) * scale,
        "high": _number(spot_row.get("high")) * scale,
        "low": _number(spot_row.get("low")) * scale,
        "volume": _number(spot_row.get("volume")),
    }
    if has_today:
        for column, value in bar.items():
            df.loc[df.index[-1], column] = value
    else:
        df = pd.concat([df, pd.DataFrame([bar])], ignore_index=True)
    return df


def analyze_daily(raw: pd.DataFrame, mode: str = "strict") -> dict[str, Any]:
    df = _numeric_history(raw)
    if len(df) < 65:
        return {"daily_ok": False, "volume_step": False, "ma_bull": False, "daily_reason": "日线不足60日"}

    for window in (5, 10, 20, 60):
        df[f"ma{window}"] = df["close"].rolling(window).mean()

    last = df.iloc[-1]
    prev5 = df.iloc[-6]
    ma_order = last["close"] >= last["ma5"] > last["ma10"] > last["ma20"] > last["ma60"]
    rising = {w: bool(last[f"ma{w}"] > prev5[f"ma{w}"]) for w in (5, 10, 20, 60)}
    ma_rising = all(rising.values())
    ma_strict = bool(ma_order and ma_rising)
    ma_rational = bool(
        last["close"] >= last["ma5"] > last["ma10"] > last["ma20"]
        and last["close"] > last["ma60"]
        and all(rising[w] for w in (5, 10, 20))
    )
    ma_bull = ma_strict if mode == "strict" else ma_rational

    volumes = df["volume"].tail(5).to_numpy(dtype=float)
    transitions = int(np.sum(np.diff(volumes[-4:]) > 0))
    slope = float(np.polyfit(np.arange(5), volumes, 1)[0])
    volume_step = bool(slope > 0 and transitions >= 2 and volumes[-1] >= np.mean(volumes) * 1.03)
    return {
        "daily_ok": True,
        "volume_step": volume_step,
        "ma_bull": ma_bull,
        "ma_strict": ma_strict,
        "ma_rational": ma_rational,
        "ma_order": bool(ma_order),
        "ma_all_rising": bool(ma_rising),
        "ma60_rising": rising[60],
        "close": float(last["close"]),
        "ma5": float(last["ma5"]),
        "ma10": float(last["ma10"]),
        "ma20": float(last["ma20"]),
        "ma60": float(last["ma60"]),
        "volume_slope": slope / max(float(np.mean(volumes)), 1),
        "daily_reason": "均线结构且放量通过" if ma_bull and volume_step else "均线或量能未确认",
    }


def analyze_minute(
    raw: pd.DataFrame,
    min_vwap_ratio: float = 0.70,
    market_return_pct: float | None = None,
) -> dict[str, Any]:
    if {"时间", "收盘", "成交量", "成交额"}.issubset(raw.columns):
        rename = {"时间": "time", "收盘": "close", "成交量": "volume", "成交额": "amount"}
    else:
        rename = {"day": "time", "close": "close", "volume": "volume", "amount": "amount"}
    if not set(rename).issubset(raw.columns):
        return {"minute_ok": False, "vwap_strong": False, "pullback_ok": False, "minute_reason": "分时字段不完整"}
    df = raw.rename(columns=rename)[list(rename.values())].copy()
    for col in ("close", "volume", "amount"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna()
    if len(df) < 30:
        return {"minute_ok": False, "vwap_strong": False, "pullback_ok": False, "minute_reason": "分时数据不足"}

    # 东方财富成交额/成交量口径偶有切换，以典型价格校准 VWAP。
    vwap = df["amount"].cumsum() / df["volume"].replace(0, np.nan).cumsum()
    median_ratio = float((vwap / df["close"]).median())
    if median_ratio > 20:
        vwap = vwap / 100
    ratio_above = float((df["close"] >= vwap).mean())
    tail = df.tail(min(30, len(df)))
    recent_high = float(tail["close"].max())
    last_price = float(df["close"].iloc[-1])
    first_price = float(df["close"].iloc[0])
    stock_return_pct = (last_price / first_price - 1) * 100 if first_price else 0.0
    last_vwap = float(vwap.iloc[-1])
    drawdown = (recent_high - last_price) / recent_high if recent_high else 1
    vwap_strong = ratio_above >= min_vwap_ratio and last_price >= last_vwap
    pullback_ok = last_price >= last_vwap and 0 <= drawdown <= 0.012
    relative_strong = market_return_pct is not None and stock_return_pct > market_return_pct
    return {
        "minute_ok": True,
        "vwap_strong": bool(vwap_strong),
        "pullback_ok": bool(pullback_ok),
        "relative_strong": bool(relative_strong),
        "stock_intraday_pct": stock_return_pct,
        "market_intraday_pct": market_return_pct,
        "vwap_ratio": ratio_above,
        "vwap": last_vwap,
        "recent_high": recent_high,
        "pullback_pct": drawdown * 100,
        "minute_reason": "均价线上方，尾盘回踩可控" if vwap_strong and pullback_ok else "分时强度不足",
    }


def finalize_candidate(row: dict[str, Any]) -> dict[str, Any]:
    checks = {
        "量比达标": bool(row.get("volume_ratio", 0) >= row.get("min_volume_ratio", 1.0)),
        "量能阶梯": bool(row.get("volume_step")),
        "均线结构": bool(row.get("ma_bull")),
        "分时强势": bool(row.get("vwap_strong")),
        "跑赢大盘": bool(row.get("relative_strong")),
        "回踩有效": bool(row.get("pullback_ok")),
    }
    hot = bool(row.get("hot_concepts"))
    change = _number(row.get("change_pct"))
    ratio = _number(row.get("volume_ratio"))
    turnover = _number(row.get("turnover"))
    cap = _number(row.get("float_cap_yi"))
    volume_slope = _number(row.get("volume_slope"))
    ma5 = _number(row.get("ma5"))
    ma60 = _number(row.get("ma60"))
    vwap_ratio = _number(row.get("vwap_ratio"))
    stock_return = _number(row.get("stock_intraday_pct"))
    market_return = _number(row.get("market_intraday_pct"))
    pullback = _number(row.get("pullback_pct"))

    hard_fit = (
        6 * _clamp(1 - abs(change - 4.0) / 1.0)
        + 5 * _clamp((ratio - 1.0) / 1.5)
        + 5 * _clamp(1 - abs(turnover - 7.5) / 2.5)
        + 4 * _clamp(1 - abs(cap - 125.0) / 75.0)
        + (5 if checks["量比达标"] else 0)
    )
    volume_fit = (8 if checks["量能阶梯"] else 0) + 7 * _clamp(volume_slope / 0.14)
    ma_spread = (ma5 / ma60 - 1) if ma60 > 0 else 0
    ma_fit = (
        (10 if checks["均线结构"] else 0)
        + (2 if row.get("ma60_rising") else 0)
        + 8 * _clamp(ma_spread / 0.08)
    )
    intraday_fit = (
        10 * _clamp((vwap_ratio - 0.70) / 0.30)
        + 8 * _clamp((stock_return - market_return) / 2.0)
        + (7 * _clamp(1 - abs(pullback - 0.4) / 0.8) if checks["回踩有效"] else 0)
    )
    theme_fit = 10 if hot else 0
    data_fit = 5 if row.get("daily_ok") and row.get("minute_ok") else 0
    breakdown = {
        "硬条件贴合": round(hard_fit, 1),
        "量能延续": round(volume_fit, 1),
        "均线结构": round(ma_fit, 1),
        "分时强度": round(intraday_fit, 1),
        "热点题材": round(theme_fit, 1),
        "数据完整": round(data_fit, 1),
    }
    score = sum(breakdown.values())
    row["score"] = min(round(score, 1), 100)
    row["score_breakdown"] = breakdown
    component_caps = {"硬条件贴合": 25, "量能延续": 15, "均线结构": 20, "分时强度": 25, "热点题材": 10}
    strengths = sorted(
        (label for label in component_caps if breakdown[label] > 0),
        key=lambda label: breakdown[label] / component_caps[label],
        reverse=True,
    )
    row["rank_reason"] = " · ".join(strengths[:3]) or "暂无明显优势"
    row["passed"] = all(checks.values())
    row["status"] = "候选" if row["passed"] else "观察"
    row["checks"] = checks
    row["failed_reasons"] = "、".join(k for k, v in checks.items() if not v) or "无"
    return row


def _number(value: Any) -> float:
    try:
        number = float(value)
        return number if np.isfinite(number) else 0.0
    except (TypeError, ValueError):
        return 0.0


def _clamp(value: float, lower: float = 0.0, upper: float = 1.0) -> float:
    return min(max(value, lower), upper)


def estimate_volume_ratio(
    current_volume: float,
    raw_history: pd.DataFrame,
    now: datetime | None = None,
) -> float | None:
    """用今日累计成交量与近5日同期预期成交量估算量比。"""
    now = now or datetime.now()
    try:
        df = _numeric_history(raw_history)
    except ValueError:
        return None
    dates = pd.to_datetime(df["date"], errors="coerce")
    if len(df) and dates.iloc[-1].date() == now.date():
        df = df.iloc[:-1]
    volumes = pd.to_numeric(df["volume"], errors="coerce").dropna().tail(5)
    if volumes.empty or volumes.mean() <= 0:
        return None

    t = now.time()
    if t < time(9, 30):
        elapsed = 240
    elif t <= time(11, 30):
        elapsed = max(1, (now.hour * 60 + now.minute) - (9 * 60 + 30))
    elif t < time(13, 0):
        elapsed = 120
    elif t <= time(15, 0):
        elapsed = 120 + (now.hour * 60 + now.minute) - 13 * 60
    else:
        elapsed = 240
    expected_so_far = float(volumes.mean()) * min(max(elapsed, 1), 240) / 240
    return float(current_volume / expected_so_far) if expected_so_far > 0 else None


def minute_return_pct(raw: pd.DataFrame) -> float | None:
    if raw is None or raw.empty:
        return None
    close_col = "收盘" if "收盘" in raw.columns else "close" if "close" in raw.columns else None
    if close_col is None:
        return None
    values = pd.to_numeric(raw[close_col], errors="coerce").dropna()
    if len(values) < 2 or values.iloc[0] == 0:
        return None
    return float((values.iloc[-1] / values.iloc[0] - 1) * 100)


def market_session_status(now: datetime | None = None) -> tuple[str, str]:
    now = now or datetime.now()
    t = now.time()
    if now.weekday() >= 5:
        return "休市", "周末仅适合查看演示或最近数据"
    if time(14, 30) <= t <= time(15, 0):
        return "筛选窗口", "当前处于策略设计的 14:30–15:00 观察窗口"
    if time(9, 30) <= t < time(14, 30):
        return "盘中", "建议 14:30 后再形成最终候选"
    return "非交易时段", "实时快照可能显示最近一个交易日收盘数据"
