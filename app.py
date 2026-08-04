from __future__ import annotations

import json
from datetime import datetime, time

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

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
    market_session_status,
    minute_return_pct,
    normalize_spot,
)
from src.validation_store import ValidationStore
from src.validation_ui import render_history_page, render_validation_page


st.set_page_config(page_title="尾盘雷达", page_icon="◉", layout="wide", initial_sidebar_state="expanded")

st.markdown(
    """
<style>
@import url('https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500&family=Noto+Serif+SC:wght@600;800&display=swap');
:root { --ink:#17211c; --paper:#f3f0e6; --signal:#f05a28; --moss:#315b45; --line:#c8c3b5; }
.stApp {
  background-color:var(--paper);
  background-image:linear-gradient(rgba(23,33,28,.035) 1px,transparent 1px),
  linear-gradient(90deg,rgba(23,33,28,.035) 1px,transparent 1px);
  background-size:24px 24px;color:var(--ink);
}
html, body, [class*="css"] { font-family:"DM Mono","PingFang SC",monospace; }
h1,h2,h3 { font-family:"Noto Serif SC","Songti SC",serif !important; letter-spacing:-.035em; }
[data-testid="stSidebar"] {
  background:#e7e2d4;
  border-right:1px solid var(--line);
  color:var(--ink);
}
[data-testid="stSidebar"] h1,
[data-testid="stSidebar"] h2,
[data-testid="stSidebar"] h3,
[data-testid="stSidebar"] p,
[data-testid="stSidebar"] label,
[data-testid="stSidebar"] small,
[data-testid="stSidebar"] summary {
  color:var(--ink) !important;
  -webkit-text-fill-color:var(--ink);
}
[data-testid="stSidebar"] [data-testid="stCaptionContainer"] p {
  color:#596159 !important;
  -webkit-text-fill-color:#596159;
}
[data-testid="stSidebar"] [data-baseweb="input"] {
  background:#fbfaf5 !important;
  border:1px solid #aaa597;
}
[data-testid="stSidebar"] input {
  color:var(--ink) !important;
  -webkit-text-fill-color:var(--ink) !important;
}
[data-testid="stSidebar"] [data-testid="stNumberInput"] button {
  color:var(--ink) !important;
  background:transparent !important;
}
[data-testid="stSidebar"] [data-testid="stExpander"] {
  background:rgba(255,255,255,.42);
}
[data-testid="stSidebar"] [data-testid="stExpander"] summary {
  background:#f7f3e9;
}
[data-testid="stMetric"] { background:rgba(255,255,255,.43);border:1px solid var(--line);padding:16px;border-radius:2px; }
.hero { padding:22px 0 30px;border-bottom:2px solid var(--ink);margin-bottom:22px;position:relative; }
.hero:after { content:"14:30";position:absolute;right:0;top:-24px;font:800 88px/1 "Noto Serif SC";color:rgba(23,33,28,.06); }
.eyebrow { color:var(--signal);font-weight:500;letter-spacing:.14em;text-transform:uppercase; }
.hero h1 { font-size:58px;margin:.08em 0; }
.hero p { max-width:780px;font-size:15px;line-height:1.8; }
.badge { display:inline-block;padding:5px 10px;border:1px solid var(--ink);margin-right:8px;font-size:12px; }
.badge.live { color:white;background:var(--moss);border-color:var(--moss); }
.card { background:rgba(255,255,255,.5);border:1px solid var(--line);padding:18px;margin:8px 0;box-shadow:5px 5px 0 rgba(23,33,28,.08); }
.candidate { border-left:5px solid var(--signal); }
.audit-head { border-left:6px solid var(--signal);padding:8px 0 8px 20px;margin:22px 0 26px; }
.audit-head h2 { font-size:40px;margin:4px 0 8px; }
.audit-head p { color:#596159;margin:0;max-width:760px; }
.muted { color:#6f756e;font-size:12px; }
.stButton>button { border-radius:1px;background:var(--ink);color:var(--paper);border:0;font-weight:600;min-height:44px; }
.stButton>button:hover { background:var(--signal);color:white; }
[data-testid="stDataFrame"] { border:1px solid var(--line); }
div[data-testid="stExpander"] { border:1px solid var(--line);border-radius:2px;background:rgba(255,255,255,.35); }
@media(max-width:700px){.hero h1{font-size:40px}.hero:after{font-size:52px}}
</style>
""",
    unsafe_allow_html=True,
)


