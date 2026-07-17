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


class RopConnection(sqlite3.Connection):
    """Close SQLite handles when a ``with connect(...)`` block finishes.

    The sqlite context manager commits/rolls back but does not close on its own,
    which leaves temporary databases locked on Windows.
    """

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> bool:
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


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
    conn = sqlite3.connect(path, factory=RopConnection)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, declaration: str) -> None:
    columns = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")


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

            CREATE TABLE IF NOT EXISTS qualification_reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                report_id INTEGER NOT NULL,
                is_correct INTEGER NOT NULL,
                issue_fields_json TEXT NOT NULL,
                corrected_statuses_json TEXT NOT NULL,
                corrected_category TEXT,
                comment TEXT,
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

            CREATE TABLE IF NOT EXISTS analysis_profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                profile_json TEXT NOT NULL,
                version INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS ui_preferences (
                preference_key TEXT NOT NULL PRIMARY KEY,
                value_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS candidate_cases (
                journey_key TEXT NOT NULL PRIMARY KEY,
                origin_lead_id TEXT,
                current_entity_type TEXT NOT NULL,
                current_entity_id TEXT NOT NULL,
                lifecycle_state TEXT NOT NULL,
                signal_hash TEXT NOT NULL,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                last_changed_at TEXT NOT NULL,
                resolved_at TEXT
            );

            CREATE TABLE IF NOT EXISTS daily_summary_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                profile_id INTEGER,
                profile_name TEXT NOT NULL,
                profile_version INTEGER NOT NULL,
                profile_snapshot_json TEXT NOT NULL,
                period_json TEXT NOT NULL,
                scope_snapshot_json TEXT NOT NULL,
                status TEXT NOT NULL,
                selected_count INTEGER NOT NULL,
                llm_required_count INTEGER NOT NULL,
                llm_allowed_count INTEGER NOT NULL,
                cost_preview_json TEXT NOT NULL,
                job_id TEXT,
                completed_at TEXT,
                actual_cost_json TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(profile_id) REFERENCES analysis_profiles(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS daily_summary_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL,
                journey_key TEXT NOT NULL,
                entity_type TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                origin_lead_id TEXT,
                lifecycle_state TEXT NOT NULL,
                selected INTEGER NOT NULL,
                candidate_snapshot_json TEXT NOT NULL,
                report_id INTEGER,
                job_id TEXT,
                processing_status TEXT NOT NULL DEFAULT 'draft',
                progress_json TEXT NOT NULL DEFAULT '{}',
                error TEXT,
                updated_at TEXT,
                FOREIGN KEY(run_id) REFERENCES daily_summary_runs(id) ON DELETE CASCADE,
                FOREIGN KEY(report_id) REFERENCES ui_reports(id)
            );

            CREATE INDEX IF NOT EXISTS idx_daily_summary_runs_created
                ON daily_summary_runs(created_at DESC);

            CREATE INDEX IF NOT EXISTS idx_daily_summary_items_run
                ON daily_summary_items(run_id, selected DESC, id);

            CREATE TABLE IF NOT EXISTS compact_shadow_runs (
                id TEXT PRIMARY KEY,
                entity_type TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                snapshot_hash TEXT NOT NULL,
                status TEXT NOT NULL,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                model TEXT,
                analysis_json TEXT,
                evidence_coverage_json TEXT,
                fallback_class TEXT,
                usage_json TEXT,
                cost_rub REAL,
                error TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_compact_shadow_runs_entity
                ON compact_shadow_runs(entity_type, entity_id, started_at DESC);

            CREATE TABLE IF NOT EXISTS compact_shadow_feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                compact_run_id TEXT NOT NULL UNIQUE,
                entity_type TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                snapshot_hash TEXT NOT NULL,
                model TEXT,
                raw_playbook TEXT,
                final_playbook TEXT,
                feedback_result TEXT NOT NULL,
                reason TEXT,
                comment TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(compact_run_id) REFERENCES compact_shadow_runs(id)
            );
            """
        )
        _ensure_column(conn, "daily_summary_runs", "completed_at", "TEXT")
        _ensure_column(conn, "daily_summary_runs", "actual_cost_json", "TEXT")
        _ensure_column(conn, "daily_summary_items", "job_id", "TEXT")
        _ensure_column(conn, "daily_summary_items", "processing_status", "TEXT NOT NULL DEFAULT 'draft'")
        _ensure_column(conn, "daily_summary_items", "progress_json", "TEXT NOT NULL DEFAULT '{}'")
        _ensure_column(conn, "daily_summary_items", "error", "TEXT")
        _ensure_column(conn, "daily_summary_items", "updated_at", "TEXT")


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


def get_latest_ui_report(
    db_path: str | Path,
    *,
    entity_type: str,
    entity_id: str,
) -> dict[str, Any] | None:
    init_db(db_path)
    with connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT * FROM ui_reports
            WHERE entity_type = ? AND entity_id = ? AND report_json IS NOT NULL
            ORDER BY id DESC
            LIMIT 1
            """,
            (str(entity_type), str(entity_id)),
        ).fetchone()
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


