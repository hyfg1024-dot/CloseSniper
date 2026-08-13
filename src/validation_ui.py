from __future__ import annotations

from datetime import datetime, time

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from src.data_source import AkshareSource
from src.validation_service import capture_open_pending, validate_pending
from src.validation_store import ValidationStore


def render_validation_page(store: ValidationStore) -> None:
    st.markdown(
        """
        <div class="audit-head">
          <span class="eyebrow">NEXT SESSION · 09:45 / 10:30 AUDIT</span>
          <h2>次日校验</h2>
          <p>从开盘到早盘中段分段留痕：9:30 验证原策略，9:45 与 10:30 观察收益能否延续。</p>
        </div>
        """,
        unsafe_allow_html=True,
    )
    now = datetime.now()
    pending_rows = store.pending_signals(now.date().isoformat())
    frame = store.validation_frame()
    should_auto = any(
        (
            row["price_0945"] is None
            and now.time() >= time(9, 45)
        )
        or (
            row["price_0945"] is not None
            and row["price_1030"] is None
            and now.time() >= time(10, 30)
        )
        for row in pending_rows
    )
    early_window = time(9, 30) <= now.time() < time(9, 51)
    if not early_window:
        early_label = "早盘获取窗口"
    elif now.time() < time(9, 45):
        early_label = "获取开盘快照"
    else:
        early_label = "完成早盘校验"
    early_col, backfill_col = st.columns([1.35, 1])
    early_update = early_col.button(
        f"{early_label} · 9:30–9:50",
        type="primary",
        width="stretch",
        disabled=not early_window,
        help="9:45前保存开盘和临时收益；9:45后同时完成固定口径校验。",
    )
    backfill = backfill_col.button("补算缺失数据", width="stretch")
    if not early_window:
        st.caption("早盘按钮仅在交易日 9:30–9:50 开放；错过后可使用“补算缺失数据”。")

    if early_update or backfill or should_auto:
        with st.status("正在读取次日 9:30–10:30 行情…", expanded=True) as status:
            try:
                source = AkshareSource()
                if early_update:
                    open_summary = capture_open_pending(store, source, now)
                    status.write(
                        f"开盘快照 {open_summary['captured']} 只，"
                        f"等待数据 {open_summary['skipped']} 只，"
                        f"异常 {len(open_summary['errors'])} 只"
                    )
                if backfill or should_auto or (early_update and now.time() >= time(9, 45)):
                    summary = validate_pending(store, source, now)
                    status.write(
                        f"写入 9:45 数据 {summary['completed_0945']} 只，"
                        f"补齐 10:30 数据 {summary['completed_1030']} 只，"
                        f"等待数据 {summary['skipped']} 只，异常 {len(summary['errors'])} 只"
                    )
                status.update(label="校验更新完成", state="complete", expanded=False)
                frame = store.validation_frame()
                pending_rows = store.pending_signals(now.date().isoformat())
            except Exception as exc:
                status.update(label="校验暂未完成", state="error")
                st.error(f"校验行情读取失败：{exc}")

    pending = len(pending_rows)
    open_captured = frame["open_return"].notna().sum() if not frame.empty else 0
    completed_0945 = frame["price_0945"].notna().sum() if not frame.empty else 0
    completed_1030 = frame["price_1030"].notna().sum() if not frame.empty else 0
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("待完成", int(pending))
    c2.metric("开盘已取", int(open_captured))
    c3.metric("已到 9:45", int(completed_0945))
    c4.metric("已到 10:30", int(completed_1030))
    st.caption(f"累计冻结信号：{len(frame)} 只")

    if frame.empty:
        st.info("尚无冻结候选。交易日完成14:30、14:45、14:52三阶段扫描后，系统会冻结综合最终名单。")
        _render_definition()
        return

    target = st.slider("冲高命中阈值 (%)", 0.5, 5.0, 1.0, 0.5)
    ready = frame.dropna(subset=["validation_date"]).copy()
    if ready.empty:
        st.info("已有候选存档，等待下一个交易日 9:30 后获取开盘结果。")
    else:
        ready["15分钟命中"] = _hits_target(ready["max_return"], target)
        ready["60分钟命中"] = _hits_target(ready["max_return_1030"], target)
        ready["9:45跑赢"] = _beats_index(ready["return_0945"], ready["index_0945_return"])
        ready["10:30跑赢"] = _beats_index(ready["return_1030"], ready["index_1030_return"])
        display = ready.rename(
            columns={
                "signal_date": "信号日", "validation_date": "校验日", "code": "代码",
                "name": "名称", "entry_price": "信号价", "score": "综合评分",
                "score_1430": "14:30评分", "score_1445": "14:45评分",
                "score_1452": "14:52评分", "persistence": "持续性",
                "open_return": "9:30涨跌", "captured_return": "抓取时涨跌",
                "open_captured_at": "抓取时间", "return_0945": "9:45涨跌",
                "return_1030": "10:30涨跌",
                "max_return": "15分钟最高涨幅", "max_drawdown": "15分钟最低涨跌",
                "max_return_1030": "60分钟最高涨幅", "max_drawdown_1030": "60分钟最低涨跌",
                "index_0945_return": "指数9:45涨跌", "index_1030_return": "指数10:30涨跌",
            }
        )
        columns = [
            "信号日", "校验日", "代码", "名称", "综合评分", "持续性",
            "14:30评分", "14:45评分", "14:52评分", "信号价", "9:30涨跌",
            "抓取时涨跌", "抓取时间", "9:45涨跌", "10:30涨跌",
            "15分钟最高涨幅", "60分钟最高涨幅",
            "60分钟最低涨跌", "指数10:30涨跌", "15分钟命中", "60分钟命中",
            "9:45跑赢", "10:30跑赢",
        ]
        return_columns = [
            "9:30涨跌", "抓取时涨跌", "9:45涨跌", "10:30涨跌",
            "15分钟最高涨幅", "60分钟最高涨幅", "60分钟最低涨跌", "指数10:30涨跌",
        ]
        styled_display = (
            display[columns]
            .style
            .map(_return_color, subset=return_columns)
            .format({column: _format_return for column in return_columns}, na_rep="—")
        )
        st.dataframe(
            styled_display,
            hide_index=True,
            width="stretch",
        )
    _render_definition()


