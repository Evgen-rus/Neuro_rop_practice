"""
Helpers for short / no-answer call detection.

Calls shorter than SHORT_CALL_MAX_SECONDS are treated as non-meaningful contact
(недозвон / автоответчик) and should not be transcribed by default.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from openai_api.audio.transcribe_core import get_audio_duration_seconds


SHORT_CALL_MAX_SECONDS = 20.0


def classify_call_duration(duration_seconds: float | None) -> dict[str, Any]:
    if duration_seconds is None:
        return {
            "duration_seconds": None,
            "is_short_no_answer": False,
            "call_quality": "unknown",
            "skip_transcribe": False,
        }
    is_short = float(duration_seconds) < SHORT_CALL_MAX_SECONDS
    return {
        "duration_seconds": round(float(duration_seconds), 1),
        "is_short_no_answer": is_short,
        "call_quality": "short_no_answer" if is_short else "ok",
        "skip_transcribe": is_short,
    }


def enrich_download_with_duration(download: dict[str, Any]) -> dict[str, Any]:
    """Add duration metadata to a successful download row when local file exists."""
    row = dict(download)
    if not row.get("ok") or not row.get("local_path"):
        return row
    if row.get("duration_seconds") is not None and "is_short_no_answer" in row:
        return row

    path = Path(str(row["local_path"]))
    if not path.exists():
        return row

    duration = get_audio_duration_seconds(path)
    meta = classify_call_duration(duration)
    row.update(meta)
    if meta["is_short_no_answer"]:
        row["skip_transcribe_reason"] = (
            f"Звонок короче {int(SHORT_CALL_MAX_SECONDS)} сек — считаем недозвоном/автоответчиком"
        )
    return row


def enrich_manifest_calls(manifest: dict[str, Any]) -> dict[str, Any]:
    """Enrich all successful downloads in a call audio manifest with duration."""
    result = dict(manifest)
    calls = []
    for call in result.get("calls") or []:
        if not isinstance(call, dict):
            continue
        row = dict(call)
        downloads = [
            enrich_download_with_duration(item) if isinstance(item, dict) else item
            for item in (row.get("downloads") or [])
        ]
        row["downloads"] = downloads
        short_hits = [
            item
            for item in downloads
            if isinstance(item, dict) and item.get("is_short_no_answer")
        ]
        if short_hits and all(
            isinstance(item, dict) and item.get("is_short_no_answer")
            for item in downloads
            if isinstance(item, dict) and item.get("ok")
        ):
            row["call_quality"] = "short_no_answer"
            row["skip_transcribe"] = True
        elif any(isinstance(item, dict) and item.get("ok") for item in downloads):
            row["call_quality"] = "ok"
            row["skip_transcribe"] = False
        calls.append(row)
    result["calls"] = calls
    result["short_call_max_seconds"] = SHORT_CALL_MAX_SECONDS
    return result


def is_short_no_answer_audio(path: str | Path, *, max_seconds: float = SHORT_CALL_MAX_SECONDS) -> bool:
    duration = get_audio_duration_seconds(path)
    if duration is None:
        return False
    return float(duration) < float(max_seconds)