@st.cache_data(ttl=90, show_spinner=False)
def load_spot(use_demo: bool) -> pd.DataFrame:
    return demo_spot() if use_demo else AkshareSource().spot()


@st.cache_data(ttl=600, show_spinner=False)
def load_hot_concepts() -> dict[str, list[str]]:
    return AkshareSource().hot_concept_members(6)


def make_config() -> StrategyConfig:
    st.sidebar.markdown("## 策略刻度")
    min_change, max_change = st.sidebar.slider("当日涨幅 (%)", 0.0, 10.0, (3.0, 5.0), 0.1)
    min_turn, max_turn = st.sidebar.slider("换手率 (%)", 0.0, 20.0, (5.0, 10.0), 0.5)
    min_cap, max_cap = st.sidebar.slider("流通市值 (亿元)", 10, 500, (50, 200), 10)
    ratio = st.sidebar.number_input("最低量比", 0.1, 5.0, 1.0, 0.1)
    max_candidates = st.sidebar.slider("深度分析数量", 5, 60, 30, 5)
    st.sidebar.caption("参数只改变筛选，不构成投资建议。首次建议保留默认值。")
    return StrategyConfig(
        min_change=min_change,
        max_change=max_change,
        min_volume_ratio=ratio,
        min_turnover=min_turn,
        max_turnover=max_turn,
        min_float_cap_yi=min_cap,
        max_float_cap_yi=max_cap,
        max_candidates=max_candidates,
    )


cfg = make_config()
status, status_note = market_session_status()
st.sidebar.markdown("---")
use_demo = st.sidebar.toggle("演示数据", value=False, help="网络异常或非交易时段可体验完整流程")
with st.sidebar.expander("排除规则"):
    st.write("ST / *ST、退市整理、上市首日 N/C、北交所股票。停牌或字段缺失股票自动跳过。")

st.markdown(
    f"""
<section class="hero">
  <div class="eyebrow">Close Auction Signal Desk · A-Shares</div>
  <h1>尾盘雷达</h1>
  <p>把全市场噪声压缩成少数可复核的尾盘候选。硬条件先过筛，再确认量价、均线、分时强度与热点题材。</p>
  <span class="badge live">{status}</span><span class="badge">{datetime.now():%Y-%m-%d %H:%M}</span>
  <span class="badge">免费行情 · 新浪 / 腾讯 / AKShare</span>
</section>
""",
    unsafe_allow_html=True,
)
st.caption(status_note)

workspace = st.segmented_control(
    "工作台",
    ["今日筛选", "次日校验", "历史表现"],
    default="今日筛选",
    label_visibility="collapsed",
    width="stretch",
)
validation_store = ValidationStore()
if workspace == "次日校验":
    render_validation_page(validation_store)
    st.stop()
if workspace == "历史表现":
    render_history_page(validation_store)
    st.stop()

run = st.button("扫描全市场", type="primary", width="stretch")
if not run:
    st.markdown(
        """
<div class="card">
  <b>等待扫描</b><br><br>
  点击上方按钮读取最新行情。最佳运行时间为交易日 14:30–15:00；收盘后可复盘，但结果代表最近快照。
</div>
""",
        unsafe_allow_html=True,
    )
    st.stop()

