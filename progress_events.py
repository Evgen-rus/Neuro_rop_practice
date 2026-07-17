"""Machine-readable progress events emitted by CLI subprocesses."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Callable

from setup import MSK_TZ


PROGRESS_PREFIX = "@@ROP_PROGRESS@@"


def progress_key(entity_type: str, entity_id: str) -> str:
    return f"{entity_type}:{entity_id}"


def emit_progress(
    entity_type: str,
    entity_id: str,
    stage: str,
    *,
    status: str = "running",
    detail: str = "",
    current: int | None = None,
    total: int | None = None,
    attempt: int | None = None,
    max_attempts: int | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    payload = {
        "entity_type": str(entity_type),
        "entity_id": str(entity_id),
        "stage": str(stage),
        "status": str(status),
        "detail": str(detail),
        "current": current,
        "total": total,
        "attempt": attempt,
        "max_attempts": max_attempts,
        "error": error,
        "updated_at": datetime.now(MSK_TZ).isoformat(timespec="seconds"),
    }
    # Машинная строка должна переживать Windows-консоли с любой системной кодировкой.
    # JSON-парсер восстановит исходный Unicode из ASCII-safe escape-последовательностей.
    print(PROGRESS_PREFIX + json.dumps(payload, ensure_ascii=True, separators=(",", ":")), flush=True)
    return payload


def retry_progress_callback(
    entity_type: str,
    entity_id: str,
    stage: str,
    *,
    detail: str,
) -> Callable[[dict[str, Any]], None]:
    def callback(event: dict[str, Any]) -> None:
        status = str(event.get("status") or "")
        error = str(event.get("error") or "") or None
        delay = event.get("delay_seconds")
        event_detail = detail
        if status == "retry_wait":
            event_detail = f"{detail}: повтор через {delay} с"
        elif status == "failed":
            event_detail = f"{detail}: попытки исчерпаны"
        emit_progress(
            entity_type,
            entity_id,
            stage,
            status="error" if status == "failed" else "running",
            detail=event_detail,
            attempt=int(event.get("attempt") or 1),
            max_attempts=int(event.get("max_attempts") or 1),
            error=error if status == "failed" else None,
        )

    return callback
