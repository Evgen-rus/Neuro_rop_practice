"""
Readable audit logging for data sent to OpenAI models.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any


DEFAULT_PREVIEW_LINES = int(os.getenv("OPENAI_LOG_PREVIEW_LINES", "25") or "25")
DEFAULT_PREVIEW_CHARS = int(os.getenv("OPENAI_LOG_PREVIEW_CHARS", "4000") or "4000")


def sha256_short(data: bytes, length: int = 16) -> str:
    return hashlib.sha256(data).hexdigest()[:length]


def file_sha256_short(path: Path, length: int = 16) -> str | None:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as file:
            for chunk in iter(lambda: file.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()[:length]
    except OSError:
        return None


def text_preview(text: str, max_lines: int = DEFAULT_PREVIEW_LINES, max_chars: int = DEFAULT_PREVIEW_CHARS) -> str:
    lines = text.splitlines()
    preview = "\n".join(lines[:max_lines])
    if len(preview) > max_chars:
        preview = preview[:max_chars].rstrip() + "\n...[truncated by chars]"
    if len(lines) > max_lines:
        preview += f"\n...[truncated: {len(lines) - max_lines} more lines]"
    return preview


def read_text_preview(path: Path, max_lines: int = DEFAULT_PREVIEW_LINES, max_chars: int = DEFAULT_PREVIEW_CHARS) -> str:
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return "<not utf-8 text>"
    except OSError as error:
        return f"<cannot read: {error}>"
    return text_preview(text, max_lines=max_lines, max_chars=max_chars)


def log_model_text_payload(
    logger: Any,
    *,
    title: str,
    model: str,
    text: str,
    metadata: dict[str, Any] | None = None,
    max_lines: int = DEFAULT_PREVIEW_LINES,
    max_chars: int = DEFAULT_PREVIEW_CHARS,
) -> None:
    """
    Log a readable preview of text that will be sent to a model.
    """
    encoded = text.encode("utf-8")
    lines = [
        "=== OPENAI REQUEST TEXT PREVIEW BEGIN ===",
        f"title: {title}",
        f"model: {model}",
        f"bytes_utf8: {len(encoded)}",
        f"chars: {len(text)}",
        f"sha256_16: {sha256_short(encoded)}",
    ]
    for key, value in (metadata or {}).items():
        lines.append(f"{key}: {value}")
    lines.extend(
        [
            "--- first lines ---",
            text_preview(text, max_lines=max_lines, max_chars=max_chars),
            "=== OPENAI REQUEST TEXT PREVIEW END ===",
        ]
    )
    logger.info("\n%s", "\n".join(lines))


def log_model_file_payload(
    logger: Any,
    *,
    title: str,
    model: str,
    path: str | Path,
    metadata: dict[str, Any] | None = None,
    preview_text: bool = True,
    max_lines: int = DEFAULT_PREVIEW_LINES,
    max_chars: int = DEFAULT_PREVIEW_CHARS,
) -> None:
    """
    Log a readable preview of a local file that will be used as model input.
    """
    file_path = Path(path)
    exists = file_path.exists()
    stat = file_path.stat() if exists else None
    lines = [
        "=== OPENAI REQUEST FILE PREVIEW BEGIN ===",
        f"title: {title}",
        f"model: {model}",
        f"path: {file_path}",
        f"exists: {exists}",
        f"size_bytes: {stat.st_size if stat else None}",
        f"suffix: {file_path.suffix}",
        f"sha256_16: {file_sha256_short(file_path) if exists else None}",
    ]
    for key, value in (metadata or {}).items():
        lines.append(f"{key}: {value}")

    if preview_text and exists and file_path.suffix.lower() in {".txt", ".md", ".json", ".csv", ".yaml", ".yml"}:
        lines.extend(
            [
                "--- first lines ---",
                read_text_preview(file_path, max_lines=max_lines, max_chars=max_chars),
            ]
        )
    elif exists:
        lines.append("--- first lines ---")
        lines.append("<binary or preview disabled>")

    lines.append("=== OPENAI REQUEST FILE PREVIEW END ===")
    logger.info("\n%s", "\n".join(lines))


def log_model_binary_payload(
    logger: Any,
    *,
    title: str,
    model: str,
    file_name: str,
    data: bytes,
    metadata: dict[str, Any] | None = None,
) -> None:
    """
    Log metadata for binary model input. Binary content itself is never logged.
    """
    lines = [
        "=== OPENAI REQUEST BINARY PREVIEW BEGIN ===",
        f"title: {title}",
        f"model: {model}",
        f"file_name: {file_name}",
        f"size_bytes: {len(data)}",
        f"sha256_16: {sha256_short(data)}",
        "--- content ---",
        "<binary content omitted>",
    ]
    for key, value in (metadata or {}).items():
        lines.insert(-2, f"{key}: {value}")
    lines.append("=== OPENAI REQUEST BINARY PREVIEW END ===")
    logger.info("\n%s", "\n".join(lines))