try:
    with st.status("正在读取全市场快照…", expanded=True) as progress:
        raw_spot = load_spot(use_demo)
        spot = normalize_spot(raw_spot)
        stage1, funnel = hard_filter(spot, cfg)
        volume_ratio_missing = not stage1["volume_ratio"].notna().any()
        analysis_limit = min(len(stage1), max(cfg.max_candidates, 80)) if volume_ratio_missing else cfg.max_candidates
        selected = stage1.head(analysis_limit).copy()
        progress.write(f"硬条件通过 {len(stage1)} 只；取前 {len(selected)} 只做日线与分时确认")

        codes = selected["code"].tolist()
        if use_demo:
            daily_map = {code: demo_daily(code) for code in codes}
            minute_map = {code: demo_minute(code) for code in codes}
            errors = {}
            concept_map = {"人工智能": codes[:3], "先进制造": codes[2:6]}
            market_return = minute_return_pct(demo_index_minute())
        else:
            source = AkshareSource()
            daily_map, daily_errors = parallel_fetch(codes, source.daily)
            progress.write(f"日线完成 {len(daily_map)}/{len(codes)}")
            # 新浪分钟接口内部使用 V8 解码器，macOS 下多线程并发可能触发进程级崩溃。
            minute_map, minute_errors = parallel_fetch(codes, source.minute, max_workers=1)
            progress.write(f"分时完成 {len(minute_map)}/{len(codes)}")
            errors = {**daily_errors, **minute_errors}
            try:
                market_return = minute_return_pct(source.index_minute())
            except Exception:
                market_return = None
                progress.write("大盘分时接口暂不可用，本轮“跑赢大盘”无法确认")
            if "新浪" in str(raw_spot.attrs.get("provider", "")):
                concept_map = {}
                progress.write("当前使用新浪主链路，本轮热点题材暂不计分")
            else:
                try:
                    concept_map = load_hot_concepts()
                except Exception:
                    concept_map = {}
                    progress.write("热点板块接口暂不可用，本轮不计题材加分")

        results = []
        for row in selected.to_dict("records"):
            code = row["code"]
            if pd.isna(row.get("volume_ratio")) and code in daily_map:
                row["volume_ratio"] = estimate_volume_ratio(row["volume"], daily_map[code])
            if pd.isna(row.get("volume_ratio")):
                row["volume_ratio"] = 0.0
            row["min_volume_ratio"] = cfg.min_volume_ratio
            daily_result = analyze_daily(daily_map[code]) if code in daily_map else {"volume_step": False, "ma_bull": False}
            minute_result = (
                analyze_minute(minute_map[code], cfg.min_vwap_ratio, market_return)
                if code in minute_map
                else {"vwap_strong": False, "relative_strong": False, "pullback_ok": False}
            )
            concepts = [name for name, members in concept_map.items() if code in members]
            enriched = {**row, **daily_result, **minute_result, "hot_concepts": concepts}
            results.append(finalize_candidate(enriched))
        progress.update(label="扫描完成", state="complete", expanded=False)
except (MarketDataError, ValueError, ConnectionError) as exc:
    st.error(f"本次行情读取失败：{exc}")
    st.info("可打开左侧“演示数据”后重试。免费接口偶有拥堵，通常稍后刷新即可。")
    st.stop()
except Exception as exc:
    st.error(f"扫描未完成：{exc}")
    st.info("请稍后重试，或切换演示数据确认本地程序运行正常。")
    st.stop()

if results:
    result_df = pd.DataFrame(results).sort_values(["passed", "score"], ascending=[False, False])
    result_df["strategy_rank"] = range(1, len(result_df) + 1)
else:
    result_df = pd.DataFrame(
        columns=[
            "strategy_rank", "passed", "status", "code", "name", "score", "change_pct", "volume_ratio",
            "turnover", "float_cap_yi", "volume_step", "ma_bull", "vwap_strong",
            "relative_strong", "pullback_ok", "hot_concepts", "failed_reasons", "rank_reason",
        ]
    )
passed = result_df[result_df["passed"] == True]  # noqa: E712

scan_now = datetime.now()
if not use_demo and time(14, 30) <= scan_now.time() <= time(15, 0):
    frozen = validation_store.freeze_scan(
        scanned_at=scan_now,
        provider=str(raw_spot.attrs.get("provider", "免费行情")),
        market_count=funnel["全市场"],
        hard_count=len(stage1),
        config=cfg.as_dict(),
        candidates=passed.to_dict("records"),
    )
    if frozen:
        st.success(f"已冻结今日候选 {len(passed)} 只，次一交易日 9:45 和 10:30 分阶段校验。")
    else:
        st.caption("今日候选已在首次扫描时冻结，本次刷新不会改写历史记录。")
elif not use_demo:
    st.caption("当前不在 14:30–15:00 策略窗口，本次结果仅供查看，不写入次日校验档案。")

m1, m2, m3, m4 = st.columns(4)
m1.metric("全市场", f"{funnel['全市场']:,}")
m2.metric("硬条件通过", len(stage1))
m3.metric("深度确认", len(result_df))
m4.metric("最终候选", len(passed), delta="宁缺毋滥")

st.markdown("## 筛选漏斗")
funnel_df = pd.DataFrame({"阶段": list(funnel.keys()), "剩余数量": list(funnel.values())})
fig = go.Figure(go.Funnel(y=funnel_df["阶段"], x=funnel_df["剩余数量"], marker={"color": "#315b45"}))
fig.update_layout(height=310, margin=dict(l=10, r=10, t=10, b=10), paper_bgcolor="rgba(0,0,0,0)", font={"family": "DM Mono", "color": "#17211c"})
st.plotly_chart(fig, width="stretch", config={"displayModeBar": False})

