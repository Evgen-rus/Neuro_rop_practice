"""
SQLite storage for local ROP assistant state.

The module intentionally uses the standard sqlite3 package: the current project
is a local file-based MVP, so adding an ORM would create more surface area than
the change-detection layer needs.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
from datetime import date, datetime, time, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

from setup import BASE_DIR, MSK_TZ


DEFAULT_DB_PATH = BASE_DIR / "reports" / "rop_assistant" / "rop_assistant.sqlite"
DEFAULT_DEAL_CONTROL_PIPELINE_ID = "15"
DEFAULT_DEAL_CONTROL_PIPELINE_IDS = ["15", "17", "47"]

DEAL_ANALYSIS_PURGE_QUERIES: tuple[tuple[str, str], ...] = (
    ("manager_trajectory_events", "DELETE FROM manager_trajectory_events WHERE entity_type = 'deal'"),
    ("manager_trajectory_entity_state", "DELETE FROM manager_trajectory_entity_state WHERE entity_type = 'deal'"),
    ("deal_manager_assistant_events", "DELETE FROM deal_manager_assistant_events"),
    ("deal_manager_full_scripts", "DELETE FROM deal_manager_full_scripts"),
    ("deal_manager_call_scripts", "DELETE FROM deal_manager_call_scripts"),
    ("deal_manager_email_scripts", "DELETE FROM deal_manager_email_scripts"),
    ("deal_manager_followups", "DELETE FROM deal_manager_followups"),
    ("deal_manager_companion_messages", "DELETE FROM deal_manager_companion_messages"),
    ("deal_manager_quick_help", "DELETE FROM deal_manager_quick_help"),
    ("deal_manager_situation_reviews", "DELETE FROM deal_manager_situation_reviews"),
    ("deal_control_task_reschedules", "DELETE FROM deal_control_task_reschedules"),
    ("deal_control_task_crm_facts", "DELETE FROM deal_control_task_crm_facts"),
    ("deal_control_task_guidance", "DELETE FROM deal_control_task_guidance"),
    ("deal_control_task_baselines", "DELETE FROM deal_control_task_baselines"),
    ("deal_control_task_outcomes", "DELETE FROM deal_control_task_outcomes"),
    ("deal_control_task_events", "DELETE FROM deal_control_task_events"),
    ("deal_control_tasks", "DELETE FROM deal_control_tasks"),
    ("deal_control_bitrix_task_state", "DELETE FROM deal_control_bitrix_task_state"),
    ("deal_daily_checklist_events", "DELETE FROM deal_daily_checklist_events"),
    ("deal_daily_checklist_items", "DELETE FROM deal_daily_checklist_items"),
    ("deal_daily_checklists", "DELETE FROM deal_daily_checklists"),
    (
        "compact_shadow_feedback",
        "DELETE FROM compact_shadow_feedback WHERE compact_run_id IN "
        "(SELECT id FROM compact_shadow_runs WHERE entity_type = 'deal')",
    ),
    ("compact_shadow_runs", "DELETE FROM compact_shadow_runs WHERE entity_type = 'deal'"),
    (
        "rop_decisions",
        "DELETE FROM rop_decisions WHERE report_id IN "
        "(SELECT id FROM ui_reports WHERE entity_type = 'deal')",
    ),
    (
        "qualification_reviews",
        "DELETE FROM qualification_reviews WHERE report_id IN "
        "(SELECT id FROM ui_reports WHERE entity_type = 'deal')",
    ),
    (
        "outcomes",
        "DELETE FROM outcomes WHERE report_id IN "
        "(SELECT id FROM ui_reports WHERE entity_type = 'deal')",
    ),
    ("candidate_review_state", "DELETE FROM candidate_review_state WHERE entity_type = 'deal'"),
    ("daily_summary_items", "DELETE FROM daily_summary_items WHERE entity_type = 'deal'"),
    ("ui_reports", "DELETE FROM ui_reports WHERE entity_type = 'deal'"),
    ("mini_recommendations", "DELETE FROM mini_recommendations WHERE entity_type = 'deal'"),
    ("analysis_runs", "DELETE FROM analysis_runs WHERE entity_type = 'deal'"),
    ("entity_memory", "DELETE FROM entity_memory WHERE entity_type = 'deal'"),
    ("entity_state", "DELETE FROM entity_state WHERE entity_type = 'deal'"),
)

AUTH_ROLES = frozenset({"admin", "rop", "manager"})
MANAGER_TRAJECTORY_ENTITY_TYPES = frozenset({"deal", "lead"})
MANAGER_TRAJECTORY_EVENT_TYPES = frozenset({
    "recommendation_generated",
    "recommendation_shown",
    "recommendation_viewed",
    "manager_communication_completed",
    "crm_activity_observed",
    "deal_stage_changed",
    "lead_stage_changed",
    "outcome_recorded",
})
MANAGER_RECOMMENDATION_KINDS = frozenset({"deal_task", "quick_help"})
_UNSET = object()
AUTH_UNSET = _UNSET


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


def _manager_script_should_replace(stored: Any, incoming: dict[str, Any]) -> bool:
    """Replace a cached material when its contract or prompt revision is outdated."""
    if not isinstance(stored, dict):
        return True
    for key in ("script_contract", "email_contract", "prompt_revision"):
        if incoming.get(key) != stored.get(key):
            return True
    return False


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
                model TEXT,
                prompt_version TEXT,
                logic_version TEXT,
                provenance_json TEXT,
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
                analysis_run_id INTEGER,
                share_token TEXT UNIQUE,
                FOREIGN KEY(analysis_run_id) REFERENCES analysis_runs(id)
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

            CREATE TABLE IF NOT EXISTS auth_users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                login TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('admin', 'rop', 'manager')),
                manager_id TEXT,
                is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0, 1)),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                CHECK(
                    role = 'manager'
                    OR manager_id IS NULL
                ),
                CHECK(
                    role != 'manager'
                    OR is_active = 0
                    OR (manager_id IS NOT NULL AND length(trim(manager_id)) > 0)
                )
            );

            CREATE UNIQUE INDEX IF NOT EXISTS idx_auth_users_active_manager
                ON auth_users(manager_id)
                WHERE role = 'manager'
                  AND is_active = 1
                  AND manager_id IS NOT NULL
                  AND length(trim(manager_id)) > 0;

            CREATE TABLE IF NOT EXISTS auth_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                token_digest TEXT NOT NULL UNIQUE,
                expires_at TEXT NOT NULL,
                revoked_at TEXT,
                created_at TEXT NOT NULL,
                last_seen_at TEXT,
                FOREIGN KEY(user_id) REFERENCES auth_users(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_auth_sessions_user
                ON auth_sessions(user_id, revoked_at, expires_at);

            CREATE TABLE IF NOT EXISTS auth_login_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                login TEXT NOT NULL,
                client_key TEXT,
                attempted_at TEXT NOT NULL,
                succeeded INTEGER NOT NULL CHECK(succeeded IN (0, 1))
            );

            CREATE INDEX IF NOT EXISTS idx_auth_login_attempts_lookup
                ON auth_login_attempts(login, client_key, attempted_at DESC);

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
                pipeline_ids_json TEXT,
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
                communications_today_json TEXT NOT NULL DEFAULT '{}',
                checklist_state_json TEXT NOT NULL DEFAULT '{}',
                last_crm_sync_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS deal_daily_checklists (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                deal_id TEXT NOT NULL,
                business_date TEXT NOT NULL,
                revision INTEGER NOT NULL DEFAULT 0,
                source_report_id INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(deal_id, business_date),
                FOREIGN KEY(deal_id) REFERENCES deal_control_deals(deal_id)
            );

            CREATE INDEX IF NOT EXISTS idx_deal_daily_checklists_latest
                ON deal_daily_checklists(deal_id, business_date DESC);

            CREATE TABLE IF NOT EXISTS deal_daily_checklist_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                checklist_id INTEGER NOT NULL,
                text TEXT NOT NULL,
                normalized_text TEXT NOT NULL,
                source TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('open', 'completed', 'retired')),
                origin_report_id INTEGER,
                carried_from_item_id INTEGER,
                last_change_type TEXT NOT NULL DEFAULT 'new',
                completed_at TEXT,
                completed_by TEXT,
                status_reason TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(checklist_id) REFERENCES deal_daily_checklists(id),
                FOREIGN KEY(carried_from_item_id) REFERENCES deal_daily_checklist_items(id)
            );

            CREATE INDEX IF NOT EXISTS idx_deal_daily_checklist_items_current
                ON deal_daily_checklist_items(checklist_id, status, id);

            CREATE TABLE IF NOT EXISTS deal_daily_checklist_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                checklist_id INTEGER NOT NULL,
                item_id INTEGER,
                event_key TEXT UNIQUE,
                event_type TEXT NOT NULL,
                actor TEXT NOT NULL,
                source_report_id INTEGER,
                reason TEXT,
                payload_json TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(checklist_id) REFERENCES deal_daily_checklists(id),
                FOREIGN KEY(item_id) REFERENCES deal_daily_checklist_items(id)
            );

            CREATE INDEX IF NOT EXISTS idx_deal_daily_checklist_events_history
                ON deal_daily_checklist_events(checklist_id, id);

            CREATE TABLE IF NOT EXISTS deal_manager_situation_reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                deal_id TEXT NOT NULL,
                manager_id TEXT NOT NULL,
                source_report_id INTEGER NOT NULL,
                revision INTEGER NOT NULL,
                action TEXT NOT NULL CHECK(action IN ('confirmed', 'context_added')),
                manager_context TEXT,
                refined_coaching_json TEXT,
                model_meta_json TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(deal_id, source_report_id, revision),
                FOREIGN KEY(deal_id) REFERENCES deal_control_deals(deal_id),
                FOREIGN KEY(source_report_id) REFERENCES ui_reports(id)
            );

            CREATE INDEX IF NOT EXISTS idx_deal_manager_situation_reviews_latest
                ON deal_manager_situation_reviews(deal_id, source_report_id, revision DESC, id DESC);

            CREATE TABLE IF NOT EXISTS deal_context_lever_priority_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                deal_id TEXT NOT NULL,
                source_report_id INTEGER NOT NULL,
                lever_id TEXT NOT NULL,
                priority INTEGER CHECK(priority IN (1, 2, 3) OR priority IS NULL),
                actor_role TEXT NOT NULL CHECK(actor_role IN ('manager', 'rop')),
                created_at TEXT NOT NULL,
                FOREIGN KEY(deal_id) REFERENCES deal_control_deals(deal_id),
                FOREIGN KEY(source_report_id) REFERENCES ui_reports(id)
            );

            CREATE INDEX IF NOT EXISTS idx_deal_context_lever_priority_current
                ON deal_context_lever_priority_events(deal_id, source_report_id, lever_id, id DESC);

            CREATE TABLE IF NOT EXISTS deal_manager_quick_help (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                deal_id TEXT NOT NULL,
                manager_id TEXT NOT NULL,
                source_report_id INTEGER NOT NULL,
                situation_review_id INTEGER NOT NULL,
                mode TEXT,
                origin TEXT,
                turn_id TEXT,
                question TEXT NOT NULL,
                answer_json TEXT NOT NULL,
                model_meta_json TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(deal_id) REFERENCES deal_control_deals(deal_id),
                FOREIGN KEY(source_report_id) REFERENCES ui_reports(id),
                FOREIGN KEY(situation_review_id) REFERENCES deal_manager_situation_reviews(id)
            );

            CREATE INDEX IF NOT EXISTS idx_deal_manager_quick_help_history
                ON deal_manager_quick_help(deal_id, manager_id, id DESC);

            CREATE TABLE IF NOT EXISTS deal_manager_full_scripts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                deal_id TEXT NOT NULL,
                manager_id TEXT NOT NULL,
                source_report_id INTEGER NOT NULL,
                situation_review_id INTEGER NOT NULL,
                quick_help_id INTEGER NOT NULL,
                selected_strategy TEXT NOT NULL CHECK(selected_strategy IN ('primary', 'alternative', 'pattern_break')),
                script_json TEXT NOT NULL,
                model_meta_json TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(deal_id) REFERENCES deal_control_deals(deal_id),
                FOREIGN KEY(source_report_id) REFERENCES ui_reports(id),
                FOREIGN KEY(situation_review_id) REFERENCES deal_manager_situation_reviews(id),
                FOREIGN KEY(quick_help_id) REFERENCES deal_manager_quick_help(id),
                UNIQUE(deal_id, manager_id, source_report_id, situation_review_id, quick_help_id, selected_strategy)
            );

            CREATE INDEX IF NOT EXISTS idx_deal_manager_full_scripts_current
                ON deal_manager_full_scripts(deal_id, manager_id, source_report_id, situation_review_id, quick_help_id);

            CREATE TABLE IF NOT EXISTS deal_manager_call_scripts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                deal_id TEXT NOT NULL,
                manager_id TEXT NOT NULL,
                source_report_id INTEGER NOT NULL,
                situation_review_id INTEGER NOT NULL,
                quick_help_id INTEGER NOT NULL,
                selected_strategy TEXT NOT NULL CHECK(selected_strategy IN ('primary', 'alternative', 'pattern_break')),
                script_json TEXT NOT NULL,
                model_meta_json TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(deal_id) REFERENCES deal_control_deals(deal_id),
                FOREIGN KEY(source_report_id) REFERENCES ui_reports(id),
                FOREIGN KEY(situation_review_id) REFERENCES deal_manager_situation_reviews(id),
                FOREIGN KEY(quick_help_id) REFERENCES deal_manager_quick_help(id),
                UNIQUE(deal_id, manager_id, source_report_id, situation_review_id, quick_help_id, selected_strategy)
            );

            CREATE INDEX IF NOT EXISTS idx_deal_manager_call_scripts_current
                ON deal_manager_call_scripts(deal_id, manager_id, source_report_id, situation_review_id, quick_help_id);

            CREATE TABLE IF NOT EXISTS deal_manager_email_scripts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                deal_id TEXT NOT NULL,
                manager_id TEXT NOT NULL,
                source_report_id INTEGER NOT NULL,
                situation_review_id INTEGER NOT NULL,
                quick_help_id INTEGER NOT NULL,
                selected_strategy TEXT NOT NULL CHECK(selected_strategy IN ('primary', 'alternative', 'pattern_break')),
                script_json TEXT NOT NULL,
                model_meta_json TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(deal_id) REFERENCES deal_control_deals(deal_id),
                FOREIGN KEY(source_report_id) REFERENCES ui_reports(id),
                FOREIGN KEY(situation_review_id) REFERENCES deal_manager_situation_reviews(id),
                FOREIGN KEY(quick_help_id) REFERENCES deal_manager_quick_help(id),
                UNIQUE(deal_id, manager_id, source_report_id, situation_review_id, quick_help_id, selected_strategy)
            );

            CREATE TABLE IF NOT EXISTS deal_manager_followups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                deal_id TEXT NOT NULL,
                manager_id TEXT NOT NULL,
                source_report_id INTEGER NOT NULL,
                situation_review_id INTEGER NOT NULL,
                followups_json TEXT NOT NULL,
                model_meta_json TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(deal_id) REFERENCES deal_control_deals(deal_id),
                FOREIGN KEY(source_report_id) REFERENCES ui_reports(id),
                FOREIGN KEY(situation_review_id) REFERENCES deal_manager_situation_reviews(id),
                UNIQUE(deal_id, manager_id, source_report_id, situation_review_id)
            );

            CREATE TABLE IF NOT EXISTS deal_manager_companion_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                deal_id TEXT NOT NULL,
                manager_id TEXT NOT NULL,
                source_report_id INTEGER NOT NULL,
                last_event_id TEXT NOT NULL,
                companion_json TEXT NOT NULL,
                model_meta_json TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(deal_id) REFERENCES deal_control_deals(deal_id),
                FOREIGN KEY(source_report_id) REFERENCES ui_reports(id),
                UNIQUE(deal_id, manager_id, source_report_id, last_event_id)
            );

            CREATE TABLE IF NOT EXISTS deal_manager_assistant_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                deal_id TEXT NOT NULL,
                manager_id TEXT NOT NULL,
                event_type TEXT NOT NULL CHECK(event_type IN ('communication_completed')),
                quick_help_id INTEGER,
                payload_json TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(deal_id) REFERENCES deal_control_deals(deal_id),
                FOREIGN KEY(quick_help_id) REFERENCES deal_manager_quick_help(id)
            );

            CREATE INDEX IF NOT EXISTS idx_deal_manager_assistant_events_history
                ON deal_manager_assistant_events(deal_id, manager_id, id DESC);

            CREATE UNIQUE INDEX IF NOT EXISTS idx_deal_manager_assistant_events_unique
                ON deal_manager_assistant_events(deal_id, manager_id, event_type, quick_help_id)
                WHERE quick_help_id IS NOT NULL;

            CREATE TABLE IF NOT EXISTS deal_control_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                deal_id TEXT NOT NULL,
                source_kind TEXT NOT NULL DEFAULT 'manual' CHECK(source_kind IN ('manual', 'neuro_rop')),
                source_report_id INTEGER,
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
                FOREIGN KEY(deal_id) REFERENCES deal_control_deals(deal_id),
                FOREIGN KEY(source_report_id) REFERENCES ui_reports(id)
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

            CREATE TABLE IF NOT EXISTS manager_trajectory_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_type TEXT NOT NULL CHECK(entity_type IN ('deal', 'lead')),
                entity_id TEXT NOT NULL,
                manager_id TEXT,
                auth_user_id INTEGER,
                event_type TEXT NOT NULL,
                recommendation_kind TEXT,
                recommendation_id TEXT,
                analysis_run_id INTEGER,
                report_id INTEGER,
                source TEXT NOT NULL,
                source_event_key TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                recorded_at TEXT NOT NULL,
                payload_json TEXT,
                UNIQUE(source, source_event_key),
                FOREIGN KEY(auth_user_id) REFERENCES auth_users(id),
                FOREIGN KEY(analysis_run_id) REFERENCES analysis_runs(id),
                FOREIGN KEY(report_id) REFERENCES ui_reports(id)
            );

            CREATE INDEX IF NOT EXISTS idx_manager_trajectory_entity_time
                ON manager_trajectory_events(entity_type, entity_id, occurred_at);

            CREATE INDEX IF NOT EXISTS idx_manager_trajectory_manager_time
                ON manager_trajectory_events(manager_id, occurred_at);

            CREATE TABLE IF NOT EXISTS manager_trajectory_collection_state (
                collection_key TEXT NOT NULL PRIMARY KEY,
                last_success_at TEXT,
                last_attempt_at TEXT,
                last_status TEXT,
                last_error TEXT,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS manager_trajectory_entity_state (
                entity_type TEXT NOT NULL CHECK(entity_type IN ('deal', 'lead')),
                entity_id TEXT NOT NULL,
                manager_id TEXT,
                stage_id TEXT,
                modified_at TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(entity_type, entity_id)
            );

            CREATE TABLE IF NOT EXISTS automatic_analysis_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                business_date TEXT NOT NULL,
                trigger TEXT NOT NULL,
                job_id TEXT,
                status TEXT NOT NULL,
                current_stage TEXT,
                started_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                finished_at TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_automatic_analysis_runs_started
                ON automatic_analysis_runs(started_at DESC);

            CREATE TABLE IF NOT EXISTS automatic_analysis_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL,
                entity_type TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                stage TEXT,
                decision_status TEXT,
                analysis_run_id INTEGER,
                report_id INTEGER,
                error TEXT,
                publication_status TEXT NOT NULL DEFAULT 'pending',
                updated_at TEXT NOT NULL,
                UNIQUE(run_id, entity_type, entity_id),
                FOREIGN KEY(run_id) REFERENCES automatic_analysis_runs(id),
                FOREIGN KEY(analysis_run_id) REFERENCES analysis_runs(id),
                FOREIGN KEY(report_id) REFERENCES ui_reports(id)
            );

            CREATE INDEX IF NOT EXISTS idx_automatic_analysis_items_run
                ON automatic_analysis_items(run_id, entity_type, entity_id);

            CREATE TABLE IF NOT EXISTS daily_control_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                business_date TEXT NOT NULL,
                creation_kind TEXT NOT NULL,
                started_at TEXT NOT NULL,
                cutoff_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                snapshot_json TEXT NOT NULL,
                source_watermark TEXT NOT NULL,
                automatic_analysis_run_id INTEGER,
                source_status TEXT NOT NULL DEFAULT 'ok',
                warnings_json TEXT NOT NULL DEFAULT '[]',
                error TEXT,
                FOREIGN KEY(automatic_analysis_run_id) REFERENCES automatic_analysis_runs(id)
            );

            CREATE INDEX IF NOT EXISTS idx_daily_control_reports_created
                ON daily_control_reports(id DESC);

            CREATE UNIQUE INDEX IF NOT EXISTS idx_daily_control_reports_planning_date
                ON daily_control_reports(business_date)
                WHERE creation_kind = 'automatic_planning';
            """
        )
        _ensure_column(conn, "analysis_runs", "model", "TEXT")
        _ensure_column(conn, "analysis_runs", "prompt_version", "TEXT")
        _ensure_column(conn, "analysis_runs", "logic_version", "TEXT")
        _ensure_column(conn, "analysis_runs", "provenance_json", "TEXT")
        _ensure_column(conn, "ui_reports", "analysis_run_id", "INTEGER")
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
        _ensure_column(conn, "deal_control_deals", "communications_today_json", "TEXT NOT NULL DEFAULT '{}'")
        _ensure_column(conn, "deal_control_deals", "checklist_state_json", "TEXT NOT NULL DEFAULT '{}'")
        _ensure_column(conn, "deal_control_tasks", "crm_match_candidate_completed", "INTEGER")
        _ensure_column(conn, "deal_control_tasks", "crm_match_confirmed", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "deal_control_tasks", "guidance_revision", "INTEGER NOT NULL DEFAULT 1")
        _ensure_column(conn, "deal_control_tasks", "source_kind", "TEXT NOT NULL DEFAULT 'manual'")
        _ensure_column(conn, "deal_control_tasks", "source_report_id", "INTEGER")
        _ensure_column(conn, "deal_control_task_reschedules", "source_role", "TEXT")
        _ensure_column(conn, "deal_control_task_crm_facts", "contact_class", "TEXT NOT NULL DEFAULT 'unknown'")
        _ensure_column(conn, "deal_control_task_crm_facts", "review_status", "TEXT NOT NULL DEFAULT 'candidate'")
        _ensure_column(conn, "deal_control_task_crm_facts", "fact_key", "TEXT")
        _ensure_column(conn, "deal_manager_quick_help", "mode", "TEXT")
        _ensure_column(conn, "deal_manager_quick_help", "origin", "TEXT")
        _ensure_column(conn, "deal_manager_quick_help", "turn_id", "TEXT")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_deal_manager_quick_help_current "
            "ON deal_manager_quick_help("
            "deal_id, manager_id, source_report_id, situation_review_id, mode, id DESC)"
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_deal_control_task_crm_facts_key "
            "ON deal_control_task_crm_facts(task_id, fact_key) WHERE fact_key IS NOT NULL"
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_deal_control_tasks_neuro_report "
            "ON deal_control_tasks(source_report_id) "
            "WHERE source_kind = 'neuro_rop' AND source_report_id IS NOT NULL"
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

        auth_migration_id = "2026-08-12-auth-core"
        if conn.execute(
            "SELECT 1 FROM local_migrations WHERE migration_id = ?",
            (auth_migration_id,),
        ).fetchone() is None:
            conn.execute(
                "INSERT INTO local_migrations (migration_id, applied_at) VALUES (?, ?)",
                (auth_migration_id, utcish_now()),
            )

        _ensure_column(conn, "deal_control_scope", "pipeline_ids_json", "TEXT")
        pipeline_migration_id = "2026-08-18-deal-control-pipelines-17-47"
        if conn.execute(
            "SELECT 1 FROM local_migrations WHERE migration_id = ?",
            (pipeline_migration_id,),
        ).fetchone() is None:
            for row in conn.execute(
                "SELECT scope_key, pipeline_id, pipeline_ids_json FROM deal_control_scope"
            ).fetchall():
                stored = loads_json(row["pipeline_ids_json"], [])
                if isinstance(stored, list) and any(str(item).strip() for item in stored):
                    continue
                pipeline_id = str(row["pipeline_id"] or "").strip()
                if pipeline_id == DEFAULT_DEAL_CONTROL_PIPELINE_ID:
                    payload = list(DEFAULT_DEAL_CONTROL_PIPELINE_IDS)
                elif pipeline_id:
                    payload = [pipeline_id]
                else:
                    payload = list(DEFAULT_DEAL_CONTROL_PIPELINE_IDS)
                conn.execute(
                    "UPDATE deal_control_scope SET pipeline_ids_json = ? WHERE scope_key = ?",
                    (dumps_json(payload), row["scope_key"]),
                )
            conn.execute(
                "INSERT INTO local_migrations (migration_id, applied_at) VALUES (?, ?)",
                (pipeline_migration_id, utcish_now()),
            )


