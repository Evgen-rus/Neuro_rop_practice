"""
SQLite storage for local ROP assistant state.

The module intentionally uses the standard sqlite3 package: the current project
is a local file-based MVP, so adding an ORM would create more surface area than
the change-detection layer needs.
"""

from __future__ import annotations

import json
import secrets
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
                report_meta_json TEXT,
                technical_log_json TEXT,
                model_context_json TEXT,
                job_id TEXT,
                share_token TEXT UNIQUE
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

            CREATE TABLE IF NOT EXISTS lead_workflow_state (
                lead_id TEXT NOT NULL PRIMARY KEY,
                source_report_id INTEGER,
                manager_review_text TEXT,
                manager_message_options_json TEXT,
                manager_full_review_text TEXT,
                manager_task_text TEXT,
                review_completed INTEGER NOT NULL DEFAULT 0,
                task_completed INTEGER NOT NULL DEFAULT 0,
                control_mode TEXT,
                control_days INTEGER,
                control_date TEXT,
                control_completed INTEGER NOT NULL DEFAULT 0,
                final_decision TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(source_report_id) REFERENCES ui_reports(id)
            );

            CREATE TABLE IF NOT EXISTS local_migrations (
                migration_id TEXT NOT NULL PRIMARY KEY,
                applied_at TEXT NOT NULL
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

            CREATE TABLE IF NOT EXISTS deal_control_scope (
                scope_key TEXT NOT NULL PRIMARY KEY,
                initial_deal_ids_json TEXT NOT NULL,
                manager_ids_json TEXT NOT NULL,
                pipeline_id TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS deal_control_deals (
                deal_id TEXT NOT NULL PRIMARY KEY,
                source TEXT NOT NULL,
                title TEXT,
                manager_id TEXT,
                manager_name TEXT,
                stage_id TEXT,
                stage_name TEXT,
                pipeline_id TEXT,
                amount TEXT,
                currency_id TEXT,
                created_at_crm TEXT,
                modified_at_crm TEXT,
                is_active INTEGER NOT NULL DEFAULT 1,
                probability INTEGER,
                expected_payment_period TEXT,
                next_control_at TEXT,
                bitrix_tasks_json TEXT NOT NULL DEFAULT '[]',
                last_crm_sync_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS deal_control_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                deal_id TEXT NOT NULL,
                task_text TEXT NOT NULL,
                touch_type TEXT,
                expected_result TEXT,
                due_at TEXT NOT NULL,
                local_status TEXT NOT NULL DEFAULT 'active',
                crm_execution_status TEXT NOT NULL DEFAULT 'not_reflected',
                crm_match_activity_id TEXT,
                crm_match_confidence TEXT,
                crm_match_candidate_completed INTEGER,
                crm_match_confirmed INTEGER NOT NULL DEFAULT 0,
                business_result_status TEXT NOT NULL DEFAULT 'no_result',
                business_result_note TEXT,
                result_activity_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT,
                FOREIGN KEY(deal_id) REFERENCES deal_control_deals(deal_id)
            );

            CREATE INDEX IF NOT EXISTS idx_deal_control_tasks_deal_due
                ON deal_control_tasks(deal_id, due_at DESC, id DESC);

            CREATE TABLE IF NOT EXISTS deal_control_bitrix_task_state (
                activity_id TEXT NOT NULL PRIMARY KEY,
                deal_id TEXT NOT NULL,
                local_completed INTEGER NOT NULL DEFAULT 0,
                local_completed_at TEXT,
                local_completed_by TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(deal_id) REFERENCES deal_control_deals(deal_id)
            );

            CREATE INDEX IF NOT EXISTS idx_deal_control_bitrix_task_state_deal
                ON deal_control_bitrix_task_state(deal_id, activity_id);

            CREATE TABLE IF NOT EXISTS deal_control_task_reschedules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                previous_due_at TEXT NOT NULL,
                next_due_at TEXT NOT NULL,
                reason TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(task_id) REFERENCES deal_control_tasks(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS deal_control_task_crm_facts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                activity_id TEXT,
                fact_kind TEXT NOT NULL,
                summary TEXT,
                occurred_at TEXT,
                payload_json TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(task_id) REFERENCES deal_control_tasks(id) ON DELETE CASCADE
            );

            CREATE UNIQUE INDEX IF NOT EXISTS idx_deal_control_task_crm_facts_unique
                ON deal_control_task_crm_facts(task_id, activity_id, fact_kind);

            CREATE TABLE IF NOT EXISTS deal_control_task_guidance (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                task_revision INTEGER NOT NULL,
                source_report_id INTEGER NOT NULL,
                guidance_json TEXT NOT NULL,
                model_meta_json TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(task_id) REFERENCES deal_control_tasks(id) ON DELETE CASCADE,
                FOREIGN KEY(source_report_id) REFERENCES ui_reports(id)
            );

            CREATE UNIQUE INDEX IF NOT EXISTS idx_deal_control_task_guidance_version
                ON deal_control_task_guidance(task_id, task_revision, source_report_id);

            CREATE TABLE IF NOT EXISTS deal_control_task_baselines (
                task_id INTEGER NOT NULL PRIMARY KEY,
                deal_snapshot_json TEXT NOT NULL,
                source_report_id INTEGER,
                created_at TEXT NOT NULL,
                FOREIGN KEY(task_id) REFERENCES deal_control_tasks(id) ON DELETE CASCADE,
                FOREIGN KEY(source_report_id) REFERENCES ui_reports(id)
            );

            CREATE TABLE IF NOT EXISTS deal_control_task_outcomes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                contact_status TEXT NOT NULL,
                result_status TEXT NOT NULL,
                result_note TEXT,
                next_step_text TEXT,
                next_step_at TEXT,
                evidence_kind TEXT,
                evidence_id TEXT,
                source_role TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(task_id) REFERENCES deal_control_tasks(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_deal_control_task_outcomes_task
                ON deal_control_task_outcomes(task_id, id DESC);

            CREATE TABLE IF NOT EXISTS deal_control_task_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                event_key TEXT,
                payload_json TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(task_id) REFERENCES deal_control_tasks(id) ON DELETE CASCADE
            );

            CREATE UNIQUE INDEX IF NOT EXISTS idx_deal_control_task_events_key
                ON deal_control_task_events(task_id, event_key)
                WHERE event_key IS NOT NULL;
            """
        )
        _ensure_column(conn, "ui_reports", "report_meta_json", "TEXT")
        _ensure_column(conn, "ui_reports", "technical_log_json", "TEXT")
        _ensure_column(conn, "ui_reports", "model_context_json", "TEXT")
        _ensure_column(conn, "ui_reports", "share_token", "TEXT")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_ui_reports_share_token "
            "ON ui_reports(share_token) WHERE share_token IS NOT NULL"
        )
        _ensure_column(conn, "daily_summary_runs", "completed_at", "TEXT")
        _ensure_column(conn, "daily_summary_runs", "actual_cost_json", "TEXT")
        _ensure_column(conn, "daily_summary_items", "job_id", "TEXT")
        _ensure_column(conn, "daily_summary_items", "processing_status", "TEXT NOT NULL DEFAULT 'draft'")
        _ensure_column(conn, "daily_summary_items", "progress_json", "TEXT NOT NULL DEFAULT '{}'")
        _ensure_column(conn, "daily_summary_items", "error", "TEXT")
        _ensure_column(conn, "daily_summary_items", "updated_at", "TEXT")
        _ensure_column(conn, "lead_workflow_state", "manager_message_options_json", "TEXT")
        _ensure_column(conn, "lead_workflow_state", "manager_full_review_text", "TEXT")
        _ensure_column(conn, "deal_control_deals", "bitrix_tasks_json", "TEXT NOT NULL DEFAULT '[]'")
        _ensure_column(conn, "deal_control_tasks", "crm_match_candidate_completed", "INTEGER")
        _ensure_column(conn, "deal_control_tasks", "crm_match_confirmed", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "deal_control_tasks", "guidance_revision", "INTEGER NOT NULL DEFAULT 1")
        _ensure_column(conn, "deal_control_task_reschedules", "source_role", "TEXT")
        _ensure_column(conn, "deal_control_task_crm_facts", "contact_class", "TEXT NOT NULL DEFAULT 'unknown'")
        _ensure_column(conn, "deal_control_task_crm_facts", "review_status", "TEXT NOT NULL DEFAULT 'candidate'")
        _ensure_column(conn, "deal_control_task_crm_facts", "fact_key", "TEXT")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_deal_control_task_crm_facts_key "
            "ON deal_control_task_crm_facts(task_id, fact_key) WHERE fact_key IS NOT NULL"
        )

        migration_id = "2026-07-22-reactivate-lead-no-attention"
        migration_applied = conn.execute(
            "SELECT 1 FROM local_migrations WHERE migration_id = ?",
            (migration_id,),
        ).fetchone()
        if migration_applied is None:
            conn.execute(
                """
                UPDATE candidate_review_state
                SET state = 'active', next_control_date = NULL
                WHERE entity_type = 'lead'
                  AND state = 'reviewed'
                  AND decision = 'Не требует внимания'
                """
            )
            conn.execute(
                "UPDATE lead_workflow_state SET final_decision = NULL WHERE final_decision = 'no_attention'"
            )
            conn.execute(
                "INSERT INTO local_migrations (migration_id, applied_at) VALUES (?, ?)",
                (migration_id, utcish_now()),
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
    value["report_meta"] = loads_json(value.pop("report_meta_json", None), None)
    value["technical_log"] = loads_json(value.pop("technical_log_json", None), None)
    value["model_context"] = loads_json(value.pop("model_context_json", None), None)
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
    report_meta: dict[str, Any] | None = None,
    technical_log: dict[str, Any] | None = None,
    model_context: dict[str, Any] | None = None,
    job_id: str | None = None,
) -> int:
    init_db(db_path)
    share_token = secrets.token_urlsafe(24)
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
                report_meta_json,
                technical_log_json,
                model_context_json,
                job_id,
                share_token
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                dumps_json(report_meta) if report_meta is not None else None,
                dumps_json(technical_log) if technical_log is not None else None,
                dumps_json(model_context) if model_context is not None else None,
                job_id,
                share_token,
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


def get_ui_report_by_share_token(db_path: str | Path, share_token: str) -> dict[str, Any] | None:
    init_db(db_path)
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM ui_reports WHERE share_token = ?",
            (share_token,),
        ).fetchone()
    return _row_to_ui_report(row)


def get_or_create_ui_report_share_token(db_path: str | Path, report_id: int) -> str | None:
    """Return a stable opaque review token, including for reports saved before this feature."""
    init_db(db_path)
    with connect(db_path) as conn:
        row = conn.execute("SELECT share_token FROM ui_reports WHERE id = ?", (int(report_id),)).fetchone()
        if row is None:
            return None
        if row["share_token"]:
            return str(row["share_token"])
        for _ in range(3):
            token = secrets.token_urlsafe(24)
            try:
                cursor = conn.execute(
                    "UPDATE ui_reports SET share_token = ? WHERE id = ? AND share_token IS NULL",
                    (token, int(report_id)),
                )
            except sqlite3.IntegrityError:
                continue
            if cursor.rowcount:
                return token
            existing = conn.execute("SELECT share_token FROM ui_reports WHERE id = ?", (int(report_id),)).fetchone()
            if existing and existing["share_token"]:
                return str(existing["share_token"])
    raise RuntimeError("Не удалось создать токен просмотра отчёта")


def list_entity_ui_reports(
    db_path: str | Path,
    *,
    entity_type: str,
    entity_id: str,
    limit: int = 20,
) -> list[dict[str, Any]]:
    init_db(db_path)
    with connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT * FROM ui_reports
            WHERE entity_type = ? AND entity_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (str(entity_type), str(entity_id), int(limit)),
        ).fetchall()
    return [_row_to_ui_report(row) for row in rows if row is not None]


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


def get_lead_workflow_state(db_path: str | Path, lead_id: str) -> dict[str, Any] | None:
    init_db(db_path)
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM lead_workflow_state WHERE lead_id = ?",
            (str(lead_id),),
        ).fetchone()
    if row is None:
        return None
    value = dict(row)
    value["manager_message_options"] = loads_json(value.pop("manager_message_options_json", None), [])
    for field in ("review_completed", "task_completed", "control_completed"):
        value[field] = bool(value.get(field))
    return value


def upsert_lead_workflow_state(
    db_path: str | Path,
    *,
    lead_id: str,
    source_report_id: int | None,
    manager_review_text: str | None,
    manager_message_options: list[str] | None,
    manager_task_text: str | None,
    review_completed: bool,
    task_completed: bool,
    control_mode: str | None,
    control_days: int | None,
    control_date: str | None,
    control_completed: bool,
    final_decision: str | None,
    manager_full_review_text: str | None = None,
) -> dict[str, Any]:
    init_db(db_path)
    now = utcish_now()
    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO lead_workflow_state (
                lead_id, source_report_id, manager_review_text, manager_message_options_json, manager_full_review_text, manager_task_text,
                review_completed, task_completed, control_mode, control_days,
                control_date, control_completed, final_decision, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(lead_id) DO UPDATE SET
                source_report_id = excluded.source_report_id,
                manager_review_text = excluded.manager_review_text,
                manager_message_options_json = excluded.manager_message_options_json,
                manager_full_review_text = excluded.manager_full_review_text,
                manager_task_text = excluded.manager_task_text,
                review_completed = excluded.review_completed,
                task_completed = excluded.task_completed,
                control_mode = excluded.control_mode,
                control_days = excluded.control_days,
                control_date = excluded.control_date,
                control_completed = excluded.control_completed,
                final_decision = excluded.final_decision,
                updated_at = excluded.updated_at
            """,
            (
                str(lead_id), source_report_id, manager_review_text,
                dumps_json(manager_message_options or []), manager_full_review_text, manager_task_text,
                int(review_completed), int(task_completed), control_mode, control_days,
                control_date, int(control_completed), final_decision, now, now,
            ),
        )
    result = get_lead_workflow_state(db_path, str(lead_id))
    assert result is not None
    return result


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
        "period_preset": "today_and_previous_workday",
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
    for key in ("period_preset", "review_view"):
        if key in incoming:
            base[key] = incoming[key]
    legacy_periods = {
        "yesterday": "previous_workday",
        "today_and_yesterday": "today_and_previous_workday",
    }
    base["period_preset"] = legacy_periods.get(str(base["period_preset"]), base["period_preset"])
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


# Deal control is separate from the lead workflow: it tracks local ROP tasks and
# only observes the corresponding read-only CRM facts.
DEAL_CONTROL_SCOPE_KEY = "active"


def _normalize_deal_control_bitrix_tasks(value: Any) -> list[dict[str, Any]]:
    """Accept list or legacy `{tasks, scheduled_activities}` wrapper from DB."""
    if isinstance(value, dict):
        value = value.get("tasks")
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _row_to_deal_control_deal(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    value = dict(row)
    value["is_active"] = bool(value.get("is_active"))
    value["bitrix_tasks"] = _normalize_deal_control_bitrix_tasks(
        loads_json(value.pop("bitrix_tasks_json", None), [])
    )
    return value


def get_deal_control_scope(db_path: str | Path) -> dict[str, Any]:
    init_db(db_path)
    with connect(db_path) as conn:
        row = conn.execute("SELECT * FROM deal_control_scope WHERE scope_key = ?", (DEAL_CONTROL_SCOPE_KEY,)).fetchone()
    if row is None:
        return {"initial_deal_ids": [], "manager_ids": [], "pipeline_id": "15", "configured": False}
    value = dict(row)
    return {
        "initial_deal_ids": loads_json(value.get("initial_deal_ids_json"), []),
        "manager_ids": loads_json(value.get("manager_ids_json"), []),
        "pipeline_id": str(value.get("pipeline_id") or "15"),
        "updated_at": value.get("updated_at"),
        "configured": True,
    }


def save_deal_control_scope(
    db_path: str | Path,
    *,
    initial_deal_ids: list[str],
    manager_ids: list[str],
    pipeline_id: str,
) -> dict[str, Any]:
    init_db(db_path)
    deals = list(dict.fromkeys(str(value).strip() for value in initial_deal_ids if str(value).strip()))
    managers = list(dict.fromkeys(str(value).strip() for value in manager_ids if str(value).strip()))
    if not deals and not managers:
        raise ValueError("Нужен хотя бы один ID сделки или ответственного")
    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO deal_control_scope (scope_key, initial_deal_ids_json, manager_ids_json, pipeline_id, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(scope_key) DO UPDATE SET
                initial_deal_ids_json = excluded.initial_deal_ids_json,
                manager_ids_json = excluded.manager_ids_json,
                pipeline_id = excluded.pipeline_id,
                updated_at = excluded.updated_at
            """,
            (DEAL_CONTROL_SCOPE_KEY, dumps_json(deals), dumps_json(managers), str(pipeline_id).strip() or "15", utcish_now()),
        )
    return get_deal_control_scope(db_path)


def upsert_deal_control_deal(db_path: str | Path, *, deal_id: str, source: str, title: str | None,
                             manager_id: str | None, manager_name: str | None, stage_id: str | None,
                             stage_name: str | None, pipeline_id: str | None, amount: str | None,
                             currency_id: str | None, created_at_crm: str | None, modified_at_crm: str | None,
                             is_active: bool) -> dict[str, Any]:
    init_db(db_path)
    now = utcish_now()
    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO deal_control_deals (
                deal_id, source, title, manager_id, manager_name, stage_id, stage_name, pipeline_id,
                amount, currency_id, created_at_crm, modified_at_crm, is_active, last_crm_sync_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(deal_id) DO UPDATE SET
                source = excluded.source, title = excluded.title, manager_id = excluded.manager_id,
                manager_name = excluded.manager_name, stage_id = excluded.stage_id, stage_name = excluded.stage_name,
                pipeline_id = excluded.pipeline_id, amount = excluded.amount, currency_id = excluded.currency_id,
                created_at_crm = excluded.created_at_crm, modified_at_crm = excluded.modified_at_crm,
                is_active = excluded.is_active, last_crm_sync_at = excluded.last_crm_sync_at, updated_at = excluded.updated_at
            """,
            (str(deal_id), source, title, manager_id, manager_name, stage_id, stage_name, pipeline_id, amount,
             currency_id, created_at_crm, modified_at_crm, int(is_active), now, now, now),
        )
        row = conn.execute("SELECT * FROM deal_control_deals WHERE deal_id = ?", (str(deal_id),)).fetchone()
    result = _row_to_deal_control_deal(row)
    assert result is not None
    return result


def update_deal_control_fields(db_path: str | Path, *, deal_id: str, probability: int | None,
                               expected_payment_period: str | None, next_control_at: str | None) -> dict[str, Any]:
    init_db(db_path)
    with connect(db_path) as conn:
        if conn.execute("SELECT 1 FROM deal_control_deals WHERE deal_id = ?", (str(deal_id),)).fetchone() is None:
            raise ValueError("Сделка ещё не добавлена в контур контроля")
        conn.execute(
            "UPDATE deal_control_deals SET probability = ?, expected_payment_period = ?, next_control_at = ?, updated_at = ? WHERE deal_id = ?",
            (probability, expected_payment_period, next_control_at, utcish_now(), str(deal_id)),
        )
        row = conn.execute("SELECT * FROM deal_control_deals WHERE deal_id = ?", (str(deal_id),)).fetchone()
    result = _row_to_deal_control_deal(row)
    assert result is not None
    return result


def save_deal_control_bitrix_tasks(
    db_path: str | Path,
    *,
    deal_id: str,
    tasks: list[dict[str, Any]],
) -> dict[str, Any]:
    init_db(db_path)
    with connect(db_path) as conn:
        if conn.execute("SELECT 1 FROM deal_control_deals WHERE deal_id = ?", (str(deal_id),)).fetchone() is None:
            raise ValueError("Сделка ещё не добавлена в контур контроля")
        conn.execute(
            "UPDATE deal_control_deals SET bitrix_tasks_json = ?, updated_at = ? WHERE deal_id = ?",
            (dumps_json(tasks), utcish_now(), str(deal_id)),
        )
        row = conn.execute("SELECT * FROM deal_control_deals WHERE deal_id = ?", (str(deal_id),)).fetchone()
    result = _row_to_deal_control_deal(row)
    assert result is not None
    return result


def set_deal_control_bitrix_task_completion(
    db_path: str | Path,
    *,
    deal_id: str,
    activity_id: str,
    completed: bool,
    source_role: str,
) -> dict[str, Any]:
    init_db(db_path)
    normalized_role = str(source_role or "").strip().lower()
    if normalized_role not in {"manager", "rop"}:
        raise ValueError("Роль должна быть manager или rop")
    normalized_activity_id = str(activity_id or "").strip()
    if not normalized_activity_id:
        raise ValueError("Не указан ID задачи Bitrix")
    now = utcish_now()
    with connect(db_path) as conn:
        deal = conn.execute(
            "SELECT bitrix_tasks_json FROM deal_control_deals WHERE deal_id = ?",
            (str(deal_id),),
        ).fetchone()
        if deal is None:
            raise ValueError("Сделка ещё не добавлена в контур контроля")
        tasks = _normalize_deal_control_bitrix_tasks(
            loads_json(deal["bitrix_tasks_json"], [])
        )
        if not any(str(item.get("activity_id") or "") == normalized_activity_id for item in tasks):
            raise ValueError("Задача Bitrix не найдена в текущем снимке сделки")
        conn.execute(
            """
            INSERT INTO deal_control_bitrix_task_state (
                activity_id, deal_id, local_completed, local_completed_at,
                local_completed_by, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(activity_id) DO UPDATE SET
                deal_id = excluded.deal_id,
                local_completed = excluded.local_completed,
                local_completed_at = excluded.local_completed_at,
                local_completed_by = excluded.local_completed_by,
                updated_at = excluded.updated_at
            """,
            (
                normalized_activity_id,
                str(deal_id),
                int(completed),
                now if completed else None,
                normalized_role,
                now,
                now,
            ),
        )
        row = conn.execute(
            "SELECT * FROM deal_control_bitrix_task_state WHERE activity_id = ?",
            (normalized_activity_id,),
        ).fetchone()
    value = dict(row)
    value["local_completed"] = bool(value.get("local_completed"))
    return value


def list_deal_control_bitrix_task_states(
    db_path: str | Path,
    *,
    deal_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    init_db(db_path)
    query = "SELECT * FROM deal_control_bitrix_task_state"
    params: list[Any] = []
    if deal_ids:
        placeholders = ", ".join("?" for _ in deal_ids)
        query += f" WHERE deal_id IN ({placeholders})"
        params.extend(str(value) for value in deal_ids)
    query += " ORDER BY updated_at DESC, activity_id DESC"
    with connect(db_path) as conn:
        rows = conn.execute(query, params).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        value = dict(row)
        value["local_completed"] = bool(value.get("local_completed"))
        result.append(value)
    return result


def list_deal_control_deals(db_path: str | Path, *, active_only: bool = True) -> list[dict[str, Any]]:
    init_db(db_path)
    query = "SELECT * FROM deal_control_deals" + (" WHERE is_active = 1" if active_only else "")
    query += " ORDER BY COALESCE(next_control_at, '9999-12-31') ASC, modified_at_crm DESC, deal_id DESC"
    with connect(db_path) as conn:
        rows = conn.execute(query).fetchall()
    return [_row_to_deal_control_deal(row) for row in rows if row is not None]


def set_deal_control_deal_active(db_path: str | Path, *, deal_id: str, is_active: bool) -> None:
    init_db(db_path)
    with connect(db_path) as conn:
        conn.execute(
            "UPDATE deal_control_deals SET is_active = ?, updated_at = ? WHERE deal_id = ?",
            (int(is_active), utcish_now(), str(deal_id)),
        )


def create_deal_control_task(db_path: str | Path, *, deal_id: str, task_text: str, touch_type: str | None,
                             expected_result: str | None, due_at: str) -> dict[str, Any]:
    init_db(db_path)
    if not task_text.strip() or not due_at.strip():
        raise ValueError("Укажите текст поручения и срок")
    now = utcish_now()
    with connect(db_path) as conn:
        deal = conn.execute("SELECT * FROM deal_control_deals WHERE deal_id = ?", (str(deal_id),)).fetchone()
        if deal is None:
            raise ValueError("Сделка ещё не добавлена в контур контроля")
        cursor = conn.execute(
            "INSERT INTO deal_control_tasks (deal_id, task_text, touch_type, expected_result, due_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (str(deal_id), task_text.strip(), touch_type, expected_result, due_at, now, now),
        )
        task_id = int(cursor.lastrowid)
        report = conn.execute(
            """
            SELECT id FROM ui_reports
            WHERE entity_type = 'deal' AND entity_id = ? AND report_json IS NOT NULL
            ORDER BY id DESC LIMIT 1
            """,
            (str(deal_id),),
        ).fetchone()
        baseline = {
            key: deal[key]
            for key in (
                "deal_id", "stage_id", "stage_name", "amount", "currency_id",
                "manager_id", "modified_at_crm", "last_crm_sync_at",
            )
        }
        conn.execute(
            """
            INSERT INTO deal_control_task_baselines (task_id, deal_snapshot_json, source_report_id, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (task_id, dumps_json(baseline), int(report["id"]) if report is not None else None, now),
        )
        conn.execute(
            """
            INSERT INTO deal_control_task_events (task_id, event_type, event_key, payload_json, created_at)
            VALUES (?, 'task_created', 'task_created', ?, ?)
            """,
            (task_id, dumps_json({"due_at": due_at, "expected_result": expected_result}), now),
        )
        row = conn.execute("SELECT * FROM deal_control_tasks WHERE id = ?", (task_id,)).fetchone()
    result = dict(row) if row is not None else None
    assert result is not None
    return result


def record_deal_control_task_event(
    db_path: str | Path,
    *,
    task_id: int,
    event_type: str,
    event_key: str | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    init_db(db_path)
    with connect(db_path) as conn:
        if conn.execute("SELECT 1 FROM deal_control_tasks WHERE id = ?", (int(task_id),)).fetchone() is None:
            raise ValueError("Поручение не найдено")
        conn.execute(
            """
            INSERT INTO deal_control_task_events (task_id, event_type, event_key, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(task_id, event_key) WHERE event_key IS NOT NULL DO UPDATE SET
                event_type = excluded.event_type,
                payload_json = excluded.payload_json
            """,
            (int(task_id), event_type, event_key, dumps_json(payload) if payload is not None else None, utcish_now()),
        )


def save_deal_control_task_outcome(
    db_path: str | Path,
    *,
    task_id: int,
    contact_status: str,
    result_status: str,
    result_note: str | None,
    next_step_text: str | None,
    next_step_at: str | None,
    evidence_kind: str | None,
    evidence_id: str | None,
    source_role: str,
) -> dict[str, Any]:
    allowed_contacts = {"not_attempted", "attempt_no_contact", "confirmed_contact", "unknown"}
    allowed_results = {"pending", "achieved", "partial", "postponed", "refused", "not_applicable", "needs_rop_review"}
    if contact_status not in allowed_contacts:
        raise ValueError("Неизвестный статус контакта")
    if result_status not in allowed_results:
        raise ValueError("Неизвестный результат задачи")
    if source_role not in {"manager", "rop"}:
        raise ValueError("Неизвестный источник результата")
    note = str(result_note or "").strip() or None
    next_text = str(next_step_text or "").strip() or None
    next_at = str(next_step_at or "").strip() or None
    if contact_status == "not_attempted":
        raise ValueError("Сначала выполните действие или зафиксируйте попытку контакта")
    if contact_status == "unknown" and not note:
        raise ValueError("Опишите, почему контакт с клиентом не подтверждён")
    if contact_status == "attempt_no_contact" and (not note or not next_text or not next_at):
        raise ValueError("Для попытки без ответа укажите, что произошло, следующий шаг и его срок")
    if contact_status == "confirmed_contact" and not note:
        raise ValueError("Кратко зафиксируйте подтверждённый ответ клиента")
    if result_status == "pending" and (not next_text or not next_at):
        raise ValueError("Для незавершённой задачи укажите следующий шаг и его срок")
    if result_status in {"achieved", "partial", "postponed"} and (not next_text or not next_at):
        raise ValueError("Для этого результата укажите следующий шаг и его срок")
    if result_status in {"refused", "not_applicable"} and not note:
        raise ValueError("Укажите причину отказа или потери актуальности")
    if result_status == "needs_rop_review" and not note:
        raise ValueError("Опишите, какая помощь РОПа требуется")
    now = utcish_now()
    with connect(db_path) as conn:
        task = conn.execute("SELECT * FROM deal_control_tasks WHERE id = ?", (int(task_id),)).fetchone()
        if task is None:
            raise ValueError("Поручение не найдено")
        cursor = conn.execute(
            """
            INSERT INTO deal_control_task_outcomes (
                task_id, contact_status, result_status, result_note, next_step_text, next_step_at,
                evidence_kind, evidence_id, source_role, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(task_id), contact_status, result_status, note, next_text, next_at,
                evidence_kind, evidence_id, source_role, now,
            ),
        )
        legacy_status = {
            "pending": "no_result",
            "achieved": "next_step" if next_text else "client_fact",
            "partial": "next_step" if next_text else "client_fact",
            "postponed": "next_step",
            "refused": "client_fact",
            "not_applicable": "client_fact",
            "needs_rop_review": "needs_rop_review",
        }[result_status]
        local_status = "active" if result_status in {"pending", "needs_rop_review"} else "completed"
        conn.execute(
            """
            UPDATE deal_control_tasks
            SET local_status = ?, business_result_status = ?, business_result_note = ?,
                completed_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                local_status, legacy_status, note,
                now if local_status == "completed" else task["completed_at"], now, int(task_id),
            ),
        )
        conn.execute(
            """
            INSERT INTO deal_control_task_events (task_id, event_type, payload_json, created_at)
            VALUES (?, 'outcome_recorded', ?, ?)
            """,
            (
                int(task_id),
                dumps_json({
                    "outcome_id": int(cursor.lastrowid),
                    "contact_status": contact_status,
                    "result_status": result_status,
                    "has_next_step": bool(next_text and next_at),
                    "source_role": source_role,
                }),
                now,
            ),
        )
        row = conn.execute(
            "SELECT * FROM deal_control_task_outcomes WHERE id = ?",
            (int(cursor.lastrowid),),
        ).fetchone()
    result = dict(row) if row is not None else None
    assert result is not None
    return result


def review_deal_control_task_crm_fact(
    db_path: str | Path,
    *,
    task_id: int,
    fact_id: int,
    review_status: str,
    contact_class: str | None = None,
) -> dict[str, Any]:
    init_db(db_path)
    if review_status not in {"confirmed", "rejected"}:
        raise ValueError("Неизвестный статус проверки CRM-факта")
    allowed_classes = {"attempt", "confirmed_contact", "internal_information", "unknown", "deal_progress"}
    if contact_class is not None and contact_class not in allowed_classes:
        raise ValueError("Неизвестный класс CRM-факта")
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM deal_control_task_crm_facts WHERE id = ? AND task_id = ?",
            (int(fact_id), int(task_id)),
        ).fetchone()
        if row is None:
            raise ValueError("CRM-факт не найден")
        next_class = contact_class if contact_class is not None else row["contact_class"]
        conn.execute(
            """
            UPDATE deal_control_task_crm_facts
            SET review_status = ?, contact_class = ?
            WHERE id = ? AND task_id = ?
            """,
            (review_status, next_class, int(fact_id), int(task_id)),
        )
        saved = conn.execute(
            "SELECT * FROM deal_control_task_crm_facts WHERE id = ?",
            (int(fact_id),),
        ).fetchone()
    result = dict(saved) if saved is not None else None
    assert result is not None
    result["payload"] = loads_json(result.pop("payload_json"), None)
    return result


def update_deal_control_task(db_path: str | Path, *, task_id: int, task_text: str | None = None,
                             touch_type: str | None = None, expected_result: str | None = None,
                             due_at: str | None = None, local_status: str | None = None,
                             business_result_status: str | None = None, business_result_note: str | None = None,
                             reschedule_reason: str | None = None, source_role: str | None = None) -> dict[str, Any]:
    init_db(db_path)
    if source_role is not None and source_role not in {"manager", "rop"}:
        raise ValueError("Неизвестный источник изменения")
    with connect(db_path) as conn:
        current = conn.execute("SELECT * FROM deal_control_tasks WHERE id = ?", (task_id,)).fetchone()
        if current is None:
            raise ValueError("Поручение не найдено")
        values = dict(current)
        next_due = due_at if due_at is not None else values["due_at"]
        next_status = local_status if local_status is not None else values["local_status"]
        next_task_text = task_text.strip() if task_text is not None else values["task_text"]
        next_touch_type = touch_type if touch_type is not None else values["touch_type"]
        next_expected_result = expected_result if expected_result is not None else values["expected_result"]
        reason = str(reschedule_reason or "").strip() or None
        if due_at is not None and due_at != values["due_at"] and source_role == "rop" and not reason:
            raise ValueError("РОПу нужно указать причину переноса срока")
        guidance_changed = any(
            (
                next_task_text != values["task_text"],
                next_touch_type != values["touch_type"],
                next_expected_result != values["expected_result"],
                next_due != values["due_at"],
            )
        )
        next_guidance_revision = int(values.get("guidance_revision") or 1) + int(guidance_changed)
        conn.execute(
            """
            UPDATE deal_control_tasks SET task_text = ?, touch_type = ?, expected_result = ?, due_at = ?, local_status = ?,
                business_result_status = ?, business_result_note = ?, completed_at = ?, guidance_revision = ?,
                updated_at = ? WHERE id = ?
            """,
            (next_task_text, next_touch_type, next_expected_result, next_due, next_status,
             business_result_status if business_result_status is not None else values["business_result_status"],
             business_result_note if business_result_note is not None else values["business_result_note"],
             utcish_now() if next_status in {"cancelled", "completed"} else values.get("completed_at"),
             next_guidance_revision, utcish_now(), task_id),
        )
        if due_at is not None and due_at != values["due_at"]:
            conn.execute(
                """
                INSERT INTO deal_control_task_reschedules (
                    task_id, previous_due_at, next_due_at, reason, source_role, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (task_id, values["due_at"], due_at, reason, source_role, utcish_now()),
            )
        row = conn.execute("SELECT * FROM deal_control_tasks WHERE id = ?", (task_id,)).fetchone()
    result = dict(row) if row is not None else None
    assert result is not None
    return result


def save_deal_control_task_crm_sync(db_path: str | Path, *, task_id: int, crm_execution_status: str,
                                    crm_match_activity_id: str | None, crm_match_confidence: str | None,
                                    crm_match_candidate_completed: bool | None = None,
                                    result_activity_id: str | None = None, fact_kind: str | None = None,
                                    fact_summary: str | None = None, fact_occurred_at: str | None = None,
                                    fact_payload: dict[str, Any] | None = None) -> dict[str, Any]:
    init_db(db_path)
    with connect(db_path) as conn:
        current = conn.execute("SELECT * FROM deal_control_tasks WHERE id = ?", (task_id,)).fetchone()
        if current is None:
            raise ValueError("Поручение не найдено")
        current_values = dict(current)
        confirmed = bool(current_values.get("crm_match_confirmed"))
        same_match = str(current_values.get("crm_match_activity_id") or "") == str(crm_match_activity_id or "")
        if confirmed and same_match and crm_execution_status == "match_review":
            crm_execution_status = "crm_closed" if crm_match_candidate_completed else "crm_open"
        elif not same_match:
            confirmed = False
        conn.execute(
            """
            UPDATE deal_control_tasks
            SET crm_execution_status = ?, crm_match_activity_id = ?, crm_match_confidence = ?,
                crm_match_candidate_completed = ?, crm_match_confirmed = ?, result_activity_id = ?, updated_at = ?
            WHERE id = ?
            """,
            (crm_execution_status, crm_match_activity_id, crm_match_confidence,
             None if crm_match_candidate_completed is None else int(crm_match_candidate_completed), int(confirmed),
             result_activity_id, utcish_now(), task_id),
        )
        if fact_kind:
            conn.execute(
                """
                INSERT INTO deal_control_task_crm_facts (task_id, activity_id, fact_kind, summary, occurred_at, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id, activity_id, fact_kind) DO UPDATE SET
                    summary = excluded.summary, occurred_at = excluded.occurred_at, payload_json = excluded.payload_json, created_at = excluded.created_at
                """,
                (task_id, result_activity_id, fact_kind, fact_summary, fact_occurred_at,
                 dumps_json(fact_payload) if fact_payload is not None else None, utcish_now()),
            )
        row = conn.execute("SELECT * FROM deal_control_tasks WHERE id = ?", (task_id,)).fetchone()
    result = dict(row) if row is not None else None
    assert result is not None
    return result


def save_deal_control_task_crm_fact(
    db_path: str | Path,
    *,
    task_id: int,
    fact_key: str,
    activity_id: str | None,
    fact_kind: str,
    summary: str | None,
    occurred_at: str | None,
    contact_class: str = "unknown",
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    allowed_classes = {"attempt", "confirmed_contact", "internal_information", "unknown", "deal_progress"}
    if contact_class not in allowed_classes:
        raise ValueError("Неизвестный класс CRM-факта")
    now = utcish_now()
    with connect(db_path) as conn:
        if conn.execute("SELECT 1 FROM deal_control_tasks WHERE id = ?", (int(task_id),)).fetchone() is None:
            raise ValueError("Поручение не найдено")
        existing = conn.execute(
            """
            SELECT id FROM deal_control_task_crm_facts
            WHERE task_id = ? AND activity_id IS ? AND fact_kind = ?
            """,
            (int(task_id), activity_id, fact_kind),
        ).fetchone()
        if existing is not None:
            conn.execute(
                """
                UPDATE deal_control_task_crm_facts
                SET summary = ?, occurred_at = ?, payload_json = ?, contact_class = ?, fact_key = COALESCE(fact_key, ?)
                WHERE id = ?
                """,
                (
                    summary, occurred_at, dumps_json(payload) if payload is not None else None,
                    contact_class, fact_key, int(existing["id"]),
                ),
            )
        else:
            conn.execute(
                """
                INSERT INTO deal_control_task_crm_facts (
                    task_id, activity_id, fact_kind, summary, occurred_at, payload_json, created_at,
                    contact_class, review_status, fact_key
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'candidate', ?)
                ON CONFLICT(task_id, fact_key) WHERE fact_key IS NOT NULL DO UPDATE SET
                    activity_id = excluded.activity_id,
                    fact_kind = excluded.fact_kind,
                    summary = excluded.summary,
                    occurred_at = excluded.occurred_at,
                    payload_json = excluded.payload_json,
                    contact_class = excluded.contact_class
                """,
                (
                    int(task_id), activity_id, fact_kind, summary, occurred_at,
                    dumps_json(payload) if payload is not None else None, now, contact_class, fact_key,
                ),
            )
        row = conn.execute(
            "SELECT * FROM deal_control_task_crm_facts WHERE task_id = ? AND fact_key = ?",
            (int(task_id), fact_key),
        ).fetchone()
        conn.execute(
            """
            INSERT INTO deal_control_task_events (task_id, event_type, event_key, payload_json, created_at)
            VALUES (?, 'crm_fact_detected', ?, ?, ?)
            ON CONFLICT(task_id, event_key) WHERE event_key IS NOT NULL DO NOTHING
            """,
            (int(task_id), f"crm_fact:{fact_key}", dumps_json({"fact_kind": fact_kind}), now),
        )
    result = dict(row) if row is not None else None
    assert result is not None
    result["payload"] = loads_json(result.pop("payload_json"), None)
    return result


def confirm_deal_control_task_crm_match(db_path: str | Path, *, task_id: int) -> dict[str, Any]:
    """Confirm a medium-confidence activity match without claiming a client result."""
    init_db(db_path)
    with connect(db_path) as conn:
        current = conn.execute("SELECT * FROM deal_control_tasks WHERE id = ?", (task_id,)).fetchone()
        if current is None:
            raise ValueError("Поручение не найдено")
        values = dict(current)
        if str(values.get("crm_execution_status") or "") != "match_review":
            raise ValueError("Подтверждение доступно только для совпадения, требующего проверки РОПа")
        execution_status = "crm_closed" if bool(values.get("crm_match_candidate_completed")) else "crm_open"
        conn.execute(
            "UPDATE deal_control_tasks SET crm_execution_status = ?, crm_match_confirmed = 1, updated_at = ? WHERE id = ?",
            (execution_status, utcish_now(), task_id),
        )
        row = conn.execute("SELECT * FROM deal_control_tasks WHERE id = ?", (task_id,)).fetchone()
    result = dict(row) if row is not None else None
    assert result is not None
    return result


def list_deal_control_tasks(db_path: str | Path, *, deal_ids: list[str] | None = None,
                            active_only: bool = False) -> list[dict[str, Any]]:
    init_db(db_path)
    clauses: list[str] = []
    params: list[Any] = []
    if deal_ids:
        values = [str(value) for value in deal_ids if str(value)]
        if values:
            clauses.append(f"deal_id IN ({','.join('?' for _ in values)})")
            params.extend(values)
    if active_only:
        clauses.append("local_status = 'active'")
    query = "SELECT * FROM deal_control_tasks" + (" WHERE " + " AND ".join(clauses) if clauses else "")
    query += " ORDER BY due_at ASC, id ASC"
    with connect(db_path) as conn:
        rows = conn.execute(query, params).fetchall()
        tasks = [dict(row) for row in rows]
        for task in tasks:
            baseline = conn.execute(
                "SELECT * FROM deal_control_task_baselines WHERE task_id = ?",
                (int(task["id"]),),
            ).fetchone()
            latest_outcome = conn.execute(
                "SELECT * FROM deal_control_task_outcomes WHERE task_id = ? ORDER BY id DESC LIMIT 1",
                (int(task["id"]),),
            ).fetchone()
            fact_rows = conn.execute(
                "SELECT * FROM deal_control_task_crm_facts WHERE task_id = ? ORDER BY id DESC LIMIT 5",
                (int(task["id"]),),
            ).fetchall()
            task["baseline"] = dict(baseline) if baseline is not None else None
            if task["baseline"] is not None:
                task["baseline"]["deal_snapshot"] = loads_json(task["baseline"].pop("deal_snapshot_json"), {})
            task["latest_outcome"] = dict(latest_outcome) if latest_outcome is not None else None
            task["crm_facts"] = [dict(item) for item in fact_rows]
            for fact in task["crm_facts"]:
                fact["payload"] = loads_json(fact.pop("payload_json"), None)
            task["guidance"] = _latest_deal_control_task_guidance(
                conn,
                task_id=int(task["id"]),
                deal_id=str(task["deal_id"]),
                task_revision=int(task.get("guidance_revision") or 1),
            )
    return tasks


def get_deal_control_task(db_path: str | Path, *, task_id: int) -> dict[str, Any] | None:
    init_db(db_path)
    with connect(db_path) as conn:
        row = conn.execute("SELECT * FROM deal_control_tasks WHERE id = ?", (int(task_id),)).fetchone()
        if row is None:
            return None
        task = dict(row)
        baseline = conn.execute(
            "SELECT * FROM deal_control_task_baselines WHERE task_id = ?",
            (int(task["id"]),),
        ).fetchone()
        latest_outcome = conn.execute(
            "SELECT * FROM deal_control_task_outcomes WHERE task_id = ? ORDER BY id DESC LIMIT 1",
            (int(task["id"]),),
        ).fetchone()
        fact_rows = conn.execute(
            "SELECT * FROM deal_control_task_crm_facts WHERE task_id = ? ORDER BY id DESC LIMIT 5",
            (int(task["id"]),),
        ).fetchall()
        task["baseline"] = dict(baseline) if baseline is not None else None
        if task["baseline"] is not None:
            task["baseline"]["deal_snapshot"] = loads_json(task["baseline"].pop("deal_snapshot_json"), {})
        task["latest_outcome"] = dict(latest_outcome) if latest_outcome is not None else None
        task["crm_facts"] = [dict(item) for item in fact_rows]
        for fact in task["crm_facts"]:
            fact["payload"] = loads_json(fact.pop("payload_json"), None)
        task["guidance"] = _latest_deal_control_task_guidance(
            conn,
            task_id=int(task["id"]),
            deal_id=str(task["deal_id"]),
            task_revision=int(task.get("guidance_revision") or 1),
        )
    return task


def _latest_deal_control_task_guidance(
    conn: sqlite3.Connection,
    *,
    task_id: int,
    deal_id: str,
    task_revision: int,
) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT * FROM deal_control_task_guidance
        WHERE task_id = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (int(task_id),),
    ).fetchone()
    if row is None:
        return None
    latest_report = conn.execute(
        """
        SELECT id FROM ui_reports
        WHERE entity_type = 'deal' AND entity_id = ? AND report_json IS NOT NULL
        ORDER BY id DESC
        LIMIT 1
        """,
        (str(deal_id),),
    ).fetchone()
    value = dict(row)
    value["content"] = loads_json(value.pop("guidance_json", None), {})
    value["model_meta"] = loads_json(value.pop("model_meta_json", None), {})
    value["is_stale"] = (
        int(value.get("task_revision") or 0) != int(task_revision)
        or latest_report is None
        or int(value.get("source_report_id") or 0) != int(latest_report["id"])
    )
    return value


def save_deal_control_task_guidance(
    db_path: str | Path,
    *,
    task_id: int,
    task_revision: int,
    source_report_id: int,
    guidance: dict[str, Any],
    model_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    init_db(db_path)
    now = utcish_now()
    with connect(db_path) as conn:
        task = conn.execute("SELECT * FROM deal_control_tasks WHERE id = ?", (int(task_id),)).fetchone()
        if task is None:
            raise ValueError("Поручение не найдено")
        conn.execute(
            """
            INSERT INTO deal_control_task_guidance (
                task_id, task_revision, source_report_id, guidance_json, model_meta_json, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(task_id, task_revision, source_report_id) DO UPDATE SET
                guidance_json = excluded.guidance_json,
                model_meta_json = excluded.model_meta_json,
                created_at = excluded.created_at
            """,
            (
                int(task_id),
                int(task_revision),
                int(source_report_id),
                dumps_json(guidance),
                dumps_json(model_meta) if model_meta is not None else None,
                now,
            ),
        )
        row = conn.execute(
            """
            SELECT * FROM deal_control_task_guidance
            WHERE task_id = ? AND task_revision = ? AND source_report_id = ?
            """,
            (int(task_id), int(task_revision), int(source_report_id)),
        ).fetchone()
        conn.execute(
            """
            INSERT INTO deal_control_task_events (task_id, event_type, event_key, payload_json, created_at)
            VALUES (?, 'guidance_ready', ?, ?, ?)
            ON CONFLICT(task_id, event_key) WHERE event_key IS NOT NULL DO UPDATE SET
                payload_json = excluded.payload_json
            """,
            (
                int(task_id),
                f"guidance:{int(task_revision)}:{int(source_report_id)}",
                dumps_json({"task_revision": int(task_revision), "source_report_id": int(source_report_id)}),
                now,
            ),
        )
        result = dict(row) if row is not None else None
    assert result is not None
    result["content"] = loads_json(result.pop("guidance_json", None), {})
    result["model_meta"] = loads_json(result.pop("model_meta_json", None), {})
    result["is_stale"] = int(task["guidance_revision"] or 1) != int(task_revision)
    return result


def list_deal_control_task_history(db_path: str | Path, *, task_id: int) -> dict[str, list[dict[str, Any]]]:
    init_db(db_path)
    with connect(db_path) as conn:
        reschedules = [dict(row) for row in conn.execute("SELECT * FROM deal_control_task_reschedules WHERE task_id = ? ORDER BY id DESC", (task_id,)).fetchall()]
        facts = [dict(row) for row in conn.execute("SELECT * FROM deal_control_task_crm_facts WHERE task_id = ? ORDER BY id DESC", (task_id,)).fetchall()]
        outcomes = [dict(row) for row in conn.execute("SELECT * FROM deal_control_task_outcomes WHERE task_id = ? ORDER BY id DESC", (task_id,)).fetchall()]
        events = [dict(row) for row in conn.execute("SELECT * FROM deal_control_task_events WHERE task_id = ? ORDER BY id DESC", (task_id,)).fetchall()]
    for fact in facts:
        fact["payload"] = loads_json(fact.pop("payload_json"), None)
    for event in events:
        event["payload"] = loads_json(event.pop("payload_json"), None)
    return {"reschedules": reschedules, "crm_facts": facts, "outcomes": outcomes, "events": events}


def get_deal_control_metrics(db_path: str | Path, *, manager_id: str | None = None) -> dict[str, Any]:
    init_db(db_path)
    params: list[Any] = []
    manager_clause = ""
    if manager_id:
        manager_clause = "WHERE d.manager_id = ?"
        params.append(str(manager_id))
    with connect(db_path) as conn:
        task_rows = conn.execute(
            f"""
            SELECT t.*, d.manager_id,
                   EXISTS(
                       SELECT 1 FROM deal_control_task_guidance g
                       WHERE g.task_id = t.id
                         AND g.task_revision = t.guidance_revision
                         AND (
                             NOT EXISTS(SELECT 1 FROM deal_control_task_outcomes ox WHERE ox.task_id = t.id)
                             OR g.created_at <= (
                                 SELECT ox.created_at FROM deal_control_task_outcomes ox
                                 WHERE ox.task_id = t.id ORDER BY ox.id DESC LIMIT 1
                             )
                         )
                   ) AS has_guidance
            FROM deal_control_tasks t
            JOIN deal_control_deals d ON d.deal_id = t.deal_id
            {manager_clause}
            ORDER BY t.id
            """,
            params,
        ).fetchall()
        tasks: list[dict[str, Any]] = []
        cancelled_tasks = 0
        for row in task_rows:
            task = dict(row)
            if str(task.get("local_status") or "") == "cancelled":
                cancelled_tasks += 1
                continue
            outcome = conn.execute(
                "SELECT * FROM deal_control_task_outcomes WHERE task_id = ? ORDER BY id DESC LIMIT 1",
                (int(task["id"]),),
            ).fetchone()
            task["outcome"] = dict(outcome) if outcome is not None else None
            task["stage_progressed"] = bool(conn.execute(
                """
                SELECT 1 FROM deal_control_task_crm_facts
                WHERE task_id = ? AND fact_kind IN ('stage_changed', 'deal_won')
                LIMIT 1
                """,
                (int(task["id"]),),
            ).fetchone())
            task["deal_won"] = bool(conn.execute(
                "SELECT 1 FROM deal_control_task_crm_facts WHERE task_id = ? AND fact_kind = 'deal_won' LIMIT 1",
                (int(task["id"]),),
            ).fetchone())
            tasks.append(task)

    def aggregate(rows: list[dict[str, Any]]) -> dict[str, int]:
        outcomes = [row.get("outcome") for row in rows]
        return {
            "tasks": len(rows),
            "actions_completed": sum(str(row.get("local_status") or "") == "completed" for row in rows),
            "confirmed_contacts": sum(
                bool(outcome) and outcome.get("contact_status") == "confirmed_contact"
                for outcome in outcomes
            ),
            "target_results": sum(
                bool(outcome) and outcome.get("result_status") == "achieved"
                for outcome in outcomes
            ),
            "next_steps": sum(
                bool(outcome) and bool(outcome.get("next_step_text")) and bool(outcome.get("next_step_at"))
                for outcome in outcomes
            ),
            "stage_progressed": sum(bool(row.get("stage_progressed")) for row in rows),
            "deals_won": sum(bool(row.get("deal_won")) for row in rows),
        }

    with_guidance = [task for task in tasks if bool(task.get("has_guidance"))]
    without_guidance = [task for task in tasks if not bool(task.get("has_guidance"))]
    return {
        "overall": aggregate(tasks),
        "with_guidance": aggregate(with_guidance),
        "without_guidance": aggregate(without_guidance),
        "cancelled_tasks": cancelled_tasks,
        "note": "Сравнение показывает связь с подготовленной AI-подсказкой, но не доказывает причинность.",
    }
