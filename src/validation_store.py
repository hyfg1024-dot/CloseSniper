from __future__ import annotations

import json
import os
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
APP_SUPPORT_ROOT = Path(
    os.getenv(
        "CLOSESNIPER_HOME",
        str(Path.home() / "Library" / "Application Support" / "CloseSniper"),
    )
)
DEFAULT_DB_PATH = APP_SUPPORT_ROOT / "data" / "closesniper.db"
LEGACY_DB_PATH = PROJECT_ROOT / "data" / "panpanc.db"


class ValidationStore:
    def __init__(self, path: str | Path = DEFAULT_DB_PATH) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path == DEFAULT_DB_PATH and not self.path.exists() and LEGACY_DB_PATH.exists():
            shutil.copy2(LEGACY_DB_PATH, self.path)
        self._init_schema()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=20)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _init_schema(self) -> None:
        with self.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS scans (
                    id INTEGER PRIMARY KEY,
                    trade_date TEXT NOT NULL UNIQUE,
                    scanned_at TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    market_count INTEGER NOT NULL,
                    hard_count INTEGER NOT NULL,
                    config_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS signals (
                    id INTEGER PRIMARY KEY,
                    scan_id INTEGER NOT NULL REFERENCES scans(id) ON DELETE CASCADE,
                    code TEXT NOT NULL,
                    name TEXT NOT NULL,
                    entry_price REAL NOT NULL,
                    score REAL NOT NULL,
                    change_pct REAL,
                    volume_ratio REAL,
                    turnover REAL,
                    float_cap_yi REAL,
                    UNIQUE(scan_id, code)
                );
                CREATE TABLE IF NOT EXISTS validations (
                    id INTEGER PRIMARY KEY,
                    signal_id INTEGER NOT NULL UNIQUE REFERENCES signals(id) ON DELETE CASCADE,
                    validation_date TEXT NOT NULL,
                    open_price REAL NOT NULL,
                    price_0945 REAL NOT NULL,
                    high_0945 REAL NOT NULL,
                    low_0945 REAL NOT NULL,
                    open_return REAL NOT NULL,
                    return_0945 REAL NOT NULL,
                    max_return REAL NOT NULL,
                    max_drawdown REAL NOT NULL,
                    index_open_return REAL,
                    index_0945_return REAL,
                    price_1030 REAL,
                    high_1030 REAL,
                    low_1030 REAL,
                    return_1030 REAL,
                    max_return_1030 REAL,
                    max_drawdown_1030 REAL,
                    index_1030_return REAL,
                    calculated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS open_snapshots (
                    id INTEGER PRIMARY KEY,
                    signal_id INTEGER NOT NULL UNIQUE REFERENCES signals(id) ON DELETE CASCADE,
                    validation_date TEXT NOT NULL,
                    open_price REAL NOT NULL,
                    captured_price REAL NOT NULL,
                    open_return REAL NOT NULL,
                    captured_return REAL NOT NULL,
                    captured_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS staged_scans (
                    id INTEGER PRIMARY KEY,
                    trade_date TEXT NOT NULL,
                    slot TEXT NOT NULL,
                    scanned_at TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    market_count INTEGER NOT NULL,
                    hard_count INTEGER NOT NULL,
                    config_json TEXT NOT NULL,
                    UNIQUE(trade_date, slot)
                );
                CREATE TABLE IF NOT EXISTS staged_candidates (
                    id INTEGER PRIMARY KEY,
                    staged_scan_id INTEGER NOT NULL REFERENCES staged_scans(id) ON DELETE CASCADE,
                    code TEXT NOT NULL,
                    name TEXT NOT NULL,
                    entry_price REAL NOT NULL,
                    score REAL NOT NULL,
                    change_pct REAL,
                    volume_ratio REAL,
                    turnover REAL,
                    float_cap_yi REAL,
                    UNIQUE(staged_scan_id, code)
                );
                CREATE TABLE IF NOT EXISTS strict_scans (
                    id INTEGER PRIMARY KEY,
                    trade_date TEXT NOT NULL,
                    slot TEXT NOT NULL,
                    scanned_at TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    UNIQUE(trade_date, slot)
                );
                CREATE TABLE IF NOT EXISTS strict_candidates (
                    id INTEGER PRIMARY KEY,
                    strict_scan_id INTEGER NOT NULL REFERENCES strict_scans(id) ON DELETE CASCADE,
                    code TEXT NOT NULL,
                    name TEXT NOT NULL,
                    entry_price REAL NOT NULL,
                    score REAL NOT NULL,
                    UNIQUE(strict_scan_id, code)
                );
                CREATE TABLE IF NOT EXISTS notification_deliveries (
                    id INTEGER PRIMARY KEY,
                    trade_date TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    sent_at TEXT NOT NULL,
                    UNIQUE(trade_date, channel)
                );
                """
            )
            existing = {
                str(row["name"])
                for row in db.execute("PRAGMA table_info(validations)").fetchall()
            }
            for column in (
                "price_1030", "high_1030", "low_1030", "return_1030",
                "max_return_1030", "max_drawdown_1030", "index_1030_return",
            ):
                if column not in existing:
                    db.execute(f"ALTER TABLE validations ADD COLUMN {column} REAL")
            signal_columns = {
                str(row["name"])
                for row in db.execute("PRAGMA table_info(signals)").fetchall()
            }
            for column in ("score_1430", "score_1445", "score_1452"):
                if column not in signal_columns:
                    db.execute(f"ALTER TABLE signals ADD COLUMN {column} REAL")
            if "appearances" not in signal_columns:
                db.execute("ALTER TABLE signals ADD COLUMN appearances INTEGER")
            if "persistence" not in signal_columns:
                db.execute("ALTER TABLE signals ADD COLUMN persistence TEXT")

    def save_staged_scan(
        self,
        *,
        slot: str,
        scanned_at: datetime,
        provider: str,
        market_count: int,
        hard_count: int,
        config: dict[str, Any],
        candidates: Iterable[dict[str, Any]],
    ) -> None:
        if slot not in {"1430", "1445", "1452"}:
            raise ValueError(f"未知扫描节点：{slot}")
        trade_date = scanned_at.date().isoformat()
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO staged_scans
                (trade_date, slot, scanned_at, provider, market_count, hard_count, config_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(trade_date, slot) DO UPDATE SET
                    scanned_at=excluded.scanned_at, provider=excluded.provider,
                    market_count=excluded.market_count, hard_count=excluded.hard_count,
                    config_json=excluded.config_json
                """,
                (
                    trade_date, slot, scanned_at.isoformat(timespec="seconds"), provider or "未知",
                    int(market_count), int(hard_count), json.dumps(config, ensure_ascii=False, sort_keys=True),
                ),
            )
            scan_id = int(db.execute(
                "SELECT id FROM staged_scans WHERE trade_date=? AND slot=?", (trade_date, slot)
            ).fetchone()["id"])
            db.execute("DELETE FROM staged_candidates WHERE staged_scan_id=?", (scan_id,))
            rows = [
                (
                    scan_id, str(item["code"]), str(item["name"]), float(item["price"]),
                    float(item["score"]), _float_or_none(item.get("change_pct")),
                    _float_or_none(item.get("volume_ratio")), _float_or_none(item.get("turnover")),
                    _float_or_none(item.get("float_cap_yi")),
                )
                for item in candidates
            ]
            db.executemany(
                """
                INSERT INTO staged_candidates
                (staged_scan_id, code, name, entry_price, score, change_pct, volume_ratio, turnover, float_cap_yi)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )

    def finalize_staged_day(self, trade_date: str) -> bool:
        """以14:52候选为门槛生成最终名单；返回 True 表示首次生成。"""
        with self.connect() as db:
            completed_slots = {
                str(row["slot"])
                for row in db.execute(
                    "SELECT slot FROM staged_scans WHERE trade_date=?",
                    (trade_date,),
                ).fetchall()
            }
            if completed_slots != {"1430", "1445", "1452"}:
                return False
            final_stage = db.execute(
                "SELECT * FROM staged_scans WHERE trade_date=? AND slot='1452'", (trade_date,)
            ).fetchone()
            if final_stage is None:
                return False
            cursor = db.execute(
                """
                INSERT OR IGNORE INTO scans
                (trade_date, scanned_at, provider, market_count, hard_count, config_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    trade_date, final_stage["scanned_at"], final_stage["provider"],
                    final_stage["market_count"], final_stage["hard_count"], final_stage["config_json"],
                ),
            )
            if cursor.rowcount == 0:
                return False
            scan_id = int(cursor.lastrowid)
            final_rows = db.execute(
                """
                SELECT c.* FROM staged_candidates c
                JOIN staged_scans ss ON ss.id=c.staged_scan_id
                WHERE ss.trade_date=? AND ss.slot='1452'
                """,
                (trade_date,),
            ).fetchall()
            for item in final_rows:
                scores: dict[str, float | None] = {}
                for slot in ("1430", "1445", "1452"):
                    row = db.execute(
                        """
                        SELECT c.score FROM staged_candidates c
                        JOIN staged_scans ss ON ss.id=c.staged_scan_id
                        WHERE ss.trade_date=? AND ss.slot=? AND c.code=?
                        """,
                        (trade_date, slot, item["code"]),
                    ).fetchone()
                    scores[slot] = float(row["score"]) if row else None
                appearances = sum(value is not None for value in scores.values())
                # 改进流程要求至少连续通过14:45与14:52；拒绝尾盘最后一刻突然进入。
                if scores["1445"] is None:
                    continue
                composite = round(
                    0.20 * (scores["1430"] or 0)
                    + 0.30 * (scores["1445"] or 0)
                    + 0.50 * (scores["1452"] or 0),
                    1,
                )
                if appearances == 3:
                    persistence = "三次稳定"
                else:
                    persistence = "连续两次"
                db.execute(
                    """
                    INSERT INTO signals
                    (scan_id, code, name, entry_price, score, change_pct, volume_ratio, turnover,
                     float_cap_yi, score_1430, score_1445, score_1452, appearances, persistence)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        scan_id, item["code"], item["name"], item["entry_price"], composite,
                        item["change_pct"], item["volume_ratio"], item["turnover"], item["float_cap_yi"],
                        scores["1430"], scores["1445"], scores["1452"], appearances, persistence,
                    ),
                )
            return True

    def staged_frame(self, trade_date: str) -> pd.DataFrame:
        query = """
            SELECT ss.trade_date, ss.slot, ss.scanned_at, c.code, c.name, c.entry_price,
                   c.score, c.change_pct, c.volume_ratio, c.turnover, c.float_cap_yi
            FROM staged_scans ss
            LEFT JOIN staged_candidates c ON c.staged_scan_id=ss.id
            WHERE ss.trade_date=?
            ORDER BY ss.slot, c.score DESC
        """
        with self.connect() as db:
            return pd.read_sql_query(query, db, params=(trade_date,))

    def save_strict_scan(
        self,
        *,
        slot: str,
        scanned_at: datetime,
        provider: str,
        candidates: Iterable[dict[str, Any]],
    ) -> None:
        if slot not in {"1430", "1445", "1452"}:
            raise ValueError(f"未知扫描节点：{slot}")
        trade_date = scanned_at.date().isoformat()
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO strict_scans (trade_date, slot, scanned_at, provider)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(trade_date, slot) DO UPDATE SET
                    scanned_at=excluded.scanned_at, provider=excluded.provider
                """,
                (trade_date, slot, scanned_at.isoformat(timespec="seconds"), provider or "未知"),
            )
            scan_id = int(db.execute(
                "SELECT id FROM strict_scans WHERE trade_date=? AND slot=?", (trade_date, slot)
            ).fetchone()["id"])
            db.execute("DELETE FROM strict_candidates WHERE strict_scan_id=?", (scan_id,))
            db.executemany(
                """
                INSERT INTO strict_candidates (strict_scan_id, code, name, entry_price, score)
                VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (scan_id, str(item["code"]), str(item["name"]), float(item["price"]), float(item["score"]))
                    for item in candidates
                ],
            )

    def latest_strict_frame(self, trade_date: str) -> pd.DataFrame:
        query = """
            SELECT ss.slot, ss.scanned_at, c.code, c.name, c.score, c.entry_price
            FROM strict_scans ss
            LEFT JOIN strict_candidates c ON c.strict_scan_id=ss.id
            WHERE ss.trade_date=? AND ss.slot=(
                SELECT MAX(slot) FROM strict_scans WHERE trade_date=?
            )
            ORDER BY c.score DESC
        """
        with self.connect() as db:
            return pd.read_sql_query(query, db, params=(trade_date, trade_date))

    def strict_frame(self, trade_date: str) -> pd.DataFrame:
        query = """
            SELECT ss.trade_date, ss.slot, ss.scanned_at, c.code, c.name, c.entry_price, c.score
            FROM strict_scans ss
            LEFT JOIN strict_candidates c ON c.strict_scan_id=ss.id
            WHERE ss.trade_date=?
            ORDER BY ss.slot, c.score DESC
        """
        with self.connect() as db:
            return pd.read_sql_query(query, db, params=(trade_date,))

    def latest_rational_frame(self, trade_date: str) -> pd.DataFrame:
        final = self.final_frame(trade_date)
        if not final.empty:
            return final
        query = """
            SELECT ss.slot, ss.scanned_at, c.code, c.name, c.score, c.entry_price
            FROM staged_scans ss
            LEFT JOIN staged_candidates c ON c.staged_scan_id=ss.id
            WHERE ss.trade_date=? AND ss.slot=(
                SELECT MAX(slot) FROM staged_scans WHERE trade_date=?
            )
            ORDER BY c.score DESC
        """
        with self.connect() as db:
            return pd.read_sql_query(query, db, params=(trade_date, trade_date))

    def final_frame(self, trade_date: str) -> pd.DataFrame:
        query = """
            SELECT s.code, s.name, s.score AS composite_score, s.entry_price,
                   s.score_1430, s.score_1445, s.score_1452, s.appearances, s.persistence
            FROM signals s JOIN scans sc ON sc.id=s.scan_id
            WHERE sc.trade_date=?
            ORDER BY s.score DESC, s.appearances DESC
        """
        with self.connect() as db:
            return pd.read_sql_query(query, db, params=(trade_date,))

    def notification_sent(self, trade_date: str, channel: str) -> bool:
        with self.connect() as db:
            row = db.execute(
                "SELECT 1 FROM notification_deliveries WHERE trade_date=? AND channel=?",
                (trade_date, channel),
            ).fetchone()
        return row is not None

    def mark_notification_sent(
        self,
        trade_date: str,
        channel: str,
        sent_at: datetime | None = None,
    ) -> None:
        timestamp = sent_at or datetime.now()
        with self.connect() as db:
            db.execute(
                """
                INSERT OR IGNORE INTO notification_deliveries (trade_date, channel, sent_at)
                VALUES (?, ?, ?)
                """,
                (trade_date, channel, timestamp.isoformat(timespec="seconds")),
            )

    def freeze_scan(
        self,
        *,
        scanned_at: datetime,
        provider: str,
        market_count: int,
        hard_count: int,
        config: dict[str, Any],
        candidates: Iterable[dict[str, Any]],
    ) -> bool:
        """冻结当天首次扫描；返回 True 表示本次新建，False 表示已有冻结记录。"""
        trade_date = scanned_at.date().isoformat()
        with self.connect() as db:
            cursor = db.execute(
                """
                INSERT OR IGNORE INTO scans
                (trade_date, scanned_at, provider, market_count, hard_count, config_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    trade_date,
                    scanned_at.isoformat(timespec="seconds"),
                    provider or "未知",
                    int(market_count),
                    int(hard_count),
                    json.dumps(config, ensure_ascii=False, sort_keys=True),
                ),
            )
            if cursor.rowcount == 0:
                return False
            scan_id = int(cursor.lastrowid)
            rows = [
                (
                    scan_id,
                    str(item["code"]),
                    str(item["name"]),
                    float(item["price"]),
                    float(item["score"]),
                    _float_or_none(item.get("change_pct")),
                    _float_or_none(item.get("volume_ratio")),
                    _float_or_none(item.get("turnover")),
                    _float_or_none(item.get("float_cap_yi")),
                )
                for item in candidates
            ]
            db.executemany(
                """
                INSERT INTO signals
                (scan_id, code, name, entry_price, score, change_pct, volume_ratio, turnover, float_cap_yi)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            return True

    def pending_signals(self, before_date: str) -> list[sqlite3.Row]:
        with self.connect() as db:
            return db.execute(
                """
                SELECT s.id AS signal_id, s.code, s.name, s.entry_price, s.score,
                       sc.trade_date AS signal_date, v.validation_date,
                       v.price_0945, v.price_1030, o.open_price AS snapshot_open_price,
                       o.captured_at AS open_captured_at
                FROM signals s
                JOIN scans sc ON sc.id = s.scan_id
                LEFT JOIN validations v ON v.signal_id = s.id
                LEFT JOIN open_snapshots o ON o.signal_id = s.id
                WHERE sc.trade_date < ? AND (v.id IS NULL OR v.price_1030 IS NULL)
                ORDER BY sc.trade_date, s.score DESC
                """,
                (before_date,),
            ).fetchall()

    def save_open_snapshot(self, signal_id: int, values: dict[str, Any]) -> None:
        columns = [
            "validation_date", "open_price", "captured_price",
            "open_return", "captured_return", "captured_at",
        ]
        with self.connect() as db:
            db.execute(
                f"""
                INSERT OR REPLACE INTO open_snapshots
                (signal_id, {", ".join(columns)})
                VALUES (?, {", ".join("?" for _ in columns)})
                """,
                [signal_id, *[values.get(column) for column in columns]],
            )

    def save_validation(self, signal_id: int, values: dict[str, Any]) -> None:
        columns = [
            "validation_date", "open_price", "price_0945", "high_0945", "low_0945",
            "open_return", "return_0945", "max_return", "max_drawdown",
            "index_open_return", "index_0945_return", "price_1030", "high_1030",
            "low_1030", "return_1030", "max_return_1030", "max_drawdown_1030",
            "index_1030_return", "calculated_at",
        ]
        with self.connect() as db:
            db.execute(
                f"""
                INSERT OR REPLACE INTO validations
                (signal_id, {", ".join(columns)})
                VALUES (?, {", ".join("?" for _ in columns)})
                """,
                [signal_id, *[values.get(column) for column in columns]],
            )

    def validation_frame(self) -> pd.DataFrame:
        query = """
            SELECT sc.trade_date AS signal_date, sc.scanned_at, s.code, s.name,
                   s.entry_price, s.score, s.score_1430, s.score_1445, s.score_1452,
                   s.appearances, s.persistence,
                   COALESCE(v.validation_date, o.validation_date) AS validation_date,
                   COALESCE(v.open_price, o.open_price) AS open_price,
                   v.price_0945, COALESCE(v.open_return, o.open_return) AS open_return,
                   o.captured_price, o.captured_return, o.captured_at AS open_captured_at,
                   v.return_0945, v.max_return,
                   v.max_drawdown, v.index_open_return, v.index_0945_return,
                   v.price_1030, v.return_1030, v.max_return_1030,
                   v.max_drawdown_1030, v.index_1030_return
            FROM signals s
            JOIN scans sc ON sc.id = s.scan_id
            LEFT JOIN validations v ON v.signal_id = s.id
            LEFT JOIN open_snapshots o ON o.signal_id = s.id
            ORDER BY sc.trade_date DESC, s.score DESC
        """
        with self.connect() as db:
            return pd.read_sql_query(query, db)

    def scan_frame(self) -> pd.DataFrame:
        query = """
            SELECT sc.trade_date, sc.scanned_at, sc.provider, sc.market_count,
                   sc.hard_count, COUNT(s.id) AS candidate_count
            FROM scans sc
            LEFT JOIN signals s ON s.scan_id = sc.id
            GROUP BY sc.id
            ORDER BY sc.trade_date DESC
        """
        with self.connect() as db:
            return pd.read_sql_query(query, db)

    def pending_count(self, before_date: str) -> int:
        return len(self.pending_signals(before_date))


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