def deal_analysis_purge_counts(db_path: str | Path = DEFAULT_DB_PATH) -> dict[str, int]:
    """Return a content-free preview of local deal analysis state to remove."""
    with connect(db_path) as conn:
        counts = {
            name: int(conn.execute(sql.replace("DELETE FROM", "SELECT COUNT(*) FROM", 1)).fetchone()[0])
            for name, sql in DEAL_ANALYSIS_PURGE_QUERIES
        }
        counts["deal_control_checklist_state"] = int(
            conn.execute(
                "SELECT COUNT(*) FROM deal_control_deals "
                "WHERE checklist_state_json IS NOT NULL AND checklist_state_json <> '{}'"
            ).fetchone()[0]
        )
    return counts


def purge_local_deal_analysis_state(db_path: str | Path = DEFAULT_DB_PATH) -> dict[str, int]:
    """Remove all derived deal-analysis and local coaching history in one transaction.

    CRM deal projections, downloaded source material, authentication, preferences,
    and analysis profiles are deliberately outside this operation.
    """
    init_db(db_path)
    deleted: dict[str, int] = {}
    with connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        for name, sql in DEAL_ANALYSIS_PURGE_QUERIES:
            deleted[name] = int(conn.execute(sql).rowcount)
        deleted["deal_control_checklist_state"] = int(
            conn.execute(
                "UPDATE deal_control_deals SET checklist_state_json = '{}' "
                "WHERE checklist_state_json IS NOT NULL AND checklist_state_json <> '{}'"
            ).rowcount
        )
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise sqlite3.IntegrityError(
                f"Foreign key violations after deal analysis purge: {len(violations)}"
            )
    return deleted

def _auth_password_hasher() -> Any:
    """Load the Argon2-backed password helper only when auth is used."""

    from pwdlib import PasswordHash

    return PasswordHash.recommended()


@lru_cache(maxsize=1)
def _cached_auth_password_hasher() -> Any:
    return _auth_password_hasher()


def hash_auth_password(password: str) -> str:
    """Hash a raw password with pwdlib's recommended Argon2 configuration."""

    if not isinstance(password, str) or not password:
        raise ValueError("Пароль должен быть непустой строкой")
    return str(_cached_auth_password_hasher().hash(password))


def verify_auth_password(password: str, password_hash: str) -> bool:
    """Verify a raw password without exposing or persisting it."""

    if not isinstance(password, str) or not isinstance(password_hash, str):
        return False
    if not password or not password_hash:
        return False
    try:
        return bool(_cached_auth_password_hasher().verify(password, password_hash))
    except Exception:
        # A malformed persisted hash must fail closed, not break login handling.
        return False


def digest_auth_token(token: str) -> str:
    """Return the digest stored for an opaque session token."""

    if not isinstance(token, str) or not token:
        raise ValueError("Токен сессии должен быть непустой строкой")
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _normalize_auth_login(login: str) -> str:
    value = str(login or "").strip().casefold()
    if not value:
        raise ValueError("Логин не может быть пустым")
    if len(value) > 254:
        raise ValueError("Логин слишком длинный")
    return value


def _normalize_auth_manager_id(manager_id: object) -> str | None:
    if manager_id is None:
        return None
    value = str(manager_id).strip()
    return value or None


def _normalize_auth_client_key(client_key: object) -> str | None:
    if client_key is None:
        return None
    value = str(client_key).strip()
    return value or None


def _validate_auth_role(role: str) -> str:
    value = str(role or "").strip().casefold()
    if value not in AUTH_ROLES:
        raise ValueError("Недопустимая роль пользователя")
    return value


def _resolve_auth_password_hash(
    *,
    password_hash: str | None,
    password: str | None,
) -> str:
    if password_hash is not None and password is not None:
        raise ValueError("Передайте password или password_hash, но не оба")
    if password_hash is None:
        if password is None:
            raise ValueError("Не задан пароль или его хэш")
        password_hash = hash_auth_password(password)
    value = str(password_hash)
    if not value:
        raise ValueError("Хэш пароля не может быть пустым")
    return value


def _parse_auth_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValueError("Некорректная дата auth") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=MSK_TZ)
    return parsed


def _auth_now(now: str | None = None) -> str:
    return now if now is not None else utcish_now()


def _begin_auth_write(conn: sqlite3.Connection) -> None:
    conn.execute("BEGIN IMMEDIATE")


def _active_admin_count(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS count FROM auth_users WHERE role = 'admin' AND is_active = 1"
    ).fetchone()
    return int(row["count"] if row is not None else 0)


def _validate_auth_user_state(*, role: str, manager_id: str | None, is_active: bool) -> None:
    if role != "manager" and manager_id is not None:
        raise ValueError("manager_id допустим только для роли manager")
    if role == "manager" and is_active and not manager_id:
        raise ValueError("Активному manager нужен непустой manager_id")


def _auth_user_row(
    row: sqlite3.Row | None,
    *,
    include_password_hash: bool = False,
) -> dict[str, Any] | None:
    if row is None:
        return None
    value = dict(row)
    value["is_active"] = bool(value.get("is_active"))
    if not include_password_hash:
        value.pop("password_hash", None)
    return value


def _get_auth_user_row(
    conn: sqlite3.Connection,
    *,
    user_id: int | None = None,
    login: str | None = None,
) -> sqlite3.Row | None:
    if (user_id is None) == (login is None):
        raise ValueError("Укажите ровно один идентификатор пользователя")
    if user_id is not None:
        return conn.execute(
            "SELECT * FROM auth_users WHERE id = ?",
            (int(user_id),),
        ).fetchone()
    return conn.execute(
        "SELECT * FROM auth_users WHERE login = ?",
        (_normalize_auth_login(str(login)),),
    ).fetchone()


def _translate_auth_integrity_error(exc: sqlite3.IntegrityError) -> ValueError:
    message = str(exc).lower()
    if "auth_users.login" in message:
        return ValueError("Логин уже занят")
    if "idx_auth_users_active_manager" in message or "auth_users.manager_id" in message:
        return ValueError("Этот manager_id уже связан с активным пользователем")
    if "auth_users" in message and "check constraint" in message:
        return ValueError("Нарушено ограничение пользователя")
    return ValueError("Не удалось сохранить auth-пользователя")


def create_auth_user(
    db_path: str | Path,
    *,
    login: str,
    role: str,
    password: str | None = None,
    password_hash: str | None = None,
    manager_id: str | None = None,
    is_active: bool = True,
) -> dict[str, Any]:
    """Create a user and enforce auth invariants in one SQLite transaction.

    The backend normally passes ``password_hash`` or uses the compatibility
    ``password`` argument through the CLI.  A raw password is never inserted
    into SQLite; it is immediately converted to an Argon2id hash.
    """

    normalized_login = _normalize_auth_login(login)
    normalized_role = _validate_auth_role(role)
    normalized_manager_id = _normalize_auth_manager_id(manager_id)
    active = bool(is_active)
    _validate_auth_user_state(
        role=normalized_role,
        manager_id=normalized_manager_id,
        is_active=active,
    )
    resolved_hash = _resolve_auth_password_hash(
        password_hash=password_hash,
        password=password,
    )
    now = utcish_now()
    init_db(db_path)
    try:
        with connect(db_path) as conn:
            _begin_auth_write(conn)
            if _active_admin_count(conn) == 0 and not (
                normalized_role == "admin" and active
            ):
                raise ValueError("Сначала создайте активного администратора")
            try:
                cursor = conn.execute(
                    """
                    INSERT INTO auth_users (
                        login, password_hash, role, manager_id, is_active, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        normalized_login,
                        resolved_hash,
                        normalized_role,
                        normalized_manager_id,
                        int(active),
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise _translate_auth_integrity_error(exc) from exc
            row = conn.execute(
                "SELECT * FROM auth_users WHERE id = ?",
                (int(cursor.lastrowid),),
            ).fetchone()
    except sqlite3.IntegrityError as exc:
        raise _translate_auth_integrity_error(exc) from exc
    result = _auth_user_row(row, include_password_hash=True)
    if result is None:
        raise ValueError("Не удалось создать пользователя")
    return result


def get_auth_user(
    db_path: str | Path,
    *,
    user_id: int | None = None,
    login: str | None = None,
    include_password_hash: bool = False,
) -> dict[str, Any] | None:
    init_db(db_path)
    with connect(db_path) as conn:
        row = _get_auth_user_row(conn, user_id=user_id, login=login)
    return _auth_user_row(row, include_password_hash=include_password_hash)


def list_auth_users(db_path: str | Path) -> list[dict[str, Any]]:
    init_db(db_path)
    with connect(db_path) as conn:
        rows = conn.execute("SELECT * FROM auth_users ORDER BY id").fetchall()
    return [item for row in rows if (item := _auth_user_row(row)) is not None]


def update_auth_user(
    db_path: str | Path,
    *,
    user_id: int,
    role: str | None = None,
    manager_id: object = _UNSET,
    is_active: bool | None = None,
) -> dict[str, Any]:
    init_db(db_path)
    with connect(db_path) as conn:
        _begin_auth_write(conn)
        current = _get_auth_user_row(conn, user_id=int(user_id))
        if current is None:
            raise ValueError("Пользователь не найден")

        current_role = str(current["role"])
        next_role = _validate_auth_role(role) if role is not None else current_role
        if manager_id is _UNSET:
            next_manager_id = (
                _normalize_auth_manager_id(current["manager_id"])
                if next_role == "manager"
                else None
            )
        else:
            next_manager_id = _normalize_auth_manager_id(manager_id)
        next_active = bool(current["is_active"]) if is_active is None else bool(is_active)
        _validate_auth_user_state(
            role=next_role,
            manager_id=next_manager_id,
            is_active=next_active,
        )

        current_is_last_admin = (
            current_role == "admin"
            and bool(current["is_active"])
            and _active_admin_count(conn) == 1
        )
        if current_is_last_admin and not (next_role == "admin" and next_active):
            raise ValueError(
                "Нельзя деактивировать или изменить последнего активного администратора"
            )
        if _active_admin_count(conn) == 0 and not (next_role == "admin" and next_active):
            raise ValueError("Нельзя оставить систему без активного администратора")

        try:
            conn.execute(
                """
                UPDATE auth_users
                SET role = ?, manager_id = ?, is_active = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    next_role,
                    next_manager_id,
                    int(next_active),
                    utcish_now(),
                    int(user_id),
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise _translate_auth_integrity_error(exc) from exc

        if not next_active or current_role != next_role:
            conn.execute(
                "UPDATE auth_sessions SET revoked_at = COALESCE(revoked_at, ?) WHERE user_id = ?",
                (utcish_now(), int(user_id)),
            )
        row = conn.execute(
            "SELECT * FROM auth_users WHERE id = ?",
            (int(user_id),),
        ).fetchone()
    result = _auth_user_row(row, include_password_hash=True)
    if result is None:
        raise ValueError("Пользователь не найден")
    return result


def set_auth_user_password(
    db_path: str | Path,
    *,
    user_id: int,
    password_hash: str | None = None,
    password: str | None = None,
) -> dict[str, Any]:
    resolved_hash = _resolve_auth_password_hash(
        password_hash=password_hash,
        password=password,
    )
    init_db(db_path)
    with connect(db_path) as conn:
        _begin_auth_write(conn)
        row = conn.execute(
            "SELECT * FROM auth_users WHERE id = ?",
            (int(user_id),),
        ).fetchone()
        if row is None:
            raise ValueError("Пользователь не найден")
        now = utcish_now()
        conn.execute(
            "UPDATE auth_users SET password_hash = ?, updated_at = ? WHERE id = ?",
            (resolved_hash, now, int(user_id)),
        )
        conn.execute(
            "UPDATE auth_sessions SET revoked_at = COALESCE(revoked_at, ?) WHERE user_id = ?",
            (now, int(user_id)),
        )
        updated = conn.execute(
            "SELECT * FROM auth_users WHERE id = ?",
            (int(user_id),),
        ).fetchone()
    result = _auth_user_row(updated, include_password_hash=True)
    if result is None:
        raise ValueError("Пользователь не найден")
    return result


def deactivate_auth_user(db_path: str | Path, *, user_id: int) -> dict[str, Any]:
    return update_auth_user(db_path, user_id=int(user_id), is_active=False)


def activate_auth_user(db_path: str | Path, *, user_id: int) -> dict[str, Any]:
    return update_auth_user(db_path, user_id=int(user_id), is_active=True)


def revoke_auth_user_sessions(db_path: str | Path, *, user_id: int) -> int:
    init_db(db_path)
    with connect(db_path) as conn:
        _begin_auth_write(conn)
        if conn.execute(
            "SELECT 1 FROM auth_users WHERE id = ?",
            (int(user_id),),
        ).fetchone() is None:
            raise ValueError("Пользователь не найден")
        cursor = conn.execute(
            """
            UPDATE auth_sessions
            SET revoked_at = COALESCE(revoked_at, ?)
            WHERE user_id = ? AND revoked_at IS NULL
            """,
            (utcish_now(), int(user_id)),
        )
        return int(cursor.rowcount)


def _auth_session_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    value = dict(row)
    if "user_is_active" in value:
        value["user_is_active"] = bool(value["user_is_active"])
    return value


def create_auth_session(
    db_path: str | Path,
    *,
    user_id: int,
    token_digest: str,
    expires_at: str,
    created_at: str | None = None,
    last_seen_at: str | None = None,
) -> dict[str, Any]:
    digest = str(token_digest or "").strip()
    if not digest:
        raise ValueError("Хэш токена сессии не может быть пустым")
    _parse_auth_datetime(expires_at)
    created = created_at or utcish_now()
    init_db(db_path)
    with connect(db_path) as conn:
        _begin_auth_write(conn)
        user = conn.execute(
            "SELECT id, is_active FROM auth_users WHERE id = ?",
            (int(user_id),),
        ).fetchone()
        if user is None:
            raise ValueError("Пользователь не найден")
        if not bool(user["is_active"]):
            raise ValueError("Нельзя создать сессию неактивного пользователя")
        try:
            cursor = conn.execute(
                """
                INSERT INTO auth_sessions (
                    user_id, token_digest, expires_at, revoked_at, created_at, last_seen_at
                ) VALUES (?, ?, ?, NULL, ?, ?)
                """,
                (int(user_id), digest, expires_at, created, last_seen_at),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError("Сессия с таким токеном уже существует") from exc
        row = conn.execute(
            "SELECT * FROM auth_sessions WHERE id = ?",
            (int(cursor.lastrowid),),
        ).fetchone()
    result = _auth_session_row(row)
    if result is None:
        raise ValueError("Не удалось создать сессию")
    return result


def get_auth_session(
    db_path: str | Path,
    *,
    token_digest: str,
    now: str | None = None,
) -> dict[str, Any] | None:
    digest = str(token_digest or "").strip()
    if not digest:
        return None
    current_time = _auth_now(now)
    current_dt = _parse_auth_datetime(current_time)
    init_db(db_path)
    with connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT s.*, u.login AS user_login, u.role AS user_role,
                   u.manager_id AS user_manager_id, u.is_active AS user_is_active
            FROM auth_sessions AS s
            JOIN auth_users AS u ON u.id = s.user_id
            WHERE s.token_digest = ?
            """,
            (digest,),
        ).fetchone()
        if row is None or row["revoked_at"] is not None or not bool(row["user_is_active"]):
            return None
        if _parse_auth_datetime(str(row["expires_at"])) <= current_dt:
            return None
        conn.execute(
            "UPDATE auth_sessions SET last_seen_at = ? WHERE id = ?",
            (current_time, int(row["id"])),
        )
        updated = conn.execute(
            """
            SELECT s.*, u.login AS user_login, u.role AS user_role,
                   u.manager_id AS user_manager_id, u.is_active AS user_is_active
            FROM auth_sessions AS s
            JOIN auth_users AS u ON u.id = s.user_id
            WHERE s.id = ?
            """,
            (int(row["id"]),),
        ).fetchone()
    return _auth_session_row(updated)


def revoke_auth_session(db_path: str | Path, *, token_digest: str) -> bool:
    digest = str(token_digest or "").strip()
    if not digest:
        return False
    init_db(db_path)
    with connect(db_path) as conn:
        _begin_auth_write(conn)
        cursor = conn.execute(
            """
            UPDATE auth_sessions
            SET revoked_at = COALESCE(revoked_at, ?)
            WHERE token_digest = ? AND revoked_at IS NULL
            """,
            (utcish_now(), digest),
        )
        return int(cursor.rowcount) > 0


def _resolve_auth_client_key(
    *,
    client_ip: object = None,
    client_key: object = None,
) -> str | None:
    if (
        client_ip is not None
        and client_key is not None
        and str(client_ip).strip() != str(client_key).strip()
    ):
        raise ValueError("Передайте client_ip или client_key, но не оба")
    return _normalize_auth_client_key(
        client_ip if client_ip is not None else client_key
    )


def record_auth_login_attempt(
    db_path: str | Path,
    *,
    login: str,
    client_key: str | None = None,
    attempted_at: str | None = None,
    succeeded: bool = False,
    client_ip: str | None = None,
) -> dict[str, Any]:
    normalized_login = _normalize_auth_login(login)
    normalized_client_key = _resolve_auth_client_key(
        client_ip=client_ip,
        client_key=client_key,
    )
    attempted = attempted_at or utcish_now()
    _parse_auth_datetime(attempted)
    init_db(db_path)
    with connect(db_path) as conn:
        _begin_auth_write(conn)
        cursor = conn.execute(
            """
            INSERT INTO auth_login_attempts (login, client_key, attempted_at, succeeded)
            VALUES (?, ?, ?, ?)
            """,
            (
                normalized_login,
                normalized_client_key,
                attempted,
                int(bool(succeeded)),
            ),
        )
        row = conn.execute(
            "SELECT * FROM auth_login_attempts WHERE id = ?",
            (int(cursor.lastrowid),),
        ).fetchone()
    result = dict(row) if row is not None else None
    if result is None:
        raise ValueError("Не удалось записать попытку входа")
    result["succeeded"] = bool(result["succeeded"])
    result["client_ip"] = result["client_key"]
    return result


def get_auth_login_throttle(
    db_path: str | Path,
    *,
    login: str,
    client_key: str | None = None,
    now: str | None = None,
    client_ip: str | None = None,
    window_seconds: int = 900,
    max_attempts: int = 5,
) -> dict[str, Any] | None:
    if window_seconds <= 0 or max_attempts <= 0:
        raise ValueError("Параметры throttling должны быть положительными")
    normalized_login = _normalize_auth_login(login)
    normalized_client_key = _resolve_auth_client_key(
        client_ip=client_ip,
        client_key=client_key,
    )
    current_time = _auth_now(now)
    current_dt = _parse_auth_datetime(current_time)
    window_start = current_dt - timedelta(seconds=int(window_seconds))
    init_db(db_path)
    with connect(db_path) as conn:
        if normalized_client_key is None:
            rows = conn.execute(
                """
                SELECT * FROM auth_login_attempts
                WHERE login = ? AND client_key IS NULL
                ORDER BY id ASC
                """,
                (normalized_login,),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT * FROM auth_login_attempts
                WHERE login = ? AND client_key = ?
                ORDER BY id ASC
                """,
                (normalized_login, normalized_client_key),
            ).fetchall()

    latest_success: datetime | None = None
    failures: list[tuple[datetime, str]] = []
    for row in rows:
        attempted_dt = _parse_auth_datetime(str(row["attempted_at"]))
        if bool(row["succeeded"]):
            if latest_success is None or attempted_dt > latest_success:
                latest_success = attempted_dt
            continue
        if window_start <= attempted_dt <= current_dt:
            failures.append((attempted_dt, str(row["attempted_at"])))
    if latest_success is not None:
        failures = [item for item in failures if item[0] > latest_success]
    if not failures:
        return None

    first_failed_at = min(failures, key=lambda item: item[0])[1]
    last_failed_at = max(failures, key=lambda item: item[0])[1]
    locked_until_dt = max(item[0] for item in failures) + timedelta(
        seconds=int(window_seconds)
    )
    locked_until = locked_until_dt.isoformat(timespec="seconds")
    return {
        "login": normalized_login,
        "client_key": normalized_client_key,
        "client_ip": normalized_client_key,
        "failure_count": len(failures),
        "first_failed_at": first_failed_at,
        "last_failed_at": last_failed_at,
        "locked_until": locked_until if len(failures) >= max_attempts else None,
        "is_locked": len(failures) >= max_attempts and current_dt < locked_until_dt,
        "window_seconds": int(window_seconds),
        "max_attempts": int(max_attempts),
    }


def clear_auth_login_attempts(
    db_path: str | Path,
    *,
    login: str,
    client_key: str | None = None,
    client_ip: str | None = None,
) -> None:
    normalized_login = _normalize_auth_login(login)
    normalized_client_key = _resolve_auth_client_key(
        client_ip=client_ip,
        client_key=client_key,
    )
    init_db(db_path)
    with connect(db_path) as conn:
        _begin_auth_write(conn)
        if normalized_client_key is None:
            conn.execute(
                "DELETE FROM auth_login_attempts WHERE login = ?",
                (normalized_login,),
            )
        else:
            conn.execute(
                "DELETE FROM auth_login_attempts WHERE login = ? AND client_key = ?",
                (normalized_login, normalized_client_key),
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
    model: str | None = None,
    prompt_version: str | None = None,
    logic_version: str | None = None,
    provenance: dict[str, Any] | None = None,
    error: str | None = None,
) -> int:
    if provenance is not None and not isinstance(provenance, dict):
        raise ValueError("Provenance должен быть JSON-объектом")
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
                model,
                prompt_version,
                logic_version,
                provenance_json,
                error,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                str(model).strip() or None if model is not None else None,
                str(prompt_version).strip() or None if prompt_version is not None else None,
                str(logic_version).strip() or None if logic_version is not None else None,
                dumps_json(provenance) if provenance is not None else None,
                error,
                utcish_now(),
            ),
        )
        return int(cursor.lastrowid)


