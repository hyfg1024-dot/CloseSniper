from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable
from copy import deepcopy

import pandas as pd

from src.data_source import (
    AkshareSource,
    MarketDataError,
    demo_daily,
    demo_index_minute,
    demo_minute,
    demo_spot,
    parallel_fetch,
)
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


Progress = Callable[[str], None]


def _cutoff_intraday(frame: pd.DataFrame, cutoff_at: datetime) -> pd.DataFrame:
    if frame.empty:
        return frame
    time_column = "day" if "day" in frame.columns else "时间" if "时间" in frame.columns else None
    if time_column is None:
        return frame
    timestamps = pd.to_datetime(frame[time_column], errors="coerce")
    return frame.loc[timestamps <= pd.Timestamp(cutoff_at)].copy()


@dataclass
class ScanResult:
    raw_spot: pd.DataFrame
    result_frame: pd.DataFrame
    funnel: dict[str, int]
    hard_count: int
    provider: str
    errors: dict[str, str]

    @property
    def candidates(self) -> pd.DataFrame:
        return self.result_frame[self.result_frame["passed"] == True].copy()  # noqa: E712


def derive_strict_frame(rational_frame: pd.DataFrame) -> pd.DataFrame:
    """复用同一份行情分析，将改进流程切换回未经改动的严格标准。"""
    if rational_frame.empty:
        return rational_frame.copy()
    rows: list[dict[str, Any]] = []
    for source in rational_frame.to_dict("records"):
        row = deepcopy(source)
        row["ma_bull"] = bool(row.get("ma_strict"))
        row["apply_improved_risk"] = False
        rows.append(finalize_candidate(row))
    frame = pd.DataFrame(rows).sort_values(["passed", "score"], ascending=[False, False])
    frame["strategy_rank"] = range(1, len(frame) + 1)
    return frame


def run_frozen_candidate_scan(
    cfg: StrategyConfig,
    prior_candidates: pd.DataFrame,
    *,
    now: datetime,
) -> ScanResult:
    """基于14:45已确认候选，仅重算尾盘七分钟内会变化的实时条件。"""
    if prior_candidates.empty:
        return ScanResult(
            raw_spot=pd.DataFrame(), result_frame=pd.DataFrame(),
            funnel={"全市场": 0}, hard_count=0,
            provider="14:45候选池为空", errors={},
        )
    source = AkshareSource()
    codes = prior_candidates["code"].astype(str).str.zfill(6).tolist()
    raw_spot = source.fast_spot(codes)
    quote_dates = set(raw_spot["报价时间"].astype(str).str[:8])
    expected_date = now.strftime("%Y%m%d")
    if quote_dates != {expected_date}:
        raise MarketDataError(f"候选报价不是当日数据：{sorted(quote_dates)}")
    market_return, index_time = source.fast_index_return()
    if not str(index_time).startswith(expected_date):
        raise MarketDataError("上证指数报价不是当日数据，今日不生成冻结决策")

    spot = normalize_spot(raw_spot)
    filtered, funnel = hard_filter(spot, cfg)
    prior = prior_candidates.copy()
    prior["code"] = prior["code"].astype(str).str.zfill(6)
    prior_map = prior.set_index("code").to_dict("index")
    rows: list[dict[str, Any]] = []

    def hard_fit(change: float, ratio: float, turnover: float) -> float:
        return (
            25 - abs(change - 4) * 4
            + (min(ratio, 3) - 1) * 5
            + max(0, 1 - abs(turnover - 7.5) / 2.5) * 5
        )

    for live in filtered.to_dict("records"):
        code = str(live["code"])
        old = prior_map.get(code)
        if old is None:
            continue
        price = float(live["price"])
        volume = float(live["volume"])
        amount = float(live["amount"])
        vwap = amount / volume if volume > 0 else float("nan")
        high = float(live["high"])
        pullback = (high - price) / high if high > 0 else 1.0
        vwap_ok = pd.notna(vwap) and price >= vwap
        relative_ok = float(live["change_pct"]) > market_return
        pullback_ok = vwap_ok and 0 <= pullback <= 0.012
        if not (vwap_ok and relative_ok and pullback_ok):
            continue

        old_fit = hard_fit(
            float(old.get("change_pct") or 0),
            float(old.get("volume_ratio") or 0),
            float(old.get("turnover") or 0),
        )
        new_fit = hard_fit(
            float(live["change_pct"]), float(live["volume_ratio"]), float(live["turnover"]),
        )
        score = max(0.0, min(100.0, float(old["score"]) + new_fit - old_fit))
        rows.append({
            "passed": True,
            "status": "符合冻结条件",
            "code": code,
            "name": live["name"],
            "price": price,
            "score": round(score, 1),
            "change_pct": float(live["change_pct"]),
            "volume_ratio": float(live["volume_ratio"]),
            "turnover": float(live["turnover"]),
            "float_cap_yi": float(live["float_cap_yi"]),
            "vwap": float(vwap),
            "market_intraday_pct": market_return,
            "pullback_pct": pullback * 100,
        })
    frame = pd.DataFrame(rows, columns=[
        "passed", "status", "code", "name", "price", "score", "change_pct",
        "volume_ratio", "turnover", "float_cap_yi", "vwap",
        "market_intraday_pct", "pullback_pct",
    ])
    if not frame.empty:
        frame = frame.sort_values("score", ascending=False).reset_index(drop=True)
        frame["strategy_rank"] = range(1, len(frame) + 1)
    return ScanResult(
        raw_spot=raw_spot,
        result_frame=frame,
        funnel=funnel,
        hard_count=len(filtered),
        provider=str(raw_spot.attrs.get("provider", "腾讯候选池增量报价")),
        errors={},
    )


