"""SQLite 持久化：规则版本、输入摘要与判定结果按批次号落库检索。"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Optional

from .schemas import EvaluateRequest, EvaluationReport

DEFAULT_DB_PATH = os.environ.get("CURVE_DB_PATH", "/workspace/data/curve_audit.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS evaluations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_no TEXT NOT NULL,
    furnace_id TEXT,
    recipe_id TEXT NOT NULL,
    recipe_version TEXT NOT NULL,
    rule_version TEXT NOT NULL,
    status TEXT NOT NULL,
    input_summary TEXT NOT NULL,
    request_json TEXT NOT NULL,
    report_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_eval_batch ON evaluations(batch_no);
CREATE INDEX IF NOT EXISTS idx_eval_batch_created ON evaluations(batch_no, id);
"""


@contextmanager
def get_conn(db_path: Optional[str] = None):
    path = db_path or DEFAULT_DB_PATH
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(db_path: Optional[str] = None) -> None:
    with get_conn(db_path) as conn:
        conn.executescript(_SCHEMA)


def save_evaluation(req: EvaluateRequest, report: EvaluationReport,
                    db_path: Optional[str] = None) -> int:
    """保存一次校核，返回自增 id（即重审序号）。"""
    with get_conn(db_path) as conn:
        cur = conn.execute(
            """INSERT INTO evaluations
               (batch_no, furnace_id, recipe_id, recipe_version, rule_version,
                status, input_summary, request_json, report_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                req.batch_no,
                req.furnace_id,
                req.recipe_id,
                req.recipe_version,
                report.rule_version,
                report.status,
                json.dumps(report.input_summary, ensure_ascii=False),
                req.model_dump_json(),
                report.model_dump_json(),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        return int(cur.lastrowid)


def _row_to_report(row: sqlite3.Row) -> dict:
    report = json.loads(row["report_json"])
    report["evaluation_id"] = row["id"]
    return report


def list_by_batch(batch_no: str, db_path: Optional[str] = None) -> list[dict]:
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM evaluations WHERE batch_no = ? ORDER BY id", (batch_no,)
        ).fetchall()
    return [_row_to_report(r) for r in rows]


def get_evaluation(evaluation_id: int, db_path: Optional[str] = None) -> Optional[dict]:
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM evaluations WHERE id = ?", (evaluation_id,)
        ).fetchone()
    return _row_to_report(row) if row else None


def latest_by_batch(batch_no: str, db_path: Optional[str] = None) -> Optional[dict]:
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM evaluations WHERE batch_no = ? ORDER BY id DESC LIMIT 1",
            (batch_no,),
        ).fetchone()
    return _row_to_report(row) if row else None