def render_history_page(store: ValidationStore) -> None:
    st.markdown(
        """
        <div class="audit-head">
          <span class="eyebrow">ROLLING EVIDENCE · NO HINDSIGHT</span>
          <h2>历史表现</h2>
          <p>只统计当日冻结的候选；零候选交易日同样保留，避免事后筛选。</p>
        </div>
        """,
        unsafe_allow_html=True,
    )
    frame = store.validation_frame().dropna(subset=["price_0945"]).copy()
    scans = store.scan_frame()
    if frame.empty:
        st.info("累计完成至少一个次日校验后，这里会展示胜率、收益分布和滚动表现。")
        if not scans.empty:
            st.markdown("### 已冻结扫描")
            st.dataframe(scans, hide_index=True, width="stretch")
        return

    win_open = float((frame["open_return"] > 0).mean() * 100)
    win_0945 = float((frame["return_0945"] > 0).mean() * 100)
    win_1030 = _positive_rate(frame["return_1030"])
    late = frame["max_return_1030"].dropna()
    hit = float((late >= 1.0).mean() * 100) if not late.empty else None
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("9:30胜率", f"{win_open:.1f}%")
    m2.metric("9:45胜率", f"{win_0945:.1f}%")
    m3.metric("10:30胜率", f"{win_1030:.1f}%" if win_1030 is not None else "—")
    m4.metric("60分钟冲高≥1%", f"{hit:.1f}%" if hit is not None else "—")

    daily = (
        frame.groupby("signal_date", as_index=False)
        .agg(
            open_return=("open_return", "mean"),
            return_0945=("return_0945", "mean"),
            return_1030=("return_1030", "mean"),
        )
        .sort_values("signal_date")
    )
    figure = go.Figure()
    figure.add_trace(
        go.Scatter(x=daily["signal_date"], y=daily["open_return"], name="9:30平均收益", line={"color": "#f05a28", "width": 3})
    )
    figure.add_trace(
        go.Scatter(x=daily["signal_date"], y=daily["return_0945"], name="9:45平均收益", line={"color": "#315b45", "width": 3})
    )
    figure.add_trace(
        go.Scatter(x=daily["signal_date"], y=daily["return_1030"], name="10:30平均收益", line={"color": "#172033", "width": 3})
    )
    figure.add_hline(y=0, line_color="#8f918a", line_dash="dot")
    figure.update_layout(
        height=380,
        margin=dict(l=10, r=10, t=20, b=10),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(255,255,255,.25)",
        yaxis_title="收益率 (%)",
        xaxis_title=None,
        legend={"orientation": "h", "y": 1.08},
    )
    st.plotly_chart(figure, width="stretch", config={"displayModeBar": False})

    st.markdown("### 交易日档案")
    st.dataframe(scans, hide_index=True, width="stretch")


