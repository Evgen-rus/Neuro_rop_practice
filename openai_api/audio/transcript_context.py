"""
Shared builders for chronological all-calls transcript context files.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from setup import get_logger


logger = get_logger(__file__)
AGGREGATE_STEM = "all_calls_transcript"


def aggregate_output_path(entity_dir: Path, entity_type: str, entity_id: str) -> Path:
    return entity_dir / "transcripts" / f"{entity_type}_{entity_id}_{AGGREGATE_STEM}.md"


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def transcript_json_files(transcripts_dir: Path) -> list[Path]:
    if not transcripts_dir.exists():
        return []
    return sorted(
        path
        for path in transcripts_dir.glob("*.json")
        if path.is_file() and AGGREGATE_STEM not in path.stem
    )


def clean_cell(value: Any) -> str:
    text = str(value or "").replace("\n", " ").strip()
    return text.replace("|", "\\|") or "-"


def clean_text(value: Any) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def source_hint(subject: str, entity_type: str, entity_id: str) -> str:
    lowered = subject.lower()
    for prefix in ("source_lead:", "deal:", "lead:"):
        if lowered.startswith(prefix):
            return subject.split(",", 1)[0].strip()
    return f"{entity_type}:{entity_id}"


def preliminary_contact_signal(subject: str, text: str) -> str:
    combined = f"{subject}\n{text}".lower()
    crm_auto_markers = (
        "автоответ",
        "голосовая почта",
        "voicemail",
    )
    no_contact_markers = (
        "голосовая почта",
        "диалог с клиентом отсутствует",
        "оставьте сообщение",
        "абонент недоступен",
    )
    has_dialog_text = len(clean_text(text)) >= 80
    if any(marker in subject.lower() for marker in crm_auto_markers) and has_dialog_text:
        return "CRM label вероятно ошибочен: в тексте есть диалог"
    if any(marker in combined for marker in no_contact_markers):
        return "вероятно нет содержательного контакта"
    if has_dialog_text:
        return "есть речь/диалог, проверить по тексту"
    return "короткая/пустая запись, проверить вручную"


def transcript_items(transcripts_dir: Path, entity_type: str, entity_id: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for path in transcript_json_files(transcripts_dir):
        try:
            payload = load_json(path)
        except ValueError as error:
            logger.warning("Could not parse transcript JSON: %s (%s)", path, error)
            continue

        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        text = clean_text(payload.get("text"))
        if not text:
            logger.warning("Transcript JSON has empty text: %s", path)

        call_start = str(metadata.get("call_start") or "")
        activity_id = str(metadata.get("activity_id") or path.stem)
        subject = str(metadata.get("subject") or "")
        items.append(
            {
                "json_path": path,
                "md_path": Path(payload.get("transcript_md_path") or ""),
                "activity_id": activity_id,
                "call_start": call_start,
                "subject": subject,
                "source_hint": source_hint(subject, entity_type, entity_id),
                "duration_seconds": metadata.get("audio_duration_seconds"),
                "workspace_audio_path": metadata.get("workspace_audio_path"),
                "text": text,
                "contact_signal": preliminary_contact_signal(subject, text),
            }
        )

    return sorted(
        items,
        key=lambda item: (
            item.get("call_start") or "",
            str(item.get("activity_id") or ""),
            str(item.get("json_path") or ""),
        ),
    )


def format_duration(value: Any) -> str:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return "-"
    minutes = int(seconds // 60)
    rest = int(round(seconds % 60))
    return f"{minutes}:{rest:02d}"


def entity_label(entity_type: str) -> str:
    return "сделке" if entity_type == "deal" else "лиду"


def render_context(entity_type: str, entity_id: str, items: list[dict[str, Any]]) -> str:
    label = entity_label(entity_type)
    lines = [
        f"# Сводная транскрибация звонков по {label} {entity_id}",
        "",
        "- Собрано: из локальных transcript bundle",
        f"- Количество transcript bundle: {len(items)}",
        "",
        "## Правила чтения для анализа",
        "",
        "- Учитывай все звонки ниже в хронологическом порядке.",
        "- Метка CRM/Bitrix в `subject` может быть ошибочной. Если `subject` говорит `автоответчик`, но текст содержит диалог с клиентом, считай звонок содержательным контактом.",
        "- Не исключай короткие звонки автоматически: используй текст, длительность и контекст истории.",
        "- Если запись действительно автоответчик, тишина или служебное сообщение, отметь это как отсутствие содержательного контакта.",
        "- Не выдумывай факты вне текста транскриптов и CRM-истории.",
        "",
        "## Индекс звонков",
        "",
        "| # | Дата звонка | Источник | Activity ID | Длительность | Subject / CRM label | Предварительный сигнал |",
        "|---:|---|---|---:|---:|---|---|",
    ]

    for index, item in enumerate(items, start=1):
        lines.append(
            "| "
            + " | ".join(
                [
                    str(index),
                    clean_cell(item.get("call_start")),
                    clean_cell(item.get("source_hint")),
                    clean_cell(item.get("activity_id")),
                    clean_cell(format_duration(item.get("duration_seconds"))),
                    clean_cell(item.get("subject")),
                    clean_cell(item.get("contact_signal")),
                ]
            )
            + " |"
        )

    if not items:
        lines.append("| - | - | - | - | - | Транскрипты не найдены | - |")

    lines.extend(["", "## Тексты звонков", ""])
    for index, item in enumerate(items, start=1):
        lines.extend(
            [
                f"### Звонок {index}: activity_id={item.get('activity_id') or '-'}",
                "",
                f"- Дата звонка: {item.get('call_start') or '-'}",
                f"- Источник: {item.get('source_hint') or '-'}",
                f"- Subject / CRM label: {item.get('subject') or '-'}",
                f"- Длительность: {format_duration(item.get('duration_seconds'))}",
                f"- Предварительный сигнал: {item.get('contact_signal') or '-'}",
                f"- Transcript JSON: `{item.get('json_path')}`",
                f"- Transcript MD: `{item.get('md_path')}`" if item.get("md_path") else "- Transcript MD: -",
                "",
                "```text",
                item.get("text") or "",
                "```",
                "",
            ]
        )

    return "\n".join(lines)


def build_all_transcript_context(
    entity_dir: Path,
    entity_type: str,
    entity_id: str,
    output_path: Path | None = None,
) -> Path:
    transcripts_dir = entity_dir / "transcripts"
    output_path = output_path or aggregate_output_path(entity_dir, entity_type, entity_id)
    items = transcript_items(transcripts_dir, entity_type, entity_id)
    content = render_context(entity_type, entity_id, items)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists() and output_path.read_text(encoding="utf-8") == content:
        logger.info("All-calls transcript context unchanged: %s", output_path)
        return output_path
    output_path.write_text(content, encoding="utf-8")
    logger.info("Saved all-calls transcript context: %s", output_path)
    return output_path
