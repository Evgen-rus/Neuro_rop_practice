"""
Interactive terminal launcher for the local ROP assistant workflow.

The script intentionally orchestrates existing read-only pipeline/analyze tools
instead of duplicating Bitrix, transcription, or LLM logic.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from setup import BASE_DIR


AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".ogg", ".flac", ".webm"}


@dataclass(frozen=True)
class WorkflowOptions:
    entity_type: str
    entity_ids: list[str]
    history_days: int
    include_related_contact_deals: bool
    include_internal_context: bool
    download_audio: bool
    redownload_audio: bool
    transcribe_audio: bool
    analyze: bool
    force_llm: bool
    transcript_mode: str


def configure_console() -> None:
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name)
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def python_executable() -> str:
    venv_python = PROJECT_ROOT / "venv" / "Scripts" / "python.exe"
    if venv_python.exists():
        return str(venv_python)
    return sys.executable


def rel(path: Path | str) -> str:
    value = Path(path)
    try:
        return str(value.resolve().relative_to(PROJECT_ROOT.resolve()))
    except (OSError, ValueError):
        return str(path)


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def parse_date_sort_value(value: Any) -> str:
    return str(value or "")


def parse_ids(value: str) -> list[str]:
    ids = [part.strip() for part in value.replace(";", ",").split(",")]
    return [item for item in ids if item]


def prompt_text(question: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default is not None else ""
    value = input(f"{question}{suffix}: ").strip()
    return value or (default or "")


def prompt_bool(question: str, default: bool = True) -> bool:
    marker = "Д/н" if default else "д/Н"
    while True:
        value = input(f"{question} [{marker}]: ").strip().lower()
        if not value:
            return default
        if value in {"д", "да", "y", "yes", "1", "+"}:
            return True
        if value in {"н", "нет", "n", "no", "0", "-"}:
            return False
        print("Введите да или нет.")


def prompt_int(question: str, default: int) -> int:
    while True:
        value = prompt_text(question, str(default))
        try:
            parsed = int(value)
        except ValueError:
            print("Введите целое число.")
            continue
        if parsed <= 0:
            print("Число должно быть больше 0.")
            continue
        return parsed


def choose_entity_type(default: str = "deal") -> str:
    print("")
    print("Что обработать?")
    print("1. Сделки")
    print("2. Лиды")
    value = prompt_text("Выбор", "1" if default == "deal" else "2").lower()
    if value in {"1", "deal", "deals", "сделка", "сделки"}:
        return "deal"
    if value in {"2", "lead", "leads", "лид", "лиды"}:
        return "lead"
    print("Не понял выбор, беру сделки.")
    return "deal"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Удобный запуск сбора контекста, транскрибации и анализа РОП-помощника."
    )
    parser.add_argument("--entity", choices=["deal", "lead"], help="Что обработать: deal или lead")
    parser.add_argument("--ids", nargs="+", help="ID сделок/лидов. Можно: --ids 18507 18508")
    parser.add_argument("--history-days", type=int, default=60, help="Глубина истории в днях. Default: 60")
    parser.add_argument("--no-related", action="store_true", help="Не включать связанные сделки контакта")
    parser.add_argument("--no-internal", action="store_true", help="Не включать внутренние комментарии/таймлайн")
    parser.add_argument("--skip-audio-download", action="store_true", help="Не скачивать аудио звонков")
    parser.add_argument("--redownload-audio", action="store_true", help="Перекачать аудио даже если уже есть")
    parser.add_argument("--no-transcribe", action="store_true", help="Не транскрибировать скачанные аудио")
    parser.add_argument("--no-analyze", action="store_true", help="Не запускать LLM-анализ")
    parser.add_argument("--no-force-llm", action="store_true", help="Не принуждать полный LLM-анализ")
    parser.add_argument(
        "--transcript",
        choices=["all", "latest", "none"],
        default="all",
        help="Какие транскрипты передать в анализ. Default: all",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Не задавать вопросы, использовать значения по умолчанию и переданные аргументы.",
    )
    return parser.parse_args()


def options_from_args(args: argparse.Namespace) -> WorkflowOptions:
    if args.yes:
        if not args.entity or not args.ids:
            raise SystemExit("--yes требует --entity и --ids")
        return WorkflowOptions(
            entity_type=args.entity,
            entity_ids=[str(item).strip() for item in args.ids if str(item).strip()],
            history_days=args.history_days,
            include_related_contact_deals=not args.no_related,
            include_internal_context=not args.no_internal,
            download_audio=not args.skip_audio_download,
            redownload_audio=args.redownload_audio,
            transcribe_audio=not args.no_transcribe,
            analyze=not args.no_analyze,
            force_llm=not args.no_force_llm,
            transcript_mode=args.transcript,
        )

    print("=== РОП-помощник: сбор контекста -> звонки -> транскрибация -> анализ ===")
    entity_type = args.entity or choose_entity_type()
    ids = [str(item).strip() for item in args.ids if str(item).strip()] if args.ids else []
    while not ids:
        ids = parse_ids(prompt_text("Введите ID через запятую"))
        if not ids:
            print("Нужен хотя бы один ID.")

    history_days = args.history_days or 60
    if args.history_days == 60:
        history_days = prompt_int("Глубина истории, дней", 60)

    include_related = not args.no_related
    include_internal = not args.no_internal
    download_audio = not args.skip_audio_download
    transcribe_audio = not args.no_transcribe
    analyze = not args.no_analyze
    force_llm = not args.no_force_llm

    if not args.no_related:
        include_related = prompt_bool("Включить связанные сделки контакта", True)
    if not args.no_internal:
        include_internal = prompt_bool("Включить внутренний контекст CRM", True)
    if not args.skip_audio_download:
        download_audio = prompt_bool("Скачать недостающие аудио звонков", True)
    redownload_audio = False
    if download_audio:
        redownload_audio = args.redownload_audio or prompt_bool("Перекачать уже скачанные аудио", False)
    if not args.no_transcribe:
        transcribe_audio = prompt_bool("Транскрибировать скачанные аудио без transcript", True)
    if not args.no_analyze:
        analyze = prompt_bool("Запустить анализ после подготовки контекста", True)
    if analyze and not args.no_force_llm:
        force_llm = prompt_bool("Принудительно сделать полный LLM-анализ", True)

    transcript_mode = args.transcript
    if analyze:
        transcript_mode = prompt_text("Режим транскриптов для анализа: all/latest/none", args.transcript).lower()
        if transcript_mode not in {"all", "latest", "none"}:
            print("Неизвестный режим, беру all.")
            transcript_mode = "all"

    options = WorkflowOptions(
        entity_type=entity_type,
        entity_ids=ids,
        history_days=history_days,
        include_related_contact_deals=include_related,
        include_internal_context=include_internal,
        download_audio=download_audio,
        redownload_audio=redownload_audio,
        transcribe_audio=transcribe_audio,
        analyze=analyze,
        force_llm=force_llm,
        transcript_mode=transcript_mode,
    )
    print_summary(options)
    if not prompt_bool("Запустить с этими настройками", True):
        raise SystemExit("Отменено пользователем.")
    return options


def print_summary(options: WorkflowOptions) -> None:
    label = "сделки" if options.entity_type == "deal" else "лиды"
    print("")
    print("Параметры запуска:")
    print(f"- Тип: {label}")
    print(f"- ID: {', '.join(options.entity_ids)}")
    print(f"- История: {options.history_days} дней")
    print(f"- Связанные сделки контакта: {'да' if options.include_related_contact_deals else 'нет'}")
    print(f"- Внутренний контекст: {'да' if options.include_internal_context else 'нет'}")
    print(f"- Скачивание аудио: {'да' if options.download_audio else 'нет'}")
    print(f"- Перекачка аудио: {'да' if options.redownload_audio else 'нет'}")
    print(f"- Транскрибация: {'да' if options.transcribe_audio else 'нет'}")
    print(f"- Анализ: {'да' if options.analyze else 'нет'}")
    if options.analyze:
        print(f"- Полный LLM принудительно: {'да' if options.force_llm else 'нет'}")
        print(f"- Транскрипты в анализ: {options.transcript_mode}")
    print("")


def run_command(command: list[str], title: str) -> None:
    print("")
    print(f"=== {title} ===")
    print(" ".join(command))
    subprocess.run(command, cwd=BASE_DIR, check=True)


def pipeline_command(options: WorkflowOptions) -> list[str]:
    entity_plural = "deals" if options.entity_type == "deal" else "leads"
    script = PROJECT_ROOT / "bitrix" / entity_plural / f"run_{entity_plural}_customer_path_pipeline.py"
    id_arg = "--deal-ids" if options.entity_type == "deal" else "--lead-ids"
    command = [
        python_executable(),
        str(script),
        id_arg,
        *options.entity_ids,
        "--history-days",
        str(options.history_days),
    ]
    if options.include_related_contact_deals:
        command.append("--include-related-contact-deals")
    if options.include_internal_context:
        command.append("--include-internal-context")
    if not options.download_audio:
        command.append("--skip-audio-download")
    if options.redownload_audio:
        command.append("--redownload-audio")
    return command


def options_for_entity(options: WorkflowOptions, entity_type: str, entity_ids: list[str]) -> WorkflowOptions:
    return replace(options, entity_type=entity_type, entity_ids=[str(item) for item in entity_ids])


def options_for_converted_deals(options: WorkflowOptions, deal_ids: list[str]) -> WorkflowOptions:
    return replace(
        options,
        entity_type="deal",
        entity_ids=[str(item) for item in deal_ids],
        include_related_contact_deals=False,
    )


def workspace_root(entity_type: str) -> Path:
    folder = "deals" if entity_type == "deal" else "leads"
    return PROJECT_ROOT / "reports" / "rop_assistant" / folder


def workspace_dir(entity_type: str, entity_id: str) -> Path:
    return workspace_root(entity_type) / f"{entity_type}_{entity_id}"


def lead_history_bundle_path(lead_id: str) -> Path:
    return workspace_dir("lead", lead_id) / "raw" / f"lead_{lead_id}_customer_history_bundle.json"


def result_item(call_container: dict[str, Any] | None) -> dict[str, Any]:
    if not call_container or not call_container.get("ok"):
        return {}
    result = call_container.get("response", {}).get("result")
    return result if isinstance(result, dict) else {}


def is_converted_lead(bundle: dict[str, Any]) -> bool:
    lead = result_item(bundle.get("lead"))
    status_id = str(lead.get("STATUS_ID") or "").upper()
    semantic_id = str(lead.get("STATUS_SEMANTIC_ID") or "").upper()
    return status_id == "CONVERTED" or semantic_id == "S"


def freshest_related_deal(bundle: dict[str, Any], lead_id: str) -> dict[str, Any] | None:
    deals = [deal for deal in bundle.get("related_deals") or [] if isinstance(deal, dict) and deal.get("id")]
    direct_deals = [deal for deal in deals if str(deal.get("lead_id") or "") == str(lead_id)]
    candidates = direct_deals or deals
    if not candidates:
        return None
    return sorted(
        candidates,
        key=lambda item: (
            parse_date_sort_value(item.get("date_modify") or item.get("date_create") or item.get("closedate")),
            int(str(item.get("id") or "0")) if str(item.get("id") or "").isdigit() else 0,
        ),
        reverse=True,
    )[0]


def converted_lead_deals(lead_ids: list[str]) -> dict[str, dict[str, Any]]:
    converted: dict[str, dict[str, Any]] = {}
    for lead_id in lead_ids:
        bundle_path = lead_history_bundle_path(str(lead_id))
        if not bundle_path.exists():
            continue
        try:
            bundle = load_json(bundle_path)
        except ValueError:
            continue
        if not is_converted_lead(bundle):
            continue
        deal = freshest_related_deal(bundle, str(lead_id))
        if deal:
            converted[str(lead_id)] = deal
            continue
        print(
            f"Лид {lead_id} сконвертирован, но сделка не найдена в related_deals. "
            f"Проверь историю: {rel(bundle_path)}"
        )
    return converted


def unique_ordered(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value)
        if text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def print_converted_switch(converted: dict[str, dict[str, Any]]) -> None:
    if not converted:
        return
    print("")
    print("=== Переключение лидов в сделки ===")
    for lead_id, deal in converted.items():
        pipeline = (deal.get("pipeline") or {}).get("name") or deal.get("category_id") or "-"
        status = "закрыта" if deal.get("is_closed") else "открыта/неясно"
        amount = f"{deal.get('opportunity') or '-'} {deal.get('currency_id') or ''}".strip()
        print(
            f"Лид {lead_id} сконвертирован -> сделка {deal.get('id')} "
            f"({pipeline}, стадия: {deal.get('stage_name') or deal.get('stage_id')}, "
            f"сумма: {amount}, {status})."
        )
    print("Lead-анализ для этих лидов пропущен; основной управленческий анализ будет выполнен по сделке.")


def diagnostics_path(entity_type: str, entity_id: str) -> Path:
    return workspace_dir(entity_type, entity_id) / "diagnostics" / "context_gaps.json"


def manifest_path(entity_type: str, entity_id: str) -> Path:
    return workspace_dir(entity_type, entity_id) / "audio" / f"{entity_type}_{entity_id}_call_audio_manifest.json"


def existing_transcript_activity_ids(entity_type: str, entity_id: str) -> set[str]:
    transcripts_dir = workspace_dir(entity_type, entity_id) / "transcripts"
    ids: set[str] = set()
    if not transcripts_dir.exists():
        return ids
    for path in transcripts_dir.glob("*.json"):
        try:
            payload = load_json(path)
        except ValueError:
            continue
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        activity_id = str(metadata.get("activity_id") or "")
        if activity_id:
            ids.add(activity_id)
    return ids


def manifest_audio_by_activity(entity_type: str, entity_id: str) -> dict[str, list[Path]]:
    path = manifest_path(entity_type, entity_id)
    if not path.exists():
        return {}
    try:
        manifest = load_json(path)
    except ValueError:
        return {}

    rows: dict[str, list[Path]] = {}
    for call in manifest.get("calls") or []:
        if not isinstance(call, dict):
            continue
        activity_id = str(call.get("activity_id") or "")
        if not activity_id:
            continue
        for item in call.get("downloads") or []:
            if not isinstance(item, dict) or not item.get("ok") or not item.get("local_path"):
                continue
            audio_path = Path(str(item["local_path"]))
            if audio_path.exists() and audio_path.suffix.lower() in AUDIO_EXTENSIONS:
                rows.setdefault(activity_id, []).append(audio_path)
    return rows


def local_audio_by_activity(entity_type: str, entity_id: str) -> dict[str, list[Path]]:
    audio_dir = workspace_dir(entity_type, entity_id) / "audio"
    rows: dict[str, list[Path]] = {}
    if not audio_dir.exists():
        return rows
    for path in audio_dir.iterdir():
        if not path.is_file() or path.suffix.lower() not in AUDIO_EXTENSIONS:
            continue
        tokens = path.stem.lower().replace("-", "_").split("_")
        for token in tokens:
            if token.isdigit():
                rows.setdefault(token, []).append(path)
    return rows


def audio_by_activity(entity_type: str, entity_id: str) -> dict[str, list[Path]]:
    rows = manifest_audio_by_activity(entity_type, entity_id)
    for activity_id, paths in local_audio_by_activity(entity_type, entity_id).items():
        rows.setdefault(activity_id, []).extend(paths)
    return rows


def diagnostic_payload(entity_type: str, entity_id: str) -> dict[str, Any]:
    path = diagnostics_path(entity_type, entity_id)
    if not path.exists():
        return {}
    try:
        return load_json(path)
    except ValueError:
        return {}


def short_no_answer_activity_ids(entity_type: str, entity_id: str) -> set[str]:
    """Activity IDs whose local audio is shorter than 20s (недозвон / автоответчик)."""
    path = manifest_path(entity_type, entity_id)
    if not path.exists():
        return set()
    try:
        from openai_api.audio.short_call import enrich_manifest_calls, is_short_no_answer_audio

        manifest = enrich_manifest_calls(load_json(path))
    except ValueError:
        return set()

    ids: set[str] = set()
    for call in manifest.get("calls") or []:
        if not isinstance(call, dict):
            continue
        activity_id = str(call.get("activity_id") or "")
        if not activity_id:
            continue
        if call.get("skip_transcribe") or call.get("call_quality") == "short_no_answer":
            ids.add(activity_id)
            continue
        for item in call.get("downloads") or []:
            if not isinstance(item, dict) or not item.get("ok") or not item.get("local_path"):
                continue
            if item.get("is_short_no_answer") or is_short_no_answer_audio(item["local_path"]):
                ids.add(activity_id)
                break
    return ids


def transcribable_gaps(entity_type: str, entity_id: str) -> list[dict[str, Any]]:
    payload = diagnostic_payload(entity_type, entity_id)
    transcript_ids = existing_transcript_activity_ids(entity_type, entity_id)
    audio_paths = audio_by_activity(entity_type, entity_id)
    short_ids = short_no_answer_activity_ids(entity_type, entity_id)
    rows: list[dict[str, Any]] = []
    for gap in payload.get("gaps") or []:
        if not isinstance(gap, dict):
            continue
        activity_id = str(gap.get("activity_id") or "")
        if not activity_id or activity_id in transcript_ids:
            continue
        if activity_id in short_ids:
            print(
                f"Пропуск транскрибации activity_id={activity_id}: "
                "звонок короче 20 сек (недозвон/автоответчик)."
            )
            continue
        candidates = audio_paths.get(activity_id) or []
        if not candidates:
            continue
        # Extra safety if manifest has no duration yet.
        from openai_api.audio.short_call import is_short_no_answer_audio

        if is_short_no_answer_audio(candidates[0]):
            print(
                f"Пропуск транскрибации activity_id={activity_id}: "
                "звонок короче 20 сек (недозвон/автоответчик)."
            )
            continue
        rows.append({**gap, "audio_path": str(candidates[0])})
    return rows


def transcribe_command(entity_type: str, entity_id: str, gap: dict[str, Any]) -> list[str]:
    entity_arg = "--deal-id" if entity_type == "deal" else "--lead-id"
    activity_id = str(gap.get("activity_id") or "")
    subject = (
        f"Bitrix {gap.get('source_entity_type') or entity_type}:"
        f"{gap.get('source_entity_id') or entity_id} activity_id={activity_id}"
    )
    if gap.get("subject"):
        subject = f"{subject}; {gap.get('subject')}"

    command = [
        python_executable(),
        str(PROJECT_ROOT / "openai_api" / "audio" / "local_file_transcribe.py"),
        entity_arg,
        str(entity_id),
        "--audio",
        str(gap["audio_path"]),
        "--activity-id",
        activity_id,
        "--subject",
        subject,
        "--no-copy-audio",
    ]
    if gap.get("date"):
        command.extend(["--call-start", str(gap["date"])])
    return command


def refresh_diagnostics(entity_type: str, ids: list[str]) -> None:
    command = [
        python_executable(),
        str(PROJECT_ROOT / "bitrix" / "context_diagnostics.py"),
        "--entity-type",
        entity_type,
        "--entity-ids",
        *ids,
        "--workspace-root",
        str(workspace_root(entity_type)),
    ]
    run_command(command, "Обновление диагностики после транскрибации")


def transcribe_missing_audio(options: WorkflowOptions) -> None:
    any_transcribed = False
    for entity_id in options.entity_ids:
        gaps = transcribable_gaps(options.entity_type, entity_id)
        if not gaps:
            print(f"Транскрибация: {options.entity_type}_{entity_id} - нет локальных аудио без transcript.")
            continue
        print(f"Транскрибация: {options.entity_type}_{entity_id} - файлов к обработке: {len(gaps)}")
        for gap in gaps:
            any_transcribed = True
            title = f"Транскрибация activity_id={gap.get('activity_id')}"
            run_command(transcribe_command(options.entity_type, entity_id, gap), title)
    if any_transcribed:
        refresh_diagnostics(options.entity_type, options.entity_ids)


def analyze_command(options: WorkflowOptions, entity_id: str) -> list[str]:
    script_name = f"analyze_{options.entity_type}_if_changed.py"
    id_arg = "--deal-id" if options.entity_type == "deal" else "--lead-id"
    root_arg = "--deal-root" if options.entity_type == "deal" else "--lead-root"
    command = [
        python_executable(),
        str(PROJECT_ROOT / "openai_api" / "llm" / script_name),
        id_arg,
        str(entity_id),
        root_arg,
        str(workspace_root(options.entity_type)),
        "--transcript",
        options.transcript_mode,
    ]
    if options.force_llm:
        command.append("--force-llm")
    return command


def run_analysis(options: WorkflowOptions) -> None:
    for entity_id in options.entity_ids:
        run_command(analyze_command(options, entity_id), f"LLM-анализ {options.entity_type}_{entity_id}")


def run_post_pipeline_steps(options: WorkflowOptions) -> None:
    if options.transcribe_audio:
        transcribe_missing_audio(options)
    if options.analyze:
        run_analysis(options)


def report_path(entity_type: str, entity_id: str) -> Path:
    return workspace_dir(entity_type, entity_id) / "analysis" / f"{entity_type}_{entity_id}_rop_report.md"


def print_final_status(options: WorkflowOptions, *, show_report: bool = True) -> None:
    print("")
    print("=== Итог ===")
    for entity_id in options.entity_ids:
        entity_dir = workspace_dir(options.entity_type, entity_id)
        manual_actions = entity_dir / "diagnostics" / "manual_actions.md"
        gaps = diagnostic_payload(options.entity_type, entity_id)
        summary = gaps.get("summary") if isinstance(gaps.get("summary"), dict) else {}
        completeness = gaps.get("context_completeness") or "unknown"
        critical = summary.get("critical_gaps", 0)
        medium = summary.get("medium_gaps", 0)
        calls = summary.get("crm_calls_found", 0)
        without_transcript = summary.get("calls_without_transcript", 0)

        print(f"{options.entity_type}_{entity_id}")
        print(f"  Workspace: {rel(entity_dir)}")
        print(f"  Диагностика: {rel(manual_actions)}")
        print(
            "  Контекст: "
            f"{completeness}; звонков={calls}, без транскрипта={without_transcript}, "
            f"critical={critical}, medium={medium}"
        )
        if int(critical or 0) or int(medium or 0):
            print("  Важно: анализ можно читать, но он обязан учитывать, что контекст неполный.")
        path = report_path(options.entity_type, entity_id)
        if show_report and path.exists():
            print(f"  ROP-отчет: {rel(path)}")
        print("")


def main() -> None:
    configure_console()
    args = parse_args()
    options = options_from_args(args)
    run_command(pipeline_command(options), "Сбор истории, аудио и диагностики")
    if options.entity_type != "lead":
        run_post_pipeline_steps(options)
        print_final_status(options)
        return

    converted = converted_lead_deals(options.entity_ids)
    converted_lead_ids = set(converted)
    remaining_lead_ids = [lead_id for lead_id in options.entity_ids if lead_id not in converted_lead_ids]

    if remaining_lead_ids:
        run_post_pipeline_steps(options_for_entity(options, "lead", remaining_lead_ids))

    if converted:
        print_converted_switch(converted)
        deal_ids = unique_ordered([str(deal.get("id")) for deal in converted.values() if deal.get("id")])
        deal_options = options_for_converted_deals(options, deal_ids)
        run_command(pipeline_command(deal_options), "Сбор истории, аудио и диагностики по сделкам сконвертированных лидов")
        run_post_pipeline_steps(deal_options)

    print_final_status(options_for_entity(options, "lead", remaining_lead_ids), show_report=True) if remaining_lead_ids else None
    if converted:
        print_final_status(options_for_entity(options, "lead", list(converted_lead_ids)), show_report=False)
        print_final_status(options_for_entity(options, "deal", unique_ordered([str(deal.get("id")) for deal in converted.values() if deal.get("id")])))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nОстановлено пользователем.")
        raise SystemExit(130)
    except subprocess.CalledProcessError as error:
        print("")
        print(f"Команда завершилась с ошибкой: exit code {error.returncode}")
        print("Проверь лог выше и файлы diagnostics/manual_actions.md по нужной сущности.")
        raise SystemExit(error.returncode)
