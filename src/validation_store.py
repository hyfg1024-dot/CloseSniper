from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import pandas as pd


DEFAULT_DB_PATH = Path(__file__).resolve().parents[1] / "data" / "panpanc.db"


class ValidationStore:
    def __init__(self, path: str | Path = DEFAULT_DB_PATH) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
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
                   s.entry_price, s.score,
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
