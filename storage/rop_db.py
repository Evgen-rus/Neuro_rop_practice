"""
SQLite storage for local ROP assistant state.

The module intentionally uses the standard sqlite3 package: the current project
is a local file-based MVP, so adding an ORM would create more surface area than
the change-detection layer needs.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from setup import BASE_DIR, MSK_TZ


DEFAULT_DB_PATH = BASE_DIR / "reports" / "rop_assistant" / "rop_assistant.sqlite"


def utcish_now() -> str:
    return datetime.now(MSK_TZ).isoformat(timespec="seconds")


def dumps_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def loads_json(value: str | None, default: Any = None) -> Any:
    if not value:
        return default
    return json.loads(value)


def connect(db_path: str | Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: str | Path = DEFAULT_DB_PATH) -> None:
    with connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS entity_state (
                entity_type TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                current_fingerprint TEXT NOT NULL,
                snapshot_json TEXT NOT NULL,
                last_analysis_status TEXT,
                last_analysis_at TEXT,
                last_analysis_path TEXT,
                last_report_path TEXT,
                last_risk_level TEXT,
                last_analysis_json TEXT,
                last_recommendation_json TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (entity_type, entity_id)
            );

            CREATE TABLE IF NOT EXISTS analysis_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_type TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                status TEXT NOT NULL,
                fingerprint TEXT,
                analysis_path TEXT,
                report_path TEXT,
                raw_path TEXT,
                mini_recommendation_path TEXT,
                decision_reason_json TEXT,
                error TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS entity_memory (
                entity_type TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                memory_json TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (entity_type, entity_id)
            );

            CREATE TABLE IF NOT EXISTS mini_recommendations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_type TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                trigger_type TEXT NOT NULL,
                recommendation_md_path TEXT NOT NULL,
                fingerprint TEXT,
                created_at TEXT NOT NULL
            );
            """
        )


def _row_to_state(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    value = dict(row)
    value["snapshot"] = loads_json(value.pop("snapshot_json"), {})
    value["last_analysis"] = loads_json(value.pop("last_analysis_json"), None)
    value["last_recommendation"] = loads_json(value.pop("last_recommendation_json"), None)
    return value


def get_entity_state(db_path: str | Path, entity_type: str, entity_id: str) -> dict[str, Any] | None:
    init_db(db_path)
    with connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT * FROM entity_state
            WHERE entity_type = ? AND entity_id = ?
            """,
            (entity_type, str(entity_id)),
        ).fetchone()
    return _row_to_state(row)


def upsert_entity_state(
    db_path: str | Path,
    *,
    entity_type: str,
    entity_id: str,
    fingerprint: str,
    snapshot: dict[str, Any],
    last_analysis_status: str,
    last_analysis_path: str | None = None,
    last_report_path: str | None = None,
    last_risk_level: str | None = None,
    last_analysis: dict[str, Any] | None = None,
    last_recommendation: dict[str, Any] | None = None,
    last_analysis_at: str | None = None,
) -> None:
    init_db(db_path)
    now = utcish_now()
    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO entity_state (
                entity_type,
                entity_id,
                current_fingerprint,
                snapshot_json,
                last_analysis_status,
                last_analysis_at,
                last_analysis_path,
                last_report_path,
                last_risk_level,
                last_analysis_json,
                last_recommendation_json,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(entity_type, entity_id) DO UPDATE SET
                current_fingerprint = excluded.current_fingerprint,
                snapshot_json = excluded.snapshot_json,
                last_analysis_status = excluded.last_analysis_status,
                last_analysis_at = COALESCE(excluded.last_analysis_at, entity_state.last_analysis_at),
                last_analysis_path = COALESCE(excluded.last_analysis_path, entity_state.last_analysis_path),
                last_report_path = COALESCE(excluded.last_report_path, entity_state.last_report_path),
                last_risk_level = COALESCE(excluded.last_risk_level, entity_state.last_risk_level),
                last_analysis_json = COALESCE(excluded.last_analysis_json, entity_state.last_analysis_json),
                last_recommendation_json = COALESCE(excluded.last_recommendation_json, entity_state.last_recommendation_json),
                updated_at = excluded.updated_at
            """,
            (
                entity_type,
                str(entity_id),
                fingerprint,
                dumps_json(snapshot),
                last_analysis_status,
                last_analysis_at,
                last_analysis_path,
                last_report_path,
                last_risk_level,
                dumps_json(last_analysis) if last_analysis is not None else None,
                dumps_json(last_recommendation) if last_recommendation is not None else None,
                now,
            ),
        )


def save_analysis_run(
    db_path: str | Path,
    *,
    entity_type: str,
    entity_id: str,
    status: str,
    fingerprint: str | None = None,
    analysis_path: str | None = None,
    report_path: str | None = None,
    raw_path: str | None = None,
    mini_recommendation_path: str | None = None,
    decision_reason: dict[str, Any] | list[Any] | None = None,
    error: str | None = None,
) -> int:
    init_db(db_path)
    with connect(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO analysis_runs (
                entity_type,
                entity_id,
                status,
                fingerprint,
                analysis_path,
                report_path,
                raw_path,
                mini_recommendation_path,
                decision_reason_json,
                error,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entity_type,
                str(entity_id),
                status,
                fingerprint,
                analysis_path,
                report_path,
                raw_path,
                mini_recommendation_path,
                dumps_json(decision_reason) if decision_reason is not None else None,
                error,
                utcish_now(),
            ),
        )
        return int(cursor.lastrowid)


def get_entity_memory(db_path: str | Path, entity_type: str, entity_id: str) -> dict[str, Any] | None:
    init_db(db_path)
    with connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT memory_json FROM entity_memory
            WHERE entity_type = ? AND entity_id = ?
            """,
            (entity_type, str(entity_id)),
        ).fetchone()
    return loads_json(row["memory_json"], None) if row else None


def update_entity_memory(
    db_path: str | Path,
    *,
    entity_type: str,
    entity_id: str,
    memory_update: dict[str, Any],
) -> None:
    init_db(db_path)
    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO entity_memory (entity_type, entity_id, memory_json, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(entity_type, entity_id) DO UPDATE SET
                memory_json = excluded.memory_json,
                updated_at = excluded.updated_at
            """,
            (entity_type, str(entity_id), dumps_json(memory_update), utcish_now()),
        )


def save_mini_recommendation(
    db_path: str | Path,
    *,
    entity_type: str,
    entity_id: str,
    trigger_type: str,
    recommendation_md_path: str,
    fingerprint: str | None = None,
) -> int:
    init_db(db_path)
    with connect(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO mini_recommendations (
                entity_type,
                entity_id,
                trigger_type,
                recommendation_md_path,
                fingerprint,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                entity_type,
                str(entity_id),
                trigger_type,
                recommendation_md_path,
                fingerprint,
                utcish_now(),
            ),
        )
        return int(cursor.lastrowid)


def get_today_mini_trigger_types(
    db_path: str | Path,
    *,
    entity_type: str,
    entity_id: str,
    date_prefix: str | None = None,
) -> set[str]:
    init_db(db_path)
    today = date_prefix or utcish_now()[:10]
    with connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT trigger_type FROM mini_recommendations
            WHERE entity_type = ?
              AND entity_id = ?
              AND substr(created_at, 1, 10) = ?
            """,
            (entity_type, str(entity_id), today),
        ).fetchall()
    return {str(row["trigger_type"]) for row in rows}
