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
from src.telegram_service import (
    TelegramError,
    TelegramSettings,
    bot_identity,
    discover_chat_id,
    load_settings as load_telegram_settings,
    save_settings as save_telegram_settings,
    send_message as send_telegram_message,
)
from src.validation_store import ValidationStore
from src.validation_ui import render_history_page, render_validation_page
from src.ai_observer import AIObserverSettings, load_settings as load_ai_settings, save_settings as save_ai_settings


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
.decision-shell { margin:22px 0 12px; }
.decision-kicker { color:var(--signal);font-size:12px;letter-spacing:.15em;font-weight:700; }
.decision-title { font-size:34px;margin:5px 0 4px; }
.decision-note { color:#596159;font-size:13px;line-height:1.7;margin:0 0 14px; }
.decision-card { background:rgba(255,255,255,.58); border:1px solid var(--line); border-left:6px solid var(--moss); padding:17px 19px; min-height:150px; box-shadow:5px 5px 0 rgba(23,33,28,.07); }
.decision-card.risk { border-left-color:var(--signal); }
.decision-card h3 { margin:3px 0 8px; font-size:24px; }
.decision-stock { border-top:1px solid rgba(23,33,28,.14); padding:10px 0 2px; margin-top:8px; }
.decision-stock b { font-size:17px; }
.signal-strip { display:inline-block; margin:4px 5px 0 0; padding:3px 7px; background:rgba(49,91,69,.10); color:var(--moss); font-size:11px; border:1px solid rgba(49,91,69,.18); }
.signal-strip.warning { background:rgba(240,90,40,.10); color:#a73c1c; border-color:rgba(240,90,40,.2); }
.page-section { margin-top:30px; padding-top:20px; border-top:1px solid var(--line); }
.page-section h2 { font-size:30px; margin:0 0 4px; }
.audit-head { border-left:6px solid var(--signal);padding:8px 0 8px 20px;margin:22px 0 26px; }
.audit-head h2 { font-size:40px;margin:4px 0 8px; }
.audit-head p { color:#596159;margin:0;max-width:760px; }
.mode-guide { display:grid;grid-template-columns:1fr 1fr;gap:12px;margin:16px 0 10px; }
.mode-note { background:rgba(255,255,255,.46);border:1px solid var(--line);padding:14px 16px;min-height:92px; }
.mode-note.recommended { border-color:var(--moss);box-shadow:inset 4px 0 0 var(--moss); }
.mode-note b { display:block;font-family:"Noto Serif SC","Songti SC",serif;font-size:18px;margin-bottom:5px; }
.mode-note span { color:#626a63;font-size:12px;line-height:1.65; }
.muted { color:#6f756e;font-size:12px; }
.telegram-ready { border-left:4px solid var(--moss);padding:8px 10px;background:rgba(49,91,69,.08);font-size:12px;line-height:1.6; }
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
    with st.sidebar.expander("筛选参数", expanded=False):
        st.caption("默认参数已按当前策略保存；仅在需要研究时调整。")
        min_change, max_change = st.slider("当日涨幅 (%)", 0.0, 10.0, (3.0, 5.0), 0.1)
        min_turn, max_turn = st.slider("换手率 (%)", 0.0, 20.0, (5.0, 10.0), 0.5)
        min_cap, max_cap = st.slider("流通市值 (亿元)", 10, 500, (50, 200), 10)
        ratio = st.number_input("最低量比", 0.1, 5.0, 1.0, 0.1)
        max_candidates = st.slider("深度分析数量", 5, 60, 30, 5)
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


def render_telegram_settings() -> None:
    settings = load_telegram_settings()
    with st.sidebar.expander("Telegram 最终结果推送"):
        if settings.configured:
            bot_name = f"@{settings.bot_username}" if settings.bot_username else "已连接机器人"
            state = "已开启" if settings.enabled else "已暂停"
            st.markdown(
                f'<div class="telegram-ready"><b>{bot_name}</b><br>{state} · Chat ID 尾号 {settings.chat_id[-4:]}</div>',
                unsafe_allow_html=True,
            )
        else:
            st.caption("输入新机器人的Token后，系统会从你刚发送的 /start 自动识别Chat ID。")

        with st.form("telegram_settings_form"):
            token_input = st.text_input(
                "Bot Token",
                type="password",
                value="",
                placeholder="已保存时可留空",
                help="Token只保存在本机Application Support，不会上传GitHub。",
            )
            enabled = st.toggle(
                "14:52完成后推送",
                value=settings.enabled if settings.configured else True,
            )
            connect = st.form_submit_button("保存并连接", width="stretch")

        if connect:
            token = token_input.strip() or settings.bot_token
            if not token:
                st.warning("请先输入Bot Token。")
            else:
                try:
                    username = bot_identity(token)
                    chat_id = discover_chat_id(token)
                    save_telegram_settings(TelegramSettings(
                        bot_token=token,
                        chat_id=chat_id,
                        bot_username=username,
                        enabled=enabled,
                    ))
                    st.success("机器人与Chat ID已连接。")
                    st.rerun()
                except TelegramError as exc:
                    st.error(str(exc))

        if settings.configured and st.button("发送测试消息", width="stretch"):
            try:
                send_telegram_message(
                    settings,
                    f"✅ CloseSniper Telegram连接成功\n测试时间：{datetime.now():%Y-%m-%d %H:%M:%S}",
                )
                st.success("测试消息已发送，请查看Telegram。")
            except TelegramError as exc:
                st.error(str(exc))


def render_ai_observer_settings() -> None:
    settings = load_ai_settings()
    with st.sidebar.expander("AI 观察解读（DeepSeek）"):
        st.caption("仅分析严格标准 14:30、14:45 连续两次稳定的股票；不预测涨跌、不替代14:52确认。")
        if settings.configured:
            st.caption("已保存本机 API Key。" + ("自动解读已开启。" if settings.enabled else "自动解读已暂停。"))
        with st.form("ai_observer_settings"):
            key_input = st.text_input("DeepSeek API Key", type="password", placeholder="已保存时可留空")
            enabled = st.toggle("14:45 自动生成观察解读", value=settings.enabled if settings.configured else False)
            saved = st.form_submit_button("保存 AI 设置", width="stretch")
        if saved:
            api_key = key_input.strip() or settings.api_key
            if enabled and not api_key:
                st.warning("开启自动解读前，请输入 DeepSeek API Key。")
            else:
                save_ai_settings(AIObserverSettings(api_key=api_key, enabled=enabled))
                st.success("AI 观察设置已保存到本机。")
                st.rerun()


def render_daily_result_panel(
    container,
    title: str,
    frame: pd.DataFrame,
    note: str,
    scan_status: dict | None = None,
) -> None:
    with container:
        st.markdown(f"#### {title} · 当日结果")
        st.caption(note)
        if frame.empty:
            status = (scan_status or {}).get("status")
            attempts = int((scan_status or {}).get("attempt_count") or 0)
            if status == "failed":
                reason = str((scan_status or {}).get("error_message") or "免费行情暂不可用")
                if len(reason) > 180:
                    reason = reason[:177] + "…"
                st.error(f"自动扫描失败（已尝试{attempts}次）。\n\n{reason}")
                return
            if status in {"running", "retrying"}:
                st.warning(f"行情读取中，系统正在自动重试（第{attempts}次）。")
                return
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


def _slot_frame(frame: pd.DataFrame, slot: str) -> pd.DataFrame:
    if frame.empty or "slot" not in frame:
        return pd.DataFrame()
    return frame[frame["slot"].astype(str) == slot].copy()


def _three_stage_strict_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Strict candidates present at every scheduled timestamp, scored with the documented 20/30/50 weights."""
    if frame.empty:
        return pd.DataFrame()
    slot_frames = {
        slot: _slot_frame(frame, slot).dropna(subset=["code"])
        for slot in ("1430", "1445", "1452")
    }
    if any(items.empty for items in slot_frames.values()):
        return pd.DataFrame()
    shared = set(slot_frames["1430"]["code"])
    shared &= set(slot_frames["1445"]["code"])
    shared &= set(slot_frames["1452"]["code"])
    rows: list[dict[str, object]] = []
    for code in shared:
        records = {
            slot: items.loc[items["code"] == code].iloc[0]
            for slot, items in slot_frames.items()
        }
        rows.append({
            "code": code,
            "name": records["1452"]["name"],
            "entry_price": records["1452"]["entry_price"],
            "composite_score": round(
                .2 * float(records["1430"]["score"])
                + .3 * float(records["1445"]["score"])
                + .5 * float(records["1452"]["score"]), 1,
            ),
        })
    return pd.DataFrame(rows).sort_values("composite_score", ascending=False) if rows else pd.DataFrame()


def render_today_decision(
    *,
    strict_frame: pd.DataFrame,
    rational_final: pd.DataFrame,
    scan_status_frame: pd.DataFrame,
) -> None:
    """The first screen answers only: whether there is a primary signal and why."""
    strict_final = _three_stage_strict_frame(strict_frame)
    completed = set(scan_status_frame["slot"].astype(str)) if not scan_status_frame.empty else set()
    ready = {"1430", "1445", "1452"}.issubset(completed)
    st.markdown(
        """
        <div class="decision-shell">
          <div class="decision-kicker">TODAY · PRIMARY DECISION</div>
          <h2 class="decision-title">今日最终观察名单</h2>
          <p class="decision-note">主名单只采用严格标准在 14:30、14:45、14:52 三次均入选的股票。改进流程仅提供风险标签，不取代严格标准。</p>
        </div>
        """,
        unsafe_allow_html=True,
    )
    left, right = st.columns([1.55, 1])
    with left:
        if not ready:
            st.markdown('<div class="decision-card"><b>等待三次扫描完成</b><p class="muted">14:30、14:45、14:52 的快照齐全后，系统会在这里生成严格三次稳定名单。</p></div>', unsafe_allow_html=True)
        elif strict_final.empty:
            st.markdown('<div class="decision-card risk"><b>今日无严格三次稳定标的</b><p class="muted">严格标准不会为了给出名单而放宽；今天宜保持空仓观察。</p></div>', unsafe_allow_html=True)
        else:
            rows = []
            for rank, (_, row) in enumerate(strict_final.iterrows(), 1):
                rows.append(
                    f'<div class="decision-stock"><b>NO.{rank:02d} · {row["name"]}</b> <span class="muted">{row["code"]}</span><br>'
                    f'<span class="signal-strip">三次稳定</span><span class="signal-strip">综合评分 {row["composite_score"]:.1f}</span>'
                    f'<span class="signal-strip">信号价 {row["entry_price"]:.2f}</span></div>'
                )
            st.markdown('<div class="decision-card"><b>严格标准 · 三次稳定</b>' + ''.join(rows) + '</div>', unsafe_allow_html=True)
    with right:
        if rational_final.empty:
            message = "改进流程尚未形成最终名单；不以单次扫描替代最终判断。"
            chips = '<span class="signal-strip">风险辅助等待中</span>'
        else:
            new_count = int((rational_final.get("persistence", pd.Series(dtype=str)) == "14:52新进入").sum())
            stable_count = int((rational_final.get("persistence", pd.Series(dtype=str)).isin(["连续两次", "三次稳定"])).sum())
            message = "历史样本显示，14:52 新进入表现偏弱，默认不应进入主名单。"
            chips = f'<span class="signal-strip">稳定确认 {stable_count} 只</span><span class="signal-strip warning">14:52 新进入 {new_count} 只</span>'
        st.markdown(f'<div class="decision-card risk"><b>改进流程 · 风险提示</b><p class="muted">{message}</p>{chips}</div>', unsafe_allow_html=True)


def render_ai_observation_panel(frame: pd.DataFrame) -> None:
    if frame.empty:
        return
    st.markdown('<div class="page-section"><h2>AI 观察解读</h2><p class="decision-note">只解释已抓取的免费行情事实；作为14:52前的观察提示，不构成买卖建议。</p></div>', unsafe_allow_html=True)
    for item in frame.to_dict("records"):
        try:
            report = json.loads(str(item.get("report_json") or "{}"))
        except json.JSONDecodeError:
            report = {}
        strengths = "<br>".join(f"· {value}" for value in report.get("strengths", [])[:3]) or "· 暂无可用依据"
        risks = "<br>".join(f"· {value}" for value in report.get("risks", [])[:2]) or "· 暂无额外风险提示"
        confirms = "<br>".join(f"· {value}" for value in report.get("confirm_before_1452", [])[:2]) or "· 等待严格标准第三次确认"
        st.markdown(
            f'<div class="card"><span class="eyebrow">AI · {item["code"]}</span><h3>{item["name"]} · {item["verdict"]}</h3>'
            f'<b>事实依据</b><br>{strengths}<br><br><b>风险</b><br>{risks}<br><br><b>14:52确认</b><br>{confirms}</div>',
            unsafe_allow_html=True,
        )


def render_daily_timeline(
    *,
    strict_frame: pd.DataFrame,
    rational_frame: pd.DataFrame,
    final_frame: pd.DataFrame,
    scan_status_frame: pd.DataFrame,
    review_status_frame: pd.DataFrame,
    review_strict_frame: pd.DataFrame,
    review_improved_frame: pd.DataFrame,
) -> None:
    st.markdown("## 三次扫描记录")
    st.caption("每一列都是当时冻结的原始快照；用于复核，不会以较晚数据覆盖较早数据。")
    status_by_slot = {
        str(row["slot"]): row
        for row in scan_status_frame.to_dict("records")
    } if not scan_status_frame.empty else {}

    st.markdown("### 严格标准")
    strict_columns = st.columns(3)
    for column, slot in zip(strict_columns, ("1430", "1445", "1452"), strict=True):
        render_daily_result_panel(
            column,
            f"{slot[:2]}:{slot[2:]}",
            _slot_frame(strict_frame, slot),
            "该时点严格标准原始结果。",
            status_by_slot.get(slot),
        )

    st.markdown("### 改进流程")
    rational_columns = st.columns(3)
    for column, slot in zip(rational_columns, ("1430", "1445", "1452"), strict=True):
        render_daily_result_panel(
            column,
            f"{slot[:2]}:{slot[2:]}",
            _slot_frame(rational_frame, slot),
            "该时点改进流程原始结果。",
            status_by_slot.get(slot),
        )

    st.markdown("### 14:52冻结决策版 · 准时结果")
    final_container = st.container(border=True)
    completed_slots = (
        set(rational_frame["slot"].astype(str))
        if not rational_frame.empty and "slot" in rational_frame
        else set()
    )
    missing_slots = [slot for slot in ("1430", "1445", "1452") if slot not in completed_slots]
    if missing_slots:
        missing_text = "、".join(f"{slot[:2]}:{slot[2:]}" for slot in missing_slots)
        with final_container:
            failed_slots = [slot for slot in missing_slots if status_by_slot.get(slot, {}).get("status") == "failed"]
            if failed_slots:
                failed_text = "、".join(f"{slot[:2]}:{slot[2:]}" for slot in failed_slots)
                st.error(f"今日无法生成冻结决策版：{failed_text} 行情读取失败，系统已完成自动重试。")
            else:
                st.info(f"尚未生成冻结决策版：等待 {missing_text} 原始节点完成。")
    elif final_frame.empty:
        with final_container:
            st.warning("14:52已冻结，准时决策版符合条件 0 只。该结果进入次日校验。")
    else:
        render_daily_result_panel(
            final_container,
            "14:52冻结名单",
            final_frame,
            "用于当日决策及次日校验：20% × 14:30评分 + 30% × 14:45评分 + 50% × 14:52评分；只复核14:45候选池。",
        )

    st.markdown("### 完整重扫复核版 · 原架构保留")
    review_container = st.container(border=True)
    if review_status_frame.empty:
        with review_container:
            st.info("等待14:52冻结决策版完成后，后台开始原架构完整重扫。")
    else:
        review_status = review_status_frame.iloc[0].to_dict()
        state = str(review_status.get("status", ""))
        if state == "running":
            with review_container:
                st.warning("完整重扫正在后台运行；不会影响或覆盖上方冻结决策版。")
        elif state == "failed":
            with review_container:
                st.error(f"完整重扫失败：{review_status.get('error_message') or '免费行情暂不可用'}")
        else:
            completed = str(review_status.get("completed_at") or "")
            completed_text = completed[11:19] if len(completed) >= 19 else "—"
            with review_container:
                st.caption(f"原架构完成时间：{completed_text}。仅供对照复核，不进入次日校验，也不覆盖准时版。")
                left, right = st.columns(2)
                with left:
                    st.markdown("#### 严格标准 · 完整重扫")
                    if review_strict_frame.empty:
                        st.warning("完整重扫符合条件 0 只。")
                    else:
                        st.dataframe(
                            review_strict_frame[["code", "name", "score"]].rename(
                                columns={"code": "代码", "name": "名称", "score": "评分"}
                            ), hide_index=True, width="stretch",
                        )
                with right:
                    st.markdown("#### 改进流程 · 完整重扫")
                    if review_improved_frame.empty:
                        st.warning("完整重扫符合条件 0 只。")
                    else:
                        st.dataframe(
                            review_improved_frame[["code", "name", "score", "persistence"]].rename(
                                columns={"code": "代码", "name": "名称", "score": "综合分", "persistence": "持续性"}
                            ), hide_index=True, width="stretch",
                        )


cfg = make_config()
status, status_note = market_session_status()
st.sidebar.markdown("---")
render_telegram_settings()
render_ai_observer_settings()
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
  <span class="badge">免费行情 · 新浪 / 东方财富 / 腾讯</span>
</section>
""",
    unsafe_allow_html=True,
)
st.caption(status_note)

workspace = st.segmented_control(
    "工作台",
    ["今日决策", "次日校验", "历史研究"],
    default="今日决策",
    label_visibility="collapsed",
    width="stretch",
)
validation_store = ValidationStore()


def optional_store_frame(store: ValidationStore, method_name: str, *args: str) -> pd.DataFrame:
    """兼容 Streamlit 热更新期间仍存活的旧 ValidationStore 实例。"""
    method = getattr(store, method_name, None)
    if not callable(method):
        return pd.DataFrame()
    return method(*args)


if workspace == "次日校验":
    render_validation_page(validation_store)
    st.stop()
if workspace == "历史研究":
    render_history_page(validation_store)
    st.stop()

render_scan_countdown()
st.markdown(
    """
<div class="mode-guide">
  <div class="mode-note"><b>严格标准 · 主策略</b><span>三次均入选才进入今日主名单，并单独进入次日校验与历史研究。</span></div>
  <div class="mode-note recommended"><b>改进流程 · 风险辅助</b><span>用于识别稳定确认与尾盘新进入风险；不再单独替代严格标准的最终判断。</span></div>
</div>
    """,
    unsafe_allow_html=True,
)
strict_col, rational_col = st.columns(2)
strict_run = strict_col.button("扫描严格标准", type="primary", width="stretch", help="完全按初始均线标准执行")
rational_run = rational_col.button("扫描改进流程（风险辅助）", width="stretch", help="增加高位过热过滤，并标记14:52新进入风险")
scan_mode = "strict" if strict_run else "rational" if rational_run else None
if scan_mode is None:
    today = datetime.now().date().isoformat()
    strict_today = validation_store.strict_frame(today)
    rational_today = validation_store.staged_frame(today)
    final_today = validation_store.final_frame(today)
    status_today = validation_store.scan_status_frame(today)
    render_today_decision(
        strict_frame=strict_today,
        rational_final=final_today,
        scan_status_frame=status_today,
    )
    render_ai_observation_panel(validation_store.ai_observation_frame(today))
    with st.expander("查看三次扫描记录与完整复核", expanded=False):
        render_daily_timeline(
            strict_frame=strict_today,
            rational_frame=rational_today,
            final_frame=final_today,
            scan_status_frame=status_today,
            review_status_frame=optional_store_frame(validation_store, "review_status_frame", today),
            review_strict_frame=optional_store_frame(validation_store, "review_frame", today, "strict"),
            review_improved_frame=optional_store_frame(validation_store, "review_frame", today, "improved"),
        )
    st.caption("14:52 准时结果用于决策；完整重扫仅作复核，不覆盖冻结记录。")
    st.stop()

scan_mode_label = "严格标准" if scan_mode == "strict" else "改进流程"
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
if not use_demo and in_scan_window:
    strict_today = validation_store.strict_frame(today)
    rational_today = validation_store.staged_frame(today)
    final_today = validation_store.final_frame(today)
    status_today = validation_store.scan_status_frame(today)
    render_today_decision(
        strict_frame=strict_today,
        rational_final=final_today,
        scan_status_frame=status_today,
    )
    render_ai_observation_panel(validation_store.ai_observation_frame(today))
    with st.expander("查看三次扫描记录与完整复核", expanded=False):
        render_daily_timeline(
            strict_frame=strict_today,
            rational_frame=rational_today,
            final_frame=final_today,
            scan_status_frame=status_today,
            review_status_frame=optional_store_frame(validation_store, "review_status_frame", today),
            review_strict_frame=optional_store_frame(validation_store, "review_frame", today, "strict"),
            review_improved_frame=optional_store_frame(validation_store, "review_frame", today, "improved"),
        )
else:
    demo_container = st.container(border=True)
    render_daily_result_panel(
        demo_container, f"{scan_mode_label} · 本次预览", passed,
        "演示数据或非尾盘时段的预览，不写入每日三时点记录。",
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
- 改进均线：现价 ≥ MA5 > MA10 > MA20 > MA60，MA5/10/20较5日前上升；另过滤近10日涨幅过热、MA20乖离过大和近期异常大阳线。
- 最终连续性：必须同时通过14:45与14:52，14:52首次出现的股票不进入最终名单。
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