def save_qualification_review(
    db_path: str | Path,
    *,
    report_id: int,
    is_correct: bool,
    issue_fields: list[str] | None = None,
    corrected_statuses: dict[str, str] | None = None,
    corrected_category: str | None = None,
    comment: str | None = None,
) -> int:
    init_db(db_path)
    with connect(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO qualification_reviews (
                report_id,
                is_correct,
                issue_fields_json,
                corrected_statuses_json,
                corrected_category,
                comment,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(report_id),
                1 if is_correct else 0,
                dumps_json(issue_fields or []),
                dumps_json(corrected_statuses or {}),
                corrected_category,
                comment,
                utcish_now(),
            ),
        )
        return int(cursor.lastrowid)


def list_qualification_reviews(db_path: str | Path, report_id: int) -> list[dict[str, Any]]:
    init_db(db_path)
    with connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT * FROM qualification_reviews
            WHERE report_id = ?
            ORDER BY id DESC
            """,
            (int(report_id),),
        ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["is_correct"] = bool(item.get("is_correct"))
        item["issue_fields"] = loads_json(item.pop("issue_fields_json", None), [])
        item["corrected_statuses"] = loads_json(item.pop("corrected_statuses_json", None), {})
        result.append(item)
    return result


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


def _row_to_compact_run(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    value = dict(row)
    value["analysis"] = loads_json(value.pop("analysis_json"), None)
    value["evidence_coverage"] = loads_json(value.pop("evidence_coverage_json"), {})
    value["usage"] = loads_json(value.pop("usage_json"), {})
    return value


def save_compact_shadow_run(
    db_path: str | Path,
    *,
    run_id: str,
    entity_type: str,
    entity_id: str,
    snapshot_hash: str,
    status: str,
    started_at: str,
    completed_at: str | None = None,
    model: str | None = None,
    analysis: dict[str, Any] | None = None,
    evidence_coverage: dict[str, Any] | None = None,
    fallback_class: str | None = None,
    usage: dict[str, Any] | None = None,
    cost_rub: float | None = None,
    error: str | None = None,
) -> None:
    init_db(db_path)
    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO compact_shadow_runs (
                id, entity_type, entity_id, snapshot_hash, status, started_at, completed_at,
                model, analysis_json, evidence_coverage_json, fallback_class, usage_json, cost_rub, error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                status = excluded.status,
                completed_at = COALESCE(excluded.completed_at, compact_shadow_runs.completed_at),
                model = COALESCE(excluded.model, compact_shadow_runs.model),
                analysis_json = COALESCE(excluded.analysis_json, compact_shadow_runs.analysis_json),
                evidence_coverage_json = COALESCE(excluded.evidence_coverage_json, compact_shadow_runs.evidence_coverage_json),
                fallback_class = COALESCE(excluded.fallback_class, compact_shadow_runs.fallback_class),
                usage_json = COALESCE(excluded.usage_json, compact_shadow_runs.usage_json),
                cost_rub = COALESCE(excluded.cost_rub, compact_shadow_runs.cost_rub),
                error = excluded.error
            """,
            (
                run_id, entity_type, str(entity_id), snapshot_hash, status, started_at, completed_at,
                model, dumps_json(analysis) if analysis is not None else None,
                dumps_json(evidence_coverage) if evidence_coverage is not None else None,
                fallback_class, dumps_json(usage) if usage is not None else None, cost_rub, error,
            ),
        )


def get_compact_shadow_run(db_path: str | Path, run_id: str) -> dict[str, Any] | None:
    init_db(db_path)
    with connect(db_path) as conn:
        row = conn.execute("SELECT * FROM compact_shadow_runs WHERE id = ?", (run_id,)).fetchone()
    return _row_to_compact_run(row)


def list_compact_shadow_runs(
    db_path: str | Path, *, entity_type: str, entity_id: str, limit: int = 20
) -> list[dict[str, Any]]:
    init_db(db_path)
    with connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT * FROM compact_shadow_runs
            WHERE entity_type = ? AND entity_id = ?
            ORDER BY started_at DESC
            LIMIT ?
            """,
            (entity_type, str(entity_id), int(limit)),
        ).fetchall()
    return [_row_to_compact_run(row) for row in rows if row is not None]


def save_compact_shadow_feedback(
    db_path: str | Path,
    *,
    compact_run_id: str,
    entity_type: str,
    entity_id: str,
    snapshot_hash: str,
    model: str | None,
    raw_playbook: str | None,
    final_playbook: str | None,
    feedback_result: str,
    reason: str | None = None,
    comment: str | None = None,
) -> dict[str, Any]:
    init_db(db_path)
    now = utcish_now()
    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO compact_shadow_feedback (
                compact_run_id, entity_type, entity_id, snapshot_hash, model, raw_playbook,
                final_playbook, feedback_result, reason, comment, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(compact_run_id) DO UPDATE SET
                feedback_result = excluded.feedback_result,
                reason = excluded.reason,
                comment = excluded.comment,
                updated_at = excluded.updated_at
            """,
            (
                compact_run_id, entity_type, str(entity_id), snapshot_hash, model, raw_playbook,
                final_playbook, feedback_result, reason, comment, now, now,
            ),
        )
        row = conn.execute(
            "SELECT * FROM compact_shadow_feedback WHERE compact_run_id = ?", (compact_run_id,)
        ).fetchone()
    return dict(row) if row else {}


