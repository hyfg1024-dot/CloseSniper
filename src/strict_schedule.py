"""Timed strict decisions. The computation worker never writes to the database."""
from __future__ import annotations

from datetime import datetime, timedelta
from io import StringIO
import hashlib
import json

import pandas as pd

from src.data_source import MarketDataError
from src.deadline_worker import run_with_deadline
from src.strategy import StrategyConfig
from src.strict_deadline import evaluate_strict_pool
from src.telegram_service import load_settings, send_message


def cache_scan(store, slot, result, cfg, saved_at):
    # Evidence failure must prevent this preparation from being certified.
    store.save_scan_evidence(slot=slot, result=result, captured_at=saved_at)
    histories = {code: frame.to_json(orient="table", date_format="iso")
                 for code, frame in result.daily_history.items()}
    with store.connect() as db:
        db.execute(
            """INSERT OR REPLACE INTO strict_scan_cache
            (trade_date,slot,saved_at,config_json,daily_json,concepts_json) VALUES (?,?,?,?,?,?)""",
            (saved_at.date().isoformat(), slot, saved_at.isoformat(timespec="seconds"),
             json.dumps(cfg.as_dict(), sort_keys=True), json.dumps(histories), json.dumps(result.concepts)),
        )


def _input_token(db, day):
    """Fingerprint both original slots inside the current SQLite transaction."""
    payload = []
    for table in ("strict_scans", "scan_runs", "strict_scan_cache"):
        payload.append([dict(row) for row in db.execute(
            f"SELECT * FROM {table} WHERE trade_date=? AND slot IN ('1430','1445') ORDER BY slot", (day,),
        ).fetchall()])
    payload.append([dict(row) for row in db.execute(
        """SELECT c.* FROM strict_candidates c JOIN strict_scans s ON s.id=c.strict_scan_id
        WHERE s.trade_date=? AND s.slot IN ('1430','1445') ORDER BY s.slot,c.code""", (day,),
    ).fetchall()])
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def prepared_inputs(store, day, cfg):
    with store.connect() as db:
        db.execute("BEGIN")
        for slot in ("1430", "1445"):
            snapshot = db.execute("SELECT * FROM strict_scans WHERE trade_date=? AND slot=?", (day, slot)).fetchone()
            run = db.execute("SELECT * FROM scan_runs WHERE trade_date=? AND slot=?", (day, slot)).fetchone()
            cache = db.execute("SELECT * FROM strict_scan_cache WHERE trade_date=? AND slot=?", (day, slot)).fetchone()
            if not snapshot or not run or not cache:
                raise MarketDataError(f"{slot[:2]}:{slot[2:]} 严格扫描或预备数据缺失")
            if run["status"] != "success" or run["error_message"]:
                raise MarketDataError(f"{slot[:2]}:{slot[2:]} 行情未完整确认")
            planned = datetime.fromisoformat(f"{day}T{slot[:2]}:{slot[2:]}:00")
            completed = datetime.fromisoformat(snapshot["scanned_at"])
            if not planned <= completed < planned + timedelta(seconds=150):
                raise MarketDataError(f"{slot[:2]}:{slot[2:]} 快照迟到，不能用于准时终判")
            if not run["completed_at"] or run["completed_at"] != snapshot["scanned_at"]:
                raise MarketDataError(f"{slot[:2]}:{slot[2:]} 完成记录与快照不一致")
            if cache["saved_at"] != snapshot["scanned_at"] or json.loads(cache["config_json"]) != cfg.as_dict():
                raise MarketDataError(f"{slot[:2]}:{slot[2:]} 缓存与扫描参数不一致")
        # The second slot's raw histories were fetched on the same day; today's
        # provisional bar is replaced from a fresh quote by the strict engine.
        daily = {code: pd.read_json(StringIO(value), orient="table")
                 for code, value in json.loads(cache["daily_json"]).items()}
        concepts = json.loads(cache["concepts_json"])
        token = _input_token(db, day)
    codes = store.strict_two_stage_frame(day)["code"].astype(str).tolist()
    missing = set(codes) - daily.keys()
    if missing:
        raise MarketDataError("交集股票缺少日线缓存：" + ",".join(sorted(missing)))
    return codes, {code: daily[code] for code in codes}, concepts, token


def decision_record(store, day):
    with store.connect() as db:
        row = db.execute("SELECT * FROM strict_decisions WHERE trade_date=?", (day,)).fetchone()
    return dict(row) if row else None