def list_analysis_runs(
    db_path: str | Path,
    *,
    entity_type: str | None = None,
    entity_ids: list[str] | None = None,
    created_at_from: str | None = None,
    limit: int = 1000,
) -> list[dict[str, Any]]:
    """Return recent analysis runs for scheduler summaries. Does not decode JSON blobs."""
    init_db(db_path)
    clauses = ["1 = 1"]
    params: list[Any] = []
    if entity_type:
        clauses.append("entity_type = ?")
        params.append(str(entity_type))
    ids = [str(item).strip() for item in (entity_ids or []) if str(item).strip()]
    if ids:
        placeholders = ", ".join("?" for _ in ids)
        clauses.append(f"entity_id IN ({placeholders})")
        params.extend(ids)
    if created_at_from:
        clauses.append("created_at >= ?")
        params.append(str(created_at_from))
    params.append(int(limit))
    query = (
        "SELECT id, entity_type, entity_id, status, fingerprint, error, created_at "
        "FROM analysis_runs WHERE "
        + " AND ".join(clauses)
        + " ORDER BY id ASC LIMIT ?"
    )
    with connect(db_path) as conn:
        rows = conn.execute(query, params).fetchall()
    return [dict(row) for row in rows]


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
    analysis_run_id: int | None = None,
) -> int:
    init_db(db_path)
    with connect(db_path) as conn:
        return _insert_ui_report(
            conn,
            entity_type=entity_type,
            entity_id=entity_id,
            risk_level=risk_level,
            attention_reason=attention_reason,
            recommended_action=recommended_action,
            analysis_path=analysis_path,
            report_path=report_path,
            report_json=report_json,
            report_meta=report_meta,
            technical_log=technical_log,
            model_context=model_context,
            job_id=job_id,
            analysis_run_id=analysis_run_id,
        )


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


def get_ui_report_by_analysis_run_id(db_path: str | Path, analysis_run_id: int) -> dict[str, Any] | None:
    """Reuse the existing UI report for one AnalysisRun instead of saving a duplicate."""
    init_db(db_path)
    with connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT * FROM ui_reports
            WHERE analysis_run_id = ?
            ORDER BY id ASC
            LIMIT 1
            """,
            (int(analysis_run_id),),
        ).fetchone()
    return _row_to_ui_report(row)


def _insert_ui_report(
    conn: sqlite3.Connection,
    *,
    entity_type: str,
    entity_id: str,
    risk_level: str | None,
    attention_reason: str | None,
    recommended_action: str | None,
    analysis_path: str | None,
    report_path: str | None,
    report_json: dict[str, Any] | None,
    report_meta: dict[str, Any] | None,
    technical_log: dict[str, Any] | None,
    model_context: dict[str, Any] | None,
    job_id: str | None,
    analysis_run_id: int | None,
) -> int:
    share_token = secrets.token_urlsafe(24)
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
            analysis_run_id,
            share_token
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            int(analysis_run_id) if analysis_run_id is not None else None,
            share_token,
        ),
    )
    return int(cursor.lastrowid)


def get_or_create_ui_report_for_analysis_run(
    db_path: str | Path,
    *,
    entity_type: str,
    entity_id: str,
    analysis_run_id: int,
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
) -> tuple[dict[str, Any], bool]:
    """Atomically reuse or insert the UI report for one analysis_run_id."""
    run_id = int(analysis_run_id)
    init_db(db_path)
    with connect(db_path) as conn:
        previous_isolation = conn.isolation_level
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                """
                SELECT * FROM ui_reports
                WHERE analysis_run_id = ?
                ORDER BY id ASC
                LIMIT 1
                """,
                (run_id,),
            ).fetchone()
            if row is not None:
                conn.execute("COMMIT")
                report = _row_to_ui_report(row)
                if report is None:
                    raise RuntimeError("Existing UI report could not be loaded")
                return report, False
            report_id = _insert_ui_report(
                conn,
                entity_type=entity_type,
                entity_id=entity_id,
                risk_level=risk_level,
                attention_reason=attention_reason,
                recommended_action=recommended_action,
                analysis_path=analysis_path,
                report_path=report_path,
                report_json=report_json,
                report_meta=report_meta,
                technical_log=technical_log,
                model_context=model_context,
                job_id=job_id,
                analysis_run_id=run_id,
            )
            created_row = conn.execute(
                "SELECT * FROM ui_reports WHERE id = ?",
                (report_id,),
            ).fetchone()
            conn.execute("COMMIT")
            report = _row_to_ui_report(created_row)
            if report is None:
                raise RuntimeError("Created UI report could not be loaded")
            return report, True
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.isolation_level = previous_isolation


def _row_to_automatic_analysis_run(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return dict(row)


def _row_to_automatic_analysis_item(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return dict(row)


def create_automatic_analysis_run(
    db_path: str | Path,
    *,
    trigger: str,
    entity_ids: list[str] | tuple[str, ...] = (),
    entity_type: str = "deal",
    job_id: str | None = None,
    status: str = "running",
    current_stage: str | None = "queued",
    business_date: str | None = None,
) -> dict[str, Any]:
    init_db(db_path)
    now = utcish_now()
    date_value = str(business_date or datetime.now(MSK_TZ).date().isoformat())
    finished_at = None if status == "running" else now
    ids = [str(item) for item in entity_ids if str(item).strip()]
    with connect(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO automatic_analysis_runs (
                business_date, trigger, job_id, status, current_stage,
                started_at, updated_at, finished_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (date_value, str(trigger), job_id, str(status), current_stage, now, now, finished_at),
        )
        run_id = int(cursor.lastrowid)
        for entity_id in ids:
            conn.execute(
                """
                INSERT INTO automatic_analysis_items (
                    run_id, entity_type, entity_id, stage, decision_status,
                    analysis_run_id, report_id, error, publication_status, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    str(entity_type),
                    str(entity_id),
                    "queued",
                    None,
                    None,
                    None,
                    None,
                    "pending",
                    now,
                ),
            )
        row = conn.execute(
            "SELECT * FROM automatic_analysis_runs WHERE id = ?",
            (run_id,),
        ).fetchone()
    run = _row_to_automatic_analysis_run(row)
    if run is None:
        raise RuntimeError("Automatic analysis run was not created")
    return run


def attach_automatic_analysis_job_id(
    db_path: str | Path,
    run_id: int,
    job_id: str,
) -> None:
    init_db(db_path)
    now = utcish_now()
    with connect(db_path) as conn:
        conn.execute(
            """
            UPDATE automatic_analysis_runs
            SET job_id = ?, updated_at = ?
            WHERE id = ?
            """,
            (str(job_id), now, int(run_id)),
        )


def finish_automatic_analysis_run(
    db_path: str | Path,
    run_id: int,
    *,
    status: str,
    current_stage: str | None = None,
) -> None:
    init_db(db_path)
    now = utcish_now()
    with connect(db_path) as conn:
        if current_stage is None:
            conn.execute(
                """
                UPDATE automatic_analysis_runs
                SET status = ?, finished_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (str(status), now, now, int(run_id)),
            )
            return
        conn.execute(
            """
            UPDATE automatic_analysis_runs
            SET status = ?, current_stage = ?, finished_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (str(status), str(current_stage), now, now, int(run_id)),
        )


def interrupt_running_automatic_analysis_runs(db_path: str | Path = DEFAULT_DB_PATH) -> int:
    """Mark leftover running runs as interrupted after an API restart. Do not resume subprocesses."""
    init_db(db_path)
    now = utcish_now()
    with connect(db_path) as conn:
        cursor = conn.execute(
            """
            UPDATE automatic_analysis_runs
            SET status = 'interrupted', finished_at = ?, updated_at = ?
            WHERE status = 'running'
            """,
            (now, now),
        )
        return int(cursor.rowcount or 0)


def get_latest_automatic_analysis_run(db_path: str | Path) -> dict[str, Any] | None:
    init_db(db_path)
    with connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT * FROM automatic_analysis_runs
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
    return _row_to_automatic_analysis_run(row)


def list_automatic_analysis_items(
    db_path: str | Path,
    run_id: int,
) -> list[dict[str, Any]]:
    init_db(db_path)
    with connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT * FROM automatic_analysis_items
            WHERE run_id = ?
            ORDER BY id ASC
            """,
            (int(run_id),),
        ).fetchall()
    return [item for item in (_row_to_automatic_analysis_item(row) for row in rows) if item is not None]


def get_automatic_analysis_item(
    db_path: str | Path,
    run_id: int,
    *,
    entity_type: str,
    entity_id: str,
) -> dict[str, Any] | None:
    init_db(db_path)
    with connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT * FROM automatic_analysis_items
            WHERE run_id = ? AND entity_type = ? AND entity_id = ?
            """,
            (int(run_id), str(entity_type), str(entity_id)),
        ).fetchone()
    return _row_to_automatic_analysis_item(row)


def update_automatic_analysis_item(
    db_path: str | Path,
    run_id: int,
    *,
    entity_type: str,
    entity_id: str,
    stage: str | None = None,
    decision_status: str | None = None,
    analysis_run_id: int | None = None,
    report_id: int | None = None,
    error: str | None = None,
    publication_status: str | None = None,
    current_stage: str | None = None,
) -> None:
    init_db(db_path)
    now = utcish_now()
    assignments: list[str] = ["updated_at = ?"]
    values: list[Any] = [now]
    if stage is not None:
        assignments.append("stage = ?")
        values.append(str(stage))
    if decision_status is not None:
        assignments.append("decision_status = ?")
        values.append(str(decision_status))
    if analysis_run_id is not None:
        assignments.append("analysis_run_id = ?")
        values.append(int(analysis_run_id))
    if report_id is not None:
        assignments.append("report_id = ?")
        values.append(int(report_id))
    if error is not None:
        assignments.append("error = ?")
        values.append(str(error).strip() or None)
    if publication_status is not None:
        assignments.append("publication_status = ?")
        values.append(str(publication_status))
    values.extend([int(run_id), str(entity_type), str(entity_id)])
    with connect(db_path) as conn:
        conn.execute(
            f"""
            UPDATE automatic_analysis_items
            SET {", ".join(assignments)}
            WHERE run_id = ? AND entity_type = ? AND entity_id = ?
            """,
            values,
        )
        if current_stage is not None:
            conn.execute(
                """
                UPDATE automatic_analysis_runs
                SET current_stage = ?, updated_at = ?
                WHERE id = ? AND status = 'running'
                """,
                (str(current_stage), now, int(run_id)),
            )
        else:
            conn.execute(
                """
                UPDATE automatic_analysis_runs
                SET updated_at = ?
                WHERE id = ? AND status = 'running'
                """,
                (now, int(run_id)),
            )


DAILY_CONTROL_METADATA_COLUMNS = (
    "id, business_date, creation_kind, started_at, cutoff_at, created_at, "
    "source_watermark, automatic_analysis_run_id, source_status, warnings_json, error"
)
DAILY_CONTROL_CREATION_KINDS = frozenset({"manual", "automatic_planning"})


def _row_to_daily_control_report(
    row: sqlite3.Row | None,
    *,
    include_snapshot: bool = True,
) -> dict[str, Any] | None:
    if row is None:
        return None
    value = dict(row)
    warnings = loads_json(value.pop("warnings_json", None), [])
    value["warnings"] = warnings if isinstance(warnings, list) else []
    snapshot_json = value.pop("snapshot_json", None)
    if include_snapshot:
        snapshot = loads_json(snapshot_json, {})
        value["snapshot"] = snapshot if isinstance(snapshot, dict) else {}
    return value