def get_compact_shadow_feedback(db_path: str | Path, compact_run_id: str) -> dict[str, Any] | None:
    init_db(db_path)
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM compact_shadow_feedback WHERE compact_run_id = ?", (compact_run_id,)
        ).fetchone()
    return dict(row) if row else None


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
        "lead_categories": [],
        "bant_filter": "",
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


DEFAULT_ANALYSIS_PROFILE_NAME = "Ежедневный контроль РОПа"
LAST_ANALYSIS_PROFILE_PREFERENCE = "last_analysis_profile_id"


def default_analysis_profile() -> dict[str, Any]:
    """Согласованный профиль ручного пилота; все лимиты остаются редактируемыми."""
    return {
        "timezone": "Europe/Moscow",
        "period_preset": "today_and_yesterday",
        "lead": {
            "enabled": True,
            "all_stages": True,
            "stage_ids": [],
            "excluded_source_codes": ["DMP", "DMP1"],
            "excluded_source_ids": [],
            "excluded_status_ids": ["309", "1583"],
            "excluded_status_names": ["спам", "биржа лидов холодные"],
            "include_incoming_calls": True,
            "include_outgoing_calls": True,
        },
        "deal": {
            "enabled": True,
            "pipeline_ids": ["15"],
            "stage_ids": [
                "C15:NEW",
                "C15:UC_JPUA1F",
                "C15:UC_JVIKFJ",
                "C15:UC_5XWLAU",
                "C15:PREPARATION",
                "C15:PREPAYMENT_INVOIC",
                "C15:EXECUTING",
                "C15:UC_0NITFH",
                "C15:UC_TMIANK",
                "C15:UC_CRN1VJ",
                "C15:UC_0E0ZF9",
                "C15:UC_IYS2AL",
                "C15:UC_TUYDP6",
                "C15:1",
                "C15:6",
                "C15:2",
                "C15:3",
                "C15:4",
                "C15:UC_BCU6T4",
                "C15:UC_PZBQIN",
            ],
            "include_all_active": True,
            "include_fresh_deals": True,
            "include_portfolio": True,
        },
        "signals": {
            "overdue_task": True,
            "no_dated_next_step": True,
            "post_proposal_without_control": True,
            "control_date_due": True,
            "payment_without_movement": True,
            "questionable_closure": True,
            "negative_fresh_lead": True,
            "call_method_gap": True,
            "meaningful_change_after_review": True,
        },
        "review_view": "active",
        "limits": {
            "workset": 15,
            "new_slots": 10,
            "backlog_slots": 5,
            "paid_per_run": 5,
            "paid_per_day": 5,
        },
        "analysis": {
            "history_days": 60,
            "include_related": True,
            "include_internal": True,
            "download_audio": True,
            "redownload_audio": False,
            "transcribe_audio": True,
            "transcript_mode": "all",
            "force_llm": False,
        },
    }


def _normalize_analysis_profile(payload: dict[str, Any] | None) -> dict[str, Any]:
    base = default_analysis_profile()
    incoming = payload if isinstance(payload, dict) else {}
    for key in ("timezone", "period_preset", "review_view"):
        if key in incoming:
            base[key] = incoming[key]
    for section in ("lead", "deal", "signals", "limits", "analysis"):
        value = incoming.get(section)
        if isinstance(value, dict):
            base[section].update(value)
    return base


def _repair_utf8_mojibake_text(value: str) -> str:
    """Восстанавливает UTF-8, однажды ошибочно декодированный как Latin-1."""
    repaired = str(value)
    for _ in range(3):
        has_c1_controls = any(0x80 <= ord(char) <= 0x9F for char in repaired)
        has_utf8_markers = repaired.count("Ð") + repaired.count("Ñ") >= 2
        if not has_c1_controls and not has_utf8_markers:
            break
        try:
            candidate = repaired.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            break
        if candidate == repaired:
            break
        repaired = candidate
    return repaired


def _repair_utf8_mojibake(value: Any) -> Any:
    if isinstance(value, str):
        return _repair_utf8_mojibake_text(value)
    if isinstance(value, list):
        return [_repair_utf8_mojibake(item) for item in value]
    if isinstance(value, dict):
        return {key: _repair_utf8_mojibake(item) for key, item in value.items()}
    return value


