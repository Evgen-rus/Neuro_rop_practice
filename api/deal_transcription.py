"""In-memory browser voice transcription for the manager deal screen.

HTTP contract for the frontend:

``POST /api/deal-control/voice/transcribe`` with ``multipart/form-data``:

* ``audio`` — an audio Blob/file; accepted MIME types are validated below;
* ``deal_id`` — the current deal identifier (validated but not sent to the
  transcription model and not persisted);
* ``confirm_paid`` — literal boolean form value ``true``;
* ``language`` — optional ISO language, default ``ru`` (the current UI sends
  ``ru``).

The response is exactly ``{"text": "..."}``. Audio is read into memory,
duration-checked through ffprobe stdin, passed to ``openai_api.audio``, and
never written as an application file. Transcript text is returned only in the
HTTP response; it is not saved or logged.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import UploadFile

from openai_api.audio.audio_handler import transcribe_voice
from openai_api.audio.transcribe_core import get_audio_duration_seconds, transcribe_file_async
from openai_api.spend_diary import record_transcription_spend


MAX_AUDIO_BYTES = 25 * 1024 * 1024
MAX_AUDIO_DURATION_SECONDS = 5 * 60
READ_CHUNK_BYTES = 1024 * 1024
MAX_UPLOADED_AUDIO_BYTES = 90 * 1024 * 1024
MAX_UPLOADED_AUDIO_DURATION_SECONDS = 90 * 60

_CONTENT_TYPE_SUFFIXES = {
    "audio/aac": ".aac",
    "audio/flac": ".flac",
    "audio/m4a": ".m4a",
    "audio/mp4": ".m4a",
    "audio/mpeg": ".mp3",
    "audio/ogg": ".ogg",
    "audio/opus": ".opus",
    "audio/wav": ".wav",
    "audio/webm": ".webm",
    "audio/x-m4a": ".m4a",
    "audio/x-wav": ".wav",
    # Chromium may label an audio-only MediaRecorder Blob as video/webm.
    "video/webm": ".webm",
}


class AudioTranscriptionRequestError(ValueError):
    """The browser request cannot be accepted safely."""


def _base_content_type(value: str | None) -> str:
    return str(value or "").split(";", 1)[0].strip().lower()


def validate_audio_upload(
    *,
    content_type: str | None,
    size_bytes: int,
    confirm_paid: bool,
    language: str | None,
) -> tuple[str, str]:
    if not confirm_paid:
        raise AudioTranscriptionRequestError("Подтвердите платную транскрибацию")
    normalized_type = _base_content_type(content_type)
    suffix = _CONTENT_TYPE_SUFFIXES.get(normalized_type)
    if suffix is None:
        raise AudioTranscriptionRequestError("Неподдерживаемый тип аудио")
    if int(size_bytes) < 1 or int(size_bytes) > MAX_AUDIO_BYTES:
        raise AudioTranscriptionRequestError("Размер аудио должен быть от 1 байта до 25 МБ")
    normalized_language = str(language or "ru").strip().lower() or "ru"
    if normalized_language != "ru":
        raise AudioTranscriptionRequestError("Сейчас поддерживается только язык ru")
    return normalized_type, normalized_language


def _probe_duration_seconds(audio_data: bytes) -> float:
    """Probe bytes through stdin; no temporary audio file is created."""
    command = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        "-i",
        "pipe:0",
    ]
    try:
        result = subprocess.run(
            command,
            input=audio_data,
            capture_output=True,
            check=False,
            timeout=15,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as error:
        raise AudioTranscriptionRequestError("Не удалось проверить длительность аудио") from error
    if result.returncode != 0:
        raise AudioTranscriptionRequestError("Не удалось прочитать аудио")
    try:
        duration = float(result.stdout.decode("utf-8", errors="replace").strip())
    except (TypeError, ValueError):
        duration = _probe_packet_timeline_duration_seconds(audio_data)
    if not math.isfinite(duration) or duration <= 0:
        raise AudioTranscriptionRequestError("Не удалось определить длительность аудио")
    if duration > MAX_AUDIO_DURATION_SECONDS:
        raise AudioTranscriptionRequestError("Одна запись не может быть длиннее 5 минут")
    return duration


def _probe_packet_timeline_duration_seconds(audio_data: bytes) -> float:
    """Derive duration for live WebM recordings that omit format.duration."""
    command = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "a:0",
        "-show_entries",
        "packet=pts_time,duration_time",
        "-of",
        "json",
        "-i",
        "pipe:0",
    ]
    try:
        result = subprocess.run(
            command,
            input=audio_data,
            capture_output=True,
            check=False,
            timeout=15,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as error:
        raise AudioTranscriptionRequestError("Не удалось проверить длительность аудио") from error
    if result.returncode != 0:
        raise AudioTranscriptionRequestError("Не удалось определить длительность аудио")
    try:
        packets = json.loads(result.stdout.decode("utf-8", errors="replace")).get("packets", [])
    except (AttributeError, json.JSONDecodeError) as error:
        raise AudioTranscriptionRequestError("Не удалось определить длительность аудио") from error
    if not isinstance(packets, list):
        raise AudioTranscriptionRequestError("Не удалось определить длительность аудио")

    starts: list[float] = []
    ends: list[float] = []
    for packet in packets:
        try:
            start = float(packet["pts_time"])
            packet_duration = max(0.0, float(packet.get("duration_time", 0.0)))
        except (KeyError, TypeError, ValueError):
            continue
        if math.isfinite(start) and math.isfinite(packet_duration):
            starts.append(start)
            ends.append(start + packet_duration)
    if not starts or not ends:
        raise AudioTranscriptionRequestError("Не удалось определить длительность аудио")
    return max(ends) - min(starts)


async def _read_upload(upload: UploadFile) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await upload.read(READ_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_AUDIO_BYTES:
            raise AudioTranscriptionRequestError("Размер аудио должен быть от 1 байта до 25 МБ")
        chunks.append(chunk)
    if not chunks:
        raise AudioTranscriptionRequestError("Аудио не передано")
    return b"".join(chunks)


async def transcribe_manager_voice(
    *,
    audio: UploadFile,
    deal_id: str,
    confirm_paid: bool,
    language: str = "ru",
) -> dict[str, str]:
    """Validate and transcribe one browser recording without persisting it."""
    if not str(deal_id or "").strip():
        raise AudioTranscriptionRequestError("Не указан deal_id")
    # The frontend contract never uses the client filename as an OpenAI name.
    normalized_type, normalized_language = validate_audio_upload(
        content_type=audio.content_type,
        # Some test clients and ASGI adapters do not expose UploadFile.size;
        # the streamed read below performs the authoritative size check.
        size_bytes=max(1, int(audio.size or 1)),
        confirm_paid=confirm_paid,
        language=language,
    )
    try:
        data = await _read_upload(audio)
        validate_audio_upload(
            content_type=normalized_type,
            size_bytes=len(data),
            confirm_paid=True,
            language=normalized_language,
        )
        # ffprobe is synchronous but receives bytes through stdin only.
        duration_seconds = await asyncio.to_thread(_probe_duration_seconds, data)
        text = await transcribe_voice(
            data,
            file_name=f"manager_voice{_CONTENT_TYPE_SUFFIXES[normalized_type]}",
            language=normalized_language,
        )
        record_transcription_spend(
            duration_seconds=duration_seconds,
            entity_type="deal",
            entity_id=str(deal_id).strip(),
            kind="transcription_voice",
        )
        return {"text": str(text)}
    finally:
        await audio.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _safe_upload_name(value: str | None, suffix: str) -> str:
    name = str(value or "recording").replace("\\", "/").rsplit("/", 1)[-1].strip()
    return (name or f"recording{suffix}")[:160]


@dataclass
class UploadedAudioJob:
    job_id: str
    deal_id: str
    file_name: str
    temp_path: str
    size_bytes: int
    duration_seconds: float
    status: str = "queued"
    stage: str = "queued"
    detail: str = "Аудио загружено"
    current: int = 0
    total: int = 0
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    transcript: str | None = None
    error: str | None = None
    expires_at: float = field(default_factory=lambda: time.time() + 3600)


_UPLOADED_AUDIO_JOBS: dict[str, UploadedAudioJob] = {}
_UPLOADED_AUDIO_LOCK = threading.Lock()
_UPLOADED_AUDIO_DEALS: set[str] = set()


def _public_uploaded_audio_job(job: UploadedAudioJob) -> dict[str, Any]:
    payload = asdict(job)
    payload.pop("temp_path", None)
    payload.pop("expires_at", None)
    transcript = payload.pop("transcript", None)
    if job.status == "done" and transcript:
        payload["attachment"] = {
            "kind": "manual_audio",
            "source_kind": "manual_audio",
            "provisional": True,
            "crm_evidence": False,
            "communication_event": False,
            "file_name": job.file_name,
            "transcript": transcript,
            "attached_at": job.created_at,
            "duration_seconds": job.duration_seconds,
        }
    return payload


def get_uploaded_audio_job(job_id: str) -> dict[str, Any] | None:
    with _UPLOADED_AUDIO_LOCK:
        now = time.time()
        for key in [key for key, item in _UPLOADED_AUDIO_JOBS.items() if item.status in {"done", "error"} and item.expires_at < now]:
            _UPLOADED_AUDIO_JOBS.pop(key, None)
        job = _UPLOADED_AUDIO_JOBS.get(str(job_id))
        return _public_uploaded_audio_job(job) if job else None


def get_uploaded_audio_attachment(job_id: str, *, deal_id: str) -> dict[str, Any]:
    job = get_uploaded_audio_job(job_id)
    if job is None or str(job.get("deal_id")) != str(deal_id):
        raise AudioTranscriptionRequestError("Аудиовложение не найдено")
    if job.get("status") != "done" or not isinstance(job.get("attachment"), dict):
        raise AudioTranscriptionRequestError("Дождитесь завершения транскрибации аудио")
    return dict(job["attachment"])


def _touch_uploaded_audio_job(job: UploadedAudioJob, **values: Any) -> None:
    with _UPLOADED_AUDIO_LOCK:
        for key, value in values.items():
            setattr(job, key, value)
        job.updated_at = _now()


def _run_uploaded_audio_job(job_id: str) -> None:
    with _UPLOADED_AUDIO_LOCK:
        job = _UPLOADED_AUDIO_JOBS[job_id]
        job.status = "running"
        job.stage = "transcribing"
        job.detail = "Транскрибируем аудио…"
        job.updated_at = _now()

    def progress(event: dict[str, Any]) -> None:
        current = int(event.get("current") or 1)
        total = int(event.get("total") or 1)
        detail = f"Транскрибируем аудио… {current}/{total}"
        if event.get("status") == "retry_wait":
            detail = f"Повторяем транскрибацию… {current}/{total}"
        _touch_uploaded_audio_job(job, current=current, total=total, detail=detail)

    try:
        text = asyncio.run(
            transcribe_file_async(
                job.temp_path,
                max_segment_concurrency=1,
                progress_callback=progress,
                entity_type="deal",
                entity_id=job.deal_id,
                log_source_file=False,
            )
        )
        text = re.sub(r"(?m)^\[Сегмент \d+/\d+ [^\]]+\]\r?\n", "", text).strip()
        if not text:
            raise ValueError("Транскрибация не вернула текст")
        _touch_uploaded_audio_job(
            job,
            status="done",
            stage="done",
            detail="Расшифровано",
            transcript=text,
            expires_at=time.time() + 3600,
        )
    except Exception:  # noqa: BLE001 - provider details stay out of job responses
        _touch_uploaded_audio_job(
            job,
            status="error",
            stage="error",
            detail="Не удалось расшифровать аудио",
            error="Транскрибация не выполнена. Проверьте файл и попробуйте ещё раз.",
            expires_at=time.time() + 3600,
        )
    finally:
        try:
            os.remove(job.temp_path)
        except OSError:
            pass
        with _UPLOADED_AUDIO_LOCK:
            job.temp_path = ""
            _UPLOADED_AUDIO_DEALS.discard(job.deal_id)


def _start_uploaded_audio_thread(job_id: str) -> None:
    threading.Thread(target=_run_uploaded_audio_job, args=(job_id,), daemon=True).start()


async def start_uploaded_audio_job(
    *,
    audio: UploadFile,
    deal_id: str,
    confirm_paid: bool,
) -> dict[str, Any]:
    normalized_deal_id = str(deal_id or "").strip()
    if not normalized_deal_id:
        raise AudioTranscriptionRequestError("Не указан deal_id")
    if not confirm_paid:
        raise AudioTranscriptionRequestError("Подтвердите платную транскрибацию")
    normalized_type = _base_content_type(audio.content_type)
    suffix = _CONTENT_TYPE_SUFFIXES.get(normalized_type)
    if suffix is None:
        raise AudioTranscriptionRequestError("Неподдерживаемый тип аудио")
    with _UPLOADED_AUDIO_LOCK:
        if normalized_deal_id in _UPLOADED_AUDIO_DEALS:
            raise AudioTranscriptionRequestError("Для сделки уже расшифровывается аудио")
        _UPLOADED_AUDIO_DEALS.add(normalized_deal_id)

    temp_path = ""
    size_bytes = 0
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as target:
            temp_path = target.name
            while True:
                chunk = await audio.read(READ_CHUNK_BYTES)
                if not chunk:
                    break
                size_bytes += len(chunk)
                if size_bytes > MAX_UPLOADED_AUDIO_BYTES:
                    raise AudioTranscriptionRequestError("Размер аудио не должен превышать 90 МБ")
                target.write(chunk)
        if size_bytes < 1:
            raise AudioTranscriptionRequestError("Аудио не передано")
        duration = await asyncio.to_thread(get_audio_duration_seconds, temp_path)
        if duration is None or not math.isfinite(duration) or duration <= 0:
            raise AudioTranscriptionRequestError("Не удалось определить длительность аудио")
        if duration > MAX_UPLOADED_AUDIO_DURATION_SECONDS:
            raise AudioTranscriptionRequestError("Запись не должна быть длиннее 90 минут")

        job = UploadedAudioJob(
            job_id=uuid.uuid4().hex,
            deal_id=normalized_deal_id,
            file_name=_safe_upload_name(audio.filename, suffix),
            temp_path=temp_path,
            size_bytes=size_bytes,
            duration_seconds=round(float(duration), 1),
        )
        with _UPLOADED_AUDIO_LOCK:
            _UPLOADED_AUDIO_JOBS[job.job_id] = job
        _start_uploaded_audio_thread(job.job_id)
        temp_path = ""
        return _public_uploaded_audio_job(job)
    finally:
        await audio.close()
        if temp_path:
            try:
                Path(temp_path).unlink()
            except OSError:
                pass
            with _UPLOADED_AUDIO_LOCK:
                _UPLOADED_AUDIO_DEALS.discard(normalized_deal_id)
