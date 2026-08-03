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
import math
import subprocess

from fastapi import UploadFile

from openai_api.audio.audio_handler import transcribe_voice


MAX_AUDIO_BYTES = 25 * 1024 * 1024
MAX_AUDIO_DURATION_SECONDS = 5 * 60
READ_CHUNK_BYTES = 1024 * 1024

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
    except (TypeError, ValueError) as error:
        raise AudioTranscriptionRequestError("Не удалось определить длительность аудио") from error
    if not math.isfinite(duration) or duration <= 0:
        raise AudioTranscriptionRequestError("Не удалось определить длительность аудио")
    if duration > MAX_AUDIO_DURATION_SECONDS:
        raise AudioTranscriptionRequestError("Одна запись не может быть длиннее 5 минут")
    return duration


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
        await asyncio.to_thread(_probe_duration_seconds, data)
        text = await transcribe_voice(
            data,
            file_name=f"manager_voice{_CONTENT_TYPE_SUFFIXES[normalized_type]}",
            language=normalized_language,
        )
        return {"text": str(text)}
    finally:
        await audio.close()
