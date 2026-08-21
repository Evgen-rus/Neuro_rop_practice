"""Read a saved deal call transcript for an explicitly requested CRM event."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from api.deal_manager_quick_help import _load_local_communications
from bitrix.workspace import deal_workspace_dir


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


def _transcript_paths(deal_id: str, activity_id: str) -> list[Path]:
    directory = deal_workspace_dir(str(deal_id)) / "transcripts"
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


def get_deal_call_transcript(deal_id: str, event_id: str) -> dict[str, Any]:
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
    if not isinstance(event, dict) or str(event.get("channel") or "") != "call":
        raise DealCallTranscriptNotFound("Звонок не найден в истории этой сделки")

    activity_id = _activity_id(event)
    if not activity_id:
        raise DealCallTranscriptNotFound("Для звонка отсутствует идентификатор CRM-активности")

    for path in _transcript_paths(str(deal_id), activity_id):
        text = _read_transcript(path)
        if not text:
            continue
        truncated = len(text) > MAX_TRANSCRIPT_CHARS
        return {
            "deal_id": str(deal_id),
            "event_id": wanted_event_id,
            "text": text[:MAX_TRANSCRIPT_CHARS],
            "truncated": truncated,
        }
    raise DealCallTranscriptNotFound("Расшифровка этого звонка пока недоступна")
