from __future__ import annotations

from datetime import datetime, time

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from src.data_source import AkshareSource
from src.validation_service import validate_pending
from src.validation_store import ValidationStore


def render_validation_page(store: ValidationStore) -> None:
    st.markdown(
        """
        <div class="audit-head">
          <span class="eyebrow">NEXT SESSION · 09:45 AUDIT</span>
          <h2>次日校验</h2>
          <p>9:30 是原策略成绩；9:45 是持有十五分钟的对照成绩。两套口径分开记录。</p>
        </div>
        """,
        unsafe_allow_html=True,
    )
    now = datetime.now()
    pending = store.pending_count(now.date().isoformat())
    frame = store.validation_frame()
    completed = frame["validation_date"].notna().sum() if not frame.empty else 0
    c1, c2, c3 = st.columns(3)
    c1.metric("待校验", int(pending))
    c2.metric("已校验", int(completed))
    c3.metric("冻结信号", len(frame))

    should_auto = pending > 0 and now.time() >= time(9, 45)
    update = st.button("立即更新校验", type="primary", width="stretch")
    if update or should_auto:
        with st.status("正在读取次日 9:30–9:45 行情…", expanded=True) as status:
            try:
                summary = validate_pending(store, AkshareSource(), now)
                status.write(
                    f"完成 {summary['completed']} 只，等待数据 {summary['skipped']} 只，异常 {len(summary['errors'])} 只"
                )
                status.update(label="校验更新完成", state="complete", expanded=False)
                frame = store.validation_frame()
            except Exception as exc:
                status.update(label="校验暂未完成", state="error")
                st.error(f"校验行情读取失败：{exc}")

    if frame.empty:
        st.info("尚无冻结候选。交易日 14:30–15:00 完成首次真实扫描后，系统会自动存档。")
        _render_definition()
        return

    target = st.slider("冲高命中阈值 (%)", 0.5, 5.0, 1.0, 0.5)
    ready = frame.dropna(subset=["validation_date"]).copy()
    if ready.empty:
        st.info("已有候选存档，等待下一个交易日 9:45 后生成结果。")
    else:
        ready["冲高命中"] = ready["max_return"] >= target
        ready["跑赢指数"] = ready["return_0945"] > ready["index_0945_return"]
        display = ready.rename(
            columns={
                "signal_date": "信号日", "validation_date": "校验日", "code": "代码",
                "name": "名称", "entry_price": "信号价", "score": "评分",
                "open_return": "9:30收益%", "return_0945": "9:45收益%",
                "max_return": "最高收益%", "max_drawdown": "最大回撤%",
                "index_0945_return": "指数9:45%",
            }
        )
        columns = [
            "信号日", "校验日", "代码", "名称", "评分", "信号价", "9:30收益%",
            "9:45收益%", "最高收益%", "最大回撤%", "指数9:45%", "冲高命中", "跑赢指数",
        ]
        st.dataframe(
            display[columns],
            hide_index=True,
            width="stretch",
            column_config={
                "9:30收益%": st.column_config.NumberColumn(format="%.2f"),
                "9:45收益%": st.column_config.NumberColumn(format="%.2f"),
                "最高收益%": st.column_config.NumberColumn(format="%.2f"),
                "最大回撤%": st.column_config.NumberColumn(format="%.2f"),
                "指数9:45%": st.column_config.NumberColumn(format="%.2f"),
            },
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
    frame = store.validation_frame().dropna(subset=["validation_date"]).copy()
    scans = store.scan_frame()
    if frame.empty:
        st.info("累计完成至少一个次日校验后，这里会展示胜率、收益分布和滚动表现。")
        if not scans.empty:
            st.markdown("### 已冻结扫描")
            st.dataframe(scans, hide_index=True, width="stretch")
        return

    win_open = float((frame["open_return"] > 0).mean() * 100)
    win_0945 = float((frame["return_0945"] > 0).mean() * 100)
    hit = float((frame["max_return"] >= 1.0).mean() * 100)
    median = float(frame["open_return"].median())
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("9:30胜率", f"{win_open:.1f}%")
    m2.metric("9:45胜率", f"{win_0945:.1f}%")
    m3.metric("冲高≥1%", f"{hit:.1f}%")
    m4.metric("开盘中位收益", f"{median:+.2f}%")

    daily = (
        frame.groupby("signal_date", as_index=False)
        .agg(open_return=("open_return", "mean"), return_0945=("return_0945", "mean"))
        .sort_values("signal_date")
    )
    figure = go.Figure()
    figure.add_trace(
        go.Scatter(x=daily["signal_date"], y=daily["open_return"], name="9:30平均收益", line={"color": "#f05a28", "width": 3})
    )
    figure.add_trace(
        go.Scatter(x=daily["signal_date"], y=daily["return_0945"], name="9:45平均收益", line={"color": "#315b45", "width": 3})
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
- **信号价**：14:30–15:00 首次成功扫描时冻结的实时价格，不事后改写。
- **9:30收益**：次一实际交易日首分钟开盘价相对信号价的收益，对应原策略退出。
- **9:45收益**：截至 9:45 分钟线收盘价相对信号价的收益，仅作对照。
- **最高收益 / 最大回撤**：9:30–9:45 区间最高价和最低价相对信号价。
- 停牌或免费行情尚未完整到达时保持“待校验”，之后自动补算。
"""
        )

