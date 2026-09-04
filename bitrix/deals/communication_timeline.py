"""Render a human-readable communication timeline from one saved deal workspace."""

from __future__ import annotations

import argparse
import json
import sys
import textwrap
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bitrix.customer_history import (
    _parse_mirrored_message,
    build_normalized_communications,
    clean_text,
    parse_bitrix_datetime,
    raw_activities_by_id,
    result_items,
)
from bitrix.deals.history_compaction import compact_history_section_coverage
from bitrix.deals.email_text import strip_quoted_history
from bitrix.deals.communication_history import include_saved_source_lead_communications
from bitrix.workspace import DEFAULT_DEAL_WORKSPACE_ROOT, deal_workspace_dir
from openai_api.audio.transcript_context import transcript_items
from setup import MSK_TZ


SECTION_LABELS = {
    "client_touchpoints": "Клиентские касания",
    "internal_context": "Внутренний контекст",
}
CHANNEL_LABELS = {
    "call": "📞 ЗВОНОК",
    "email": "📧 EMAIL",
    "message": "💬 СООБЩЕНИЕ",
    "whatsapp": "💬 WHATSAPP",
    "telegram": "💬 TELEGRAM",
    "max": "💬 MAX",
    "internal_comment": "📝 ВНУТРЕННИЙ КОММЕНТАРИЙ",
    "internal_chat": "🗨️ ВНУТРЕННИЙ ЧАТ",
}
DIRECTION_LABELS = {
    "incoming": "входящее",
    "outgoing": "исходящее",
    "internal": "внутреннее",
    "unknown": "неясно",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a local Markdown communication timeline for one saved deal workspace."
    )
    parser.add_argument("--deal-id", required=True, help="Deal ID")
    parser.add_argument(
        "--deal-root",
        default=str(DEFAULT_DEAL_WORKSPACE_ROOT),
        help="Root folder with prepared deal workspaces",
    )
    return parser.parse_args()


def load_bundle(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(
            f"Локальный customer history bundle не найден: {path}. Bitrix не вызывался."
        )
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"Некорректный JSON customer history bundle: {path}") from error
    if not isinstance(value, dict) or value.get("bundle_type") != "customer_history_bundle":
        raise ValueError(f"Файл не является customer_history_bundle: {path}")
    deal_id = path.name.removeprefix("deal_").removesuffix("_customer_history_bundle.json")
    return include_saved_source_lead_communications(
        value, path.with_name(f"deal_{deal_id}_context.json"), deal_id=deal_id,
    )


def _source_key(item: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(item.get("entity_type") or ""),
        str(item.get("entity_id") or ""),
        str(item.get("id") or ""),
    )


def _coverage_indexes(bundle: dict[str, Any]) -> dict[str, dict[tuple[str, str, str], dict[str, Any]]]:
    indexes: dict[str, dict[tuple[str, str, str], dict[str, Any]]] = {}
    for section in SECTION_LABELS:
        entries = compact_history_section_coverage(section, bundle.get(section) or [])
        indexes[section] = {_source_key(entry["item"]): entry for entry in entries}
    return indexes


def _event_coverage(
    event: dict[str, Any],
    indexes: dict[str, dict[tuple[str, str, str], dict[str, Any]]],
) -> list[dict[str, Any]]:
    entity_type = str(event.get("entity_type") or "")
    entity_id = str(event.get("entity_id") or "")
    source_ids = [str(value) for value in event.get("source_ids") or [] if value]
    entries: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for section, index in indexes.items():
        for source_id in source_ids:
            candidates = [
                (entity_type, entity_id, source_id),
                *[key for key in index if key[2] == source_id],
            ]
            for key in candidates:
                entry = index.get(key)
                marker = (section, *key)
                if entry is not None and marker not in seen:
                    entries.append({"section": section, **entry})
                    seen.add(marker)
                    break
    return entries


