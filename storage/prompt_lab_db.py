"""Isolated SQLite storage for Prompt Lab.

Lab data never lives in ``rop_assistant.sqlite``. Callers must not write
production manager tables, trajectory events or Bitrix from this module.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from setup import BASE_DIR, MSK_TZ


DEFAULT_PROMPT_LAB_DB_PATH = BASE_DIR / "reports" / "rop_assistant" / "prompt_lab.sqlite"

REVIEW_VERDICTS = ("current_better", "same", "experiment_better", "both_bad")
BRANCHES = ("current", "experiment")


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _loads(value: str | None, default: Any = None) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def _now() -> str:
    return datetime.now(MSK_TZ).isoformat(timespec="seconds")


@contextmanager
def connect(db_path: str | Path = DEFAULT_PROMPT_LAB_DB_PATH) -> Iterator[sqlite3.Connection]:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path: str | Path = DEFAULT_PROMPT_LAB_DB_PATH) -> None:
    with connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS prompt_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                prompt_key TEXT NOT NULL,
                version_number INTEGER NOT NULL,
                prompt_text TEXT NOT NULL,
                prompt_hash TEXT NOT NULL,
                base_production_hash TEXT,
                based_on_id INTEGER,
                candidate INTEGER NOT NULL DEFAULT 0,
                verified INTEGER NOT NULL DEFAULT 0,
                archived INTEGER NOT NULL DEFAULT 0,
                note TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(prompt_key, version_number)
            );

            CREATE TABLE IF NOT EXISTS snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                deal_id TEXT NOT NULL,
                source_report_id INTEGER,
                analysis_run_id INTEGER,
                situation_id INTEGER,
                situation_status TEXT,
                snapshot_hash TEXT NOT NULL,
                provenance_json TEXT NOT NULL,
                context_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                deal_id TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS turns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL,
                snapshot_id INTEGER NOT NULL,
                module_key TEXT NOT NULL,
                question TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                FOREIGN KEY(session_id) REFERENCES sessions(id),
                FOREIGN KEY(snapshot_id) REFERENCES snapshots(id)
            );

            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER,
                turn_id INTEGER,
                snapshot_id INTEGER NOT NULL,
                deal_id TEXT NOT NULL,
                module_key TEXT NOT NULL,
                branch TEXT NOT NULL,
                prompt_version_id INTEGER,
                prompt_hash TEXT NOT NULL,
                prompt_text TEXT NOT NULL,
                effective_prompt TEXT,
                dependency_fingerprints_json TEXT,
                schema_version TEXT,
                material_revision TEXT,
                model TEXT NOT NULL,
                reasoning TEXT NOT NULL,
                max_output_tokens INTEGER,
                question TEXT NOT NULL DEFAULT '',
                selected_strategy TEXT,
                upstream_run_id INTEGER,
                fingerprint TEXT NOT NULL,
                status TEXT NOT NULL,
                error TEXT,
                result_json TEXT,
                usage_json TEXT,
                cost_json TEXT,
                latency_seconds REAL,
                response_status TEXT,
                semantic_attempt_count INTEGER,
                call_type TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(session_id) REFERENCES sessions(id),
                FOREIGN KEY(turn_id) REFERENCES turns(id),
                FOREIGN KEY(snapshot_id) REFERENCES snapshots(id),
                FOREIGN KEY(prompt_version_id) REFERENCES prompt_versions(id)
            );

            CREATE TABLE IF NOT EXISTS reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                current_run_id INTEGER NOT NULL,
                experiment_run_id INTEGER NOT NULL,
                prompt_version_id INTEGER,
                verdict TEXT NOT NULL,
                comment TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(current_run_id) REFERENCES runs(id),
                FOREIGN KEY(experiment_run_id) REFERENCES runs(id)
            );

            CREATE INDEX IF NOT EXISTS idx_prompt_lab_versions_key
                ON prompt_versions(prompt_key, version_number DESC);
            CREATE INDEX IF NOT EXISTS idx_prompt_lab_snapshots_deal
                ON snapshots(deal_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_prompt_lab_runs_deal
                ON runs(deal_id, module_key, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_prompt_lab_runs_fingerprint
                ON runs(fingerprint, created_at DESC);
            """
        )


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return dict(row)