def save_decision(store, day, started, completed, status, rows, pool_count, error=None, *, expected_input_token=None):
    """Freeze the decision and validation signals atomically, including failure."""
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        if db.execute("SELECT 1 FROM strict_decisions WHERE trade_date=?", (day,)).fetchone():
            return False
        if status == "success" and expected_input_token is not None and _input_token(db, day) != expected_input_token:
            raise MarketDataError("前两次严格快照在终判期间发生变化，拒绝发布不一致名单")
        # Before the first official decision a manual scan may already have
        # populated the legacy final table. Replace only that day's provisional
        # signals atomically; never delete validated or observed history.
        if db.execute(
            """SELECT 1 FROM strict_final_signals s WHERE s.signal_date=? AND
            (EXISTS (SELECT 1 FROM strict_validations v WHERE v.strict_signal_id=s.id)
             OR EXISTS (SELECT 1 FROM strict_exit_observations o WHERE o.strict_signal_id=s.id))""", (day,),
        ).fetchone():
            raise MarketDataError("当天已有校验或观察记录，拒绝覆盖历史信号")
        db.execute("DELETE FROM strict_final_signals WHERE signal_date=?", (day,))
        if status == "success":
            db.execute(
                """INSERT INTO strict_scans (trade_date,slot,scanned_at,provider) VALUES (?,'1452',?,?)
                ON CONFLICT(trade_date,slot) DO UPDATE SET scanned_at=excluded.scanned_at,provider=excluded.provider""",
                (day, completed.isoformat(timespec="seconds"), "严格准时终判 · 前两次交集重新计算"),
            )
            scan_id = db.execute("SELECT id FROM strict_scans WHERE trade_date=? AND slot='1452'", (day,)).fetchone()[0]
            db.execute("DELETE FROM strict_candidates WHERE strict_scan_id=?", (scan_id,))
            db.executemany(
                "INSERT INTO strict_candidates(strict_scan_id,code,name,entry_price,score) VALUES (?,?,?,?,?)",
                [(scan_id, r["code"], r["name"], float(r["price"]), float(r["score"])) for r in rows],
            )
            db.execute(
                """INSERT INTO strict_final_signals
                (signal_date,code,name,entry_price,composite_score,score_1430,score_1445,score_1452,persistence)
                SELECT a.trade_date,x.code,z.name,z.entry_price,ROUND(.2*x.score+.3*y.score+.5*z.score,1),
                       x.score,y.score,z.score,'三次稳定'
                FROM strict_scans a JOIN strict_candidates x ON x.strict_scan_id=a.id
                JOIN strict_scans b ON b.trade_date=a.trade_date AND b.slot='1445'
                JOIN strict_candidates y ON y.strict_scan_id=b.id AND y.code=x.code
                JOIN strict_scans c ON c.trade_date=a.trade_date AND c.slot='1452'
                JOIN strict_candidates z ON z.strict_scan_id=c.id AND z.code=x.code
                WHERE a.trade_date=? AND a.slot='1430'""", (day,),
            )
            # Snapshot writes are protected by the same transaction, and the
            # official message must agree exactly with the validation queue.
            actual = {r["code"]: r for r in db.execute(
                "SELECT * FROM strict_final_signals WHERE signal_date=?", (day,),
            ).fetchall()}
            if set(actual) != {r["code"] for r in rows} or any(
                any(abs(float(actual[r["code"]][field]) - float(r[field])) > 1e-8
                    for field in ("score_1430", "score_1445", "score_1452", "composite_score"))
                for r in rows
            ):
                raise MarketDataError("前两次严格快照在终判期间发生变化，拒绝发布不一致名单")
        db.execute(
            """INSERT INTO strict_decisions
            (trade_date,status,started_at,completed_at,error_message,pool_count,candidates_json)
            VALUES (?,?,?,?,?,?,?)""",
            (day, status, started.isoformat(timespec="seconds"), completed.isoformat(timespec="seconds"),
             error, pool_count, json.dumps(rows, ensure_ascii=False, default=str)),
        )
        db.execute(
            """INSERT INTO scan_runs(trade_date,slot,status,attempted_at,completed_at,attempt_count,provider,candidate_count,error_message)
            VALUES (?,'1452',?,?,?,1,?,?,?) ON CONFLICT(trade_date,slot) DO UPDATE SET
            status=excluded.status,attempted_at=excluded.attempted_at,attempt_count=excluded.attempt_count,
            completed_at=excluded.completed_at,provider=excluded.provider,
            candidate_count=excluded.candidate_count,error_message=excluded.error_message""",
            (day, status, started.isoformat(timespec="seconds"), completed.isoformat(timespec="seconds"),
             "严格准时终判", len(rows) if status == "success" else None, error),
        )
    return True