def _repair_analysis_profile_rows(conn: sqlite3.Connection) -> int:
    """Однократно исправляет уже сохранённые повреждённые строки профилей."""
    repaired_count = 0
    rows = conn.execute("SELECT id, name, profile_json FROM analysis_profiles").fetchall()
    for row in rows:
        name = str(row["name"] or "")
        profile = loads_json(row["profile_json"], {})
        repaired_name = _repair_utf8_mojibake_text(name)
        repaired_profile = _repair_utf8_mojibake(profile)
        if repaired_name == name and repaired_profile == profile:
            continue
        conn.execute(
            "UPDATE analysis_profiles SET name = ?, profile_json = ? WHERE id = ?",
            (repaired_name, dumps_json(repaired_profile), int(row["id"])),
        )
        repaired_count += 1
    return repaired_count


def _row_to_analysis_profile(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    value = dict(row)
    value["name"] = _repair_utf8_mojibake_text(str(value.get("name") or ""))
    profile = _repair_utf8_mojibake(loads_json(value.pop("profile_json"), {}))
    value["profile"] = _normalize_analysis_profile(profile)
    return value


def _legacy_filter_profile(conn: sqlite3.Connection) -> dict[str, Any]:
    profile = default_analysis_profile()
    row = conn.execute(
        "SELECT filter_json FROM ui_candidate_filters WHERE profile_key = ?",
        (DEFAULT_CANDIDATE_FILTER_PROFILE,),
    ).fetchone()
    if not row:
        return profile
    legacy = loads_json(row["filter_json"], {})
    if not isinstance(legacy, dict):
        return profile
    entity_type = str(legacy.get("entity_type") or "lead")
    stages = [str(item) for item in legacy.get("stage_ids") or [] if str(item)]
    pipelines = [str(item) for item in legacy.get("pipeline_ids") or [] if str(item)]
    if entity_type == "lead" and stages:
        profile["lead"]["all_stages"] = False
        profile["lead"]["stage_ids"] = stages
    if entity_type == "deal":
        if pipelines:
            profile["deal"]["pipeline_ids"] = pipelines
        if stages:
            profile["deal"]["stage_ids"] = stages
    if legacy.get("review_view") in {"active", "reviewed", "all"}:
        profile["review_view"] = legacy["review_view"]
    return profile


def ensure_default_analysis_profile(db_path: str | Path) -> dict[str, Any]:
    """Создаёт согласованный default; старый фильтр сохраняет отдельным импортированным профилем."""
    init_db(db_path)
    with connect(db_path) as conn:
        _repair_analysis_profile_rows(conn)
        row = conn.execute("SELECT * FROM analysis_profiles ORDER BY id LIMIT 1").fetchone()
        if row:
            return _row_to_analysis_profile(row) or {}
        now = utcish_now()
        default_profile = default_analysis_profile()
        cursor = conn.execute(
            """
            INSERT INTO analysis_profiles (name, profile_json, version, created_at, updated_at)
            VALUES (?, ?, 1, ?, ?)
            """,
            (DEFAULT_ANALYSIS_PROFILE_NAME, dumps_json(default_profile), now, now),
        )
        profile_id = int(cursor.lastrowid)
        legacy_profile = _legacy_filter_profile(conn)
        if legacy_profile != default_profile:
            conn.execute(
                """
                INSERT INTO analysis_profiles (name, profile_json, version, created_at, updated_at)
                VALUES (?, ?, 1, ?, ?)
                """,
                ("Импортированный фильтр кандидатов", dumps_json(legacy_profile), now, now),
            )
        conn.execute(
            """
            INSERT INTO ui_preferences (preference_key, value_json, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(preference_key) DO UPDATE SET
                value_json = excluded.value_json,
                updated_at = excluded.updated_at
            """,
            (LAST_ANALYSIS_PROFILE_PREFERENCE, dumps_json(profile_id), now),
        )
        row = conn.execute("SELECT * FROM analysis_profiles WHERE id = ?", (profile_id,)).fetchone()
    return _row_to_analysis_profile(row) or {}


def list_analysis_profiles(db_path: str | Path) -> list[dict[str, Any]]:
    ensure_default_analysis_profile(db_path)
    with connect(db_path) as conn:
        rows = conn.execute("SELECT * FROM analysis_profiles ORDER BY name COLLATE NOCASE, id").fetchall()
    return [_row_to_analysis_profile(row) for row in rows if row is not None]


def get_analysis_profile(db_path: str | Path, profile_id: int) -> dict[str, Any] | None:
    ensure_default_analysis_profile(db_path)
    with connect(db_path) as conn:
        row = conn.execute("SELECT * FROM analysis_profiles WHERE id = ?", (int(profile_id),)).fetchone()
    return _row_to_analysis_profile(row)


def create_analysis_profile(
    db_path: str | Path,
    *,
    name: str,
    profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    init_db(db_path)
    clean_name = _repair_utf8_mojibake_text(str(name or "").strip())
    if not clean_name:
        raise ValueError("Название профиля обязательно")
    now = utcish_now()
    with connect(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO analysis_profiles (name, profile_json, version, created_at, updated_at)
            VALUES (?, ?, 1, ?, ?)
            """,
            (clean_name, dumps_json(_normalize_analysis_profile(_repair_utf8_mojibake(profile))), now, now),
        )
        profile_id = int(cursor.lastrowid)
        row = conn.execute("SELECT * FROM analysis_profiles WHERE id = ?", (profile_id,)).fetchone()
    return _row_to_analysis_profile(row) or {}


def update_analysis_profile(
    db_path: str | Path,
    profile_id: int,
    *,
    name: str,
    profile: dict[str, Any],
) -> dict[str, Any]:
    init_db(db_path)
    clean_name = _repair_utf8_mojibake_text(str(name or "").strip())
    if not clean_name:
        raise ValueError("Название профиля обязательно")
    with connect(db_path) as conn:
        cursor = conn.execute(
            """
            UPDATE analysis_profiles
            SET name = ?, profile_json = ?, version = version + 1, updated_at = ?
            WHERE id = ?
            """,
            (
                clean_name,
                dumps_json(_normalize_analysis_profile(_repair_utf8_mojibake(profile))),
                utcish_now(),
                int(profile_id),
            ),
        )
        if cursor.rowcount == 0:
            raise KeyError("Профиль не найден")
        row = conn.execute("SELECT * FROM analysis_profiles WHERE id = ?", (int(profile_id),)).fetchone()
    return _row_to_analysis_profile(row) or {}


def delete_analysis_profile(db_path: str | Path, profile_id: int) -> int:
    """Удаляет профиль, но не позволяет оставить UI без единого профиля."""
    ensure_default_analysis_profile(db_path)
    with connect(db_path) as conn:
        count = int(conn.execute("SELECT COUNT(*) FROM analysis_profiles").fetchone()[0])
        if count <= 1:
            raise ValueError("Нельзя удалить единственный профиль")
        cursor = conn.execute("DELETE FROM analysis_profiles WHERE id = ?", (int(profile_id),))
        if cursor.rowcount == 0:
            raise KeyError("Профиль не найден")
        fallback = conn.execute("SELECT id FROM analysis_profiles ORDER BY id LIMIT 1").fetchone()
        preference = conn.execute(
            "SELECT value_json FROM ui_preferences WHERE preference_key = ?",
            (LAST_ANALYSIS_PROFILE_PREFERENCE,),
        ).fetchone()
        selected = loads_json(preference["value_json"], None) if preference else None
        if int(selected or 0) == int(profile_id) and fallback:
            conn.execute(
                """
                INSERT INTO ui_preferences (preference_key, value_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(preference_key) DO UPDATE SET
                    value_json = excluded.value_json,
                    updated_at = excluded.updated_at
                """,
                (LAST_ANALYSIS_PROFILE_PREFERENCE, dumps_json(int(fallback["id"])), utcish_now()),
            )
    return int(fallback["id"]) if fallback else 0


def set_last_analysis_profile(db_path: str | Path, profile_id: int) -> dict[str, Any]:
    profile = get_analysis_profile(db_path, profile_id)
    if not profile:
        raise KeyError("Профиль не найден")
    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO ui_preferences (preference_key, value_json, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(preference_key) DO UPDATE SET
                value_json = excluded.value_json,
                updated_at = excluded.updated_at
            """,
            (LAST_ANALYSIS_PROFILE_PREFERENCE, dumps_json(int(profile_id)), utcish_now()),
        )
    return profile


def get_last_analysis_profile(db_path: str | Path) -> dict[str, Any]:
    fallback = ensure_default_analysis_profile(db_path)
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT value_json FROM ui_preferences WHERE preference_key = ?",
            (LAST_ANALYSIS_PROFILE_PREFERENCE,),
        ).fetchone()
    selected_id = loads_json(row["value_json"], None) if row else None
    if selected_id is not None:
        selected = get_analysis_profile(db_path, int(selected_id))
        if selected:
            return selected
    set_last_analysis_profile(db_path, int(fallback["id"]))
    return fallback


def reconcile_candidate_cases(
    db_path: str | Path,
    candidates: list[dict[str, Any]],
    *,
    as_of: str,
) -> list[dict[str, Any]]:
    """Назначает new/backlog/reactivation и сохраняет только локальный lifecycle."""
    init_db(db_path)
    as_of_date = str(as_of)[:10]
    with connect(db_path) as conn:
        for item in candidates:
            journey_key = str(item.get("journey_key") or "").strip()
            if not journey_key:
                continue
            signal_hash = str(item.get("signal_hash") or "")
            row = conn.execute(
                "SELECT * FROM candidate_cases WHERE journey_key = ?",
                (journey_key,),
            ).fetchone()
            if row is None:
                lifecycle = str(item.get("lifecycle") or "new")
                conn.execute(
                    """
                    INSERT INTO candidate_cases (
                        journey_key, origin_lead_id, current_entity_type, current_entity_id,
                        lifecycle_state, signal_hash, first_seen_at, last_seen_at, last_changed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        journey_key,
                        item.get("origin_lead_id"),
                        str(item.get("entity_type") or ""),
                        str(item.get("entity_id") or ""),
                        lifecycle,
                        signal_hash,
                        as_of,
                        as_of,
                        as_of,
                    ),
                )
            else:
                previous_hash = str(row["signal_hash"] or "")
                first_seen_date = str(row["first_seen_at"] or "")[:10]
                if first_seen_date == as_of_date:
                    lifecycle = "new"
                elif item.get("lifecycle") == "reactivation" or (previous_hash and signal_hash != previous_hash):
                    lifecycle = "reactivation"
                else:
                    lifecycle = "backlog"
                conn.execute(
                    """
                    UPDATE candidate_cases
                    SET origin_lead_id = ?, current_entity_type = ?, current_entity_id = ?,
                        lifecycle_state = ?, signal_hash = ?, last_seen_at = ?,
                        last_changed_at = CASE WHEN signal_hash <> ? THEN ? ELSE last_changed_at END,
                        resolved_at = NULL
                    WHERE journey_key = ?
                    """,
                    (
                        item.get("origin_lead_id"),
                        str(item.get("entity_type") or ""),
                        str(item.get("entity_id") or ""),
                        lifecycle,
                        signal_hash,
                        as_of,
                        signal_hash,
                        as_of,
                        journey_key,
                    ),
                )
            item["lifecycle"] = lifecycle
    return candidates


def create_daily_summary_run(
    db_path: str | Path,
    *,
    profile: dict[str, Any],
    period: dict[str, Any],
    scope: dict[str, Any],
    candidates: list[dict[str, Any]],
    selected_journey_keys: list[str],
    cost_preview: dict[str, Any],
) -> dict[str, Any]:
    """Создаёт immutable snapshot ручной сводки; Bitrix и OpenAI не вызываются."""
    init_db(db_path)
    selected = {str(item) for item in selected_journey_keys if str(item)}
    cost_preview = dict(cost_preview)
    profile_settings = profile.get("profile") if isinstance(profile.get("profile"), dict) else profile
    limits = profile_settings.get("limits") if isinstance(profile_settings.get("limits"), dict) else {}
    per_run = max(0, int(limits.get("paid_per_run") or 0))
    per_day = max(0, int(limits.get("paid_per_day") or 0))
    day_prefix = str(period.get("as_of") or utcish_now())[:10]
    used_today = daily_paid_capacity_used(db_path, day_prefix=day_prefix)
    cost_preview.update(
        {
            "paid_per_run_limit": per_run,
            "paid_per_day_limit": per_day,
            "paid_used_today": used_today,
            "paid_entity_limit": min(per_run, max(0, per_day - used_today)),
        }
    )
    selected_items = [item for item in candidates if str(item.get("journey_key") or "") in selected]
    llm_required = [
        item for item in selected_items
        if str(item.get("analysis_freshness") or "missing") in {"missing", "changed", "failed"}
    ]
    paid_limit = max(0, int(cost_preview.get("paid_entity_limit") or 0))
    now = utcish_now()
    with connect(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO daily_summary_runs (
                profile_id, profile_name, profile_version, profile_snapshot_json,
                period_json, scope_snapshot_json, status, selected_count,
                llm_required_count, llm_allowed_count, cost_preview_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'draft', ?, ?, ?, ?, ?)
            """,
            (
                profile.get("id"),
                str(profile.get("name") or "Профиль"),
                int(profile.get("version") or 1),
                dumps_json(profile.get("profile") if isinstance(profile.get("profile"), dict) else profile),
                dumps_json(period),
                dumps_json(scope),
                len(selected_items),
                len(llm_required),
                min(len(llm_required), paid_limit),
                dumps_json(cost_preview),
                now,
            ),
        )
        run_id = int(cursor.lastrowid)
        for item in candidates:
            conn.execute(
                """
                INSERT INTO daily_summary_items (
                    run_id, journey_key, entity_type, entity_id, origin_lead_id,
                    lifecycle_state, selected, candidate_snapshot_json,
                    processing_status, progress_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '{}', ?)
                """,
                (
                    run_id,
                    str(item.get("journey_key") or ""),
                    str(item.get("entity_type") or ""),
                    str(item.get("entity_id") or ""),
                    item.get("origin_lead_id"),
                    str(item.get("lifecycle") or "new"),
                    int(str(item.get("journey_key") or "") in selected),
                    dumps_json(item),
                    "draft" if str(item.get("journey_key") or "") in selected else "reserve",
                    now,
                ),
            )
    return get_daily_summary_run(db_path, run_id) or {}


def _row_to_daily_summary(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    value = dict(row)
    value["profile_snapshot"] = loads_json(value.pop("profile_snapshot_json"), {})
    value["period"] = loads_json(value.pop("period_json"), {})
    value["scope"] = loads_json(value.pop("scope_snapshot_json"), {})
    value["cost_preview"] = loads_json(value.pop("cost_preview_json"), {})
    value["actual_cost"] = loads_json(value.pop("actual_cost_json", None), None)
    return value


def get_daily_summary_run(db_path: str | Path, run_id: int) -> dict[str, Any] | None:
    init_db(db_path)
    with connect(db_path) as conn:
        row = conn.execute("SELECT * FROM daily_summary_runs WHERE id = ?", (int(run_id),)).fetchone()
        if row is None:
            return None
        item_rows = conn.execute(
            "SELECT * FROM daily_summary_items WHERE run_id = ? ORDER BY selected DESC, id",
            (int(run_id),),
        ).fetchall()
    value = _row_to_daily_summary(row) or {}
    items = []
    for item_row in item_rows:
        item = dict(item_row)
        item["candidate"] = loads_json(item.pop("candidate_snapshot_json"), {})
        item["progress"] = loads_json(item.pop("progress_json", None), {})
        items.append(item)
    value["items"] = items
    return value


def list_daily_summary_runs(db_path: str | Path, *, limit: int = 30) -> list[dict[str, Any]]:
    init_db(db_path)
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM daily_summary_runs ORDER BY id DESC LIMIT ?",
            (max(1, min(int(limit), 100)),),
        ).fetchall()
    return [_row_to_daily_summary(row) or {} for row in rows]


def attach_job_to_daily_summary(db_path: str | Path, run_id: int, job_id: str) -> dict[str, Any]:
    init_db(db_path)
    with connect(db_path) as conn:
        cursor = conn.execute(
            """
            UPDATE daily_summary_runs
            SET status = CASE WHEN status = 'draft' THEN 'analyzing' ELSE status END, job_id = ?
            WHERE id = ? AND job_id IS NULL
            """,
            (str(job_id), int(run_id)),
        )
        if cursor.rowcount == 0:
            raise ValueError("Сводка уже запущена или не найдена")
    return get_daily_summary_run(db_path, run_id) or {}


def prepare_daily_summary_items(
    db_path: str | Path,
    run_id: int,
    eligible_journey_keys: list[str],
) -> None:
    init_db(db_path)
    eligible = {str(item) for item in eligible_journey_keys}
    now = utcish_now()
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT id, journey_key FROM daily_summary_items WHERE run_id = ? AND selected = 1",
            (int(run_id),),
        ).fetchall()
        for row in rows:
            status = "queued" if str(row["journey_key"]) in eligible else "skipped_limit"
            detail = "Ожидает запуска" if status == "queued" else "Не запущено из-за платного лимита"
            progress = {
                "stage": "queued" if status == "queued" else "skipped",
                "status": status,
                "detail": detail,
                "updated_at": now,
            }
            conn.execute(
                """
                UPDATE daily_summary_items
                SET processing_status = ?, progress_json = ?, error = NULL, updated_at = ?
                WHERE id = ?
                """,
                (status, dumps_json(progress), now, int(row["id"])),
            )


def register_daily_summary_job(
    db_path: str | Path,
    run_id: int,
    job_id: str,
    entity_type: str,
    entity_ids: list[str],
) -> None:
    init_db(db_path)
    now = utcish_now()
    ids = {str(item) for item in entity_ids}
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT id, entity_id FROM daily_summary_items WHERE run_id = ? AND entity_type = ? AND selected = 1",
            (int(run_id), str(entity_type)),
        ).fetchall()
        for row in rows:
            if str(row["entity_id"]) not in ids:
                continue
            conn.execute(
                """
                UPDATE daily_summary_items
                SET job_id = ?, processing_status = 'queued', updated_at = ?
                WHERE id = ?
                """,
                (str(job_id), now, int(row["id"])),
            )


