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

            CREATE TABLE IF NOT EXISTS ui_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_type TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                risk_level TEXT,
                attention_reason TEXT,
                recommended_action TEXT,
                analysis_path TEXT,
                report_path TEXT,
                report_json TEXT,
                job_id TEXT
            );

            CREATE TABLE IF NOT EXISTS rop_decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                report_id INTEGER NOT NULL,
                decision TEXT NOT NULL,
                comment TEXT,
                next_control_date TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(report_id) REFERENCES ui_reports(id)
            );

            CREATE TABLE IF NOT EXISTS outcomes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                report_id INTEGER NOT NULL,
                outcome_type TEXT NOT NULL,
                deal_stage_after TEXT,
                payment_status TEXT,
                manager_action_done INTEGER,
                notes TEXT,
                checked_at TEXT NOT NULL,
                FOREIGN KEY(report_id) REFERENCES ui_reports(id)
            );

            CREATE TABLE IF NOT EXISTS candidate_review_state (
                entity_type TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                state TEXT NOT NULL,
                report_id INTEGER,
                decision TEXT,
                next_control_date TEXT,
                reviewed_stage_id TEXT,
                reviewed_pipeline_id TEXT,
                reviewed_amount TEXT,
                reviewed_date_modify TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (entity_type, entity_id),
                FOREIGN KEY(report_id) REFERENCES ui_reports(id)
            );

            CREATE TABLE IF NOT EXISTS ui_candidate_filters (
                profile_key TEXT NOT NULL PRIMARY KEY,
                filter_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
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


def _row_to_ui_report(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    value = dict(row)
    value["report_json"] = loads_json(value.get("report_json"), None)
    return value


def save_ui_report(
    db_path: str | Path,
    *,
    entity_type: str,
    entity_id: str,
    risk_level: str | None = None,
    attention_reason: str | None = None,
    recommended_action: str | None = None,
    analysis_path: str | None = None,
    report_path: str | None = None,
    report_json: dict[str, Any] | None = None,
    job_id: str | None = None,
) -> int:
    init_db(db_path)
    with connect(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO ui_reports (
                entity_type,
                entity_id,
                created_at,
                risk_level,
                attention_reason,
                recommended_action,
                analysis_path,
                report_path,
                report_json,
                job_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entity_type,
                str(entity_id),
                utcish_now(),
                risk_level,
                attention_reason,
                recommended_action,
                analysis_path,
                report_path,
                dumps_json(report_json) if report_json is not None else None,
                job_id,
            ),
        )
        return int(cursor.lastrowid)


def list_ui_reports(db_path: str | Path, *, limit: int = 50) -> list[dict[str, Any]]:
    init_db(db_path)
    with connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT * FROM ui_reports
            ORDER BY id DESC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
    return [_row_to_ui_report(row) for row in rows if row is not None]


def get_ui_report(db_path: str | Path, report_id: int) -> dict[str, Any] | None:
    init_db(db_path)
    with connect(db_path) as conn:
        row = conn.execute("SELECT * FROM ui_reports WHERE id = ?", (int(report_id),)).fetchone()
    return _row_to_ui_report(row)


def save_rop_decision(
    db_path: str | Path,
    *,
    report_id: int,
    decision: str,
    comment: str | None = None,
    next_control_date: str | None = None,
) -> int:
    init_db(db_path)
    with connect(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO rop_decisions (
                report_id, decision, comment, next_control_date, created_at
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (int(report_id), decision, comment, next_control_date, utcish_now()),
        )
        return int(cursor.lastrowid)


def list_rop_decisions(db_path: str | Path, report_id: int) -> list[dict[str, Any]]:
    init_db(db_path)
    with connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT * FROM rop_decisions
            WHERE report_id = ?
            ORDER BY id DESC
            """,
            (int(report_id),),
        ).fetchall()
    return [dict(row) for row in rows]


def save_outcome(
    db_path: str | Path,
    *,
    report_id: int,
    outcome_type: str,
    deal_stage_after: str | None = None,
    payment_status: str | None = None,
    manager_action_done: bool | None = None,
    notes: str | None = None,
) -> int:
    init_db(db_path)
    done_value = None if manager_action_done is None else (1 if manager_action_done else 0)
    with connect(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO outcomes (
                report_id,
                outcome_type,
                deal_stage_after,
                payment_status,
                manager_action_done,
                notes,
                checked_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(report_id),
                outcome_type,
                deal_stage_after,
                payment_status,
                done_value,
                notes,
                utcish_now(),
            ),
        )
        return int(cursor.lastrowid)


def list_outcomes(db_path: str | Path, report_id: int) -> list[dict[str, Any]]:
    init_db(db_path)
    with connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT * FROM outcomes
            WHERE report_id = ?
            ORDER BY id DESC
            """,
            (int(report_id),),
        ).fetchall()
    return [dict(row) for row in rows]


def get_candidate_review_states(
    db_path: str | Path,
    *,
    entity_type: str,
    entity_ids: list[str] | None = None,
) -> dict[str, dict[str, Any]]:
    """Текущее решение РОПа по конкретным сущностям, без блокировки всей воронки."""
    init_db(db_path)
    ids = [str(item) for item in entity_ids or [] if str(item)]
    query = "SELECT * FROM candidate_review_state WHERE entity_type = ?"
    params: list[Any] = [entity_type]
    if ids:
        query += f" AND entity_id IN ({','.join('?' for _ in ids)})"
        params.extend(ids)
    with connect(db_path) as conn:
        rows = conn.execute(query, params).fetchall()
    return {str(row["entity_id"]): dict(row) for row in rows}


def upsert_candidate_review_state(
    db_path: str | Path,
    *,
    entity_type: str,
    entity_id: str,
    state: str,
    report_id: int | None = None,
    decision: str | None = None,
    next_control_date: str | None = None,
    reviewed_stage_id: str | None = None,
    reviewed_pipeline_id: str | None = None,
    reviewed_amount: str | None = None,
    reviewed_date_modify: str | None = None,
) -> dict[str, Any]:
    init_db(db_path)
    now = utcish_now()
    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO candidate_review_state (
                entity_type, entity_id, state, report_id, decision, next_control_date,
                reviewed_stage_id, reviewed_pipeline_id, reviewed_amount, reviewed_date_modify,
                created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(entity_type, entity_id) DO UPDATE SET
                state = excluded.state,
                report_id = COALESCE(excluded.report_id, candidate_review_state.report_id),
                decision = COALESCE(excluded.decision, candidate_review_state.decision),
                next_control_date = excluded.next_control_date,
                reviewed_stage_id = COALESCE(excluded.reviewed_stage_id, candidate_review_state.reviewed_stage_id),
                reviewed_pipeline_id = COALESCE(excluded.reviewed_pipeline_id, candidate_review_state.reviewed_pipeline_id),
                reviewed_amount = COALESCE(excluded.reviewed_amount, candidate_review_state.reviewed_amount),
                reviewed_date_modify = COALESCE(excluded.reviewed_date_modify, candidate_review_state.reviewed_date_modify),
                updated_at = excluded.updated_at
            """,
            (
                entity_type,
                str(entity_id),
                state,
                report_id,
                decision,
                next_control_date,
                reviewed_stage_id,
                reviewed_pipeline_id,
                reviewed_amount,
                reviewed_date_modify,
                now,
                now,
            ),
        )
    return get_candidate_review_states(db_path, entity_type=entity_type, entity_ids=[str(entity_id)]).get(str(entity_id), {})


DEFAULT_CANDIDATE_FILTER_PROFILE = "default"


def default_candidate_filter() -> dict[str, Any]:
    """Стартовый фильтр UI: лиды, даты 15/15, воронка/этапы пустые — поиск ещё не готов."""
    return {
        "entity_type": "lead",
        "created_days": 15,
        "modified_days": 15,
        "priority": None,
        "pipeline_ids": [],
        "stage_ids": [],
        "review_view": "active",
        "limit": 20,
    }


def get_candidate_filter(
    db_path: str | Path,
    profile_key: str = DEFAULT_CANDIDATE_FILTER_PROFILE,
) -> dict[str, Any]:
    init_db(db_path)
    with connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT filter_json FROM ui_candidate_filters
            WHERE profile_key = ?
            """,
            (profile_key,),
        ).fetchone()
    if not row:
        return default_candidate_filter()
    saved = loads_json(row["filter_json"], {})
    if not isinstance(saved, dict):
        return default_candidate_filter()
    base = default_candidate_filter()
    base.update(saved)
    return base


def save_candidate_filter(
    db_path: str | Path,
    filter_payload: dict[str, Any],
    profile_key: str = DEFAULT_CANDIDATE_FILTER_PROFILE,
) -> dict[str, Any]:
    init_db(db_path)
    payload = default_candidate_filter()
    payload.update(filter_payload or {})
    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO ui_candidate_filters (profile_key, filter_json, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(profile_key) DO UPDATE SET
                filter_json = excluded.filter_json,
                updated_at = excluded.updated_at
            """,
            (profile_key, dumps_json(payload), utcish_now()),
        )
    return payload