def publish_decision(store, record, now_fn=datetime.now):
    day = record["trade_date"]
    channel = "telegram-strict-deadline"
    settings = load_settings()
    if not settings.enabled or not settings.configured:
        return "disabled"
    try:
        if store.notification_sent(day, channel):
            return "already_sent"
    except Exception:
        # Saving the decision may already have failed; still attempt one clear
        # failure notice without attaching an unpersisted stock list.
        pass
    lines = [f"CloseSniper｜{day} 严格三次稳定 · 准时终判",
             "完成时间：" + record["completed_at"][11:19]]
    if record["status"] != "success":
        lines.extend(["未能确认最终名单：" + str(record["error_message"]), "本次为数据/时间异常，不能理解为没有符合条件股票。"])
    else:
        rows = json.loads(record["candidates_json"])
        lines.append(f"严格三次稳定 {len(rows)} 只")
        for row in rows:
            lines.append(f"{row['name']}（{row['code']}）｜综合 {row['composite_score']:.1f}｜信号价 {row['price']:.2f}")
        if not rows:
            lines.append("无符合条件标的")
        lines.append("仅复核前两次严格入选交集，已重新计算严格均线、量能与分时。")
    lines.append("仅供策略研究，不构成投资建议。")
    try:
        budget = 8.0
        if record["status"] == "success":
            notification_cutoff = datetime.fromisoformat(f"{day}T14:52:55")
            budget = min(budget, (notification_cutoff - now_fn()).total_seconds())
        if budget <= 0:
            return "failed_see_web"
        run_with_deadline(send_message, args=(settings, "\n".join(lines)),
                          kwargs={"attempts": 1, "timeout": 5}, timeout_seconds=budget)
        store.mark_notification_sent(day, channel, now_fn())
        return "sent"
    except Exception:
        return "failed_see_web"


def run_strict_final(*, store, cfg, now_fn=datetime.now, runner=run_with_deadline):
    started = now_fn()
    day = started.date().isoformat()
    planned = started.replace(hour=14, minute=52, second=0, microsecond=0)
    if started < planned:
        return {"status": "skipped", "reason": "尚未到14:52，未修改当天决策"}
    existing = decision_record(store, day)
    if existing:
        return {"status": existing["status"], "already_frozen": True}
    cutoff = started.replace(hour=14, minute=52, second=40, microsecond=0)
    rows, codes = [], []
    input_token = None
    error = None
    try:
        if started >= cutoff:
            raise MarketDataError("已错过14:52准时计算窗口；不会用盘后数据生成尾盘信号")
        codes, daily, concepts, input_token = prepared_inputs(store, day, cfg)
        if codes:
            remaining = (cutoff - now_fn()).total_seconds()
            if remaining <= 0:
                raise MarketDataError("预备数据读取超过14:52:40截止时间")
            result = runner(
                evaluate_strict_pool, args=(cfg, codes, daily),
                kwargs={"now": started, "concepts": concepts},
                timeout_seconds=remaining,
            )
            if result.errors:
                raise MarketDataError("部分交集标的数据缺失：" + "; ".join(result.errors))
            rows = result.candidates.to_dict("records")
            prior = store.strict_two_stage_frame(day).set_index("code").to_dict("index")
            for row in rows:
                earlier = prior[row["code"]]
                row["score_1430"] = float(earlier["score_1430"])
                row["score_1445"] = float(earlier["score_1445"])
                row["score_1452"] = float(row["score"])
                row["composite_score"] = round(.2 * row["score_1430"] + .3 * row["score_1445"] + .5 * row["score_1452"], 1)
            rows.sort(key=lambda row: (-row["composite_score"], row["code"]))
            store.save_scan_evidence(slot="1452", result=result, captured_at=started)
        if now_fn() >= cutoff:
            raise MarketDataError("计算超过14:52:40截止时间")
        status = "success"
    except Exception as exc:
        status, rows, error = "failed", [], str(exc)[:500]
    completed = now_fn()
    try:
        save_decision(store, day, started, completed, status, rows, len(codes), error,
                      expected_input_token=input_token)
        record = decision_record(store, day)
        # A competing process may have frozen the day first. Report only the
        # accepted decision, never this process's discarded local result.
        status = record["status"]
        rows = json.loads(record["candidates_json"])
        error = record["error_message"]
        completed = datetime.fromisoformat(record["completed_at"])
    except Exception as exc:
        # If SQLite is locked or the disk fails, never send an unpersisted list.
        status, rows, error = "failed", [], f"结果保存失败：{type(exc).__name__}，请检查网页和后台日志"
        try:
            save_decision(store, day, started, completed, status, [], len(codes), error)
            record = decision_record(store, day)
            status = record["status"]
            rows = json.loads(record["candidates_json"])
            error = record["error_message"]
        except Exception:
            record = {"trade_date": day, "status": status, "completed_at": completed.isoformat(timespec="seconds"),
                      "error_message": error, "candidates_json": "[]"}
    telegram = publish_decision(store, record, now_fn)
    return {"status": status, "strict_final": len(rows), "pool_count": len(codes),
            "completed_at": completed.isoformat(timespec="seconds"), "reason": error, "telegram": telegram}