def update_daily_summary_item_progress(
    db_path: str | Path,
    run_id: int,
    progress: dict[str, Any],
) -> None:
    init_db(db_path)
    status = str(progress.get("status") or "running")
    stage = str(progress.get("stage") or "")
    if status == "error" or stage == "error":
        processing_status = "error"
    elif status == "done" and stage == "done":
        processing_status = "done"
    else:
        processing_status = "running"
    now = str(progress.get("updated_at") or utcish_now())
    with connect(db_path) as conn:
        conn.execute(
            """
            UPDATE daily_summary_items
            SET processing_status = ?, progress_json = ?, error = ?, updated_at = ?
            WHERE run_id = ? AND entity_type = ? AND entity_id = ? AND selected = 1
            """,
            (
                processing_status,
                dumps_json(progress),
                progress.get("error") if processing_status == "error" else None,
                now,
                int(run_id),
                str(progress.get("entity_type") or ""),
                str(progress.get("entity_id") or ""),
            ),
        )
    refresh_daily_summary_run_status(db_path, run_id)


def complete_daily_summary_item(
    db_path: str | Path,
    run_id: int,
    *,
    entity_type: str,
    entity_id: str,
    report_id: int | None,
    error: str | None = None,
) -> None:
    init_db(db_path)
    now = utcish_now()
    status = "error" if error else "done"
    with connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT progress_json FROM daily_summary_items
            WHERE run_id = ? AND entity_type = ? AND entity_id = ? AND selected = 1
            """,
            (int(run_id), str(entity_type), str(entity_id)),
        ).fetchone()
        previous_progress = loads_json(row[0] if row else None, {})
        progress = {
            **previous_progress,
            "entity_type": str(entity_type),
            "entity_id": str(entity_id),
            "stage": "error" if error else "done",
            "status": status,
            "detail": "Анализ не сформирован" if error else "Отчёт готов",
            "error": error,
            "updated_at": now,
        }
        conn.execute(
            """
            UPDATE daily_summary_items
            SET processing_status = ?, progress_json = ?, report_id = COALESCE(?, report_id),
                error = ?, updated_at = ?
            WHERE run_id = ? AND entity_type = ? AND entity_id = ? AND selected = 1
            """,
            (status, dumps_json(progress), report_id, error, now, int(run_id), str(entity_type), str(entity_id)),
        )
    refresh_daily_summary_run_status(db_path, run_id)


def record_daily_summary_actual_cost(
    db_path: str | Path,
    run_id: int,
    *,
    entity_type: str,
    entity_id: str,
    cost: dict[str, Any],
) -> dict[str, Any]:
    init_db(db_path)
    key = f"{entity_type}:{entity_id}"
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT actual_cost_json FROM daily_summary_runs WHERE id = ?",
            (int(run_id),),
        ).fetchone()
        payload = loads_json(row[0] if row else None, {})
        entities = payload.get("entities") if isinstance(payload.get("entities"), dict) else {}
        entities[key] = dict(cost)
        total_rub = round(sum(float(item.get("estimated_cost_rub") or 0) for item in entities.values()), 2)
        total_usd = round(sum(float(item.get("estimated_cost_usd") or 0) for item in entities.values()), 6)
        payload = {
            "entities": entities,
            "estimated_cost_rub": total_rub,
            "estimated_cost_usd": total_usd,
            "updated_at": utcish_now(),
        }
        conn.execute(
            "UPDATE daily_summary_runs SET actual_cost_json = ? WHERE id = ?",
            (dumps_json(payload), int(run_id)),
        )
    return payload


def refresh_daily_summary_run_status(db_path: str | Path, run_id: int) -> str:
    init_db(db_path)
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT processing_status FROM daily_summary_items WHERE run_id = ? AND selected = 1",
            (int(run_id),),
        ).fetchall()
        statuses = [str(row[0] or "draft") for row in rows]
        if not statuses:
            status = "done"
        elif any(item in {"draft", "queued", "running"} for item in statuses):
            status = "analyzing"
        else:
            errors = sum(item == "error" for item in statuses)
            completed = sum(item in {"done", "skipped_limit"} for item in statuses)
            if errors and completed:
                status = "completed_with_errors"
            elif errors:
                status = "error"
            else:
                status = "done"
        completed_at = utcish_now() if status in {"done", "completed_with_errors", "error"} else None
        conn.execute(
            "UPDATE daily_summary_runs SET status = ?, completed_at = COALESCE(?, completed_at) WHERE id = ?",
            (status, completed_at, int(run_id)),
        )
    return status


def fail_orphaned_daily_summary_items(
    db_path: str | Path,
    run_id: int,
    *,
    active_job_ids: set[str] | None = None,
) -> int:
    """Завершает зависшие карточки, фоновые jobs которых исчезли после рестарта API."""
    init_db(db_path)
    active = {str(item) for item in (active_job_ids or set()) if item}
    now = utcish_now()
    error = "Процесс анализа прерван перезапуском сервера. Запустите новую сводку."
    updated = 0
    with connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT id, entity_type, entity_id, job_id, progress_json
            FROM daily_summary_items
            WHERE run_id = ? AND selected = 1
              AND COALESCE(processing_status, 'queued') IN ('draft', 'queued', 'running')
            """,
            (int(run_id),),
        ).fetchall()
        for row in rows:
            item_job_id = str(row["job_id"] or "")
            if item_job_id and item_job_id in active:
                continue
            previous_progress = loads_json(row["progress_json"], {})
            progress = {
                **previous_progress,
                "entity_type": str(row["entity_type"]),
                "entity_id": str(row["entity_id"]),
                "stage": "error",
                "status": "error",
                "detail": "Анализ прерван перезапуском сервера",
                "error": error,
                "updated_at": now,
            }
            conn.execute(
                """
                UPDATE daily_summary_items
                SET processing_status = 'error', progress_json = ?, error = ?, updated_at = ?
                WHERE id = ?
                """,
                (dumps_json(progress), error, now, int(row["id"])),
            )
            updated += 1
    if updated:
        refresh_daily_summary_run_status(db_path, run_id)
    return updated


def daily_paid_capacity_used(db_path: str | Path, *, day_prefix: str) -> int:
    """Считает уже зарезервированные платные карточки сводок за календарный день."""
    init_db(db_path)
    with connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT COALESCE(SUM(llm_allowed_count), 0)
            FROM daily_summary_runs
            WHERE substr(created_at, 1, 10) = ? AND status <> 'cancelled'
            """,
            (str(day_prefix),),
        ).fetchone()
    return int(row[0] or 0)