def _raw_activities(bundle: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return raw_activities_by_id(bundle)


def _raw_comments(bundle: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for responses in (bundle.get("timeline_comments_by_entity") or {}).values():
        for response in responses if isinstance(responses, list) else []:
            for item in result_items(response):
                item_id = str(item.get("ID") or "")
                if item_id:
                    rows[item_id] = item
    return rows


def _source_id(event: dict[str, Any]) -> str:
    return next((str(value) for value in event.get("source_ids") or [] if value), "")


def _human_text(
    event: dict[str, Any],
    activities: dict[str, dict[str, Any]],
    comments: dict[str, dict[str, Any]],
) -> tuple[str, bool, bool, bool]:
    source_id = _source_id(event)
    source_type = str(event.get("source_type") or "")
    attachment = False
    if source_type == "crm_activity":
        raw = activities.get(source_id) or {}
        attachment = bool(raw.get("FILES"))
        text = clean_text(raw.get("DESCRIPTION"))
        available_text = text or clean_text(event.get("content"))
        quoted_history_hidden = False
        if str(event.get("channel") or "") == "email" and available_text:
            current_message = strip_quoted_history(available_text)
            quoted_history_hidden = current_message != available_text
            available_text = current_message
        return available_text, bool(text), attachment, quoted_history_hidden
    if source_type == "crm_timeline_comment":
        raw = comments.get(source_id) or {}
        raw_text = clean_text(raw.get("COMMENT") or raw.get("TEXT") or raw.get("DESCRIPTION"))
        attachment = bool(raw.get("FILES"))
        if str(event.get("channel") or "") in {"whatsapp", "telegram", "max"}:
            _speaker, content = _parse_mirrored_message(raw_text)
            text = clean_text(content)
        else:
            text = raw_text
        return (text or clean_text(event.get("content")), bool(text), attachment, False)
    text = clean_text(event.get("content"))
    attachment = any(line.lower().startswith("файл:") for line in text.splitlines())
    return text, bool(text), attachment, False


def _format_msk(value: Any) -> str:
    parsed = parse_bitrix_datetime(value)
    if parsed is None:
        return str(value or "дата неизвестна")
    return parsed.astimezone(MSK_TZ).strftime("%d.%m.%Y %H:%M МСК")


def _quote(text: str) -> list[str]:
    if not text:
        return ["> В локальном снимке полный текст недоступен."]
    quoted: list[str] = []
    for line in text.splitlines():
        wrapped = textwrap.wrap(
            line,
            width=120,
            replace_whitespace=False,
            drop_whitespace=True,
            break_long_words=False,
            break_on_hyphens=False,
        )
        quoted.extend(f"> {part}" for part in wrapped)
        if not wrapped:
            quoted.append(">")
    return quoted


def _compact_status(entry: dict[str, Any], *, mirror: bool) -> str:
    section = str(entry["section"])
    label = SECTION_LABELS[section]
    prefix = (
        "для глаз — переписка; как внутренний контекст, не как слова клиента"
        if mirror and section == "internal_context"
        else f"раздел «{label}»"
    )
    if not entry["included"]:
        return f"не ушло в сжатую историю ({prefix}; событие старше лимита раздела)"
    if entry["truncated"]:
        field = "тема" if entry["selected_field"] == "subject" else "текст"
        return (
            f"урезано: в промпт {entry['prompt_chars']} из {entry['full_chars']} символов "
            f"({prefix}; модель увидела {field}: «{entry['prompt_text']}»)"
        )
    field = "тема" if entry["selected_field"] == "subject" else "текст"
    return (
        f"ушло целиком: {entry['prompt_chars']} символов ({prefix}; "
        f"модель увидела {field}: «{entry['prompt_text'] or '-'}»)"
    )


def _missing_transcript_reason(event: dict[str, Any], raw: dict[str, Any]) -> str:
    if not raw.get("FILES") and not event.get("has_recording"):
        return "в локальном снимке CRM нет ссылки или метаданных файла записи"
    duration = event.get("duration_seconds")
    if duration is not None:
        return f"transcript JSON не найден; длительность CRM-активности: {duration} с"
    return "transcript JSON не найден в локальном workspace"


def _record_from_event(
    event: dict[str, Any],
    *,
    coverage: list[dict[str, Any]],
    activities: dict[str, dict[str, Any]],
    comments: dict[str, dict[str, Any]],
    transcripts: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    source_id = _source_id(event)
    text, full_available, attachment, quoted_history_hidden = _human_text(event, activities, comments)
    transcript = transcripts.get(source_id) if str(event.get("channel") or "") == "call" else None
    return {
        "event": event,
        "when": event.get("occurred_at"),
        "source_ids": list(event.get("source_ids") or []),
        "text": text,
        "full_available": full_available,
        "attachment": attachment,
        "quoted_history_hidden": quoted_history_hidden,
        "coverage": coverage,
        "transcript": transcript,
        "raw_activity": activities.get(source_id) or {},
    }


def _merge_visual_duplicates(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    indexes: dict[tuple[str, str], int] = {}
    for record in records:
        event = record["event"]
        channel = str(event.get("channel") or "")
        text_key = " ".join(str(record.get("text") or "").lower().split())
        key = (str(record.get("when") or ""), text_key)
        existing_index = indexes.get(key) if text_key and channel != "call" else None
        if existing_index is None:
            indexes[key] = len(merged)
            merged.append(record)
            continue
        existing = merged[existing_index]
        existing_channel = str(existing["event"].get("channel") or "")
        mirror_channels = {"whatsapp", "telegram", "max"}
        if not ({channel, existing_channel} & mirror_channels):
            indexes[(key[0], f"{key[1]}:{len(merged)}")] = len(merged)
            merged.append(record)
            continue
        existing["coverage"].extend(record["coverage"])
        existing["source_ids"] = sorted(
            {str(value) for value in [*existing["source_ids"], *record["source_ids"]] if value}
        )
        if channel in mirror_channels:
            existing["event"] = event
        existing["attachment"] = existing["attachment"] or record["attachment"]
    return merged


def build_timeline_records(bundle: dict[str, Any], transcripts_dir: Path) -> list[dict[str, Any]]:
    indexes = _coverage_indexes(bundle)
    activities = _raw_activities(bundle)
    comments = _raw_comments(bundle)
    transcript_rows = transcript_items(transcripts_dir, "deal", str((bundle.get("root_entity") or {}).get("id") or ""))
    transcripts = {str(item.get("activity_id") or ""): item for item in transcript_rows}
    records = [
        _record_from_event(
            event,
            coverage=_event_coverage(event, indexes),
            activities=activities,
            comments=comments,
            transcripts=transcripts,
        )
        for event in build_normalized_communications(bundle)
    ]

    known_call_ids = {
        _source_id(record["event"])
        for record in records
        if str(record["event"].get("channel") or "") == "call"
    }
    for activity_id, transcript in transcripts.items():
        if activity_id in known_call_ids:
            continue
        records.append(
            {
                "event": {
                    "channel": "call",
                    "direction": "unknown",
                    "source_type": "transcript_bundle",
                    "subject": transcript.get("subject"),
                },
                "when": transcript.get("call_start"),
                "source_ids": [activity_id],
                "text": "",
                "full_available": False,
                "attachment": False,
                "quoted_history_hidden": False,
                "coverage": [],
                "transcript": transcript,
                "raw_activity": {},
            }
        )
    return sorted(
        _merge_visual_duplicates(records),
        key=lambda item: (
            parse_bitrix_datetime(item.get("when")) or datetime.min.replace(tzinfo=MSK_TZ),
            ",".join(item.get("source_ids") or []),
        ),
    )


def _coverage_class(record: dict[str, Any]) -> str:
    included = [entry for entry in record["coverage"] if entry["included"]]
    if not included:
        return "not_included"
    if any(entry["truncated"] for entry in included):
        return "truncated"
    return "full"


def _summary(records: list[dict[str, Any]], bundle: dict[str, Any]) -> list[str]:
    classes = [_coverage_class(record) for record in records]
    omitted_chars = sum(
        entry["omitted_chars"]
        for record in records
        for entry in record["coverage"]
        if entry["included"] and entry["truncated"]
    )
    mirrors = [
        record for record in records
        if str(record["event"].get("channel") or "") in {"whatsapp", "telegram", "max"}
        and any(entry["section"] == "internal_context" and entry["included"] for entry in record["coverage"])
    ]
    calls = [record for record in records if str(record["event"].get("channel") or "") == "call"]
    channel_counts = {
        channel: sum(str(record["event"].get("channel") or "") == channel for record in records)
        for channel in ("email", "call", "whatsapp", "telegram", "max", "internal_comment", "internal_chat")
    }
    task_count = len(bundle.get("tasks_and_control") or [])
    system_count = len(bundle.get("system_events") or [])
    return [
        f"- Всего событий ленты: {len(records)}.",
        "- По каналам: "
        f"email — {channel_counts['email']}; звонки — {channel_counts['call']}; "
        f"WhatsApp — {channel_counts['whatsapp']}; Telegram — {channel_counts['telegram']}; "
        f"Max — {channel_counts['max']}; внутренние комментарии/чаты — "
        f"{channel_counts['internal_comment'] + channel_counts['internal_chat']}.",
        f"- В compact ушло целиком: {classes.count('full')}; урезано: {classes.count('truncated')} (не показано {omitted_chars} символов); не попало: {classes.count('not_included')}.",
        f"- Зеркал мессенджеров, ушедших как internal_context, не как слова клиента: {len(mirrors)}.",
        f"- Звонков с полной расшифровкой во втором блоке: {sum(bool(item['transcript']) for item in calls)}; без расшифровки: {sum(not bool(item['transcript']) for item in calls)}.",
        f"- В ленту не включены задачи/контроль ({task_count}) и системные CRM-события ({system_count}); их собственное compact-покрытие здесь не считается коммуникацией.",
    ]


def render_timeline(bundle: dict[str, Any], transcripts_dir: Path) -> str:
    root = bundle.get("root_entity") or {}
    period = bundle.get("history_period") or {}
    records = build_timeline_records(bundle, transcripts_dir)
    lines = [
        f"# Таймлайн общения по сделке {root.get('id') or '-'}",
        "",
        f"- Сделка: {clean_text(root.get('title')) or '-'} (ID: {root.get('id') or '-'})",
        f"- Период bundle: {period.get('date_from') or '-'} — {period.get('date_to') or '-'} ({period.get('days') or '-'} дней)",
        f"- Сформировано: {datetime.now(MSK_TZ).strftime('%d.%m.%Y %H:%M МСК')}",
        "- Покрытие ниже описывает политику ПОЛНОГО анализа. Для MINI/skip/incremental старая история может повторно не передаваться модели.",
        "",
        "## Сводка покрытия",
        "",
        *_summary(records, bundle),
        "",
        "## Легенда",
        "",
        "- «Ушло целиком» / «урезано» / «не ушло» относится к compact-истории полного анализа.",
        "- Зеркало мессенджера показано как переписка для человека, но модель получает его в разделе внутреннего контекста, не как доказанные слова клиента.",
        "- У звонка два статуса: короткая CRM-строка в compact и полная расшифровка отдельным блоком.",
        "",
        "## Хронология",
        "",
    ]
    if not records:
        lines.append("Коммуникации в локальном bundle не найдены.")
        return "\n".join(lines) + "\n"

    for index, record in enumerate(records, start=1):
        event = record["event"]
        channel = str(event.get("channel") or "unknown")
        direction = str(event.get("direction") or "unknown")
        source_ids = ", ".join(record["source_ids"]) or "-"
        lines.extend(
            [
                f"### {index}. {_format_msk(record['when'])} — {CHANNEL_LABELS.get(channel, channel)}",
                "",
                f"- Направление: {DIRECTION_LABELS.get(direction, direction)}",
                f"- ID источника: {source_ids}",
            ]
        )
        if event.get("participant_name"):
            lines.append(f"- Автор: {clean_text(event.get('participant_name'))}")
        if channel in {"whatsapp", "telegram", "max"} and direction == "unknown":
            lines.append("- Автор не совпал с контактом, направление не подтверждено.")
        if event.get("subject"):
            lines.append(f"- Тема / CRM label: {clean_text(event.get('subject'))}")
        if channel != "call":
            lines.extend(["", *_quote(record["text"])])
            if not record["full_available"] and record["text"]:
                lines.extend(["", "_Полный текст отсутствует в raw; показан доступный текст из bundle._"])
            if record["quoted_history_hidden"]:
                lines.extend(["", "_Процитированная предыдущая email-цепочка не показана._"])
            if record["attachment"]:
                lines.extend(["", "_Есть файл; текст вложения не читался._"])

        transcript = record["transcript"]
        if channel == "call":
            lines.extend(["", "**Расшифровка звонка:**", ""])
            if transcript:
                lines.extend(_quote(str(transcript.get("text") or "")))
            else:
                reason = _missing_transcript_reason(event, record["raw_activity"])
                lines.append(f"> Расшифровки нет: {reason}.")

        lines.extend(["", "**В полный анализ:**", ""])
        mirror = channel in {"whatsapp", "telegram", "max"}
        if record["coverage"]:
            for entry in record["coverage"]:
                lines.append(f"- {_compact_status(entry, mirror=mirror)}.")
        else:
            lines.append("- CRM-строка не ушла в сжатую историю.")
        if channel == "call":
            if transcript:
                transcript_text = str(transcript.get("text") or "")
                lines.append(
                    f"- Транскрипт ушёл вторым блоком полностью: {len(transcript_text)} символов."
                )
            else:
                lines.append("- Транскрипт вторым блоком не ушёл: локального transcript JSON нет.")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def build_deal_communication_timeline(deal_id: str, *, deal_root: Path = DEFAULT_DEAL_WORKSPACE_ROOT) -> Path:
    normalized_id = str(deal_id or "").strip()
    if not normalized_id.isdigit() or int(normalized_id) < 1:
        raise ValueError("deal_id должен быть положительным числом")
    deal_dir = deal_workspace_dir(normalized_id, workspace_root=deal_root)
    bundle_path = deal_dir / "raw" / f"deal_{normalized_id}_customer_history_bundle.json"
    bundle = load_bundle(bundle_path)
    root_id = str((bundle.get("root_entity") or {}).get("id") or "")
    if root_id != normalized_id:
        raise ValueError(f"Bundle относится к другой сделке: ожидалась {normalized_id}, найдена {root_id or '-'}")
    content = render_timeline(bundle, deal_dir / "transcripts")
    output_path = deal_dir / "history" / f"deal_{normalized_id}_communication_timeline.md"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(content, encoding="utf-8")
    return output_path


def main() -> None:
    args = parse_args()
    output_path = build_deal_communication_timeline(args.deal_id, deal_root=Path(args.deal_root))
    print(f"Communication timeline saved: {output_path}")


if __name__ == "__main__":
    main()