def _version_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    value = _row(row)
    if value is None:
        return None
    value["candidate"] = bool(value.get("candidate"))
    value["verified"] = bool(value.get("verified"))
    value["archived"] = bool(value.get("archived"))
    return value


def _run_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    value = _row(row)
    if value is None:
        return None
    value["dependency_fingerprints"] = _loads(value.pop("dependency_fingerprints_json", None), {})
    value["result"] = _loads(value.pop("result_json", None), None)
    value["usage"] = _loads(value.pop("usage_json", None), None)
    value["cost"] = _loads(value.pop("cost_json", None), None)
    return value


def _snapshot_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    value = _row(row)
    if value is None:
        return None
    value["provenance"] = _loads(value.pop("provenance_json", None), {})
    value["context"] = _loads(value.pop("context_json", None), {})
    return value


def create_session(db_path: str | Path, *, deal_id: str) -> dict[str, Any]:
    init_db(db_path)
    created_at = _now()
    with connect(db_path) as conn:
        cursor = conn.execute(
            "INSERT INTO sessions (deal_id, created_at) VALUES (?, ?)",
            (str(deal_id), created_at),
        )
        session_id = int(cursor.lastrowid)
    return {"id": session_id, "deal_id": str(deal_id), "created_at": created_at}


def save_snapshot(
    db_path: str | Path,
    *,
    deal_id: str,
    source_report_id: int | None,
    analysis_run_id: int | None,
    situation_id: int | None,
    situation_status: str | None,
    snapshot_hash: str,
    provenance: dict[str, Any],
    context: dict[str, Any],
) -> dict[str, Any]:
    init_db(db_path)
    created_at = _now()
    with connect(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO snapshots (
                deal_id, source_report_id, analysis_run_id, situation_id, situation_status,
                snapshot_hash, provenance_json, context_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(deal_id),
                source_report_id,
                analysis_run_id,
                situation_id,
                situation_status,
                snapshot_hash,
                _dumps(provenance),
                _dumps(context),
                created_at,
            ),
        )
        snapshot_id = int(cursor.lastrowid)
    return get_snapshot(db_path, snapshot_id) or {}


def get_snapshot(db_path: str | Path, snapshot_id: int) -> dict[str, Any] | None:
    init_db(db_path)
    with connect(db_path) as conn:
        return _snapshot_row(
            conn.execute("SELECT * FROM snapshots WHERE id = ?", (int(snapshot_id),)).fetchone()
        )


def latest_snapshot(db_path: str | Path, *, deal_id: str) -> dict[str, Any] | None:
    init_db(db_path)
    with connect(db_path) as conn:
        return _snapshot_row(
            conn.execute(
                "SELECT * FROM snapshots WHERE deal_id = ? ORDER BY id DESC LIMIT 1",
                (str(deal_id),),
            ).fetchone()
        )


def create_turn(
    db_path: str | Path,
    *,
    session_id: int,
    snapshot_id: int,
    module_key: str,
    question: str = "",
) -> dict[str, Any]:
    init_db(db_path)
    created_at = _now()
    with connect(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO turns (session_id, snapshot_id, module_key, question, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (int(session_id), int(snapshot_id), str(module_key), str(question or ""), created_at),
        )
        turn_id = int(cursor.lastrowid)
    return {
            "id": turn_id,
            "session_id": int(session_id),
            "snapshot_id": int(snapshot_id),
            "module_key": str(module_key),
            "question": str(question or ""),
            "created_at": created_at,
        }


def next_version_number(db_path: str | Path, prompt_key: str) -> int:
    init_db(db_path)
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT MAX(version_number) AS version_number FROM prompt_versions WHERE prompt_key = ?",
            (str(prompt_key),),
        ).fetchone()
    current = row["version_number"] if row is not None else None
    return int(current or 0) + 1