def create_daily_control_report(
    db_path: str | Path,
    *,
    business_date: str,
    creation_kind: str,
    started_at: str,
    cutoff_at: str,
    snapshot: dict[str, Any],
    source_watermark: str,
    automatic_analysis_run_id: int | None = None,
    source_status: str = "ok",
    warnings: list[str] | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    """Persist an immutable daily-control snapshot. Planning reports are unique per MSK date."""
    kind = str(creation_kind or "").strip()
    if kind not in DAILY_CONTROL_CREATION_KINDS:
        raise ValueError("creation_kind должен быть manual или automatic_planning")
    init_db(db_path)
    now = utcish_now()
    payload = (
        str(business_date),
        kind,
        str(started_at),
        str(cutoff_at),
        now,
        dumps_json(snapshot),
        str(source_watermark),
        int(automatic_analysis_run_id) if automatic_analysis_run_id is not None else None,
        str(source_status or "ok"),
        dumps_json(list(warnings or [])),
        str(error) if error else None,
    )
    with connect(db_path) as conn:
        if kind == "automatic_planning":
            existing = conn.execute(
                f"SELECT {DAILY_CONTROL_METADATA_COLUMNS}, snapshot_json FROM daily_control_reports "
                "WHERE business_date = ? AND creation_kind = 'automatic_planning' LIMIT 1",
                (str(business_date),),
            ).fetchone()
            if existing is not None:
                return _row_to_daily_control_report(existing, include_snapshot=True) or {}
        try:
            cursor = conn.execute(
                """
                INSERT INTO daily_control_reports (
                    business_date, creation_kind, started_at, cutoff_at, created_at,
                    snapshot_json, source_watermark, automatic_analysis_run_id,
                    source_status, warnings_json, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                payload,
            )
            report_id = int(cursor.lastrowid)
        except sqlite3.IntegrityError:
            existing = conn.execute(
                f"SELECT {DAILY_CONTROL_METADATA_COLUMNS}, snapshot_json FROM daily_control_reports "
                "WHERE business_date = ? AND creation_kind = 'automatic_planning' LIMIT 1",
                (str(business_date),),
            ).fetchone()
            if existing is not None:
                return _row_to_daily_control_report(existing, include_snapshot=True) or {}
            raise
    return get_daily_control_report(db_path, report_id, include_snapshot=True) or {}


def get_daily_control_report(
    db_path: str | Path,
    report_id: int,
    *,
    include_snapshot: bool = True,
) -> dict[str, Any] | None:
    init_db(db_path)
    columns = f"{DAILY_CONTROL_METADATA_COLUMNS}, snapshot_json" if include_snapshot else DAILY_CONTROL_METADATA_COLUMNS
    with connect(db_path) as conn:
        row = conn.execute(
            f"SELECT {columns} FROM daily_control_reports WHERE id = ?",
            (int(report_id),),
        ).fetchone()
    return _row_to_daily_control_report(row, include_snapshot=include_snapshot)


def list_daily_control_reports(db_path: str | Path) -> list[dict[str, Any]]:
    """Newest-first metadata only: snapshot JSON stays in the detail query."""
    init_db(db_path)
    with connect(db_path) as conn:
        rows = conn.execute(
            f"SELECT {DAILY_CONTROL_METADATA_COLUMNS} FROM daily_control_reports ORDER BY id DESC"
        ).fetchall()
    return [
        item
        for item in (_row_to_daily_control_report(row, include_snapshot=False) for row in rows)
        if item is not None
    ]


def get_latest_daily_control_report(
    db_path: str | Path,
    *,
    include_snapshot: bool = False,
) -> dict[str, Any] | None:
    init_db(db_path)
    columns = f"{DAILY_CONTROL_METADATA_COLUMNS}, snapshot_json" if include_snapshot else DAILY_CONTROL_METADATA_COLUMNS
    with connect(db_path) as conn:
        row = conn.execute(
            f"SELECT {columns} FROM daily_control_reports ORDER BY id DESC LIMIT 1"
        ).fetchone()
    return _row_to_daily_control_report(row, include_snapshot=include_snapshot)


def list_deal_daily_checklist_summaries(
    db_path: str | Path,
    *,
    business_date: str,
) -> list[dict[str, Any]]:
    """Read-only checklist watermarks for a Moscow business date. Does not create rows."""
    init_db(db_path)
    with connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT checklists.deal_id AS deal_id,
                   checklists.revision AS revision,
                   checklists.updated_at AS updated_at,
                   SUM(CASE WHEN items.status = 'completed' THEN 1 ELSE 0 END) AS completed,
                   SUM(CASE WHEN items.status != 'retired' THEN 1 ELSE 0 END) AS total
            FROM deal_daily_checklists AS checklists
            LEFT JOIN deal_daily_checklist_items AS items
                ON items.checklist_id = checklists.id
            WHERE checklists.business_date = ?
            GROUP BY checklists.id
            ORDER BY checklists.deal_id
            """,
            (str(business_date),),
        ).fetchall()
    return [
        {
            "deal_id": str(row["deal_id"]),
            "revision": int(row["revision"] or 0),
            "updated_at": row["updated_at"],
            "completed": int(row["completed"] or 0),
            "total": int(row["total"] or 0),
        }
        for row in rows
    ]


def _row_to_deal_manager_situation_review(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    value = dict(row)
    value["refined_coaching"] = loads_json(value.pop("refined_coaching_json", None), None)
    value["model_meta"] = loads_json(value.pop("model_meta_json", None), None)
    return value


def get_latest_deal_manager_situation_review(
    db_path: str | Path,
    *,
    deal_id: str,
    source_report_id: int | None = None,
) -> dict[str, Any] | None:
    """Return the newest append-only review for a deal or one source report."""
    init_db(db_path)
    clauses = ["deal_id = ?"]
    params: list[Any] = [str(deal_id)]
    if source_report_id is not None:
        clauses.append("source_report_id = ?")
        params.append(int(source_report_id))
    query = (
        "SELECT * FROM deal_manager_situation_reviews WHERE "
        + " AND ".join(clauses)
        + " ORDER BY source_report_id DESC, revision DESC, id DESC LIMIT 1"
    )
    with connect(db_path) as conn:
        row = conn.execute(query, params).fetchone()
    return _row_to_deal_manager_situation_review(row)


def next_deal_manager_situation_revision(
    db_path: str | Path,
    *,
    deal_id: str,
    source_report_id: int,
) -> int:
    """Return the next revision number without creating a review row."""
    init_db(db_path)
    with connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT COALESCE(MAX(revision), 0) + 1
            FROM deal_manager_situation_reviews
            WHERE deal_id = ? AND source_report_id = ?
            """,
            (str(deal_id), int(source_report_id)),
        ).fetchone()
    return int(row[0]) if row is not None else 1


def get_next_deal_manager_situation_revision(
    db_path: str | Path,
    *,
    deal_id: str,
    source_report_id: int,
) -> int:
    """Explicit ``get_*`` alias for callers that treat revision as a read."""
    return next_deal_manager_situation_revision(
        db_path,
        deal_id=deal_id,
        source_report_id=source_report_id,
    )


def _deal_manager_situation_review_context(
    conn: sqlite3.Connection,
    *,
    deal_id: str,
    source_report_id: int,
) -> str:
    deal = conn.execute(
        "SELECT manager_id FROM deal_control_deals WHERE deal_id = ?",
        (str(deal_id),),
    ).fetchone()
    if deal is None:
        raise ValueError("Сделка ещё не добавлена в контур контроля")
    manager_id = str(deal["manager_id"] or "").strip()
    if not manager_id:
        raise ValueError("У сделки не указан локальный ответственный менеджер")
    report = conn.execute(
        """
        SELECT id FROM ui_reports
        WHERE id = ? AND entity_type = 'deal' AND entity_id = ? AND report_json IS NOT NULL
        """,
        (int(source_report_id), str(deal_id)),
    ).fetchone()
    if report is None:
        raise ValueError("Отчёт сделки не найден или не содержит анализа")
    return manager_id


def _append_deal_manager_situation_review(
    db_path: str | Path,
    *,
    deal_id: str,
    source_report_id: int,
    action: str,
    manager_context: str | None,
    refined_coaching: dict[str, Any] | None,
    model_meta: dict[str, Any] | None,
) -> dict[str, Any]:
    if action not in {"confirmed", "context_added"}:
        raise ValueError("Неизвестное действие подтверждения manager situation")
    if refined_coaching is not None and not isinstance(refined_coaching, dict):
        raise ValueError("Refined coaching должен быть JSON-объектом")
    if model_meta is not None and not isinstance(model_meta, dict):
        raise ValueError("Метаданные модели должны быть JSON-объектом")
    normalized_context = None if manager_context is None else str(manager_context).strip() or None
    init_db(db_path)
    now = utcish_now()
    with connect(db_path) as conn:
        # The row is append-only. Serialise the max(revision)+insert pair so
        # two simultaneous manager actions cannot receive the same revision.
        conn.execute("BEGIN IMMEDIATE")
        manager_id = _deal_manager_situation_review_context(
            conn,
            deal_id=str(deal_id),
            source_report_id=int(source_report_id),
        )
        revision = int(conn.execute(
            """
            SELECT COALESCE(MAX(revision), 0) + 1
            FROM deal_manager_situation_reviews
            WHERE deal_id = ? AND source_report_id = ?
            """,
            (str(deal_id), int(source_report_id)),
        ).fetchone()[0])
        cursor = conn.execute(
            """
            INSERT INTO deal_manager_situation_reviews (
                deal_id, manager_id, source_report_id, revision, action,
                manager_context, refined_coaching_json, model_meta_json, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(deal_id),
                manager_id,
                int(source_report_id),
                revision,
                action,
                normalized_context,
                dumps_json(refined_coaching) if refined_coaching is not None else None,
                dumps_json(model_meta) if model_meta is not None else None,
                now,
            ),
        )
        row = conn.execute(
            "SELECT * FROM deal_manager_situation_reviews WHERE id = ?",
            (int(cursor.lastrowid),),
        ).fetchone()
    result = _row_to_deal_manager_situation_review(row)
    assert result is not None
    return result


def save_deal_manager_situation_review(
    db_path: str | Path,
    *,
    deal_id: str,
    source_report_id: int,
    action: str,
    manager_context: str | None = None,
    refined_coaching: dict[str, Any] | None = None,
    model_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Append one manager-provided review for a saved deal analysis."""
    return _append_deal_manager_situation_review(
        db_path,
        deal_id=deal_id,
        source_report_id=source_report_id,
        action=action,
        manager_context=manager_context,
        refined_coaching=refined_coaching,
        model_meta=model_meta,
    )


def save_deal_manager_situation_confirmation(
    db_path: str | Path,
    *,
    deal_id: str,
    source_report_id: int,
    manager_context: str | None = None,
    model_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Append a plain confirmation without changing the AI coaching."""
    return _append_deal_manager_situation_review(
        db_path,
        deal_id=deal_id,
        source_report_id=source_report_id,
        action="confirmed",
        manager_context=manager_context,
        refined_coaching=None,
        model_meta=model_meta,
    )


def save_deal_manager_situation_confirmed(
    db_path: str | Path,
    *,
    deal_id: str,
    source_report_id: int,
    manager_context: str | None = None,
    model_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Alias for ``save_deal_manager_situation_confirmation``."""
    return save_deal_manager_situation_confirmation(
        db_path,
        deal_id=deal_id,
        source_report_id=source_report_id,
        manager_context=manager_context,
        model_meta=model_meta,
    )


def save_deal_manager_situation_refined_projection(
    db_path: str | Path,
    *,
    deal_id: str,
    source_report_id: int,
    refined_coaching: dict[str, Any],
    manager_context: str | None = None,
    model_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Append a manager-provided refined projection for the saved analysis."""
    return _append_deal_manager_situation_review(
        db_path,
        deal_id=deal_id,
        source_report_id=source_report_id,
        action="context_added",
        manager_context=manager_context,
        refined_coaching=refined_coaching,
        model_meta=model_meta,
    )


def save_deal_manager_situation_refined(
    db_path: str | Path,
    *,
    deal_id: str,
    source_report_id: int,
    refined_coaching: dict[str, Any],
    manager_context: str | None = None,
    model_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Short alias for ``save_deal_manager_situation_refined_projection``."""
    return save_deal_manager_situation_refined_projection(
        db_path,
        deal_id=deal_id,
        source_report_id=source_report_id,
        refined_coaching=refined_coaching,
        manager_context=manager_context,
        model_meta=model_meta,
    )


def list_deal_manager_situation_review_history(
    db_path: str | Path,
    *,
    deal_id: str,
    source_report_id: int | None = None,
) -> list[dict[str, Any]]:
    """Return append-only review history, newest revision first."""
    init_db(db_path)
    clauses = ["deal_id = ?"]
    params: list[Any] = [str(deal_id)]
    if source_report_id is not None:
        clauses.append("source_report_id = ?")
        params.append(int(source_report_id))
    query = (
        "SELECT * FROM deal_manager_situation_reviews WHERE "
        + " AND ".join(clauses)
        + " ORDER BY source_report_id DESC, revision DESC, id DESC"
    )
    with connect(db_path) as conn:
        rows = conn.execute(query, params).fetchall()
    return [
        value
        for row in rows
        if (value := _row_to_deal_manager_situation_review(row)) is not None
    ]


def list_deal_manager_situation_reviews(
    db_path: str | Path,
    *,
    deal_id: str,
    source_report_id: int | None = None,
) -> list[dict[str, Any]]:
    """Alias for ``list_deal_manager_situation_review_history``."""
    return list_deal_manager_situation_review_history(
        db_path,
        deal_id=deal_id,
        source_report_id=source_report_id,
    )


def get_deal_manager_situation_review_history(
    db_path: str | Path,
    *,
    deal_id: str,
    source_report_id: int | None = None,
) -> list[dict[str, Any]]:
    """Explicit ``get_*`` alias for the append-only review history."""
    return list_deal_manager_situation_review_history(
        db_path,
        deal_id=deal_id,
        source_report_id=source_report_id,
    )


def get_deal_manager_situation_state(db_path: str | Path, *, deal_id: str) -> dict[str, Any]:
    """Project the current manager-situation state without mutating history."""
    report = get_latest_ui_report(db_path, entity_type="deal", entity_id=str(deal_id))
    source_report_id = int(report["id"]) if report is not None else None
    review = (
        get_latest_deal_manager_situation_review(
            db_path,
            deal_id=str(deal_id),
            source_report_id=source_report_id,
        )
        if source_report_id is not None
        else None
    )
    status = "pending"
    is_current = False
    if review is not None:
        status = "refined" if review.get("action") == "context_added" else "confirmed"
        is_current = int(review.get("source_report_id") or 0) == int(source_report_id or 0)
    return {
        "status": status,
        "state": status,
        "review_id": int(review["id"]) if review is not None and review.get("id") is not None else None,
        "source_report_id": source_report_id,
        "revision": int(review["revision"]) if review is not None and review.get("revision") is not None else None,
        "manager_context": review.get("manager_context") if review is not None else None,
        "confirmed_at": review.get("created_at") if review is not None else None,
        "is_current": is_current,
    }


def _normalize_quick_help_mode(value: Any, *, content: dict[str, Any] | None = None) -> str:
    mode = str(value or "").strip()
    if mode in {"push", "reanimator"}:
        return mode
    if isinstance(content, dict):
        content_mode = str(content.get("mode") or "").strip()
        if content_mode in {"push", "reanimator"}:
            return content_mode
    return "reanimator"


def _normalize_quick_help_origin(value: Any) -> str:
    origin = str(value or "").strip()
    return origin if origin in {"auto", "manager"} else "manager"


def _row_to_deal_manager_quick_help(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    value = dict(row)
    content = loads_json(value.pop("answer_json", None), {})
    value["content"] = content if isinstance(content, dict) else {}
    value["model_meta"] = loads_json(value.pop("model_meta_json", None), None)
    value["mode"] = _normalize_quick_help_mode(value.get("mode"), content=value["content"])
    value["origin"] = _normalize_quick_help_origin(value.get("origin"))
    turn_id = str(value.get("turn_id") or "").strip()
    value["turn_id"] = turn_id or None
    return value


def list_deal_context_lever_priorities(
    db_path: str | Path,
    *,
    deal_id: str,
    source_report_id: int,
) -> list[dict[str, Any]]:
    init_db(db_path)
    with connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT events.*
            FROM deal_context_lever_priority_events AS events
            INNER JOIN (
                SELECT lever_id, MAX(id) AS latest_id
                FROM deal_context_lever_priority_events
                WHERE deal_id = ? AND source_report_id = ?
                GROUP BY lever_id
            ) AS latest ON latest.latest_id = events.id
            ORDER BY CASE WHEN events.priority IS NULL THEN 4 ELSE events.priority END, events.id DESC
            """,
            (str(deal_id), int(source_report_id)),
        ).fetchall()
    return [dict(row) for row in rows]


def save_deal_context_lever_priority(
    db_path: str | Path,
    *,
    deal_id: str,
    source_report_id: int,
    lever_id: str,
    priority: int | None,
    actor_role: str,
) -> dict[str, Any]:
    init_db(db_path)
    normalized_deal_id = str(deal_id).strip()
    normalized_lever_id = str(lever_id).strip()
    normalized_role = "manager" if str(actor_role) == "manager" else "rop"
    if not normalized_lever_id or len(normalized_lever_id) > 120:
        raise ValueError("lever_id должен содержать от 1 до 120 символов")
    if priority is not None and (isinstance(priority, bool) or int(priority) not in {1, 2, 3}):
        raise ValueError("priority должен быть 1, 2, 3 или null")
    normalized_priority = int(priority) if priority is not None else None
    created_at = utcish_now()
    with connect(db_path) as conn:
        report = conn.execute(
            "SELECT id FROM ui_reports WHERE id = ? AND entity_type = 'deal' AND entity_id = ?",
            (int(source_report_id), normalized_deal_id),
        ).fetchone()
        if report is None:
            raise ValueError("Отчёт не принадлежит выбранной сделке")
        if normalized_priority is not None:
            occupied = conn.execute(
                """
                SELECT events.lever_id
                FROM deal_context_lever_priority_events AS events
                INNER JOIN (
                    SELECT lever_id, MAX(id) AS latest_id
                    FROM deal_context_lever_priority_events
                    WHERE deal_id = ? AND source_report_id = ?
                    GROUP BY lever_id
                ) AS latest ON latest.latest_id = events.id
                WHERE events.priority = ? AND events.lever_id <> ?
                """,
                (normalized_deal_id, int(source_report_id), normalized_priority, normalized_lever_id),
            ).fetchall()
            for row in occupied:
                conn.execute(
                    """
                    INSERT INTO deal_context_lever_priority_events (
                        deal_id, source_report_id, lever_id, priority, actor_role, created_at
                    ) VALUES (?, ?, ?, NULL, ?, ?)
                    """,
                    (normalized_deal_id, int(source_report_id), str(row["lever_id"]), normalized_role, created_at),
                )
        cursor = conn.execute(
            """
            INSERT INTO deal_context_lever_priority_events (
                deal_id, source_report_id, lever_id, priority, actor_role, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                normalized_deal_id,
                int(source_report_id),
                normalized_lever_id,
                normalized_priority,
                normalized_role,
                created_at,
            ),
        )
        event_id = int(cursor.lastrowid)
    return {
        "id": event_id,
        "deal_id": normalized_deal_id,
        "source_report_id": int(source_report_id),
        "lever_id": normalized_lever_id,
        "priority": normalized_priority,
        "actor_role": normalized_role,
        "created_at": created_at,
    }


def save_deal_manager_quick_help(
    db_path: str | Path,
    *,
    deal_id: str,
    source_report_id: int,
    situation_review_id: int,
    question: str,
    answer_json: dict[str, Any],
    model_meta: dict[str, Any] | None = None,
    mode: str | None = None,
    origin: str | None = None,
    turn_id: str | None = None,
) -> dict[str, Any]:
    """Append one independent quick-help question and validated answer."""
    normalized_question = str(question or "").strip()
    if not 1 <= len(normalized_question) <= 4000:
        raise ValueError("Вопрос должен содержать от 1 до 4000 знаков")
    if not isinstance(answer_json, dict) or not answer_json:
        raise ValueError("Ответ quick help должен быть непустым JSON-объектом")
    if model_meta is not None and not isinstance(model_meta, dict):
        raise ValueError("Метаданные модели должны быть JSON-объектом")
    normalized_mode = _normalize_quick_help_mode(mode, content=answer_json)
    normalized_origin = _normalize_quick_help_origin(origin)
    normalized_turn_id = str(turn_id or "").strip() or None
    init_db(db_path)
    with connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        manager_id = _deal_manager_situation_review_context(
            conn,
            deal_id=str(deal_id),
            source_report_id=int(source_report_id),
        )
        review = conn.execute(
            """
            SELECT id FROM deal_manager_situation_reviews
            WHERE id = ? AND deal_id = ? AND manager_id = ? AND source_report_id = ?
            """,
            (int(situation_review_id), str(deal_id), manager_id, int(source_report_id)),
        ).fetchone()
        if review is None:
            raise ValueError("Текущая подтверждённая ситуация сделки не найдена")
        cursor = conn.execute(
            """
            INSERT INTO deal_manager_quick_help (
                deal_id, manager_id, source_report_id, situation_review_id,
                mode, origin, turn_id, question, answer_json, model_meta_json, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(deal_id),
                manager_id,
                int(source_report_id),
                int(situation_review_id),
                normalized_mode,
                normalized_origin,
                normalized_turn_id,
                normalized_question,
                dumps_json(answer_json),
                dumps_json(model_meta) if model_meta is not None else None,
                utcish_now(),
            ),
        )
        row = conn.execute(
            "SELECT * FROM deal_manager_quick_help WHERE id = ?",
            (int(cursor.lastrowid),),
        ).fetchone()
        report = conn.execute(
            "SELECT analysis_run_id FROM ui_reports WHERE id = ?",
            (int(source_report_id),),
        ).fetchone()
        assert row is not None
        _insert_manager_trajectory_event(
            conn,
            entity_type="deal",
            entity_id=str(deal_id),
            manager_id=manager_id,
            event_type="recommendation_generated",
            recommendation_kind="quick_help",
            recommendation_id=int(row["id"]),
            analysis_run_id=(
                int(report["analysis_run_id"])
                if report is not None and report["analysis_run_id"] is not None
                else None
            ),
            report_id=int(source_report_id),
            source="neuro_rop",
            source_event_key=f"generated:quick_help:{int(row['id'])}",
            occurred_at=str(row["created_at"]),
        )
    result = _row_to_deal_manager_quick_help(row)
    assert result is not None
    return result


def list_deal_manager_quick_help(
    db_path: str | Path,
    *,
    deal_id: str,
    limit: int = 20,
    before_id: int | None = None,
) -> list[dict[str, Any]]:
    """Return quick-help history for the deal's current local manager."""
    if not 1 <= int(limit) <= 100:
        raise ValueError("limit должен быть от 1 до 100")
    if before_id is not None and int(before_id) < 1:
        raise ValueError("before_id должен быть положительным")
    init_db(db_path)
    with connect(db_path) as conn:
        deal = conn.execute(
            "SELECT manager_id FROM deal_control_deals WHERE deal_id = ?",
            (str(deal_id),),
        ).fetchone()
        if deal is None:
            raise ValueError("Сделка ещё не добавлена в контур контроля")
        manager_id = str(deal["manager_id"] or "").strip()
        if not manager_id:
            raise ValueError("У сделки не указан локальный ответственный менеджер")
        clauses = ["deal_id = ?", "manager_id = ?"]
        params: list[Any] = [str(deal_id), manager_id]
        if before_id is not None:
            clauses.append("id < ?")
            params.append(int(before_id))
        params.append(int(limit))
        rows = conn.execute(
            "SELECT * FROM deal_manager_quick_help WHERE "
            + " AND ".join(clauses)
            + " ORDER BY id DESC LIMIT ?",
            params,
        ).fetchall()
    return [
        value
        for row in rows
        if (value := _row_to_deal_manager_quick_help(row)) is not None
    ]


def get_deal_manager_quick_help(
    db_path: str | Path,
    *,
    deal_id: str,
    quick_help_id: int,
) -> dict[str, Any] | None:
    """Return one quick-help row scoped to the deal's current local manager."""
    init_db(db_path)
    with connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT quick_help.* FROM deal_manager_quick_help AS quick_help
            JOIN deal_control_deals AS deal ON deal.deal_id = quick_help.deal_id
            WHERE quick_help.id = ? AND quick_help.deal_id = ?
              AND quick_help.manager_id = deal.manager_id
            """,
            (int(quick_help_id), str(deal_id)),
        ).fetchone()
    return _row_to_deal_manager_quick_help(row)


def get_current_deal_manager_quick_help(
    db_path: str | Path,
    *,
    deal_id: str,
    source_report_id: int,
    situation_review_id: int,
    mode: str,
) -> dict[str, Any] | None:
    """Return the latest recommendation for one mode on the current situation."""
    normalized_mode = _normalize_quick_help_mode(mode)
    init_db(db_path)
    with connect(db_path) as conn:
        deal = conn.execute(
            "SELECT manager_id FROM deal_control_deals WHERE deal_id = ?",
            (str(deal_id),),
        ).fetchone()
        if deal is None:
            raise ValueError("Сделка ещё не добавлена в контур контроля")
        manager_id = str(deal["manager_id"] or "").strip()
        if not manager_id:
            raise ValueError("У сделки не указан локальный ответственный менеджер")
        rows = conn.execute(
            """
            SELECT * FROM deal_manager_quick_help
            WHERE deal_id = ? AND manager_id = ?
              AND source_report_id = ? AND situation_review_id = ?
            ORDER BY id DESC
            """,
            (str(deal_id), manager_id, int(source_report_id), int(situation_review_id)),
        ).fetchall()
    for row in rows:
        value = _row_to_deal_manager_quick_help(row)
        if value is not None and value.get("mode") == normalized_mode:
            return value
    return None


def _row_to_deal_manager_full_script(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    value = dict(row)
    value["content"] = loads_json(value.pop("script_json", None), {})
    value["model_meta"] = loads_json(value.pop("model_meta_json", None), None)
    return value


def get_deal_manager_full_script(
    db_path: str | Path,
    *,
    deal_id: str,
    source_report_id: int,
    situation_review_id: int,
    quick_help_id: int,
    selected_strategy: str,
) -> dict[str, Any] | None:
    """Return only an exact current-context script, never a stale variant."""
    init_db(db_path)
    with connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT script.* FROM deal_manager_full_scripts AS script
            JOIN deal_control_deals AS deal ON deal.deal_id = script.deal_id
            WHERE script.deal_id = ? AND script.manager_id = deal.manager_id
              AND script.source_report_id = ? AND script.situation_review_id = ?
              AND script.quick_help_id = ? AND script.selected_strategy = ?
            LIMIT 1
            """,
            (
                str(deal_id), int(source_report_id), int(situation_review_id),
                int(quick_help_id), str(selected_strategy),
            ),
        ).fetchone()
    return _row_to_deal_manager_full_script(row)


def save_deal_manager_full_script(
    db_path: str | Path,
    *,
    deal_id: str,
    source_report_id: int,
    situation_review_id: int,
    quick_help_id: int,
    selected_strategy: str,
    script_json: dict[str, Any],
    model_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist one validated on-demand script idempotently for its exact context."""
    if selected_strategy not in {"primary", "alternative", "pattern_break"}:
        raise ValueError("Неизвестный вариант сообщения")
    if not isinstance(script_json, dict) or not script_json:
        raise ValueError("Полный скрипт должен быть непустым JSON-объектом")
    if model_meta is not None and not isinstance(model_meta, dict):
        raise ValueError("Метаданные модели должны быть JSON-объектом")
    init_db(db_path)
    with connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        manager_id = _deal_manager_situation_review_context(
            conn, deal_id=str(deal_id), source_report_id=int(source_report_id),
        )
        linked = conn.execute(
            """
            SELECT quick_help.id FROM deal_manager_quick_help AS quick_help
            JOIN deal_manager_situation_reviews AS review ON review.id = quick_help.situation_review_id
            WHERE quick_help.id = ? AND quick_help.deal_id = ? AND quick_help.manager_id = ?
              AND quick_help.source_report_id = ? AND quick_help.situation_review_id = ?
              AND review.source_report_id = ?
            """,
            (
                int(quick_help_id), str(deal_id), manager_id, int(source_report_id),
                int(situation_review_id), int(source_report_id),
            ),
        ).fetchone()
        if linked is None:
            raise ValueError("Quick Help не относится к текущей подтверждённой ситуации")
        conn.execute(
            """
            INSERT INTO deal_manager_full_scripts (
                deal_id, manager_id, source_report_id, situation_review_id,
                quick_help_id, selected_strategy, script_json, model_meta_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(deal_id, manager_id, source_report_id, situation_review_id, quick_help_id, selected_strategy)
            DO NOTHING
            """,
            (
                str(deal_id), manager_id, int(source_report_id), int(situation_review_id),
                int(quick_help_id), selected_strategy, dumps_json(script_json),
                dumps_json(model_meta) if model_meta is not None else None, utcish_now(),
            ),
        )
        existing = conn.execute(
            """
            SELECT id, script_json FROM deal_manager_full_scripts
            WHERE deal_id = ? AND manager_id = ? AND source_report_id = ?
              AND situation_review_id = ? AND quick_help_id = ? AND selected_strategy = ?
            """,
            (
                str(deal_id), manager_id, int(source_report_id), int(situation_review_id),
                int(quick_help_id), selected_strategy,
            ),
        ).fetchone()
        if existing is not None and _manager_script_should_replace(loads_json(existing["script_json"], {}), script_json):
            conn.execute(
                """
                UPDATE deal_manager_full_scripts
                SET script_json = ?, model_meta_json = ?, created_at = ?
                WHERE id = ?
                """,
                (
                    dumps_json(script_json),
                    dumps_json(model_meta) if model_meta is not None else None,
                    utcish_now(), int(existing["id"]),
                ),
            )
        row = conn.execute(
            """
            SELECT * FROM deal_manager_full_scripts
            WHERE deal_id = ? AND manager_id = ? AND source_report_id = ?
              AND situation_review_id = ? AND quick_help_id = ? AND selected_strategy = ?
            """,
            (
                str(deal_id), manager_id, int(source_report_id), int(situation_review_id),
                int(quick_help_id), selected_strategy,
            ),
        ).fetchone()
    result = _row_to_deal_manager_full_script(row)
    assert result is not None
    return result


def get_deal_manager_call_script(
    db_path: str | Path,
    *,
    deal_id: str,
    source_report_id: int,
    situation_review_id: int,
    quick_help_id: int,
    selected_strategy: str,
) -> dict[str, Any] | None:
    """Return the exact current-context phone call script."""
    init_db(db_path)
    with connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT script.* FROM deal_manager_call_scripts AS script
            JOIN deal_control_deals AS deal ON deal.deal_id = script.deal_id
            WHERE script.deal_id = ? AND script.manager_id = deal.manager_id
              AND script.source_report_id = ? AND script.situation_review_id = ?
              AND script.quick_help_id = ? AND script.selected_strategy = ?
            LIMIT 1
            """,
            (
                str(deal_id), int(source_report_id), int(situation_review_id),
                int(quick_help_id), str(selected_strategy),
            ),
        ).fetchone()
    return _row_to_deal_manager_full_script(row)


def save_deal_manager_call_script(
    db_path: str | Path,
    *,
    deal_id: str,
    source_report_id: int,
    situation_review_id: int,
    quick_help_id: int,
    selected_strategy: str,
    script_json: dict[str, Any],
    model_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist one validated phone call script for its exact context.

    Same-contract writes stay idempotent. A newer script_contract replaces an outdated row
    so a regenerated call script is not blocked by an old cached JSON shape.
    """
    if selected_strategy not in {"primary", "alternative", "pattern_break"}:
        raise ValueError("Неизвестный вариант сообщения")
    if not isinstance(script_json, dict) or not script_json:
        raise ValueError("Сценарий звонка должен быть непустым JSON-объектом")
    if model_meta is not None and not isinstance(model_meta, dict):
        raise ValueError("Метаданные модели должны быть JSON-объектом")
    init_db(db_path)
    with connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        manager_id = _deal_manager_situation_review_context(
            conn, deal_id=str(deal_id), source_report_id=int(source_report_id),
        )
        linked = conn.execute(
            """
            SELECT quick_help.id FROM deal_manager_quick_help AS quick_help
            JOIN deal_manager_situation_reviews AS review ON review.id = quick_help.situation_review_id
            WHERE quick_help.id = ? AND quick_help.deal_id = ? AND quick_help.manager_id = ?
              AND quick_help.source_report_id = ? AND quick_help.situation_review_id = ?
              AND review.source_report_id = ?
            """,
            (
                int(quick_help_id), str(deal_id), manager_id, int(source_report_id),
                int(situation_review_id), int(source_report_id),
            ),
        ).fetchone()
        if linked is None:
            raise ValueError("Quick Help не относится к текущей подтверждённой ситуации")
        existing = conn.execute(
            """
            SELECT id, script_json FROM deal_manager_call_scripts
            WHERE deal_id = ? AND manager_id = ? AND source_report_id = ?
              AND situation_review_id = ? AND quick_help_id = ? AND selected_strategy = ?
            """,
            (
                str(deal_id), manager_id, int(source_report_id), int(situation_review_id),
                int(quick_help_id), selected_strategy,
            ),
        ).fetchone()
        payload = (
            str(deal_id), manager_id, int(source_report_id), int(situation_review_id),
            int(quick_help_id), selected_strategy, dumps_json(script_json),
            dumps_json(model_meta) if model_meta is not None else None, utcish_now(),
        )
        if existing is None:
            conn.execute(
                """
                INSERT INTO deal_manager_call_scripts (
                    deal_id, manager_id, source_report_id, situation_review_id,
                    quick_help_id, selected_strategy, script_json, model_meta_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                payload,
            )
        else:
            if _manager_script_should_replace(loads_json(existing["script_json"], {}), script_json):
                conn.execute(
                    """
                    UPDATE deal_manager_call_scripts
                    SET script_json = ?, model_meta_json = ?, created_at = ?
                    WHERE id = ?
                    """,
                    (payload[6], payload[7], payload[8], int(existing["id"])),
                )
        row = conn.execute(
            """
            SELECT * FROM deal_manager_call_scripts
            WHERE deal_id = ? AND manager_id = ? AND source_report_id = ?
              AND situation_review_id = ? AND quick_help_id = ? AND selected_strategy = ?
            """,
            (
                str(deal_id), manager_id, int(source_report_id), int(situation_review_id),
                int(quick_help_id), selected_strategy,
            ),
        ).fetchone()
    result = _row_to_deal_manager_full_script(row)
    assert result is not None
    return result


def get_deal_manager_email_script(
    db_path: str | Path, *, deal_id: str, source_report_id: int,
    situation_review_id: int, quick_help_id: int, selected_strategy: str,
) -> dict[str, Any] | None:
    """Return the exact current-context email draft."""
    init_db(db_path)
    with connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT script.* FROM deal_manager_email_scripts AS script
            JOIN deal_control_deals AS deal ON deal.deal_id = script.deal_id
            WHERE script.deal_id = ? AND script.manager_id = deal.manager_id
              AND script.source_report_id = ? AND script.situation_review_id = ?
              AND script.quick_help_id = ? AND script.selected_strategy = ?
            LIMIT 1
            """,
            (str(deal_id), int(source_report_id), int(situation_review_id),
             int(quick_help_id), str(selected_strategy)),
        ).fetchone()
    return _row_to_deal_manager_full_script(row)


def save_deal_manager_email_script(
    db_path: str | Path, *, deal_id: str, source_report_id: int,
    situation_review_id: int, quick_help_id: int, selected_strategy: str,
    script_json: dict[str, Any], model_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist one validated email draft for its exact current context."""
    if selected_strategy not in {"primary", "alternative", "pattern_break"}:
        raise ValueError("Неизвестный вариант сообщения")
    if not isinstance(script_json, dict) or not script_json:
        raise ValueError("Email должен быть непустым JSON-объектом")
    init_db(db_path)
    with connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        manager_id = _deal_manager_situation_review_context(
            conn, deal_id=str(deal_id), source_report_id=int(source_report_id),
        )
        linked = conn.execute(
            """SELECT id FROM deal_manager_quick_help
               WHERE id = ? AND deal_id = ? AND manager_id = ?
                 AND source_report_id = ? AND situation_review_id = ?""",
            (int(quick_help_id), str(deal_id), manager_id, int(source_report_id), int(situation_review_id)),
        ).fetchone()
        if linked is None:
            raise ValueError("Quick Help не относится к текущей подтверждённой ситуации")
        conn.execute(
            """INSERT INTO deal_manager_email_scripts (
                   deal_id, manager_id, source_report_id, situation_review_id,
                   quick_help_id, selected_strategy, script_json, model_meta_json, created_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(deal_id, manager_id, source_report_id, situation_review_id, quick_help_id, selected_strategy)
               DO NOTHING""",
            (str(deal_id), manager_id, int(source_report_id), int(situation_review_id),
             int(quick_help_id), selected_strategy, dumps_json(script_json),
             dumps_json(model_meta) if model_meta is not None else None, utcish_now()),
        )
        existing = conn.execute(
            """SELECT id, script_json FROM deal_manager_email_scripts
               WHERE deal_id = ? AND manager_id = ? AND source_report_id = ?
                 AND situation_review_id = ? AND quick_help_id = ? AND selected_strategy = ?""",
            (str(deal_id), manager_id, int(source_report_id), int(situation_review_id),
             int(quick_help_id), selected_strategy),
        ).fetchone()
        if existing is not None and _manager_script_should_replace(loads_json(existing["script_json"], {}), script_json):
            conn.execute(
                """UPDATE deal_manager_email_scripts
                   SET script_json = ?, model_meta_json = ?, created_at = ?
                   WHERE id = ?""",
                (
                    dumps_json(script_json),
                    dumps_json(model_meta) if model_meta is not None else None,
                    utcish_now(), int(existing["id"]),
                ),
            )
        row = conn.execute(
            """SELECT * FROM deal_manager_email_scripts
               WHERE deal_id = ? AND manager_id = ? AND source_report_id = ?
                 AND situation_review_id = ? AND quick_help_id = ? AND selected_strategy = ?""",
            (str(deal_id), manager_id, int(source_report_id), int(situation_review_id),
             int(quick_help_id), selected_strategy),
        ).fetchone()
    result = _row_to_deal_manager_full_script(row)
    assert result is not None
    return result


def _row_to_deal_manager_followups(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    value = dict(row)
    value["content"] = loads_json(value.pop("followups_json", None), {})
    value["model_meta"] = loads_json(value.pop("model_meta_json", None), None)
    return value


def get_deal_manager_followups(
    db_path: str | Path, *, deal_id: str, source_report_id: int,
    situation_review_id: int,
) -> dict[str, Any] | None:
    """Return follow-up ideas only for the exact current situation."""
    init_db(db_path)
    with connect(db_path) as conn:
        row = conn.execute(
            """SELECT item.* FROM deal_manager_followups AS item
               JOIN deal_control_deals AS deal ON deal.deal_id = item.deal_id
               WHERE item.deal_id = ? AND item.manager_id = deal.manager_id
                 AND item.source_report_id = ? AND item.situation_review_id = ?
               LIMIT 1""",
            (str(deal_id), int(source_report_id), int(situation_review_id)),
        ).fetchone()
    return _row_to_deal_manager_followups(row)


def save_deal_manager_followups(
    db_path: str | Path, *, deal_id: str, source_report_id: int,
    situation_review_id: int, followups_json: dict[str, Any],
    model_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist validated follow-up ideas idempotently for the current situation."""
    if not isinstance(followups_json, dict) or not followups_json:
        raise ValueError("Фоллоуапы должны быть непустым JSON-объектом")
    init_db(db_path)
    with connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        manager_id = _deal_manager_situation_review_context(
            conn, deal_id=str(deal_id), source_report_id=int(source_report_id),
        )
        review = conn.execute(
            """SELECT id FROM deal_manager_situation_reviews
               WHERE id = ? AND deal_id = ? AND manager_id = ? AND source_report_id = ?""",
            (int(situation_review_id), str(deal_id), manager_id, int(source_report_id)),
        ).fetchone()
        if review is None:
            raise ValueError("Фоллоуапы не относятся к текущей подтверждённой ситуации")
        conn.execute(
            """INSERT INTO deal_manager_followups (
                   deal_id, manager_id, source_report_id, situation_review_id,
                   followups_json, model_meta_json, created_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(deal_id, manager_id, source_report_id, situation_review_id) DO NOTHING""",
            (str(deal_id), manager_id, int(source_report_id), int(situation_review_id),
             dumps_json(followups_json), dumps_json(model_meta) if model_meta is not None else None, utcish_now()),
        )
        row = conn.execute(
            """SELECT * FROM deal_manager_followups
               WHERE deal_id = ? AND manager_id = ? AND source_report_id = ? AND situation_review_id = ?""",
            (str(deal_id), manager_id, int(source_report_id), int(situation_review_id)),
        ).fetchone()
    result = _row_to_deal_manager_followups(row)
    assert result is not None
    return result


def _row_to_deal_manager_companion(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    value = dict(row)
    value["content"] = loads_json(value.pop("companion_json", None), {})
    value["model_meta"] = loads_json(value.pop("model_meta_json", None), None)
    return value


def get_deal_manager_companion(
    db_path: str | Path, *, deal_id: str, source_report_id: int, last_event_id: str,
) -> dict[str, Any] | None:
    """Return companion text only for this report and last CRM event."""
    init_db(db_path)
    with connect(db_path) as conn:
        row = conn.execute(
            """SELECT item.* FROM deal_manager_companion_messages AS item
               JOIN deal_control_deals AS deal ON deal.deal_id = item.deal_id
               WHERE item.deal_id = ? AND item.manager_id = deal.manager_id
                 AND item.source_report_id = ? AND item.last_event_id = ?
               LIMIT 1""",
            (str(deal_id), int(source_report_id), str(last_event_id)),
        ).fetchone()
    return _row_to_deal_manager_companion(row)


def save_deal_manager_companion(
    db_path: str | Path, *, deal_id: str, source_report_id: int, last_event_id: str,
    companion_json: dict[str, Any], model_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist a validated companion message for the current report and event."""
    if not isinstance(companion_json, dict) or not companion_json:
        raise ValueError("Сопроводительный текст должен быть непустым JSON-объектом")
    event_id = str(last_event_id or "").strip()
    if not event_id:
        raise ValueError("Нужен идентификатор последней коммуникации")
    init_db(db_path)
    with connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        manager_id = _deal_manager_situation_review_context(
            conn, deal_id=str(deal_id), source_report_id=int(source_report_id),
        )
        conn.execute(
            """INSERT INTO deal_manager_companion_messages (
                   deal_id, manager_id, source_report_id, last_event_id,
                   companion_json, model_meta_json, created_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(deal_id, manager_id, source_report_id, last_event_id) DO UPDATE SET
                   companion_json = excluded.companion_json,
                   model_meta_json = excluded.model_meta_json,
                   created_at = excluded.created_at""",
            (str(deal_id), manager_id, int(source_report_id), event_id,
             dumps_json(companion_json), dumps_json(model_meta) if model_meta is not None else None, utcish_now()),
        )
        row = conn.execute(
            """SELECT * FROM deal_manager_companion_messages
               WHERE deal_id = ? AND manager_id = ? AND source_report_id = ? AND last_event_id = ?""",
            (str(deal_id), manager_id, int(source_report_id), event_id),
        ).fetchone()
    result = _row_to_deal_manager_companion(row)
    assert result is not None
    return result


def record_deal_manager_assistant_event(
    db_path: str | Path,
    *,
    deal_id: str,
    event_type: str,
    quick_help_id: int | None = None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Append one manager-assistant action without changing CRM task/result state."""
    normalized_type = str(event_type or "").strip()
    if normalized_type != "communication_completed":
        raise ValueError("Неизвестное событие помощника менеджера")
    if payload is not None and not isinstance(payload, dict):
        raise ValueError("Данные события должны быть JSON-объектом")
    init_db(db_path)
    with connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        deal = conn.execute(
            "SELECT manager_id FROM deal_control_deals WHERE deal_id = ?",
            (str(deal_id),),
        ).fetchone()
        if deal is None:
            raise ValueError("Сделка ещё не добавлена в контур контроля")
        manager_id = str(deal["manager_id"] or "").strip()
        if not manager_id:
            raise ValueError("У сделки не указан локальный ответственный менеджер")
        if quick_help_id is not None:
            quick_help = conn.execute(
                """
                SELECT id FROM deal_manager_quick_help
                WHERE id = ? AND deal_id = ? AND manager_id = ?
                """,
                (int(quick_help_id), str(deal_id), manager_id),
            ).fetchone()
            if quick_help is None:
                raise ValueError("Ответ помощника для этой сделки не найден")
        conn.execute(
            """
            INSERT INTO deal_manager_assistant_events (
                deal_id, manager_id, event_type, quick_help_id, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(deal_id, manager_id, event_type, quick_help_id)
            WHERE quick_help_id IS NOT NULL DO NOTHING
            """,
            (
                str(deal_id),
                manager_id,
                normalized_type,
                int(quick_help_id) if quick_help_id is not None else None,
                dumps_json(payload) if payload is not None else None,
                utcish_now(),
            ),
        )
        row = conn.execute(
            """
            SELECT * FROM deal_manager_assistant_events
            WHERE deal_id = ? AND manager_id = ? AND event_type = ?
              AND ((quick_help_id = ?) OR (quick_help_id IS NULL AND ? IS NULL))
            ORDER BY id DESC LIMIT 1
            """,
            (
                str(deal_id), manager_id, normalized_type,
                int(quick_help_id) if quick_help_id is not None else None,
                int(quick_help_id) if quick_help_id is not None else None,
            ),
        ).fetchone()
    assert row is not None
    result = dict(row)
    result["payload"] = loads_json(result.pop("payload_json", None), None)
    return result


def list_deal_manager_assistant_events(
    db_path: str | Path,
    *,
    deal_id: str,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Return local assistant actions for the deal's current manager."""
    if not 1 <= int(limit) <= 200:
        raise ValueError("limit должен быть от 1 до 200")
    init_db(db_path)
    with connect(db_path) as conn:
        deal = conn.execute(
            "SELECT manager_id FROM deal_control_deals WHERE deal_id = ?",
            (str(deal_id),),
        ).fetchone()
        if deal is None:
            raise ValueError("Сделка ещё не добавлена в контур контроля")
        manager_id = str(deal["manager_id"] or "").strip()
        if not manager_id:
            raise ValueError("У сделки не указан локальный ответственный менеджер")
        rows = conn.execute(
            """
            SELECT * FROM deal_manager_assistant_events
            WHERE deal_id = ? AND manager_id = ?
            ORDER BY id DESC LIMIT ?
            """,
            (str(deal_id), manager_id, int(limit)),
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["payload"] = loads_json(item.pop("payload_json", None), None)
        result.append(item)
    return result


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


def _normalize_pipeline_id_list(values: Any) -> list[str]:
    result: list[str] = []
    if not isinstance(values, list):
        return result
    for item in values:
        text = str(item).strip()
        if text and text not in result:
            result.append(text)
    return result


def _scope_pipeline_ids(row: sqlite3.Row | dict[str, Any]) -> list[str]:
    stored = _normalize_pipeline_id_list(loads_json(row["pipeline_ids_json"] if "pipeline_ids_json" in row.keys() else None, []))
    if stored:
        return stored
    pipeline_id = str(row["pipeline_id"] or "").strip()
    return [pipeline_id] if pipeline_id else list(DEFAULT_DEAL_CONTROL_PIPELINE_IDS)


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
    communications_today = loads_json(value.pop("communications_today_json", None), {})
    value["communications_today"] = communications_today if isinstance(communications_today, dict) else {}
    checklist_state = loads_json(value.pop("checklist_state_json", None), {})
    value["checklist_state"] = checklist_state if isinstance(checklist_state, dict) else {}
    return value


def get_deal_control_scope(db_path: str | Path) -> dict[str, Any]:
    init_db(db_path)
    with connect(db_path) as conn:
        row = conn.execute("SELECT * FROM deal_control_scope WHERE scope_key = ?", (DEAL_CONTROL_SCOPE_KEY,)).fetchone()
    if row is None:
        return {
            "initial_deal_ids": [],
            "manager_ids": [],
            "pipeline_id": DEFAULT_DEAL_CONTROL_PIPELINE_ID,
            "pipeline_ids": list(DEFAULT_DEAL_CONTROL_PIPELINE_IDS),
            "configured": False,
        }
    pipeline_ids = _scope_pipeline_ids(row)
    return {
        "initial_deal_ids": loads_json(row["initial_deal_ids_json"], []),
        "manager_ids": loads_json(row["manager_ids_json"], []),
        "pipeline_id": pipeline_ids[0] if pipeline_ids else DEFAULT_DEAL_CONTROL_PIPELINE_ID,
        "pipeline_ids": pipeline_ids,
        "updated_at": row["updated_at"],
        "configured": True,
    }


def save_deal_control_scope(
    db_path: str | Path,
    *,
    initial_deal_ids: list[str],
    manager_ids: list[str],
    pipeline_id: str,
    pipeline_ids: list[str] | None = None,
) -> dict[str, Any]:
    init_db(db_path)
    deals = list(dict.fromkeys(str(value).strip() for value in initial_deal_ids if str(value).strip()))
    managers = list(dict.fromkeys(str(value).strip() for value in manager_ids if str(value).strip()))
    if not deals and not managers:
        raise ValueError("Нужен хотя бы один ID сделки или ответственного")
    normalized_pipelines = _normalize_pipeline_id_list(pipeline_ids)
    if not normalized_pipelines:
        normalized_pipelines = [str(pipeline_id).strip() or DEFAULT_DEAL_CONTROL_PIPELINE_ID]
    primary_pipeline = normalized_pipelines[0]
    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO deal_control_scope (
                scope_key, initial_deal_ids_json, manager_ids_json, pipeline_id, pipeline_ids_json, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(scope_key) DO UPDATE SET
                initial_deal_ids_json = excluded.initial_deal_ids_json,
                manager_ids_json = excluded.manager_ids_json,
                pipeline_id = excluded.pipeline_id,
                pipeline_ids_json = excluded.pipeline_ids_json,
                updated_at = excluded.updated_at
            """,
            (
                DEAL_CONTROL_SCOPE_KEY,
                dumps_json(deals),
                dumps_json(managers),
                primary_pipeline,
                dumps_json(normalized_pipelines),
                utcish_now(),
            ),
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


def save_deal_control_communications_today(
    db_path: str | Path,
    *,
    deal_id: str,
    summary: dict[str, Any],
) -> dict[str, Any]:
    init_db(db_path)
    with connect(db_path) as conn:
        if conn.execute("SELECT 1 FROM deal_control_deals WHERE deal_id = ?", (str(deal_id),)).fetchone() is None:
            raise ValueError("Сделка ещё не добавлена в контур контроля")
        conn.execute(
            "UPDATE deal_control_deals SET communications_today_json = ?, updated_at = ? WHERE deal_id = ?",
            (dumps_json(summary), utcish_now(), str(deal_id)),
        )
        row = conn.execute("SELECT * FROM deal_control_deals WHERE deal_id = ?", (str(deal_id),)).fetchone()
    result = _row_to_deal_control_deal(row)
    assert result is not None
    return result


def save_deal_control_checklist_item_state(
    db_path: str | Path,
    *,
    deal_id: str,
    item_id: str,
    completed: bool,
    source_report_id: int,
) -> dict[str, Any]:
    """Persist manager-owned checklist state without changing Bitrix task semantics."""
    init_db(db_path)
    normalized_item_id = str(item_id or "").strip()
    if not normalized_item_id:
        raise ValueError("Не указан пункт чек-листа")
    now = utcish_now()
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT checklist_state_json FROM deal_control_deals WHERE deal_id = ?",
            (str(deal_id),),
        ).fetchone()
        if row is None:
            raise ValueError("Сделка ещё не добавлена в контур контроля")
        state = loads_json(row["checklist_state_json"], {})
        if not isinstance(state, dict) or int(state.get("source_report_id") or 0) != int(source_report_id):
            state = {"source_report_id": int(source_report_id), "items": {}}
        items = state.get("items")
        if not isinstance(items, dict):
            items = {}
        items[normalized_item_id] = {
            "completed": bool(completed),
            "completed_at": now if completed else None,
            "completed_by": "manager" if completed else None,
        }
        state["items"] = items
        state["updated_at"] = now
        conn.execute(
            "UPDATE deal_control_deals SET checklist_state_json = ?, updated_at = ? WHERE deal_id = ?",
            (dumps_json(state), now, str(deal_id)),
        )
    return state


DAILY_CHECKLIST_LIMIT = 5


def _daily_checklist_business_date(value: date | datetime | str | None = None) -> str:
    if isinstance(value, str):
        return date.fromisoformat(value).isoformat()
    if isinstance(value, datetime):
        current = value if value.tzinfo else value.replace(tzinfo=MSK_TZ)
        return current.astimezone(MSK_TZ).date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return datetime.now(MSK_TZ).date().isoformat()


def _normalize_daily_checklist_text(value: Any) -> str:
    return re.sub(r"\W+", " ", str(value or "").lower(), flags=re.UNICODE).strip()


def _daily_checklist_event(
    conn: sqlite3.Connection,
    *,
    checklist_id: int,
    event_type: str,
    actor: str,
    item_id: int | None = None,
    event_key: str | None = None,
    source_report_id: int | None = None,
    reason: str | None = None,
    payload: dict[str, Any] | None = None,
    created_at: str,
) -> None:
    conn.execute(
        """
        INSERT INTO deal_daily_checklist_events (
            checklist_id, item_id, event_key, event_type, actor,
            source_report_id, reason, payload_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            checklist_id,
            item_id,
            event_key,
            event_type,
            actor,
            source_report_id,
            reason,
            dumps_json(payload) if payload is not None else None,
            created_at,
        ),
    )


def _insert_daily_checklist_item(
    conn: sqlite3.Connection,
    *,
    checklist_id: int,
    text: str,
    source: str,
    origin_report_id: int | None,
    carried_from_item_id: int | None,
    last_change_type: str,
    now: str,
) -> int | None:
    clean_text = re.sub(r"\s+", " ", str(text or "")).strip()
    normalized = _normalize_daily_checklist_text(clean_text)
    if not normalized:
        return None
    existing = conn.execute(
        """
        SELECT id FROM deal_daily_checklist_items
        WHERE checklist_id = ? AND normalized_text = ? AND status != 'retired'
        ORDER BY id DESC LIMIT 1
        """,
        (checklist_id, normalized),
    ).fetchone()
    if existing is not None:
        return None
    cursor = conn.execute(
        """
        INSERT INTO deal_daily_checklist_items (
            checklist_id, text, normalized_text, source, status,
            origin_report_id, carried_from_item_id, last_change_type,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, 'open', ?, ?, ?, ?, ?)
        """,
        (
            checklist_id,
            clean_text,
            normalized,
            str(source or "ai"),
            origin_report_id,
            carried_from_item_id,
            last_change_type,
            now,
            now,
        ),
    )
    return int(cursor.lastrowid)


def _ensure_daily_checklist(
    conn: sqlite3.Connection,
    *,
    deal_id: str,
    business_date: str,
    seed_items: list[dict[str, Any]] | None,
    source_report_id: int | None,
    now: str,
) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM deal_daily_checklists WHERE deal_id = ? AND business_date = ?",
        (deal_id, business_date),
    ).fetchone()
    if row is not None:
        return row
    if conn.execute("SELECT 1 FROM deal_control_deals WHERE deal_id = ?", (deal_id,)).fetchone() is None:
        raise ValueError("Сделка ещё не добавлена в контур контроля")
    cursor = conn.execute(
        """
        INSERT INTO deal_daily_checklists (
            deal_id, business_date, revision, source_report_id, created_at, updated_at
        ) VALUES (?, ?, 0, ?, ?, ?)
        """,
        (deal_id, business_date, source_report_id, now, now),
    )
    checklist_id = int(cursor.lastrowid)
    _daily_checklist_event(
        conn,
        checklist_id=checklist_id,
        event_type="created",
        actor="system",
        source_report_id=source_report_id,
        created_at=now,
    )
    previous = conn.execute(
        """
        SELECT * FROM deal_daily_checklists
        WHERE deal_id = ? AND business_date < ?
        ORDER BY business_date DESC LIMIT 1
        """,
        (deal_id, business_date),
    ).fetchone()
    inserted = 0
    if previous is not None:
        previous_items = conn.execute(
            """
            SELECT * FROM deal_daily_checklist_items
            WHERE checklist_id = ? AND status = 'open'
            ORDER BY id
            """,
            (int(previous["id"]),),
        ).fetchall()
        for previous_item in previous_items[:DAILY_CHECKLIST_LIMIT]:
            item_id = _insert_daily_checklist_item(
                conn,
                checklist_id=checklist_id,
                text=str(previous_item["text"]),
                source=str(previous_item["source"]),
                origin_report_id=previous_item["origin_report_id"],
                carried_from_item_id=int(previous_item["id"]),
                last_change_type="carried",
                now=now,
            )
            if item_id is None:
                continue
            inserted += 1
            _daily_checklist_event(
                conn,
                checklist_id=checklist_id,
                item_id=item_id,
                event_type="carried",
                actor="system",
                source_report_id=source_report_id,
                payload={"from_business_date": str(previous["business_date"]), "from_item_id": int(previous_item["id"])},
                created_at=now,
            )
    if previous is None:
        for candidate in (seed_items or [])[:DAILY_CHECKLIST_LIMIT]:
            item_id = _insert_daily_checklist_item(
                conn,
                checklist_id=checklist_id,
                text=str(candidate.get("text") or ""),
                source=str(candidate.get("source") or "ai"),
                origin_report_id=source_report_id,
                carried_from_item_id=None,
                last_change_type="new",
                now=now,
            )
            if item_id is None:
                continue
            inserted += 1
            if bool(candidate.get("completed")):
                conn.execute(
                    """
                    UPDATE deal_daily_checklist_items
                    SET status = 'completed', completed_at = ?, completed_by = ?,
                        last_change_type = 'completed', updated_at = ? WHERE id = ?
                    """,
                    (
                        candidate.get("completed_at") or now,
                        candidate.get("completed_by") or "manager",
                        now,
                        item_id,
                    ),
                )
            _daily_checklist_event(
                conn,
                checklist_id=checklist_id,
                item_id=item_id,
                event_type="added",
                actor="system",
                source_report_id=source_report_id,
                created_at=now,
            )
            if bool(candidate.get("completed")):
                _daily_checklist_event(
                    conn,
                    checklist_id=checklist_id,
                    item_id=item_id,
                    event_type="migrated_completed",
                    actor="system",
                    source_report_id=source_report_id,
                    created_at=now,
                )
    if inserted:
        conn.execute(
            "UPDATE deal_daily_checklists SET revision = 1, updated_at = ? WHERE id = ?",
            (now, checklist_id),
        )
    return conn.execute("SELECT * FROM deal_daily_checklists WHERE id = ?", (checklist_id,)).fetchone()


def _daily_checklist_projection(conn: sqlite3.Connection, checklist: sqlite3.Row) -> dict[str, Any]:
    rows = conn.execute(
        """
        SELECT * FROM deal_daily_checklist_items
        WHERE checklist_id = ? AND status != 'retired'
        ORDER BY id
        """,
        (int(checklist["id"]),),
    ).fetchall()
    items = [
        {
            "id": str(row["id"]),
            "text": str(row["text"]),
            "completed": str(row["status"]) == "completed",
            "completed_at": row["completed_at"],
            "completed_by": row["completed_by"],
            "source": str(row["source"]),
            "change_kind": str(row["last_change_type"]),
        }
        for row in rows
    ]
    completed = sum(1 for item in items if item["completed"])
    return {
        "business_date": str(checklist["business_date"]),
        "revision": int(checklist["revision"]),
        "source_report_id": int(checklist["source_report_id"]) if checklist["source_report_id"] is not None else None,
        "items": items,
        "completed": completed,
        "total": len(items),
        "progress_percent": round(completed * 100 / len(items)) if items else 0,
    }


def get_or_create_deal_daily_checklist(
    db_path: str | Path,
    *,
    deal_id: str,
    business_date: date | datetime | str | None = None,
    seed_items: list[dict[str, Any]] | None = None,
    source_report_id: int | None = None,
) -> dict[str, Any]:
    init_db(db_path)
    normalized_date = _daily_checklist_business_date(business_date)
    now = utcish_now()
    with connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        checklist = _ensure_daily_checklist(
            conn,
            deal_id=str(deal_id),
            business_date=normalized_date,
            seed_items=seed_items,
            source_report_id=source_report_id,
            now=now,
        )
        return _daily_checklist_projection(conn, checklist)


def get_deal_daily_checklist_analysis_projection(
    db_path: str | Path,
    deal_id: str,
    *,
    business_date: date | datetime | str | None = None,
) -> dict[str, Any]:
    """Return bounded dynamic checklist context; manager marks are explicitly self-reported."""
    init_db(db_path)
    normalized_date = _daily_checklist_business_date(business_date)
    now = utcish_now()
    with connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("SELECT 1 FROM deal_control_deals WHERE deal_id = ?", (str(deal_id),)).fetchone() is None:
            return {
                "tracked": False,
                "business_date": normalized_date,
                "revision": 0,
                "items": [],
                "previous_day": None,
                "manager_marks_are_client_evidence": False,
            }
        checklist = _ensure_daily_checklist(
            conn,
            deal_id=str(deal_id),
            business_date=normalized_date,
            seed_items=None,
            source_report_id=None,
            now=now,
        )
        current = _daily_checklist_projection(conn, checklist)
        previous = conn.execute(
            """
            SELECT * FROM deal_daily_checklists
            WHERE deal_id = ? AND business_date < ?
            ORDER BY business_date DESC LIMIT 1
            """,
            (str(deal_id), normalized_date),
        ).fetchone()
        previous_summary = None
        if previous is not None:
            statuses = conn.execute(
                """
                SELECT status, COUNT(*) AS count FROM deal_daily_checklist_items
                WHERE checklist_id = ? GROUP BY status
                """,
                (int(previous["id"]),),
            ).fetchall()
            counts = {str(row["status"]): int(row["count"]) for row in statuses}
            previous_summary = {
                "business_date": str(previous["business_date"]),
                "completed": counts.get("completed", 0),
                "unfinished": counts.get("open", 0),
                "retired": counts.get("retired", 0),
            }
        return {
            "tracked": True,
            "business_date": current["business_date"],
            "revision": current["revision"],
            "items": [
                {
                    "id": item["id"],
                    "text": item["text"],
                    "completed": item["completed"],
                    "change_kind": item["change_kind"],
                }
                for item in current["items"]
            ],
            "previous_day": previous_summary,
            "manager_marks_are_client_evidence": False,
        }


def save_deal_daily_checklist_item_completion(
    db_path: str | Path,
    *,
    deal_id: str,
    item_id: str,
    completed: bool,
    business_date: date | datetime | str | None = None,
) -> dict[str, Any]:
    init_db(db_path)
    normalized_date = _daily_checklist_business_date(business_date)
    now = utcish_now()
    with connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        checklist = _ensure_daily_checklist(
            conn,
            deal_id=str(deal_id),
            business_date=normalized_date,
            seed_items=None,
            source_report_id=None,
            now=now,
        )
        item = conn.execute(
            """
            SELECT * FROM deal_daily_checklist_items
            WHERE id = ? AND checklist_id = ? AND status != 'retired'
            """,
            (str(item_id), int(checklist["id"])),
        ).fetchone()
        if item is None:
            raise ValueError("Пункт не найден в актуальном чек-листе")
        next_status = "completed" if completed else "open"
        if str(item["status"]) != next_status:
            conn.execute(
                """
                UPDATE deal_daily_checklist_items
                SET status = ?, completed_at = ?, completed_by = ?,
                    last_change_type = ?, status_reason = NULL, updated_at = ?
                WHERE id = ?
                """,
                (
                    next_status,
                    now if completed else None,
                    "manager" if completed else None,
                    "completed" if completed else "returned",
                    now,
                    int(item["id"]),
                ),
            )
            conn.execute(
                "UPDATE deal_daily_checklists SET revision = revision + 1, updated_at = ? WHERE id = ?",
                (now, int(checklist["id"])),
            )
            _daily_checklist_event(
                conn,
                checklist_id=int(checklist["id"]),
                item_id=int(item["id"]),
                event_type="completed" if completed else "returned",
                actor="manager",
                created_at=now,
            )
        current = conn.execute("SELECT * FROM deal_daily_checklists WHERE id = ?", (int(checklist["id"]),)).fetchone()
        return _daily_checklist_projection(conn, current)


def apply_deal_daily_checklist_update(
    db_path: str | Path,
    *,
    deal_id: str,
    source_report_id: int,
    update: dict[str, Any] | None,
    fallback_items: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Merge one validated AI delta without replacing concurrent manager state."""
    init_db(db_path)
    now = utcish_now()
    payload = update if isinstance(update, dict) else {}
    normalized_date = _daily_checklist_business_date(payload.get("business_date"))
    event_key = f"daily-checklist-analysis:{deal_id}:{int(source_report_id)}"
    with connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("SELECT 1 FROM deal_control_deals WHERE deal_id = ?", (str(deal_id),)).fetchone() is None:
            return None
        existing_event = conn.execute(
            "SELECT checklist_id FROM deal_daily_checklist_events WHERE event_key = ?",
            (event_key,),
        ).fetchone()
        if existing_event is not None:
            checklist = conn.execute(
                "SELECT * FROM deal_daily_checklists WHERE id = ?",
                (int(existing_event["checklist_id"]),),
            ).fetchone()
            return _daily_checklist_projection(conn, checklist)
        checklist = _ensure_daily_checklist(
            conn,
            deal_id=str(deal_id),
            business_date=normalized_date,
            seed_items=None,
            source_report_id=int(source_report_id),
            now=now,
        )
        checklist_id = int(checklist["id"])
        base_revision = int(payload.get("base_revision") or 0)
        stale_revision = base_revision != int(checklist["revision"])
        applied: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []

        for action in payload.get("retire") or []:
            item = conn.execute(
                "SELECT * FROM deal_daily_checklist_items WHERE id = ? AND checklist_id = ?",
                (str(action.get("item_id") or ""), checklist_id),
            ).fetchone()
            reason = str(action.get("reason") or "").strip()
            if stale_revision or item is None or str(item["status"]) != "open":
                skipped.append({"action": "retire", "item_id": str(action.get("item_id") or ""), "reason": "stale_or_not_open"})
                continue
            conn.execute(
                """
                UPDATE deal_daily_checklist_items
                SET status = 'retired', last_change_type = 'retired', status_reason = ?, updated_at = ?
                WHERE id = ?
                """,
                (reason, now, int(item["id"])),
            )
            applied.append({"action": "retire", "item_id": str(item["id"])})
            _daily_checklist_event(
                conn, checklist_id=checklist_id, item_id=int(item["id"]), event_type="retired",
                actor="ai", source_report_id=int(source_report_id), reason=reason, created_at=now,
            )

        for action in payload.get("reopen") or []:
            item = conn.execute(
                "SELECT * FROM deal_daily_checklist_items WHERE id = ? AND checklist_id = ?",
                (str(action.get("item_id") or ""), checklist_id),
            ).fetchone()
            reason = str(action.get("reason") or "").strip()
            if stale_revision or item is None or str(item["status"]) != "completed" or not reason:
                skipped.append({"action": "reopen", "item_id": str(action.get("item_id") or ""), "reason": "stale_or_not_completed"})
                continue
            conn.execute(
                """
                UPDATE deal_daily_checklist_items
                SET status = 'open', completed_at = NULL, completed_by = NULL,
                    last_change_type = 'reopened', status_reason = ?, updated_at = ?
                WHERE id = ?
                """,
                (reason, now, int(item["id"])),
            )
            applied.append({"action": "reopen", "item_id": str(item["id"])})
            _daily_checklist_event(
                conn, checklist_id=checklist_id, item_id=int(item["id"]), event_type="reopened",
                actor="ai", source_report_id=int(source_report_id), reason=reason, created_at=now,
            )

        additions = list(payload.get("add") or [])
        if not additions and not payload.get("retire") and not payload.get("reopen"):
            visible_count = conn.execute(
                "SELECT COUNT(*) FROM deal_daily_checklist_items WHERE checklist_id = ? AND status != 'retired'",
                (checklist_id,),
            ).fetchone()[0]
            if not visible_count:
                additions = list(fallback_items or [])
        for action in additions:
            visible_count = int(conn.execute(
                "SELECT COUNT(*) FROM deal_daily_checklist_items WHERE checklist_id = ? AND status != 'retired'",
                (checklist_id,),
            ).fetchone()[0])
            if visible_count >= DAILY_CHECKLIST_LIMIT:
                skipped.append({"action": "add", "text": str(action.get("text") or ""), "reason": "limit"})
                continue
            text = str(action.get("text") or "").strip()
            normalized = _normalize_daily_checklist_text(text)
            duplicate = conn.execute(
                """
                SELECT * FROM deal_daily_checklist_items
                WHERE checklist_id = ? AND normalized_text = ?
                ORDER BY id DESC LIMIT 1
                """,
                (checklist_id, normalized),
            ).fetchone()
            if duplicate is not None and str(duplicate["status"]) == "retired":
                conn.execute(
                    """
                    UPDATE deal_daily_checklist_items
                    SET status = 'open', last_change_type = 'reopened', status_reason = ?,
                        origin_report_id = ?, updated_at = ? WHERE id = ?
                    """,
                    (str(action.get("reason") or "").strip() or None, int(source_report_id), now, int(duplicate["id"])),
                )
                item_id = int(duplicate["id"])
                event_type = "reopened"
            else:
                item_id = _insert_daily_checklist_item(
                    conn,
                    checklist_id=checklist_id,
                    text=text,
                    source="ai",
                    origin_report_id=int(source_report_id),
                    carried_from_item_id=None,
                    last_change_type="new",
                    now=now,
                )
                event_type = "added"
            if item_id is None:
                skipped.append({"action": "add", "text": text, "reason": "duplicate_or_empty"})
                continue
            applied.append({"action": "add", "item_id": str(item_id)})
            _daily_checklist_event(
                conn, checklist_id=checklist_id, item_id=item_id, event_type=event_type,
                actor="ai", source_report_id=int(source_report_id),
                reason=str(action.get("reason") or "").strip() or None, created_at=now,
            )

        if applied:
            conn.execute(
                """
                UPDATE deal_daily_checklists
                SET revision = revision + 1, source_report_id = ?, updated_at = ? WHERE id = ?
                """,
                (int(source_report_id), now, checklist_id),
            )
        else:
            conn.execute(
                "UPDATE deal_daily_checklists SET source_report_id = ? WHERE id = ?",
                (int(source_report_id), checklist_id),
            )
        _daily_checklist_event(
            conn,
            checklist_id=checklist_id,
            event_type="analysis_applied",
            actor="ai",
            event_key=event_key,
            source_report_id=int(source_report_id),
            payload={
                "base_revision": base_revision,
                "stale_revision": stale_revision,
                "applied": applied,
                "skipped": skipped,
            },
            created_at=now,
        )
        current = conn.execute("SELECT * FROM deal_daily_checklists WHERE id = ?", (checklist_id,)).fetchone()
        return _daily_checklist_projection(conn, current)


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


def _recommendation_due_at(deadline: Any) -> str | None:
    value = str(deadline or "").strip()
    if not value:
        return None
    if len(value) == 10:
        try:
            parsed_date = date.fromisoformat(value)
        except ValueError:
            return None
        return datetime.combine(parsed_date, time(18, 0), tzinfo=MSK_TZ).isoformat(timespec="seconds")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        try:
            parsed_date = date.fromisoformat(value)
        except ValueError:
            return None
        return datetime.combine(parsed_date, time(18, 0), tzinfo=MSK_TZ).isoformat(timespec="seconds")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=MSK_TZ)
    return parsed.astimezone(MSK_TZ).isoformat(timespec="seconds")


def materialize_deal_recommendation_from_report(
    db_path: str | Path,
    deal_id: str,
    source_report_id: int,
    report_json: dict[str, Any],
) -> dict[str, Any] | None:
    """Create one immutable Neuro ROP task for an in-scope deal report."""
    init_db(db_path)
    if not isinstance(report_json, dict):
        return None
    rop = report_json.get("rop_manager_message_block")
    manager_action = report_json.get("manager_action_block")
    if not isinstance(rop, dict) or not isinstance(manager_action, dict):
        return None
    task_text = str(rop.get("message_to_manager") or "").strip()
    expected_result = str(rop.get("success_condition") or "").strip()
    touch_type = str(manager_action.get("recommended_channel") or "").strip()
    due_at = _recommendation_due_at(rop.get("deadline"))
    if not task_text or not expected_result or not touch_type or due_at is None:
        return None
    with connect(db_path) as conn:
        deal = conn.execute(
            "SELECT * FROM deal_control_deals WHERE deal_id = ? AND is_active = 1",
            (str(deal_id),),
        ).fetchone()
        scope = conn.execute("SELECT * FROM deal_control_scope LIMIT 1").fetchone()
        if deal is not None and scope is not None:
            initial_ids = loads_json(scope["initial_deal_ids_json"], [])
            manager_ids = loads_json(scope["manager_ids_json"], [])
            pipeline_ids = _scope_pipeline_ids(scope)
            in_scope = (
                str(deal_id) in {str(item) for item in initial_ids}
                or (
                    str(deal["pipeline_id"] or "") in set(pipeline_ids)
                    and str(deal["manager_id"] or "") in {str(item) for item in manager_ids}
                )
            )
            if not in_scope:
                deal = None
        report = conn.execute(
            """
            SELECT id, analysis_run_id FROM ui_reports
            WHERE id = ? AND entity_type = 'deal' AND entity_id = ? AND report_json IS NOT NULL
            """,
            (int(source_report_id), str(deal_id)),
        ).fetchone()
        if deal is None or report is None:
            return None
        existing = conn.execute(
            """
            SELECT * FROM deal_control_tasks
            WHERE source_kind = 'neuro_rop' AND source_report_id = ?
            """,
            (int(source_report_id),),
        ).fetchone()
        if existing is None:
            now = utcish_now()
            try:
                cursor = conn.execute(
                    """
                    INSERT INTO deal_control_tasks (
                        deal_id, source_kind, source_report_id, task_text, touch_type,
                        expected_result, due_at, created_at, updated_at
                    ) VALUES (?, 'neuro_rop', ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(deal_id), int(source_report_id), task_text, touch_type,
                        expected_result, due_at, now, now,
                    ),
                )
                task_id = int(cursor.lastrowid)
                baseline = {
                    key: deal[key]
                    for key in (
                        "deal_id", "stage_id", "stage_name", "amount", "currency_id",
                        "manager_id", "modified_at_crm", "last_crm_sync_at",
                    )
                }
                conn.execute(
                    """
                    INSERT INTO deal_control_task_baselines (
                        task_id, deal_snapshot_json, source_report_id, created_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (task_id, dumps_json(baseline), int(source_report_id), now),
                )
                conn.execute(
                    """
                    INSERT INTO deal_control_task_events (
                        task_id, event_type, event_key, payload_json, created_at
                    ) VALUES (?, 'task_created', 'task_created', ?, ?)
                    """,
                    (task_id, dumps_json({"due_at": due_at, "expected_result": expected_result}), now),
                )
                existing = conn.execute(
                    "SELECT * FROM deal_control_tasks WHERE id = ?", (task_id,)
                ).fetchone()
            except sqlite3.IntegrityError:
                existing = conn.execute(
                    """
                    SELECT * FROM deal_control_tasks
                    WHERE source_kind = 'neuro_rop' AND source_report_id = ?
                    """,
                    (int(source_report_id),),
                ).fetchone()
        if existing is None:
            return None
        task_id = int(existing["id"])
        _insert_manager_trajectory_event(
            conn,
            entity_type="deal",
            entity_id=str(deal_id),
            manager_id=str(deal["manager_id"] or "") or None,
            event_type="recommendation_generated",
            recommendation_kind="deal_task",
            recommendation_id=task_id,
            analysis_run_id=(
                int(report["analysis_run_id"])
                if report["analysis_run_id"] is not None
                else None
            ),
            report_id=int(source_report_id),
            source="neuro_rop",
            source_event_key=f"generated:deal_task:{task_id}",
            occurred_at=str(existing["created_at"] or utcish_now()),
        )
    return get_deal_control_task(db_path, task_id=task_id)


def _fallback_next_recommendation(fallback: dict[str, Any] | None) -> tuple[str | None, str | None, str | None]:
    if not isinstance(fallback, dict):
        return None, None, None
    rop = fallback.get("rop_manager_message_block") if isinstance(fallback.get("rop_manager_message_block"), dict) else fallback
    manager_action = fallback.get("manager_action_block")
    text = _compact_recommendation_text(
        fallback.get("next_action_text") or rop.get("message_to_manager") or rop.get("success_condition")
    )
    next_at = _recommendation_due_at(fallback.get("next_action_at") or rop.get("deadline"))
    reason = _compact_recommendation_text(
        fallback.get("next_action_reason")
        or rop.get("expected_crm_update")
        or rop.get("success_condition")
        or (manager_action.get("channel_reason") if isinstance(manager_action, dict) else None)
    )
    return text, next_at, reason


def apply_deal_recommendation_feedback(
    db_path: str | Path,
    deal_id: str,
    feedback: dict[str, Any] | None,
    new_report_id: int,
    fallback_next_recommendation: dict[str, Any] | None = None,
    *,
    fallback_recommendation: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Apply one validated model feedback block to the prior Neuro ROP task.

    The source report is checked against the deal before any append-only rows are
    written. The keyed system event makes retries safe and keeps the new report
    materialization separate from the prior task outcome.
    """
    if fallback_next_recommendation is None:
        fallback_next_recommendation = fallback_recommendation
    if not isinstance(feedback, dict) or not bool(feedback.get("applicable")):
        return None
    source_report_id = feedback.get("source_report_id")
    if isinstance(source_report_id, bool) or not isinstance(source_report_id, int) or source_report_id <= 0:
        return None
    status = str(feedback.get("status") or "unconfirmed")
    if status not in {"not_done", "attempted", "contacted", "achieved", "unconfirmed"}:
        return None
    event_key = f"system_recommendation_feedback:{int(new_report_id)}"
    evidence = feedback.get("evidence") if isinstance(feedback.get("evidence"), list) else []
    what_manager_did = _compact_recommendation_text(feedback.get("what_manager_did"))
    next_text = _compact_recommendation_text(feedback.get("next_action_text"))
    next_at = _recommendation_due_at(feedback.get("next_action_at"))
    next_reason = _compact_recommendation_text(feedback.get("next_action_reason"))
    fallback_text, fallback_at, fallback_reason = _fallback_next_recommendation(fallback_next_recommendation)

    # Higher statuses need auditable facts and a usable next step. A current
    # recommendation may provide the latter, but never supplies missing evidence.
    effective_status = status
    if status == "not_done" and (feedback.get("contact_confirmed") or feedback.get("target_result_achieved")):
        effective_status = "unconfirmed"
    elif status == "attempted" and (feedback.get("contact_confirmed") or feedback.get("target_result_achieved")):
        effective_status = "unconfirmed"
    elif status == "contacted" and (
        not feedback.get("contact_confirmed") or feedback.get("target_result_achieved")
    ):
        effective_status = "unconfirmed"
    elif status == "achieved" and (
        not feedback.get("contact_confirmed") or not feedback.get("target_result_achieved")
    ):
        effective_status = "unconfirmed"
    if status in {"attempted", "contacted", "achieved"}:
        has_contact_evidence = bool(evidence) and bool(what_manager_did)
        if status in {"contacted", "achieved"}:
            has_contact_evidence = has_contact_evidence and bool(feedback.get("contact_confirmed"))
        has_result_evidence = status != "achieved" or (
            bool(feedback.get("target_result_achieved")) and has_contact_evidence
        )
        if not has_contact_evidence or not has_result_evidence:
            effective_status = "unconfirmed"
        else:
            next_text = next_text or fallback_text
            next_at = next_at or fallback_at
            next_reason = next_reason or fallback_reason
            if not (next_text and next_at and next_reason):
                effective_status = "unconfirmed"

    if effective_status in {"not_done", "unconfirmed"}:
        contact_status = "unknown"
        result_status = "needs_rop_review"
        result_note = what_manager_did or (
            "Предыдущая рекомендация не подтверждена текущими evidence."
        )
        next_text = next_at = None
    elif effective_status == "attempted":
        contact_status = "attempt_no_contact"
        result_status = "pending"
        result_note = what_manager_did
    elif effective_status == "contacted":
        contact_status = "confirmed_contact"
        result_status = "pending"
        result_note = what_manager_did
    else:
        contact_status = "confirmed_contact"
        result_status = "achieved"
        result_note = what_manager_did

    init_db(db_path)
    with connect(db_path) as conn:
        # Serialize the read/check/insert sequence so concurrent retries cannot
        # both observe a missing idempotency event.
        conn.execute("BEGIN IMMEDIATE")
        task = conn.execute(
            """
            SELECT t.* FROM deal_control_tasks t
            JOIN ui_reports r ON r.id = t.source_report_id
            WHERE t.deal_id = ? AND t.source_kind = 'neuro_rop'
              AND t.source_report_id = ? AND r.entity_type = 'deal' AND r.entity_id = ?
              AND t.source_report_id <> ?
              AND EXISTS (
                  SELECT 1 FROM ui_reports nr
                  WHERE nr.id = ? AND nr.entity_type = 'deal' AND nr.entity_id = ?
              )
            ORDER BY t.id DESC LIMIT 1
            """,
            (str(deal_id), int(source_report_id), str(deal_id), int(new_report_id), int(new_report_id), str(deal_id)),
        ).fetchone()
        if task is None:
            return None
        existing_event = conn.execute(
            "SELECT payload_json FROM deal_control_task_events WHERE task_id = ? AND event_key = ?",
            (int(task["id"]), event_key),
        ).fetchone()
        if existing_event is not None:
            payload = loads_json(existing_event["payload_json"], {})
            outcome_id = payload.get("outcome_id") if isinstance(payload, dict) else None
            row = conn.execute(
                "SELECT * FROM deal_control_task_outcomes WHERE id = ?",
                (int(outcome_id),),
            ).fetchone() if outcome_id else None
            return dict(row) if row is not None else None

        now = utcish_now()
        cursor = conn.execute(
            """
            INSERT INTO deal_control_task_outcomes (
                task_id, contact_status, result_status, result_note, next_step_text,
                next_step_at, evidence_kind, evidence_id, source_role, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'system', ?)
            """,
            (
                int(task["id"]), contact_status, result_status, result_note,
                next_text, next_at, "neuro_rop_recommendation_feedback", str(new_report_id), now,
            ),
        )
        outcome_id = int(cursor.lastrowid)
        local_status = "completed" if effective_status == "achieved" else "active"
        legacy_status = "next_step" if effective_status == "achieved" else "no_result"
        conn.execute(
            """
            UPDATE deal_control_tasks
            SET local_status = ?, business_result_status = ?, business_result_note = ?,
                completed_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (local_status, legacy_status, result_note, now if local_status == "completed" else task["completed_at"], now, int(task["id"])),
        )
        conn.execute(
            """
            INSERT INTO deal_control_task_events (task_id, event_type, event_key, payload_json, created_at)
            VALUES (?, 'system_outcome', ?, ?, ?)
            """,
            (
                int(task["id"]), event_key,
                dumps_json({
                    "outcome_id": outcome_id,
                    "feedback_status": effective_status,
                    "source_report_id": int(source_report_id),
                    "new_report_id": int(new_report_id),
                    "evidence_count": min(len(evidence), 7),
                }),
                now,
            ),
        )
        row = conn.execute("SELECT * FROM deal_control_task_outcomes WHERE id = ?", (outcome_id,)).fetchone()
    return dict(row) if row is not None else None


def _compact_recommendation_text(value: Any, *, limit: int = 800) -> str | None:
    text = str(value or "").strip()
    return text[:limit] if text else None


def _recommendation_state_without_system_outcomes(
    outcome: dict[str, Any] | None,
    crm_facts: list[dict[str, Any]],
    system_feedback_status: str | None = None,
) -> str:
    if system_feedback_status in {"not_done", "attempted", "contacted", "achieved", "unconfirmed"}:
        return str(system_feedback_status)
    allowed_evidence = {"transcript", "manager_confirmation", "rop_confirmation"}
    explicit_contact = (
        isinstance(outcome, dict)
        and outcome.get("contact_status") == "confirmed_contact"
        and outcome.get("evidence_kind") in allowed_evidence
        and bool(str(outcome.get("result_note") or "").strip())
    )
    if isinstance(outcome, dict) and outcome.get("result_status") == "achieved" and explicit_contact:
        return "achieved"
    if explicit_contact:
        return "contacted"
    if isinstance(outcome, dict) and outcome.get("contact_status") == "attempt_no_contact":
        return "attempted"
    if isinstance(outcome, dict):
        return "unconfirmed"
    if any(str(fact.get("contact_class") or "") == "attempt" for fact in crm_facts):
        return "attempted"
    return "unconfirmed" if crm_facts else "not_done"


def get_latest_neuro_rop_recommendation_projection(
    db_path: str | Path,
    deal_id: str,
) -> dict[str, Any] | None:
    """Return the bounded, JSON-safe prior recommendation context for a deal.

    A recommendation is visible to a new analysis only after both its task and
    baseline exist. Raw report, event, CRM payload, manager, and quick-help data
    are intentionally excluded from this projection.
    """
    if not Path(db_path).is_file():
        return None
    try:
        return _read_latest_neuro_rop_recommendation_projection(db_path, deal_id)
    except (OSError, sqlite3.Error):
        return None


def _read_latest_neuro_rop_recommendation_projection(
    db_path: str | Path,
    deal_id: str,
) -> dict[str, Any] | None:
    with connect(db_path) as conn:
        task_row = conn.execute(
            """
            SELECT t.*
            FROM deal_control_tasks t
            JOIN deal_control_task_baselines b ON b.task_id = t.id
            WHERE t.deal_id = ? AND t.source_kind = 'neuro_rop'
            ORDER BY t.source_report_id DESC, t.id DESC
            LIMIT 1
            """,
            (str(deal_id),),
        ).fetchone()
        if task_row is None:
            return None
        task = dict(task_row)
        outcome_row = conn.execute(
            """
            SELECT id, contact_status, result_status, result_note, next_step_text,
                   next_step_at, evidence_kind, evidence_id, source_role, created_at
            FROM deal_control_task_outcomes
            WHERE task_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (int(task["id"]),),
        ).fetchone()
        outcome = dict(outcome_row) if outcome_row is not None else None
        if outcome is not None:
            for key in ("result_note", "next_step_text"):
                outcome[key] = _compact_recommendation_text(outcome.get(key))
        fact_rows = conn.execute(
            """
            SELECT id, fact_key, activity_id, fact_kind, summary, occurred_at,
                   contact_class, review_status, created_at
            FROM deal_control_task_crm_facts
            WHERE task_id = ?
            ORDER BY id DESC
            LIMIT 5
            """,
            (int(task["id"]),),
        ).fetchall()
        crm_facts = [dict(row) for row in fact_rows]
        for fact in crm_facts:
            fact["summary"] = _compact_recommendation_text(fact.get("summary"))
        system_event = conn.execute(
            """
            SELECT payload_json FROM deal_control_task_events
            WHERE task_id = ? AND event_type = 'system_outcome'
            ORDER BY id DESC LIMIT 1
            """,
            (int(task["id"]),),
        ).fetchone()
        system_payload = loads_json(system_event["payload_json"], {}) if system_event else {}
        return {
            "task_id": int(task["id"]),
            "source_report_id": int(task["source_report_id"]),
            "task_text": _compact_recommendation_text(task.get("task_text")),
            "expected_result": _compact_recommendation_text(task.get("expected_result")),
            "due_at": task.get("due_at"),
            "recommendation_state": _recommendation_state_without_system_outcomes(
                outcome,
                crm_facts,
                str(system_payload.get("feedback_status")) if isinstance(system_payload, dict) else None,
            ),
            "latest_outcome": outcome,
            "crm_facts": crm_facts,
        }


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
    manual_evidence_kinds = {"transcript", "manager_confirmation", "rop_confirmation"}
    if contact_status not in allowed_contacts:
        raise ValueError("Неизвестный статус контакта")
    if result_status not in allowed_results:
        raise ValueError("Неизвестный результат задачи")
    if source_role not in {"manager", "rop", "system"}:
        raise ValueError("Неизвестный источник результата")
    if source_role == "system":
        raise ValueError("System outcomes must use the specialized recommendation feedback apply")
    note = str(result_note or "").strip() or None
    next_text = str(next_step_text or "").strip() or None
    next_at = str(next_step_at or "").strip() or None
    if contact_status == "attempt_no_contact" and result_status == "achieved":
        raise ValueError("attempt_no_contact cannot be paired with achieved")
    if contact_status == "not_attempted":
        raise ValueError("Сначала выполните действие или зафиксируйте попытку контакта")
    if contact_status == "unknown" and not note:
        raise ValueError("Опишите, почему контакт с клиентом не подтверждён")
    if contact_status == "attempt_no_contact" and (not note or not next_text or not next_at):
        raise ValueError("Для попытки без ответа укажите, что произошло, следующий шаг и его срок")
    if contact_status == "confirmed_contact" and not note:
        raise ValueError("Кратко зафиксируйте подтверждённый ответ клиента")
    if (contact_status == "confirmed_contact" or result_status == "achieved") and (
        evidence_kind not in manual_evidence_kinds or not note
    ):
        raise ValueError(
            "Confirmed contact or achieved requires transcript, manager_confirmation, or rop_confirmation evidence and a meaningful note"
        )
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
        if task is not None and (
            (result_status == "achieved" and str(task["local_status"] or "") == "completed")
            or (
                (contact_status == "confirmed_contact" or result_status == "achieved")
                and str(task["crm_execution_status"] or "") == "crm_closed"
            )
        ):
            raise ValueError("Closed tasks cannot confirm contact or achieved")
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
        outcome_id = int(cursor.lastrowid)
        context = conn.execute(
            """
            SELECT deal.manager_id, report.analysis_run_id
            FROM deal_control_tasks AS task
            JOIN deal_control_deals AS deal ON deal.deal_id = task.deal_id
            LEFT JOIN ui_reports AS report ON report.id = task.source_report_id
            WHERE task.id = ?
            """,
            (int(task_id),),
        ).fetchone()
        _insert_manager_trajectory_event(
            conn,
            entity_type="deal",
            entity_id=str(task["deal_id"]),
            manager_id=str(context["manager_id"] or "") or None if context is not None else None,
            event_type="outcome_recorded",
            recommendation_kind="deal_task" if str(task["source_kind"] or "") == "neuro_rop" else None,
            recommendation_id=int(task_id) if str(task["source_kind"] or "") == "neuro_rop" else None,
            analysis_run_id=(
                int(context["analysis_run_id"])
                if context is not None and context["analysis_run_id"] is not None
                else None
            ),
            report_id=int(task["source_report_id"]) if task["source_report_id"] is not None else None,
            source="local_outcome",
            source_event_key=f"outcome:{outcome_id}",
            occurred_at=now,
            payload={
                "contact_status": contact_status,
                "result_status": result_status,
                "source_role": source_role,
            },
        )
        row = conn.execute(
            "SELECT * FROM deal_control_task_outcomes WHERE id = ?",
            (outcome_id,),
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
            WHERE task_id = ? AND fact_key = ?
            """,
            (int(task_id), fact_key),
        ).fetchone()
        if existing is None and activity_id is not None:
            existing = conn.execute(
                """
                SELECT id FROM deal_control_task_crm_facts
                WHERE task_id = ? AND activity_id = ? AND fact_kind = ?
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


def _task_recommendation_projection(
    conn: sqlite3.Connection,
    *,
    task: dict[str, Any],
) -> dict[str, Any]:
    outcome = task.get("latest_outcome")
    state: str
    system_outcome = conn.execute(
        """
        SELECT payload_json FROM deal_control_task_events
        WHERE task_id = ? AND event_type = 'system_outcome'
        ORDER BY id DESC LIMIT 1
        """,
        (int(task["id"]),),
    ).fetchone()
    system_payload = loads_json(system_outcome["payload_json"], {}) if system_outcome else {}
    system_status = system_payload.get("feedback_status") if isinstance(system_payload, dict) else None
    if system_status in {"not_done", "attempted", "contacted", "achieved", "unconfirmed"}:
        state = str(system_status)
    elif isinstance(outcome, dict) and outcome.get("result_status") == "achieved":
        state = "achieved"
    elif isinstance(outcome, dict) and outcome.get("contact_status") == "confirmed_contact":
        state = "contacted"
    elif isinstance(outcome, dict) and outcome.get("contact_status") == "attempt_no_contact":
        state = "attempted"
    elif isinstance(outcome, dict):
        state = "unconfirmed"
    else:
        attempt_fact = any(
            str(fact.get("contact_class") or "") == "attempt"
            and str(fact.get("fact_kind") or "") not in {"stage_changed", "deal_won"}
            for fact in task.get("crm_facts") or []
        )
        state = "attempted" if attempt_fact else "unconfirmed" if task.get("crm_facts") else "not_done"
    priority = {"achieved": 0, "contacted": 1, "attempted": 2, "unconfirmed": 3, "not_done": 3}[state]
    task["recommendation_state"] = state
    task["attention_priority"] = priority
    task["needs_follow_up"] = bool(task.get("local_status") == "active" and state != "achieved")
    return task


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
            _task_recommendation_projection(conn, task=task)
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
        _task_recommendation_projection(conn, task=task)
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


def _require_aware_timestamp(value: str, *, field: str) -> str:
    normalized = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{field} должен быть ISO timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} должен содержать timezone")
    return parsed.astimezone(MSK_TZ).isoformat(timespec="seconds")


def _insert_manager_trajectory_event(
    conn: sqlite3.Connection,
    *,
    entity_type: str,
    entity_id: str,
    manager_id: str | None,
    event_type: str,
    source: str,
    source_event_key: str,
    occurred_at: str,
    auth_user_id: int | None = None,
    recommendation_kind: str | None = None,
    recommendation_id: str | int | None = None,
    analysis_run_id: int | None = None,
    report_id: int | None = None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    normalized_entity_type = str(entity_type or "").strip()
    normalized_event_type = str(event_type or "").strip()
    normalized_source = str(source or "").strip()
    normalized_key = str(source_event_key or "").strip()
    normalized_kind = str(recommendation_kind or "").strip() or None
    if normalized_entity_type not in MANAGER_TRAJECTORY_ENTITY_TYPES:
        raise ValueError("entity_type должен быть deal или lead")
    if normalized_event_type not in MANAGER_TRAJECTORY_EVENT_TYPES:
        raise ValueError("Неизвестный тип trajectory event")
    if not str(entity_id or "").strip() or not normalized_source or not normalized_key:
        raise ValueError("entity_id, source и source_event_key обязательны")
    if normalized_kind is not None and normalized_kind not in MANAGER_RECOMMENDATION_KINDS:
        raise ValueError("Неизвестный recommendation_kind")
    if payload is not None and not isinstance(payload, dict):
        raise ValueError("Payload trajectory event должен быть JSON-объектом")
    normalized_occurred_at = _require_aware_timestamp(occurred_at, field="occurred_at")
    recorded_at = utcish_now()
    conn.execute(
        """
        INSERT INTO manager_trajectory_events (
            entity_type, entity_id, manager_id, auth_user_id, event_type,
            recommendation_kind, recommendation_id, analysis_run_id, report_id,
            source, source_event_key, occurred_at, recorded_at, payload_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source, source_event_key) DO NOTHING
        """,
        (
            normalized_entity_type,
            str(entity_id).strip(),
            (str(manager_id).strip() or None) if manager_id is not None else None,
            int(auth_user_id) if auth_user_id is not None else None,
            normalized_event_type,
            normalized_kind,
            str(recommendation_id) if recommendation_id is not None else None,
            int(analysis_run_id) if analysis_run_id is not None else None,
            int(report_id) if report_id is not None else None,
            normalized_source,
            normalized_key,
            normalized_occurred_at,
            recorded_at,
            dumps_json(payload) if payload is not None else None,
        ),
    )
    row = conn.execute(
        "SELECT * FROM manager_trajectory_events WHERE source = ? AND source_event_key = ?",
        (normalized_source, normalized_key),
    ).fetchone()
    assert row is not None
    value = dict(row)
    value["payload"] = loads_json(value.pop("payload_json", None), None)
    return value


def record_manager_trajectory_event(
    db_path: str | Path,
    **event: Any,
) -> dict[str, Any]:
    """Append one factual, idempotent event to the manager trajectory."""
    init_db(db_path)
    with connect(db_path) as conn:
        return _insert_manager_trajectory_event(conn, **event)


def record_recommendation_lifecycle_event(
    db_path: str | Path,
    *,
    deal_id: str,
    recommendation_kind: str,
    recommendation_id: str | int,
    event_type: str,
    auth_user_id: int,
) -> dict[str, Any]:
    """Validate recommendation ownership and derive all actor fields server-side."""
    if event_type not in {"recommendation_shown", "recommendation_viewed"}:
        raise ValueError("Допустимы только recommendation_shown/recommendation_viewed")
    if recommendation_kind not in MANAGER_RECOMMENDATION_KINDS:
        raise ValueError("Неизвестный recommendation_kind")
    init_db(db_path)
    with connect(db_path) as conn:
        auth_user = _get_auth_user_row(conn, user_id=int(auth_user_id))
        if (
            auth_user is None
            or not bool(auth_user["is_active"])
            or str(auth_user["role"] or "") != "manager"
            or not str(auth_user["manager_id"] or "").strip()
        ):
            raise PermissionError("Событие использования может записать только активный менеджер")
        if recommendation_kind == "deal_task":
            row = conn.execute(
                """
                SELECT task.id, task.deal_id, task.source_report_id, deal.manager_id,
                       report.analysis_run_id
                FROM deal_control_tasks AS task
                JOIN deal_control_deals AS deal ON deal.deal_id = task.deal_id
                LEFT JOIN ui_reports AS report ON report.id = task.source_report_id
                WHERE task.id = ? AND task.deal_id = ? AND task.source_kind = 'neuro_rop'
                """,
                (int(recommendation_id), str(deal_id)),
            ).fetchone()
        else:
            row = conn.execute(
                """
                SELECT quick.id, quick.deal_id, quick.source_report_id, quick.manager_id,
                       report.analysis_run_id
                FROM deal_manager_quick_help AS quick
                LEFT JOIN ui_reports AS report ON report.id = quick.source_report_id
                WHERE quick.id = ? AND quick.deal_id = ?
                """,
                (int(recommendation_id), str(deal_id)),
            ).fetchone()
        if row is None:
            raise ValueError("Рекомендация не найдена для этой сделки")
        manager_id = str(row["manager_id"] or "").strip()
        actor_manager_id = str(auth_user["manager_id"] or "").strip()
        if not manager_id or actor_manager_id != manager_id:
            raise PermissionError("Менеджер может записать событие только для своей сделки")
        event_name = event_type.removeprefix("recommendation_")
        return _insert_manager_trajectory_event(
            conn,
            entity_type="deal",
            entity_id=str(deal_id),
            manager_id=manager_id,
            auth_user_id=int(auth_user_id),
            event_type=event_type,
            recommendation_kind=recommendation_kind,
            recommendation_id=str(row["id"]),
            analysis_run_id=int(row["analysis_run_id"]) if row["analysis_run_id"] is not None else None,
            report_id=int(row["source_report_id"]) if row["source_report_id"] is not None else None,
            source="manager_ui",
            source_event_key=(
                f"{event_name}:{recommendation_kind}:{row['id']}:user:{int(auth_user_id)}"
            ),
            occurred_at=utcish_now(),
            payload={
                "actor_verified": True,
                "actor_role": "manager",
                "actor_manager_id": actor_manager_id,
            },
        )


def list_manager_trajectory_events(
    db_path: str | Path,
    *,
    from_at: str,
    to_at: str,
    manager_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    init_db(db_path)
    start = _require_aware_timestamp(from_at, field="from_at")
    end = _require_aware_timestamp(to_at, field="to_at")
    managers = [str(item).strip() for item in (manager_ids or []) if str(item).strip()]
    clauses = ["occurred_at >= ?", "occurred_at < ?"]
    params: list[Any] = [start, end]
    if managers:
        clauses.append("manager_id IN (" + ",".join("?" for _ in managers) + ")")
        params.extend(managers)
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM manager_trajectory_events WHERE "
            + " AND ".join(clauses)
            + " ORDER BY occurred_at, id",
            params,
        ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        value = dict(row)
        value["payload"] = loads_json(value.pop("payload_json", None), None)
        result.append(value)
    return result


def get_manager_trajectory_collection_state(
    db_path: str | Path,
    *,
    collection_key: str = "bitrix_manager_wide",
) -> dict[str, Any] | None:
    init_db(db_path)
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM manager_trajectory_collection_state WHERE collection_key = ?",
            (str(collection_key),),
        ).fetchone()
    return dict(row) if row is not None else None


def save_manager_trajectory_collection_state(
    db_path: str | Path,
    *,
    status: str,
    successful_through: str | None = None,
    error: str | None = None,
    collection_key: str = "bitrix_manager_wide",
) -> dict[str, Any]:
    now = utcish_now()
    normalized_success = (
        _require_aware_timestamp(successful_through, field="successful_through")
        if successful_through is not None
        else None
    )
    init_db(db_path)
    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO manager_trajectory_collection_state (
                collection_key, last_success_at, last_attempt_at, last_status, last_error, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(collection_key) DO UPDATE SET
                last_success_at = COALESCE(excluded.last_success_at, manager_trajectory_collection_state.last_success_at),
                last_attempt_at = excluded.last_attempt_at,
                last_status = excluded.last_status,
                last_error = excluded.last_error,
                updated_at = excluded.updated_at
            """,
            (str(collection_key), normalized_success, now, str(status), error, now),
        )
    result = get_manager_trajectory_collection_state(db_path, collection_key=collection_key)
    assert result is not None
    return result


def observe_manager_trajectory_entity(
    db_path: str | Path,
    *,
    entity_type: str,
    entity_id: str,
    manager_id: str | None,
    stage_id: str | None,
    modified_at: str | None,
) -> dict[str, Any] | None:
    """Update the compact CRM snapshot and append a stage-change fact when needed."""
    if entity_type not in MANAGER_TRAJECTORY_ENTITY_TYPES:
        raise ValueError("entity_type должен быть deal или lead")
    init_db(db_path)
    with connect(db_path) as conn:
        previous = conn.execute(
            "SELECT * FROM manager_trajectory_entity_state WHERE entity_type = ? AND entity_id = ?",
            (entity_type, str(entity_id)),
        ).fetchone()
        event = None
        normalized_modified = (
            _require_aware_timestamp(modified_at, field="modified_at")
            if modified_at
            else utcish_now()
        )
        if previous is not None and str(previous["stage_id"] or "") != str(stage_id or ""):
            event = _insert_manager_trajectory_event(
                conn,
                entity_type=entity_type,
                entity_id=str(entity_id),
                manager_id=str(manager_id or "") or None,
                event_type=f"{entity_type}_stage_changed",
                source="bitrix",
                source_event_key=(
                    f"{entity_type}_stage:{entity_id}:{previous['stage_id'] or ''}:{stage_id or ''}:{normalized_modified}"
                ),
                occurred_at=normalized_modified,
                payload={
                    "from_stage_id": previous["stage_id"],
                    "to_stage_id": stage_id,
                    "timestamp_kind": "entity_date_modify",
                },
            )
        conn.execute(
            """
            INSERT INTO manager_trajectory_entity_state (
                entity_type, entity_id, manager_id, stage_id, modified_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(entity_type, entity_id) DO UPDATE SET
                manager_id = excluded.manager_id,
                stage_id = excluded.stage_id,
                modified_at = excluded.modified_at,
                updated_at = excluded.updated_at
            """,
            (entity_type, str(entity_id), manager_id, stage_id, normalized_modified, utcish_now()),
        )
        return event
