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


def run_market_scan(
    cfg: StrategyConfig,
    *,
    mode: str = "rational",
    use_demo: bool = False,
    now: datetime | None = None,
    progress: Progress | None = None,
) -> ScanResult:
    """执行一次完整扫描，供 Streamlit 页面与后台定时任务共同调用。"""
    now = now or datetime.now()
    report = progress or (lambda _message: None)
    raw_spot = demo_spot() if use_demo else AkshareSource().spot()
    spot = normalize_spot(raw_spot)
    stage1, funnel = hard_filter(spot, cfg)
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
        source = AkshareSource()
        daily_map, daily_errors = parallel_fetch(codes, source.daily)
        report(f"日线完成 {len(daily_map)}/{len(codes)}")
        # 新浪分钟解码器在 macOS 下并发可能触发进程级崩溃。
        minute_map, minute_errors = parallel_fetch(codes, source.minute, max_workers=1)
        report(f"分时完成 {len(minute_map)}/{len(codes)}")
        errors = {**daily_errors, **minute_errors}
        index_minutes = source.index_minute()
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
