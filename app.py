from __future__ import annotations

import base64
import json
from datetime import datetime, time

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from src.data_source import (
    MarketDataError,
)
from src.scan_service import run_market_scan
from src.strategy import (
    StrategyConfig,
    market_session_status,
)
from src.validation_store import ValidationStore
from src.validation_ui import render_history_page, render_validation_page


st.set_page_config(page_title="尾盘狙击 · CloseSniper", page_icon="◉", layout="wide", initial_sidebar_state="expanded")

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
.mode-guide { display:grid;grid-template-columns:1fr 1fr;gap:12px;margin:16px 0 10px; }
.mode-note { background:rgba(255,255,255,.46);border:1px solid var(--line);padding:14px 16px;min-height:92px; }
.mode-note.recommended { border-color:var(--moss);box-shadow:inset 4px 0 0 var(--moss); }
.mode-note b { display:block;font-family:"Noto Serif SC","Songti SC",serif;font-size:18px;margin-bottom:5px; }
.mode-note span { color:#626a63;font-size:12px;line-height:1.65; }
.muted { color:#6f756e;font-size:12px; }
.stButton>button { border-radius:1px;background:var(--ink);color:var(--paper);border:0;font-weight:600;min-height:44px; }
.stButton>button:hover { background:var(--signal);color:white; }
[data-testid="stDataFrame"] { border:1px solid var(--line); }
div[data-testid="stExpander"] { border:1px solid var(--line);border-radius:2px;background:rgba(255,255,255,.35); }
@media(max-width:700px){.hero h1{font-size:40px}.hero:after{font-size:52px}.mode-guide{grid-template-columns:1fr}}
</style>
""",
    unsafe_allow_html=True,
)


def render_scan_countdown() -> None:
    countdown_html = """
<!doctype html><html><head><meta charset="utf-8"><style>
*{box-sizing:border-box}body{margin:0;background:transparent;color:#17211c;font-family:"SFMono-Regular","PingFang SC",monospace}
.clock{display:grid;grid-template-columns:minmax(190px,.72fr) 1.28fr;border:1px solid #c8c3b5;background:rgba(255,255,255,.58);box-shadow:5px 5px 0 rgba(23,33,28,.08)}
.time{padding:17px 20px;border-right:1px solid #c8c3b5}.label{font-size:11px;letter-spacing:.14em;color:#f05a28;font-weight:700}.digits{font-size:29px;font-weight:750;letter-spacing:.04em;margin-top:5px;font-variant-numeric:tabular-nums}
.action{padding:17px 20px}.action b{font:700 18px/1.35 Georgia,"Songti SC",serif}.action p{font-size:12px;color:#616861;margin:7px 0 0;line-height:1.55}.dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:#315b45;margin-right:8px;animation:pulse 1.5s infinite}@keyframes pulse{50%{opacity:.25;transform:scale(.72)}}
@media(max-width:620px){.clock{grid-template-columns:1fr}.time{border-right:0;border-bottom:1px solid #c8c3b5}.digits{font-size:25px}}
</style></head><body><div class="clock"><div class="time"><div class="label" id="label">NEXT SCAN</div><div class="digits" id="digits">--:--:--</div></div><div class="action"><b><span class="dot"></span><span id="action">正在校准尾盘时钟</span></b><p id="note">系统在三个节点自动扫描，14:52生成综合最终名单。</p></div></div>
<script>
const pad=n=>String(n).padStart(2,'0');
function at(d,h,m){const x=new Date(d);x.setHours(h,m,0,0);return x}
function nextWeekday(d){const x=new Date(d);do{x.setDate(x.getDate()+1)}while(x.getDay()===0||x.getDay()===6);return at(x,14,30)}
function update(){
 const now=new Date(), day=now.getDay(); let target,label,action,note;
 if(day!==0&&day!==6&&now<at(now,14,30)){target=at(now,14,30);label='14:30 · FIRST LOOK';action='等待14:30首次观察';note='先建立候选池，不急于下结论。'}
 else if(day!==0&&day!==6&&now<at(now,14,45)){target=at(now,14,45);label='14:45 · RECHECK';action='现在可做首次扫描';note='距离14:45量价复核还有一段时间。'}
 else if(day!==0&&day!==6&&now<at(now,14,52)){target=at(now,14,52);label='14:52 · DECISION';action='14:45复核已经完成';note='14:52将自动生成综合最终名单。'}
 else if(day!==0&&day!==6&&now<at(now,15,0)){target=at(now,15,0);label='FINAL WINDOW';action='最终扫描窗口已开启';note='请点击扫描；倒计时表示距离15:00的剩余时间。'}
 else{target=nextWeekday(now);label='NEXT TRADING DAY';action='今日尾盘窗口已结束';note='已进入下一个工作日14:30倒计时；节假日请以交易所日历为准。'}
 const diff=Math.max(0,target-now), total=Math.floor(diff/1000), days=Math.floor(total/86400), hours=Math.floor(total%86400/3600), mins=Math.floor(total%3600/60), secs=total%60;
 document.getElementById('label').textContent=label;document.getElementById('digits').textContent=(days?days+'天 ':'')+pad(hours)+':'+pad(mins)+':'+pad(secs);document.getElementById('action').textContent=action;document.getElementById('note').textContent=note;
}
update();setInterval(update,1000);
</script></body></html>
    """
    payload = base64.b64encode(countdown_html.encode("utf-8")).decode("ascii")
    st.iframe(f"data:text/html;base64,{payload}", height=126)


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


def render_daily_result_panel(container, title: str, frame: pd.DataFrame, note: str) -> None:
    with container:
        st.markdown(f"#### {title} · 当日结果")
        st.caption(note)
        if frame.empty:
            st.info("今日尚无结果。")
            return
        available = frame[frame["code"].notna()].copy() if "code" in frame else pd.DataFrame()
        slot = str(frame.iloc[0].get("slot", "")) if not frame.empty else ""
        if available.empty:
            suffix = f"（{slot[:2]}:{slot[2:]}）" if slot else ""
            st.warning(f"本节点已完成，但没有符合条件的股票{suffix}。")
            return
        if "composite_score" in available:
            display = available[[
                "code", "name", "composite_score", "score_1430", "score_1445", "score_1452", "persistence",
            ]].rename(columns={
                "code": "代码", "name": "名称", "composite_score": "综合分",
                "score_1430": "14:30", "score_1445": "14:45", "score_1452": "14:52",
                "persistence": "持续性",
            })
        else:
            score_column = "score" if "score" in available else "匹配度"
            display = available[["code", "name", score_column]].rename(columns={
                "code": "代码", "name": "名称", score_column: "评分",
            })
        st.dataframe(display, hide_index=True, width="stretch")


cfg = make_config()
status, status_note = market_session_status()
st.sidebar.markdown("---")
use_demo = st.sidebar.toggle("演示数据", value=False, help="网络异常或非交易时段可体验完整流程")
with st.sidebar.expander("排除规则"):
    st.write("ST / *ST、退市整理、上市首日 N/C、北交所股票。停牌或字段缺失股票自动跳过。")

st.markdown(
    f"""
<section class="hero">
  <div class="eyebrow">CloseSniper · A-Share Closing Scanner</div>
  <h1>尾盘狙击</h1>
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

render_scan_countdown()
st.markdown(
    """
<div class="mode-guide">
  <div class="mode-note"><b>严格标准</b><span>初始规则完整保留：现价 ≥ MA5 &gt; MA10 &gt; MA20 &gt; MA60，四条均线均较5日前上升。用于对照，不写入次日校验。</span></div>
  <div class="mode-note recommended"><b>理性流程 · 推荐</b><span>将当日实时行情合成临时日K；短中期均线向上且现价位于MA60上方，MA60上升改为排名加分。</span></div>
</div>
    """,
    unsafe_allow_html=True,
)
strict_col, rational_col = st.columns(2)
strict_run = strict_col.button("严格标准扫描", width="stretch", help="完全按初始均线标准执行")
rational_run = rational_col.button("理性流程扫描（推荐）", type="primary", width="stretch", help="包含当日临时日K与分阶段复核")
strict_panel = strict_col.container(border=True)
rational_panel = rational_col.container(border=True)
scan_mode = "strict" if strict_run else "rational" if rational_run else None
if scan_mode is None:
    today = datetime.now().date().isoformat()
    render_daily_result_panel(
        strict_panel,
        "严格标准",
        validation_store.latest_strict_frame(today),
        "同一时点严格均线筛选，仅用于两种方法对照。",
    )
    render_daily_result_panel(
        rational_panel,
        "理性流程",
        validation_store.latest_rational_frame(today),
        "14:52后显示三时点综合名单，并自动进入次日校验。",
    )
    st.caption("后台任务将在14:30、14:45和14:52自动更新上方两个结果区。")
    st.stop()

scan_mode_label = "严格标准" if scan_mode == "strict" else "理性流程"
st.caption(f"本次执行：{scan_mode_label}")

try:
    with st.status("正在读取全市场快照…", expanded=True) as progress:
        scan_result = run_market_scan(
            cfg, mode=scan_mode, use_demo=use_demo, progress=progress.write,
        )
        progress.update(label="扫描完成", state="complete", expanded=False)
except (MarketDataError, ValueError, ConnectionError) as exc:
    st.error(f"本次行情读取失败：{exc}")
    st.info("可打开左侧“演示数据”后重试。免费接口偶有拥堵，通常稍后刷新即可。")
    st.stop()
except Exception as exc:
    st.error(f"扫描未完成：{exc}")
    st.info("请稍后重试，或切换演示数据确认本地程序运行正常。")
    st.stop()

result_df = scan_result.result_frame
funnel = scan_result.funnel
errors = scan_result.errors
passed = result_df[result_df["passed"] == True]  # noqa: E712

scan_now = datetime.now()
in_scan_window = time(14, 30) <= scan_now.time() <= time(15, 0)
if scan_now.time() < time(14, 45):
    slot = "1430"
elif scan_now.time() < time(14, 52):
    slot = "1445"
else:
    slot = "1452"
if scan_mode == "rational" and not use_demo and in_scan_window:
    validation_store.save_staged_scan(
        slot=slot,
        scanned_at=scan_now,
        provider=scan_result.provider,
        market_count=funnel["全市场"],
        hard_count=scan_result.hard_count,
        config=cfg.as_dict(),
        candidates=passed.to_dict("records"),
    )
    if slot == "1452":
        frozen = validation_store.finalize_staged_day(scan_now.date().isoformat())
        final = validation_store.final_frame(scan_now.date().isoformat())
        if frozen:
            st.success(f"14:52综合名单已冻结 {len(final)} 只，并纳入次日校验。")
        else:
            st.caption("今日综合最终名单已经生成，本次扫描不会改写历史记录。")
    else:
        st.success(f"已保存 {slot[:2]}:{slot[2:]} 候选 {len(passed)} 只，等待后续节点确认。")
elif scan_mode == "strict" and not use_demo and in_scan_window:
    validation_store.save_strict_scan(
        slot=slot,
        scanned_at=scan_now,
        provider=scan_result.provider,
        candidates=passed.to_dict("records"),
    )
    st.caption("严格标准结果已保存到左侧结果区，用于同时点对照；不重复写入次日校验。")
elif not use_demo:
    st.caption("当前不在 14:30–15:00 策略窗口，本次结果仅供查看，不写入次日校验档案。")

today = scan_now.date().isoformat()
strict_panel_frame = passed if scan_mode == "strict" else validation_store.latest_strict_frame(today)
if scan_mode == "rational" and in_scan_window and slot == "1452" and not use_demo:
    rational_panel_frame = validation_store.final_frame(today)
else:
    rational_panel_frame = passed if scan_mode == "rational" else validation_store.latest_rational_frame(today)
render_daily_result_panel(
    strict_panel, "严格标准", strict_panel_frame, "同一时点严格均线筛选，仅用于两种方法对照。"
)
render_daily_result_panel(
    rational_panel, "理性流程", rational_panel_frame, "14:52综合结果自动进入次日校验。"
)

m1, m2, m3, m4 = st.columns(4)
m1.metric("全市场", f"{funnel['全市场']:,}")
m2.metric("硬条件通过", scan_result.hard_count)
m3.metric("深度确认", len(result_df))
m4.metric("最终候选", len(passed), delta="宁缺毋滥")

st.markdown("## 筛选漏斗")
funnel_df = pd.DataFrame({"阶段": list(funnel.keys()), "剩余数量": list(funnel.values())})
fig = go.Figure(go.Funnel(y=funnel_df["阶段"], x=funnel_df["剩余数量"], marker={"color": "#315b45"}))
fig.update_layout(height=310, margin=dict(l=10, r=10, t=10, b=10), paper_bgcolor="rgba(0,0,0,0)", font={"family": "DM Mono", "color": "#17211c"})
st.plotly_chart(fig, width="stretch", config={"displayModeBar": False})

st.markdown("## 今日结论")
if passed.empty:
    st.warning(f"本次“{scan_mode_label}”无完全符合标的。可查看观察结果的具体未通过项，但不视为策略候选。")
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
        "ma_bull": "均线通过", "vwap_strong": "分时强势", "pullback_ok": "回踩有效",
        "relative_strong": "跑赢大盘",
        "failed_reasons": "未通过项",
        "rank_reason": "主要优势",
    }
)
cols = ["策略排名", "结论", "代码", "名称", "匹配度", "主要优势", "涨幅%", "量比", "换手率%", "流通市值(亿)", "阶梯放量", "均线通过", "分时强势", "跑赢大盘", "回踩有效", "题材", "未通过项"]
st.dataframe(display[cols], hide_index=True, width="stretch")
csv = display[cols].to_csv(index=False).encode("utf-8-sig")
st.download_button("导出本次结果 CSV", csv, file_name=f"CloseSniper_{scan_mode_label}_{datetime.now():%Y%m%d_%H%M}.csv", mime="text/csv")

if errors:
    with st.expander(f"数据缺失记录（{len(errors)}）"):
        st.json(errors)

with st.expander("规则口径与风控"):
    st.markdown(
        """
- 阶梯放量：近 5 日成交量回归斜率向上、近 4 日至少两次递增，且最新成交量高于 5 日均量 3%。
- 量比：新浪快照不提供量比时，用今日累计成交量 ÷ 近 5 日同期预期成交量估算。
- 当日临时日K：将扫描时的实时价、开高低、成交量合并到历史日线后重新计算均线。
- 严格均线：现价 ≥ MA5 > MA10 > MA20 > MA60，且四条均线均较 5 日前上升。
- 理性均线：现价 ≥ MA5 > MA10 > MA20，MA5/10/20较 5 日前上升，且现价在 MA60 上方；MA60上升只作加分。
- 分时强势：至少 70% 的分钟收盘价位于当日成交均价线上方，最新价仍在均价线上方。
- 跑赢大盘：个股从首个分钟点至最新分钟点的涨幅高于同期上证指数。
- 回踩有效：最新价未跌破成交均价，且距离最近 30 分钟高点不超过 1.2%。
- 热点题材：东方财富概念板块实时涨幅前六，仅作为 10 分加分项，不替代技术确认。
- 策略排名：先按“候选 / 观察”分组，再按 100 分匹配度降序排列。权重为硬条件贴合 25、量能 15、均线 20、分时 25、热点 10、数据完整性 5。

建议把“次日 9:30 卖出”理解为需要验证的策略规则，而不是收益承诺。实盘应预先规定单笔仓位、最大亏损和异常停牌处理；本工具不连接券商、不自动下单。
"""
    )
    st.code(json.dumps({**cfg.as_dict(), "scan_mode": scan_mode}, ensure_ascii=False, indent=2), language="json")

st.caption("数据仅供学习与策略研究，不构成任何投资建议。免费数据可能延迟、缺失或中断，下单前请以券商行情为准。")