def run_market_scan(
    cfg: StrategyConfig,
    *,
    mode: str = "rational",
    use_demo: bool = False,
    now: datetime | None = None,
    progress: Progress | None = None,
    restrict_codes: set[str] | None = None,
    prefer_tencent: bool = False,
    cutoff_at: datetime | None = None,
) -> ScanResult:
    """执行一次完整扫描，供 Streamlit 页面与后台定时任务共同调用。"""
    now = now or datetime.now()
    report = progress or (lambda _message: None)
    source = None if use_demo else AkshareSource()
    raw_spot = (
        demo_spot()
        if use_demo
        else source._tencent_spot() if prefer_tencent else source.spot()
    )
    spot = normalize_spot(raw_spot)
    stage1, funnel = hard_filter(spot, cfg)
    if restrict_codes is not None:
        stage1 = stage1[stage1["code"].isin({str(code).zfill(6) for code in restrict_codes})].copy()
    volume_ratio_missing = not stage1["volume_ratio"].notna().any()
    analysis_limit = min(len(stage1), max(cfg.max_candidates, 80)) if volume_ratio_missing else cfg.max_candidates
    selected = stage1.head(analysis_limit).copy()
    report(f"硬条件通过 {len(stage1)} 只；取前 {len(selected)} 只做日线与分时确认")

    codes = selected["code"].tolist()
    if use_demo:
        daily_map = {code: demo_daily(code) for code in codes}
        minute_map = {code: demo_minute(code) for code in codes}
        errors: dict[str, str] = {}
        concept_map = {"人工智能": codes[:3], "先进制造": codes[2:6]}
        market_return = minute_return_pct(demo_index_minute())
    else:
        assert source is not None
        daily_map, daily_errors = parallel_fetch(codes, source.daily)
        report(f"日线完成 {len(daily_map)}/{len(codes)}")
        if prefer_tencent and cutoff_at is not None:
            # 冻结决策版只处理 14:45 留下的少量股票，并用带硬超时的
            # 东财分钟接口并发读取，避免新浪链路逐只阻塞到收盘以后。
            minute_map, minute_errors = parallel_fetch(
                codes,
                lambda code: source.minute_fast(code, cutoff_at),
                max_workers=min(4, max(1, len(codes))),
            )
        else:
            # 完整重扫复核版保留原架构。新浪分钟解码器在 macOS 下并发
            # 可能触发进程级崩溃，因此仍按原先方式串行读取。
            minute_map, minute_errors = parallel_fetch(codes, source.minute, max_workers=1)
        if cutoff_at is not None:
            minute_map = {
                code: _cutoff_intraday(frame, cutoff_at)
                for code, frame in minute_map.items()
            }
        report(f"分时完成 {len(minute_map)}/{len(codes)}")
        errors = {**daily_errors, **minute_errors}
        index_minutes = (
            source.index_minute_fast(cutoff_at)
            if prefer_tencent and cutoff_at is not None
            else source.index_minute()
        )
        if cutoff_at is not None:
            index_minutes = _cutoff_intraday(index_minutes, cutoff_at)
        if index_minutes.empty:
            raise MarketDataError("未取得当日指数分钟行情，无法确认今天为有效交易日")
        market_return = minute_return_pct(index_minutes)
        if market_return is None:
            raise MarketDataError("当日指数分钟行情不完整，暂不生成策略结果")
        if "新浪" in str(raw_spot.attrs.get("provider", "")):
            concept_map = {}
            report("当前使用新浪主链路，本轮热点题材暂不计分")
        else:
            try:
                concept_map = source.hot_concept_members(6)
            except Exception:
                concept_map = {}
                report("热点板块接口暂不可用，本轮不计题材加分")

    results: list[dict[str, Any]] = []
    for row in selected.to_dict("records"):
        code = row["code"]
        if pd.isna(row.get("volume_ratio")) and code in daily_map:
            row["volume_ratio"] = estimate_volume_ratio(row["volume"], daily_map[code], now=now)
        if pd.isna(row.get("volume_ratio")):
            row["volume_ratio"] = 0.0
        row["min_volume_ratio"] = cfg.min_volume_ratio
        if code in daily_map:
            live_daily = merge_live_daily_bar(daily_map[code], row, now=now)
            daily_result = analyze_daily(
                live_daily,
                mode=mode,
                max_10d_return_pct=cfg.max_10d_return_pct,
                max_ma20_distance_pct=cfg.max_ma20_distance_pct,
                max_recent_daily_gain_pct=cfg.max_recent_daily_gain_pct,
            )
        else:
            daily_result = {"volume_step": False, "ma_bull": False}
        minute_result = (
            analyze_minute(minute_map[code], cfg.min_vwap_ratio, market_return)
            if code in minute_map
            else {"vwap_strong": False, "relative_strong": False, "pullback_ok": False}
        )
        concepts = [name for name, members in concept_map.items() if code in members]
        results.append(finalize_candidate({
            **row,
            **daily_result,
            **minute_result,
            "hot_concepts": concepts,
            "apply_improved_risk": mode != "strict",
        }))

    if results:
        frame = pd.DataFrame(results).sort_values(["passed", "score"], ascending=[False, False])
        frame["strategy_rank"] = range(1, len(frame) + 1)
    else:
        frame = pd.DataFrame(columns=[
            "strategy_rank", "passed", "status", "code", "name", "score", "price",
            "change_pct", "volume_ratio", "turnover", "float_cap_yi", "volume_step",
            "ma_bull", "vwap_strong", "relative_strong", "pullback_ok", "hot_concepts",
            "failed_reasons", "rank_reason",
        ])
    return ScanResult(
        raw_spot=raw_spot,
        result_frame=frame,
        funnel=funnel,
        hard_count=len(stage1),
        provider=str(raw_spot.attrs.get("provider", "免费行情")),
        errors=errors,
    )