def save_prompt_version(
    db_path: str | Path,
    *,
    prompt_key: str,
    prompt_text: str,
    prompt_hash: str,
    base_production_hash: str | None = None,
    based_on_id: int | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    init_db(db_path)
    created_at = _now()
    version_number = next_version_number(db_path, prompt_key)
    with connect(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO prompt_versions (
                prompt_key, version_number, prompt_text, prompt_hash, base_production_hash,
                based_on_id, note, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(prompt_key),
                version_number,
                str(prompt_text),
                str(prompt_hash),
                base_production_hash,
                based_on_id,
                note,
                created_at,
            ),
        )
        version_id = int(cursor.lastrowid)
    return get_prompt_version(db_path, version_id) or {}


def get_prompt_version(db_path: str | Path, version_id: int) -> dict[str, Any] | None:
    init_db(db_path)
    with connect(db_path) as conn:
        return _version_row(
            conn.execute("SELECT * FROM prompt_versions WHERE id = ?", (int(version_id),)).fetchone()
        )


def list_prompt_versions(
    db_path: str | Path,
    *,
    prompt_key: str | None = None,
    include_archived: bool = False,
    candidates_only: bool = False,
) -> list[dict[str, Any]]:
    init_db(db_path)
    clauses: list[str] = []
    params: list[Any] = []
    if prompt_key:
        clauses.append("prompt_key = ?")
        params.append(str(prompt_key))
    if not include_archived:
        clauses.append("archived = 0")
    if candidates_only:
        clauses.append("candidate = 1")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with connect(db_path) as conn:
        rows = conn.execute(
            f"SELECT * FROM prompt_versions {where} ORDER BY prompt_key, version_number DESC",
            params,
        ).fetchall()
    return [_version_row(row) for row in rows if _version_row(row)]


def update_prompt_version_labels(
    db_path: str | Path,
    version_id: int,
    *,
    candidate: bool | None = None,
    verified: bool | None = None,
    note: str | None = None,
    archived: bool | None = None,
) -> dict[str, Any]:
    current = get_prompt_version(db_path, version_id)
    if current is None:
        raise ValueError("Версия prompt не найдена")
    next_candidate = current["candidate"] if candidate is None else bool(candidate)
    next_verified = current["verified"] if verified is None else bool(verified)
    next_archived = current["archived"] if archived is None else bool(archived)
    next_note = current.get("note") if note is None else note
    with connect(db_path) as conn:
        conn.execute(
            """
            UPDATE prompt_versions
            SET candidate = ?, verified = ?, archived = ?, note = ?
            WHERE id = ?
            """,
            (int(next_candidate), int(next_verified), int(next_archived), next_note, int(version_id)),
        )
    return get_prompt_version(db_path, version_id) or current


def version_run_count(db_path: str | Path, version_id: int) -> int:
    init_db(db_path)
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS count FROM runs WHERE prompt_version_id = ?",
            (int(version_id),),
        ).fetchone()
    return int(row["count"] if row else 0)


def archive_prompt_version(db_path: str | Path, version_id: int) -> dict[str, Any]:
    return update_prompt_version_labels(db_path, version_id, archived=True)


def delete_prompt_version(db_path: str | Path, version_id: int) -> None:
    if version_run_count(db_path, version_id) > 0:
        raise ValueError("Нельзя удалить версию, которая уже участвовала в Lab run")
    with connect(db_path) as conn:
        conn.execute("DELETE FROM prompt_versions WHERE id = ?", (int(version_id),))


def save_run(
    db_path: str | Path,
    *,
    session_id: int | None,
    turn_id: int | None,
    snapshot_id: int,
    deal_id: str,
    module_key: str,
    branch: str,
    prompt_version_id: int | None,
    prompt_hash: str,
    prompt_text: str,
    effective_prompt: str | None,
    dependency_fingerprints: dict[str, Any] | None,
    schema_version: str | None,
    material_revision: str | None,
    model: str,
    reasoning: str,
    max_output_tokens: int | None,
    question: str,
    selected_strategy: str | None,
    upstream_run_id: int | None,
    fingerprint: str,
    status: str,
    error: str | None,
    result: dict[str, Any] | None,
    usage: dict[str, Any] | None,
    cost: dict[str, Any] | None,
    latency_seconds: float | None,
    response_status: str | None,
    semantic_attempt_count: int | None,
    call_type: str | None,
) -> dict[str, Any]:
    if branch not in BRANCHES:
        raise ValueError("branch должен быть current или experiment")
    init_db(db_path)
    created_at = _now()
    with connect(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO runs (
                session_id, turn_id, snapshot_id, deal_id, module_key, branch,
                prompt_version_id, prompt_hash, prompt_text, effective_prompt,
                dependency_fingerprints_json, schema_version, material_revision,
                model, reasoning, max_output_tokens, question, selected_strategy,
                upstream_run_id, fingerprint, status, error, result_json, usage_json,
                cost_json, latency_seconds, response_status, semantic_attempt_count,
                call_type, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                turn_id,
                int(snapshot_id),
                str(deal_id),
                str(module_key),
                str(branch),
                prompt_version_id,
                prompt_hash,
                prompt_text,
                effective_prompt,
                _dumps(dependency_fingerprints or {}),
                schema_version,
                material_revision,
                model,
                reasoning,
                max_output_tokens,
                str(question or ""),
                selected_strategy,
                upstream_run_id,
                fingerprint,
                status,
                error,
                _dumps(result) if result is not None else None,
                _dumps(usage) if usage is not None else None,
                _dumps(cost) if cost is not None else None,
                latency_seconds,
                response_status,
                semantic_attempt_count,
                call_type,
                created_at,
            ),
        )
        run_id = int(cursor.lastrowid)
    return get_run(db_path, run_id) or {}


