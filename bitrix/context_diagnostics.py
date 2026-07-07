"""
Build workspace diagnostics for incomplete CRM context and manual recovery steps.

The module is read-only for Bitrix24. It inspects local raw JSON, local audio,
and transcript bundles, then writes files into workspace diagnostics/.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bitrix.customer_history import activity_type, clean_text, merge_activity_detail, result_items
from bitrix.workspace import (
    DEFAULT_DEAL_WORKSPACE_ROOT,
    DEFAULT_LEAD_WORKSPACE_ROOT,
    entity_workspace_dir,
)
from openai_api.audio.transcript_context import transcript_items
from openai_api.bitrix_links import bitrix_entity_activity_url, bitrix_entity_url
from setup import BASE_DIR, MSK_TZ, get_logger


logger = get_logger(__file__)

AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".ogg", ".flac", ".webm"}


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def rel(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(BASE_DIR.resolve()))
    except ValueError:
        return str(path)


def quote(value: str) -> str:
    return f'"{value}"' if value else '""'


def source_entity_from_key(entity_key: str, fallback_type: str, fallback_id: str) -> tuple[str, str]:
    if ":" in entity_key:
        entity_type, entity_id = entity_key.split(":", 1)
        entity_type = entity_type.strip().lower()
        entity_id = entity_id.strip()
        if entity_type in {"lead", "deal"} and entity_id:
            return entity_type, entity_id
    return fallback_type, fallback_id


def call_record_from_touchpoint(row: dict[str, Any], root_type: str, root_id: str) -> dict[str, Any]:
    source_type, source_id = source_entity_from_key(str(row.get("entity_key") or ""), root_type, root_id)
    activity_id = str(row.get("id") or "")
    raw = row.get("raw") if isinstance(row.get("raw"), dict) else {}
    return {
        "activity_id": activity_id,
        "source_entity_type": source_type,
        "source_entity_id": source_id,
        "source_entity_key": row.get("entity_key"),
        "when": row.get("when") or raw.get("START_TIME") or raw.get("CREATED"),
        "subject": row.get("subject") or clean_text(raw.get("SUBJECT"), 240),
        "direction": row.get("direction") or raw.get("DIRECTION"),
        "completed": row.get("completed") or raw.get("COMPLETED"),
        "responsible_id": raw.get("RESPONSIBLE_ID"),
    }


def calls_from_customer_history(bundle: dict[str, Any], root_type: str, root_id: str) -> list[dict[str, Any]]:
    calls = []
    for row in bundle.get("client_touchpoints") or []:
        if isinstance(row, dict) and row.get("event_type") == "call":
            call = call_record_from_touchpoint(row, root_type, root_id)
            if call.get("activity_id"):
                calls.append(call)
    return calls


def calls_from_single_context(bundle: dict[str, Any], root_type: str, root_id: str) -> list[dict[str, Any]]:
    calls = []

    def add_from_history(history: dict[str, Any], source_type: str, source_id: str) -> None:
        details = history.get("activity_details") or {}
        for activity in result_items(history.get("activities")):
            item = merge_activity_detail(activity, details)
            if activity_type(item) != "call":
                continue
            activity_id = str(item.get("ID") or "")
            if not activity_id:
                continue
            calls.append(
                {
                    "activity_id": activity_id,
                    "source_entity_type": source_type,
                    "source_entity_id": source_id,
                    "source_entity_key": f"{source_type}:{source_id}",
                    "when": item.get("START_TIME") or item.get("CREATED"),
                    "subject": clean_text(item.get("SUBJECT"), 240),
                    "direction": item.get("DIRECTION"),
                    "completed": item.get("COMPLETED"),
                    "responsible_id": item.get("RESPONSIBLE_ID"),
                }
            )

    add_from_history(bundle, root_type, root_id)
    source_lead = bundle.get("source_lead")
    if isinstance(source_lead, dict) and source_lead.get("lead_id"):
        add_from_history(source_lead, "lead", str(source_lead.get("lead_id")))
    return calls


def dedupe_calls(calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for call in calls:
        activity_id = str(call.get("activity_id") or "")
        if not activity_id or activity_id in seen:
            continue
        seen.add(activity_id)
        deduped.append(call)
    return sorted(deduped, key=lambda item: (str(item.get("when") or ""), str(item.get("activity_id") or "")))


def collect_calls(context: dict[str, Any], root_type: str, root_id: str) -> list[dict[str, Any]]:
    if context.get("bundle_type") == "customer_history_bundle":
        return dedupe_calls(calls_from_customer_history(context, root_type, root_id))
    return dedupe_calls(calls_from_single_context(context, root_type, root_id))


def local_audio_by_activity(entity_dir: Path) -> dict[str, list[str]]:
    audio_dir = entity_dir / "audio"
    rows: dict[str, list[str]] = {}
    if not audio_dir.exists():
        return rows

    for path in audio_dir.iterdir():
        if not path.is_file() or path.suffix.lower() not in AUDIO_EXTENSIONS:
            continue
        stem = path.stem.lower()
        for token in stem.replace("-", "_").split("_"):
            if token.isdigit():
                rows.setdefault(token, []).append(rel(path))
    return rows


def transcript_activity_ids(entity_dir: Path, entity_type: str, entity_id: str) -> set[str]:
    ids: set[str] = set()
    for item in transcript_items(entity_dir / "transcripts", entity_type, entity_id):
        activity_id = str(item.get("activity_id") or "")
        if activity_id:
            ids.add(activity_id)
    return ids


def downloaded_audio_from_manifest(entity_dir: Path, entity_type: str, entity_id: str) -> dict[str, list[str]]:
    manifest_path = entity_dir / "audio" / f"{entity_type}_{entity_id}_call_audio_manifest.json"
    if not manifest_path.exists():
        return {}
    try:
        manifest = load_json(manifest_path)
    except ValueError:
        return {}

    rows: dict[str, list[str]] = {}
    for call in manifest.get("calls") or []:
        if not isinstance(call, dict):
            continue
        activity_id = str(call.get("activity_id") or "")
        if not activity_id:
            continue
        paths: list[str] = []
        for item in call.get("downloads") or []:
            if isinstance(item, dict) and item.get("ok") and item.get("local_path"):
                paths.append(str(item["local_path"]))
        if paths:
            rows.setdefault(activity_id, []).extend(paths)
    return rows


def unavailable_source_gaps(context: dict[str, Any]) -> list[dict[str, Any]]:
    diagnostics = context.get("diagnostics") if isinstance(context.get("diagnostics"), dict) else {}
    rows = []
    for item in diagnostics.get("unavailable_sources") or []:
        if not isinstance(item, dict):
            continue
        source = str(item.get("source") or "unknown")
        severity = "warning" if source == "task_comments" else "medium"
        rows.append(
            {
                "severity": severity,
                "source": source,
                "entity": item.get("entity"),
                "reason": item.get("reason") or item.get("note") or "не указано",
                "impact": "Источник не вошел в историю автоматически; при важности нужно проверить вручную.",
            }
        )

    if diagnostics.get("missing_contact"):
        rows.append(
            {
                "severity": "medium",
                "source": "contact_resolution",
                "reason": "Контакт не найден или fallback неоднозначен.",
                "impact": "История клиента может быть неполной: связанные сделки и коммуникации могли не попасть в контекст.",
            }
        )
    return rows


def result_item(call_container: dict[str, Any] | None) -> dict[str, Any]:
    if not call_container or not call_container.get("ok"):
        return {}
    result = call_container.get("response", {}).get("result")
    return result if isinstance(result, dict) else {}


def converted_lead_deal_gaps(context: dict[str, Any], entity_type: str, entity_id: str) -> list[dict[str, Any]]:
    if entity_type != "lead":
        return []

    lead = result_item(context.get("lead"))
    status_id = str(lead.get("STATUS_ID") or "").upper()
    semantic_id = str(lead.get("STATUS_SEMANTIC_ID") or "").upper()
    if status_id != "CONVERTED" and semantic_id != "S":
        return []

    related_deals = [item for item in context.get("related_deals") or [] if isinstance(item, dict)]
    direct_deals = [item for item in related_deals if str(item.get("lead_id") or "") == str(entity_id)]
    if direct_deals:
        return []

    return [
        {
            "severity": "critical",
            "source": "converted_lead_deal_missing",
            "entity": f"lead:{entity_id}",
            "bitrix_entity_url": bitrix_entity_url("lead", entity_id),
            "reason": "Лид сконвертирован, но связанная сделка не найдена автоматически в related_deals.",
            "impact": "Основной ROP-анализ должен выполняться по сделке; без нее история после конвертации может быть упущена.",
            "manual_action": (
                "Открыть карточку лида в Bitrix24 и найти созданную сделку. "
                "Если сделка есть, проверить, что crm.deal.list по фильтру LEAD_ID возвращает ее через REST."
            ),
        }
    ]


def build_transcribe_command(entity_type: str, entity_id: str, audio_path: Path, call: dict[str, Any]) -> str:
    entity_arg = "--deal-id" if entity_type == "deal" else "--lead-id"
    parts = [
        r".\venv\Scripts\python.exe",
        r".\openai_api\audio\local_file_transcribe.py",
        entity_arg,
        str(entity_id),
        "--audio",
        quote(rel(audio_path)),
        "--activity-id",
        str(call.get("activity_id") or ""),
    ]
    if call.get("when"):
        parts.extend(["--call-start", quote(str(call["when"]))])
    subject = f"Bitrix {call.get('source_entity_type')}:{call.get('source_entity_id')} activity_id={call.get('activity_id')}"
    if call.get("subject"):
        subject = f"{subject}; {clean_text(call.get('subject'), 120)}"
    parts.extend(["--subject", quote(subject), "--no-copy-audio"])
    return " ".join(parts)


def call_gap(
    *,
    entity_type: str,
    entity_id: str,
    entity_dir: Path,
    call: dict[str, Any],
    has_local_audio: bool,
) -> dict[str, Any]:
    activity_id = str(call.get("activity_id") or "")
    expected_audio = entity_dir / "audio" / f"activity_{activity_id}.mp3"
    source_type = str(call.get("source_entity_type") or entity_type)
    source_id = str(call.get("source_entity_id") or entity_id)
    source_url = bitrix_entity_url(source_type, source_id)
    source_activity_url = bitrix_entity_activity_url(source_type, source_id, activity_id)
    severity = "critical" if not has_local_audio else "medium"
    reason = (
        "Звонок найден в CRM, но локальный transcript bundle не найден."
        if has_local_audio
        else "Звонок найден в CRM, но локальные аудио и transcript bundle не найдены."
    )
    return {
        "severity": severity,
        "source": "call_transcript" if has_local_audio else "call_audio_transcript",
        "activity_id": activity_id,
        "date": call.get("when"),
        "subject": call.get("subject"),
        "source_entity_type": source_type,
        "source_entity_id": source_id,
        "bitrix_entity_url": source_url,
        "bitrix_activity_url": source_activity_url,
        "expected_audio_path": rel(expected_audio),
        "transcribe_command": build_transcribe_command(entity_type, entity_id, expected_audio, call),
        "reason": reason,
        "impact": "LLM не сможет надежно оценить возражения клиента, качество разговора и договоренности по этому звонку.",
    }


def build_context_diagnostics(entity_type: str, entity_id: str, entity_dir: Path, context_path: Path) -> dict[str, Any]:
    context = load_json(context_path)
    calls = collect_calls(context, entity_type, entity_id)
    transcript_ids = transcript_activity_ids(entity_dir, entity_type, entity_id)
    audio_ids = local_audio_by_activity(entity_dir)
    for activity_id, paths in downloaded_audio_from_manifest(entity_dir, entity_type, entity_id).items():
        audio_ids.setdefault(activity_id, []).extend(paths)

    gaps = unavailable_source_gaps(context)
    gaps.extend(converted_lead_deal_gaps(context, entity_type, entity_id))
    for call in calls:
        activity_id = str(call.get("activity_id") or "")
        if not activity_id or activity_id in transcript_ids:
            continue
        gaps.append(
            call_gap(
                entity_type=entity_type,
                entity_id=entity_id,
                entity_dir=entity_dir,
                call=call,
                has_local_audio=activity_id in audio_ids,
            )
        )

    critical_count = sum(1 for item in gaps if item.get("severity") == "critical")
    medium_count = sum(1 for item in gaps if item.get("severity") == "medium")
    status = "full_enough" if not critical_count and not medium_count else "partial"
    return {
        "generated_at": datetime.now(MSK_TZ).isoformat(timespec="seconds"),
        "entity_type": entity_type,
        "entity_id": str(entity_id),
        "workspace_dir": str(entity_dir),
        "context_path": str(context_path),
        "context_completeness": status,
        "critical_missing": critical_count > 0,
        "summary": {
            "crm_calls_found": len(calls),
            "transcript_activity_ids_found": sorted(transcript_ids),
            "calls_without_transcript": sum(1 for call in calls if str(call.get("activity_id") or "") not in transcript_ids),
            "critical_gaps": critical_count,
            "medium_gaps": medium_count,
            "warning_gaps": sum(1 for item in gaps if item.get("severity") == "warning"),
        },
        "gaps": gaps,
    }


def render_manual_actions(payload: dict[str, Any]) -> str:
    entity_type = str(payload.get("entity_type") or "")
    entity_id = str(payload.get("entity_id") or "")
    root_url = bitrix_entity_url(entity_type, entity_id)
    lines = [
        f"# Что нужно добрать вручную по {entity_type} {entity_id}",
        "",
        f"- Статус полноты контекста: {payload.get('context_completeness')}",
        f"- Критичные пробелы: {payload.get('summary', {}).get('critical_gaps', 0)}",
        f"- Средние пробелы: {payload.get('summary', {}).get('medium_gaps', 0)}",
        f"- Карточка Bitrix: {root_url or 'не настроена BITRIX_PORTAL_URL'}",
        "",
    ]
    gaps = payload.get("gaps") or []
    actionable = [item for item in gaps if isinstance(item, dict) and item.get("source") in {"call_audio_transcript", "call_transcript"}]
    if not actionable:
        lines.extend(["Критичных ручных действий по звонкам не найдено.", ""])
    for index, item in enumerate(actionable, start=1):
        activity_id = item.get("activity_id") or "-"
        lines.extend(
            [
                f"## {index}. Звонок activity_id={activity_id}",
                "",
                f"Причина: {item.get('reason')}",
                f"Дата: {item.get('date') or 'не указана'}",
                f"Тема/метка CRM: {item.get('subject') or 'не указана'}",
                f"Источник: {item.get('source_entity_type')}:{item.get('source_entity_id')}",
                "",
                f"Карточка Bitrix: {item.get('bitrix_entity_url') or 'не указана'}",
                f"Ссылка с activity_id: {item.get('bitrix_activity_url') or 'не указана'}",
                "",
                "Скачать запись звонка из Bitrix24 вручную и положить сюда:",
                "",
                f"`{item.get('expected_audio_path')}`",
                "",
                "Если формат не mp3, оставь тот же activity_id в имени, например `activity_"
                f"{activity_id}.wav` или `activity_{activity_id}.m4a`, и замени путь в команде.",
                "",
                "После этого запустить транскрибацию:",
                "",
                "```powershell",
                item.get("transcribe_command") or "",
                "```",
                "",
                "Потом перезапустить анализ:",
                "",
                "```powershell",
                (
                    rf".\venv\Scripts\python.exe .\openai_api\llm\analyze_deal_if_changed.py --deal-id {entity_id} --transcript all --force-llm"
                    if entity_type == "deal"
                    else rf".\venv\Scripts\python.exe .\openai_api\llm\analyze_lead_if_changed.py --lead-id {entity_id} --transcript all --force-llm"
                ),
                "```",
                "",
            ]
        )

    other = [item for item in gaps if isinstance(item, dict) and item not in actionable]
    if other:
        lines.extend(["## Остальные ограничения контекста", ""])
        for item in other:
            if item.get("source") == "converted_lead_deal_missing":
                lines.extend(
                    [
                        f"### Сконвертированный лид без найденной сделки: {item.get('entity') or ''}",
                        "",
                        f"Причина: {item.get('reason')}",
                        f"Влияние: {item.get('impact')}",
                        f"Карточка Bitrix: {item.get('bitrix_entity_url') or 'не указана'}",
                        f"Что сделать: {item.get('manual_action')}",
                        "",
                    ]
                )
                continue
            lines.append(
                f"- `{item.get('source')}`"
                f"{' по ' + str(item.get('entity')) if item.get('entity') else ''}: "
                f"{item.get('reason')}. Влияние: {item.get('impact')}"
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_llm_context(payload: dict[str, Any]) -> str:
    summary = payload.get("summary") or {}
    lines = [
        "# Диагностика полноты контекста",
        "",
        f"- Статус: {payload.get('context_completeness')}",
        f"- Критичные пробелы: {summary.get('critical_gaps', 0)}",
        f"- Звонков в CRM: {summary.get('crm_calls_found', 0)}",
        f"- Звонков без транскрипта: {summary.get('calls_without_transcript', 0)}",
        "",
        "Правило для анализа: если контекст неполный, анализируй доступную историю, но явно укажи ограничения. Не делай выводы о содержании отсутствующих звонков.",
        "",
    ]
    gaps = payload.get("gaps") or []
    if gaps:
        lines.extend(["## Пробелы", ""])
        for item in gaps[:20]:
            if not isinstance(item, dict):
                continue
            detail = item.get("activity_id") or item.get("entity") or ""
            lines.append(
                f"- {item.get('severity')} / {item.get('source')}"
                f"{' / ' + str(detail) if detail else ''}: {item.get('reason')}. "
                f"Влияние: {item.get('impact')}"
            )
    else:
        lines.append("Критичных пробелов по локальной диагностике не найдено.")
    return "\n".join(lines).rstrip() + "\n"


def write_diagnostics(payload: dict[str, Any], entity_dir: Path) -> dict[str, Path]:
    diagnostics_dir = entity_dir / "diagnostics"
    diagnostics_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "context_gaps": diagnostics_dir / "context_gaps.json",
        "manual_actions_json": diagnostics_dir / "manual_actions.json",
        "manual_actions_md": diagnostics_dir / "manual_actions.md",
        "llm_context": diagnostics_dir / "context_gaps_for_llm.md",
    }
    save_json(paths["context_gaps"], payload)
    save_json(paths["manual_actions_json"], {"items": payload.get("gaps") or []})
    paths["manual_actions_md"].write_text(render_manual_actions(payload), encoding="utf-8")
    paths["llm_context"].write_text(render_llm_context(payload), encoding="utf-8")
    return paths


def find_context_path(entity_dir: Path, entity_type: str, entity_id: str) -> Path:
    raw_dir = entity_dir / "raw"
    full = raw_dir / f"{entity_type}_{entity_id}_customer_history_bundle.json"
    if full.exists():
        return full
    single = raw_dir / f"{entity_type}_{entity_id}_context.json"
    if single.exists():
        return single
    raise FileNotFoundError(f"Context JSON not found in workspace: {full} or {single}")


def ensure_context_diagnostics(entity_type: str, entity_id: str, workspace_root: Path) -> dict[str, Path]:
    entity_dir = entity_workspace_dir(entity_id, entity_type=entity_type, workspace_root=workspace_root)
    context_path = find_context_path(entity_dir, entity_type, entity_id)
    payload = build_context_diagnostics(entity_type, entity_id, entity_dir, context_path)
    return write_diagnostics(payload, entity_dir)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build local context completeness diagnostics")
    parser.add_argument("--entity-type", choices=["deal", "lead"], required=True)
    parser.add_argument("--entity-ids", nargs="+", required=True)
    parser.add_argument("--workspace-root", help="Workspace root. Defaults to matching deals/leads root.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    workspace_root = Path(
        args.workspace_root
        or (DEFAULT_DEAL_WORKSPACE_ROOT if args.entity_type == "deal" else DEFAULT_LEAD_WORKSPACE_ROOT)
    )
    for entity_id in args.entity_ids:
        paths = ensure_context_diagnostics(args.entity_type, str(entity_id), workspace_root)
        logger.info("Saved context diagnostics for %s %s: %s", args.entity_type, entity_id, paths["manual_actions_md"])
        print(f"Manual actions saved: {paths['manual_actions_md']}")


if __name__ == "__main__":
    main()