def _render_definition() -> None:
    with st.expander("校验口径"):
        st.markdown(
            """
- **信号价**：14:52最终扫描时的实时价格，不事后改写。
- **综合评分**：20% × 14:30评分 + 30% × 14:45评分 + 50% × 14:52评分；未入选节点按0分计算。
- **9:30收益**：次一实际交易日首分钟开盘价相对信号价的收益，对应原策略退出。
- **抓取时收益**：9:30–9:50 点击早盘按钮时的最新分钟价相对信号价收益，仅作现场参考，不进入固定口径历史统计。
- **9:45收益**：截至 9:45 分钟线收盘价相对信号价的收益，仅作对照。
- **10:30收益**：截至 10:30 分钟线收盘价相对信号价的收益，用于观察早盘强势能否延续。
- **15分钟最高**：9:30–9:45 区间最高价相对信号价的收益。
- **60分钟最高 / 回撤**：9:30–10:30 区间最高价、最低价相对信号价的收益。
- 9:45前点击只保存开盘快照；9:45后点击会完成固定口径校验并进入“历史表现”。
- 停牌或免费行情尚未完整到达时保持“待校验”，之后自动补算。
"""
        )


def _positive_rate(series: pd.Series) -> float | None:
    values = series.dropna()
    return float((values > 0).mean() * 100) if not values.empty else None


def _return_color(value: object) -> str:
    """A股行情配色：上涨红、下跌绿，零值与缺失保持中性。"""
    if pd.isna(value):
        return ""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    if number > 0:
        return "color:#c92f2f;background-color:rgba(201,47,47,.10);font-weight:700"
    if number < 0:
        return "color:#16835d;background-color:rgba(22,131,93,.10);font-weight:700"
    return "color:#656b66;font-weight:600"


def _format_return(value: object) -> str:
    """将涨跌幅显示为带方向符号和百分号的行情格式。"""
    if pd.isna(value):
        return "—"
    try:
        number = float(value)
        return "0.00%" if number == 0 else f"{number:+.2f}%"
    except (TypeError, ValueError):
        return "—"


def _beats_index(returns: pd.Series, index_returns: pd.Series) -> pd.Series:
    available = returns.notna() & index_returns.notna()
    result = pd.Series(pd.NA, index=returns.index, dtype="boolean")
    result.loc[available] = returns.loc[available] > index_returns.loc[available]
    return result


def _hits_target(returns: pd.Series, target: float) -> pd.Series:
    available = returns.notna()
    result = pd.Series(pd.NA, index=returns.index, dtype="boolean")
    result.loc[available] = returns.loc[available] >= target
    return result