def get_run(db_path: str | Path, run_id: int) -> dict[str, Any] | None:
    init_db(db_path)
    with connect(db_path) as conn:
        return _run_row(conn.execute("SELECT * FROM runs WHERE id = ?", (int(run_id),)).fetchone())


def find_run_by_fingerprint(
    db_path: str | Path,
    fingerprint: str,
    *,
    branch: str | None = None,
) -> dict[str, Any] | None:
    init_db(db_path)
    with connect(db_path) as conn:
        if branch:
            row = conn.execute(
                "SELECT * FROM runs WHERE fingerprint = ? AND branch = ? ORDER BY id DESC LIMIT 1",
                (str(fingerprint), str(branch)),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM runs WHERE fingerprint = ? ORDER BY id DESC LIMIT 1",
                (str(fingerprint),),
            ).fetchone()
        return _run_row(row)


def list_runs(
    db_path: str | Path,
    *,
    deal_id: str | None = None,
    module_key: str | None = None,
    snapshot_id: int | None = None,
    prompt_version_id: int | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    init_db(db_path)
    clauses: list[str] = []
    params: list[Any] = []
    if deal_id:
        clauses.append("deal_id = ?")
        params.append(str(deal_id))
    if module_key:
        clauses.append("module_key = ?")
        params.append(str(module_key))
    if snapshot_id is not None:
        clauses.append("snapshot_id = ?")
        params.append(int(snapshot_id))
    if prompt_version_id is not None:
        clauses.append("prompt_version_id = ?")
        params.append(int(prompt_version_id))
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(int(limit))
    with connect(db_path) as conn:
        rows = conn.execute(
            f"SELECT * FROM runs {where} ORDER BY id DESC LIMIT ?",
            params,
        ).fetchall()
    return [_run_row(row) for row in rows if _run_row(row)]


def save_review(
    db_path: str | Path,
    *,
    current_run_id: int,
    experiment_run_id: int,
    prompt_version_id: int | None,
    verdict: str,
    comment: str | None = None,
) -> dict[str, Any]:
    if verdict not in REVIEW_VERDICTS:
        raise ValueError("Неизвестный вердикт сравнения")
    init_db(db_path)
    created_at = _now()
    with connect(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO reviews (
                current_run_id, experiment_run_id, prompt_version_id, verdict, comment, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (int(current_run_id), int(experiment_run_id), prompt_version_id, verdict, comment, created_at),
        )
        review_id = int(cursor.lastrowid)
    with connect(db_path) as conn:
        row = conn.execute("SELECT * FROM reviews WHERE id = ?", (review_id,)).fetchone()
    return _row(row) or {}


def review_stats(db_path: str | Path, *, prompt_version_id: int) -> dict[str, int]:
    init_db(db_path)
    with connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT verdict, COUNT(*) AS count
            FROM reviews
            WHERE prompt_version_id = ?
            GROUP BY verdict
            """,
            (int(prompt_version_id),),
        ).fetchall()
    counts = {item: 0 for item in REVIEW_VERDICTS}
    for row in rows:
        counts[str(row["verdict"])] = int(row["count"])
    counts["total"] = sum(counts[item] for item in REVIEW_VERDICTS)
    return counts
