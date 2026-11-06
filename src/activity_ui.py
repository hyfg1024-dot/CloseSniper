from datetime import datetime, time

import streamlit as st
import json

from src.activity import build_activity_frame


@st.fragment(run_every=5)
def render_activity_panel(store, *, expanded: bool = False) -> None:
    """Read fresh background state on every screen, including days with no signals."""
    frame = build_activity_frame(store)
    with st.container(border=True):
        header, refresh = st.columns([5, 1])
        header.markdown("#### 扫描运行记录")
        refresh.button("刷新记录", key="refresh_activity", width="stretch")
        if frame.empty:
            st.info("尚无后台扫描记录。请从 12Agent 启动本程序，并检查定时任务。")
            return
        with store.connect() as db:
            signal_date = db.execute("SELECT MAX(signal_date) FROM strict_final_signals").fetchone()[0]
            validation_date = db.execute("SELECT MAX(validation_date) FROM strict_validations").fetchone()[0]
        latest = frame.iloc[0]
        c1, c2, c3 = st.columns(3)
        c1.metric("最近扫描日", latest["trade_date"])
        c2.metric("最近三次稳定信号日", signal_date or "暂无")
        c3.metric("最近严格校验日", validation_date or "暂无")
        now = datetime.now()
        with store.connect() as db:
            decision = db.execute("SELECT * FROM strict_decisions WHERE trade_date=?", (now.date().isoformat(),)).fetchone()
        if decision:
            if decision["status"] == "success":
                final = json.loads(decision["candidates_json"])
                names = "、".join(f"{row['name']}（{row['code']}）" for row in final) or "无符合条件标的"
                st.success(f"严格三次稳定最终结果：{names}。完成于 {decision['completed_at'][11:19]}。")
            else:
                st.error(f"严格最终结果未能确认：{decision['error_message']}。不可将其理解为零候选。")
        if now.weekday() < 5 and now.time() >= time(15, 0) and latest["trade_date"] < now.date().isoformat():
            st.warning("今日尚无扫描记录。若今天开市，请检查 Mac 是否开机、联网及定时任务；休市日不产生行情。")
        elif (latest["status"] == "partial" and latest["trade_date"] == now.date().isoformat()
              and now.time() < time(14, 52)):
            st.info("今日已开始记录，等待后续 14:45 / 14:52 节点完成。")
        elif latest["status"] in {"failed", "partial", "missing", "data_incomplete"}:
            st.warning(f"{latest['trade_date']} 扫描记录存在异常：{latest['detail']}。不能将异常或缺失当成零候选。")
        elif latest["status"] == "running":
            st.info(f"{latest['trade_date']} 扫描仍在进行。{latest['detail']}")
        elif signal_date and signal_date < latest["trade_date"]:
            st.info(
                f"扫描已记录至 {latest['trade_date']}；最近一次严格三次稳定信号为 {signal_date}。"
                "之后尚无新信号，收益表因此不增加新股票；每日运行情况见下表。"
            )
        if latest["delayed_slots"]:
            completed_text = str(latest["completed_at"]).replace("T", " ")
            st.warning(f"最新扫描存在延迟：{latest['delayed_slots']}；当日全部严格扫描完成于 {completed_text}。")
        with st.expander("每日三次扫描与校验记录（含零候选日）", expanded=expanded):
            display = frame.rename(columns={
                "trade_date": "日期", "status": "运行情况",
                "strict_1430": "14:30严格入选", "strict_1445": "14:45严格入选",
                "strict_1452": "14:52严格入选", "strict_final_count": "三次稳定",
                "validated_count": "已校验", "completed_at": "实际完成时间",
                "delayed_slots": "延迟节点", "detail": "说明",
            })
            display["运行情况"] = display["运行情况"].replace({
                "complete": "已完成", "delayed": "已完成·延迟", "failed": "失败",
                "running": "运行中", "partial": "节点不齐", "missing": "缺少记录",
                "data_incomplete": "数据不完整",
            })
            display["实际完成时间"] = display["实际完成时间"].str.replace("T", " ", regex=False)
            st.dataframe(
                display[["日期", "运行情况", "14:30严格入选", "14:45严格入选", "14:52严格入选",
                         "三次稳定", "已校验", "实际完成时间", "延迟节点", "说明"]],
                hide_index=True, width="stretch", height=300,
            )
            st.caption("0＝已保存但无入选；空白＝未保存严格结果。14:52仅复核前两次严格交集，完整全市场对照另存。14:52节点在14:53或以后完成即标记迟到；前两次超过3分钟标记延迟。页面每5秒刷新终判与运行记录。")