st.markdown("## 今日结论")
if passed.empty:
    st.warning("今日无完全符合标的。不要为了交易而放宽条件；可在“观察池”中复核，但不视为策略候选。")
else:
    for rank, (_, row) in enumerate(passed.iterrows(), 1):
        concepts = " / ".join(row["hot_concepts"]) or "未命中前六热点"
        st.markdown(
            f"""
<div class="card candidate">
  <span class="eyebrow">NO.{rank:02d} · 策略匹配 {row['score']:.0f}</span>
  <h3>{row['name']} <span class="muted">{row['code']}</span></h3>
  <b>{row['change_pct']:.2f}%</b> 涨幅　·　量比 {row['volume_ratio']:.2f}　·　换手 {row['turnover']:.2f}%　·　流通 {row['float_cap_yi']:.1f}亿<br>
  <span class="muted">主要优势：{row.get('rank_reason', '—')}　｜　题材：{concepts}　｜　分时站上均价比例：{row.get('vwap_ratio', 0):.0%}　｜　距尾盘高点：{row.get('pullback_pct', 0):.2f}%</span>
</div>
""",
            unsafe_allow_html=True,
        )

st.markdown("## 全部深度结果")
display = result_df.copy()
display["题材"] = display["hot_concepts"].apply(lambda x: " / ".join(x) if isinstance(x, list) and x else "—")
display = display.rename(
    columns={
        "strategy_rank": "策略排名", "status": "结论", "code": "代码", "name": "名称", "score": "匹配度",
        "change_pct": "涨幅%", "volume_ratio": "量比", "turnover": "换手率%",
        "float_cap_yi": "流通市值(亿)", "volume_step": "阶梯放量",
        "ma_bull": "均线多头", "vwap_strong": "分时强势", "pullback_ok": "回踩有效",
        "relative_strong": "跑赢大盘",
        "failed_reasons": "未通过项",
        "rank_reason": "主要优势",
    }
)
cols = ["策略排名", "结论", "代码", "名称", "匹配度", "主要优势", "涨幅%", "量比", "换手率%", "流通市值(亿)", "阶梯放量", "均线多头", "分时强势", "跑赢大盘", "回踩有效", "题材", "未通过项"]
st.dataframe(display[cols], hide_index=True, width="stretch")
csv = display[cols].to_csv(index=False).encode("utf-8-sig")
st.download_button("导出本次结果 CSV", csv, file_name=f"尾盘雷达_{datetime.now():%Y%m%d_%H%M}.csv", mime="text/csv")

if errors:
    with st.expander(f"数据缺失记录（{len(errors)}）"):
        st.json(errors)

with st.expander("规则口径与风控"):
    st.markdown(
        """
- 阶梯放量：近 5 日成交量回归斜率向上、近 4 日至少两次递增，且最新成交量高于 5 日均量 3%。
- 量比：新浪快照不提供量比时，用今日累计成交量 ÷ 近 5 日同期预期成交量估算。
- 均线多头：收盘价 ≥ MA5 > MA10 > MA20 > MA60，且四条均线均较 5 日前上升。
- 分时强势：至少 70% 的分钟收盘价位于当日成交均价线上方，最新价仍在均价线上方。
- 跑赢大盘：个股从首个分钟点至最新分钟点的涨幅高于同期上证指数。
- 回踩有效：最新价未跌破成交均价，且距离最近 30 分钟高点不超过 1.2%。
- 热点题材：东方财富概念板块实时涨幅前六，仅作为 10 分加分项，不替代技术确认。
- 策略排名：先按“候选 / 观察”分组，再按 100 分匹配度降序排列。权重为硬条件贴合 25、量能 15、均线 20、分时 25、热点 10、数据完整性 5。

建议把“次日 9:30 卖出”理解为需要验证的策略规则，而不是收益承诺。实盘应预先规定单笔仓位、最大亏损和异常停牌处理；本工具不连接券商、不自动下单。
"""
    )
    st.code(json.dumps(cfg.as_dict(), ensure_ascii=False, indent=2), language="json")

st.caption("数据仅供学习与策略研究，不构成任何投资建议。免费数据可能延迟、缺失或中断，下单前请以券商行情为准。")
