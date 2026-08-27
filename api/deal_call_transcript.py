"""Read a saved deal call transcript for an explicitly requested CRM event."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from api.deal_manager_quick_help import _load_local_communications
from api.deal_communication_content import _stored_daily_event
from bitrix.workspace import DEFAULT_LEAD_WORKSPACE_ROOT, deal_workspace_dir, entity_workspace_dir
from storage.rop_db import DEFAULT_DB_PATH


MAX_TRANSCRIPT_CHARS = 1_000_000
_SAFE_ACTIVITY_ID = re.compile(r"^[A-Za-z0-9_-]+$")


class DealCallTranscriptNotFound(LookupError):
    """The event or its saved transcript is unavailable."""


def _activity_id(event: dict[str, Any]) -> str:
    source_ids = event.get("source_ids") if isinstance(event.get("source_ids"), list) else []
    if source_ids:
        value = str(source_ids[0] or "").strip()
    else:
        event_id = str(event.get("event_id") or "")
        value = event_id.split(":", 1)[1].strip() if event_id.startswith("crm_activity:") else ""
    return value if _SAFE_ACTIVITY_ID.fullmatch(value) else ""


def _read_transcript(path: Path) -> str:
    if path.suffix.lower() == ".json":
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            return ""
        if not isinstance(payload, dict):
            return ""
        return str(payload.get("text") or payload.get("transcript") or "").strip()
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _transcript_paths(entity_id: str, activity_id: str, *, entity_type: str = "deal") -> list[Path]:
    directory = (
        deal_workspace_dir(str(entity_id))
        if entity_type == "deal"
        else entity_workspace_dir(
            str(entity_id), entity_type="lead", workspace_root=DEFAULT_LEAD_WORKSPACE_ROOT,
        )
    ) / "transcripts"
    if not directory.is_dir():
        return []
    prefix = f"call_{activity_id}"
    try:
        matches = [
            path
            for path in directory.iterdir()
            if path.is_file()
            and path.suffix.lower() in {".json", ".txt", ".md"}
            and (path.stem == prefix or path.stem.startswith(f"{prefix}_"))
        ]
    except OSError:
        return []
    priority = {".json": 0, ".txt": 1, ".md": 2}
    return sorted(matches, key=lambda path: (priority.get(path.suffix.lower(), 9), path.name))


def find_call_transcript(entity_type: str, entity_id: str, activity_id: str) -> dict[str, Any] | None:
    """Find a saved transcript by an already verified CRM activity reference."""

    normalized_type = str(entity_type or "").lower()
    normalized_activity_id = str(activity_id or "").strip()
    if normalized_type not in {"deal", "lead"} or not _SAFE_ACTIVITY_ID.fullmatch(normalized_activity_id):
        return None
    for path in _transcript_paths(
        str(entity_id), normalized_activity_id, entity_type=normalized_type,
    ):
        text = _read_transcript(path)
        if not text:
            continue
        truncated = len(text) > MAX_TRANSCRIPT_CHARS
        return {
            "text": text[:MAX_TRANSCRIPT_CHARS],
            "truncated": truncated,
        }
    return None


def get_deal_call_transcript(
    deal_id: str, event_id: str, *, db_path: str | Path = DEFAULT_DB_PATH,
) -> dict[str, Any]:
    """Return a transcript only when the call event belongs to this deal."""

    wanted_event_id = str(event_id or "").strip()
    event = next(
        (
            item
            for item in _load_local_communications(str(deal_id))
            if str(item.get("event_id") or "") == wanted_event_id
        ),
        None,
    )
    if event is None:
        event = _stored_daily_event(db_path, str(deal_id), wanted_event_id)
    if not isinstance(event, dict) or str(event.get("channel") or "") != "call":
        raise DealCallTranscriptNotFound("Звонок не найден в истории этой сделки")

    activity_id = _activity_id(event)
    if not activity_id:
        raise DealCallTranscriptNotFound("Для звонка отсутствует идентификатор CRM-активности")

    transcript = find_call_transcript("deal", str(deal_id), activity_id)
    lead_id = event.get("source_lead_id") or (event.get("entity_id") if event.get("entity_type") == "lead" else None)
    if not transcript and lead_id:
        transcript = find_call_transcript("lead", str(lead_id), activity_id)
    if transcript:
        return {
            "deal_id": str(deal_id),
            "event_id": wanted_event_id,
            **transcript,
        }
    raise DealCallTranscriptNotFound("Расшифровка этого звонка пока недоступна")
