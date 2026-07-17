"""
Локальный скрипт для транскрибации выбранного аудиофайла.

Логика транскрибации такая же, как у бота:
- файл конвертируется через ffmpeg;
- при необходимости режется на сегменты;
- результат сохраняется рядом с исходным аудио.
"""

import asyncio
import argparse
import sys
from pathlib import Path
from tkinter import Tk, filedialog

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from openai_api.config import TRANSCRIPTION_MODEL, logger
from bitrix.workspace import (
    DEFAULT_AUDIO_MANIFEST_DIR,
    DEFAULT_LEAD_AUDIO_MANIFEST_DIR,
    DEFAULT_DEAL_WORKSPACE_ROOT,
    DEFAULT_LEAD_WORKSPACE_ROOT,
    copy_audio_to_workspace,
    ensure_entity_workspace,
    ensure_deal_workspace,
    normalize_dt_for_filename,
    safe_slug,
)
from bitrix.deals.download_deals_call_audio import load_existing_manifest, record_transcribed_and_purged
from openai_api.audio.transcribe_core import (
    estimate_transcription_cost_details,
    get_audio_duration_seconds,
    save_transcription,
    save_transcription_bundle,
    transcribe_file_async,
)
from openai_api.logging_utils import log_model_file_payload
from openai_api.pricing import format_usd_rub
from progress_events import emit_progress


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Locally transcribe an audio file")
    parser.add_argument("--audio", help="Path to audio file. If omitted, a file picker opens.")
    parser.add_argument("--lead-id", help="Lead ID. If provided, transcript is saved into the lead workspace.")
    parser.add_argument("--deal-id", help="Backward-compatible deal ID.")
    parser.add_argument("--entity-type", choices=["lead", "deal"], default="lead", help="Workspace entity type.")
    parser.add_argument("--activity-id", help="Bitrix CRM activity/call ID for clear naming.")
    parser.add_argument("--call-start", help="Call start timestamp, e.g. 2026-06-17T13:51:19+03:00.")
    parser.add_argument("--subject", help="Call subject/comment saved into transcript metadata.")
    parser.add_argument("--workspace-root", help="Lead/deal workspace root. Defaults to the matching leads/deals root.")
    parser.add_argument("--max-segment-concurrency", type=int, default=1, help="Concurrent OpenAI transcription segments.")
    parser.add_argument("--no-copy-audio", action="store_true", help="Do not copy audio into the workspace.")
    return parser.parse_args()


def choose_audio_file() -> str | None:
    """
    Открывает диалог выбора файла и возвращает путь к выбранному файлу
    или None, если пользователь ничего не выбрал.
    """
    root = Tk()
    root.withdraw()
    root.update()

    filetypes = (
        ("Аудиофайлы", "*.wav *.mp3 *.ogg *.m4a *.flac *.webm"),
        ("Все файлы", "*.*"),
    )

    filepath = filedialog.askopenfilename(
        title="Выберите аудиофайл для транскрибации",
        filetypes=filetypes,
    )

    root.destroy()
    return filepath or None


def purge_manifest_audio_after_transcription(
    *,
    entity_type: str,
    entity_id: str | None,
    activity_id: str | None,
    audio_path: Path,
    transcript_paths: dict[str, str],
) -> bool:
    """Delete only audio that belongs to the managed Bitrix download manifest."""
    if not entity_id or not activity_id:
        return False
    manifest_dir = DEFAULT_AUDIO_MANIFEST_DIR if entity_type == "deal" else DEFAULT_LEAD_AUDIO_MANIFEST_DIR
    manifest_path = manifest_dir / f"{entity_type}_{entity_id}_call_audio_manifest.json"
    if not manifest_path.exists():
        return False

    # Do not delete a manually selected file: it must be the exact path recorded
    # in the Bitrix download manifest for this entity and activity.
    manifest = load_existing_manifest(manifest_path)
    is_manifest_audio = any(
        audio_path.resolve() == Path(str(download["local_path"])).resolve()
        for call in manifest.get("calls") or []
        if isinstance(call, dict) and str(call.get("activity_id") or "") == str(activity_id)
        for download in call.get("downloads") or []
        if isinstance(download, dict) and download.get("local_path")
    )
    if not is_manifest_audio:
        return False

    audio_path.unlink()
    return record_transcribed_and_purged(manifest_path, audio_path, str(activity_id), transcript_paths)


def main() -> None:
    args = parse_args()
    print("=== Локальная транскрибация аудиофайла ===")

    filepath = args.audio
    if not filepath:
        print("Сейчас откроется окно выбора файла.")
        filepath = choose_audio_file()
    if not filepath:
        print("Файл не выбран. Выходим.")
        return

    audio_path = Path(filepath)
    if not audio_path.exists():
        print(f"Файл не найден: {audio_path}")
        return

    transcribed_audio_path = audio_path
    entity_id = args.lead_id or args.deal_id
    entity_type = "deal" if args.deal_id and not args.lead_id else args.entity_type
    workspace_root = Path(
        args.workspace_root
        or (DEFAULT_DEAL_WORKSPACE_ROOT if entity_type == "deal" else DEFAULT_LEAD_WORKSPACE_ROOT)
    )

    entity_dir = None
    if entity_id:
        if entity_type == "deal":
            entity_dir = ensure_deal_workspace(entity_id, workspace_root=workspace_root)
        else:
            entity_dir = ensure_entity_workspace(entity_id, entity_type="lead", workspace_root=workspace_root)
        if not args.no_copy_audio:
            transcribed_audio_path = copy_audio_to_workspace(
                audio_path,
                entity_dir,
                activity_id=args.activity_id,
                call_start=args.call_start,
            )

    print(f"Файл для транскрибации: {transcribed_audio_path}")
    duration_seconds = get_audio_duration_seconds(transcribed_audio_path)
    estimated_cost = estimate_transcription_cost_details(TRANSCRIPTION_MODEL, duration_seconds)
    log_model_file_payload(
        logger,
        title="manual transcription input file",
        model=TRANSCRIPTION_MODEL,
        path=transcribed_audio_path,
        metadata={
            "entity_type": entity_type if entity_id else None,
            "entity_id": entity_id,
            "activity_id": args.activity_id,
            "call_start": args.call_start,
            "subject": args.subject,
            "duration_seconds": duration_seconds,
            "estimated_cost": estimated_cost,
            "estimated_cost_usd": estimated_cost.get("estimated_cost_usd"),
            "estimated_cost_rub": estimated_cost.get("estimated_cost_rub"),
            "source_audio_path": str(audio_path),
        },
        preview_text=False,
    )
    if duration_seconds is not None:
        print(f"Длительность аудио: {duration_seconds:.1f} сек.")
    print(
        "Оценка стоимости транскрибации: "
        f"{format_usd_rub(estimated_cost.get('estimated_cost_usd'), estimated_cost.get('estimated_cost_rub'))} "
        f"({TRANSCRIPTION_MODEL})"
    )

    try:
        def transcription_progress(event: dict[str, Any]) -> None:
            if not entity_id:
                return
            status = str(event.get("status") or "")
            current = int(event.get("current") or 1)
            total = int(event.get("total") or 1)
            attempt = int(event.get("attempt") or 1)
            max_attempts = int(event.get("max_attempts") or 3)
            detail = f"Сегмент {current} из {total}"
            if status == "retry_wait":
                detail += f": повторная попытка {attempt + 1} из {max_attempts}"
            emit_progress(
                entity_type,
                str(entity_id),
                "transcription",
                detail=detail,
                current=current,
                total=total,
                attempt=attempt,
                max_attempts=max_attempts,
            )

        text = asyncio.run(
            transcribe_file_async(
                str(transcribed_audio_path),
                max_segment_concurrency=args.max_segment_concurrency,
                progress_callback=transcription_progress,
            )
        )
    except Exception as e:
        logger.error(f"Ошибка при транскрибации файла: {e}")
        print(f"Ошибка транскрибации: {e}")
        return

    if entity_dir:
        transcript_stem = "_".join(
            part
            for part in [
                f"call_{safe_slug(args.activity_id)}" if args.activity_id else "manual_call",
                normalize_dt_for_filename(args.call_start),
                "transcript",
            ]
            if part
        )
        saved = save_transcription_bundle(
            text,
            str(transcribed_audio_path),
            entity_dir / "transcripts",
            transcript_stem,
            metadata={
                "entity_type": entity_type,
                "entity_id": entity_id,
                f"{entity_type}_id": entity_id,
                "activity_id": args.activity_id,
                "call_start": args.call_start,
                "subject": args.subject,
                "transcription_model": TRANSCRIPTION_MODEL,
                "audio_duration_seconds": duration_seconds,
                "estimated_transcription_cost": estimated_cost,
                "estimated_transcription_cost_usd": estimated_cost.get("estimated_cost_usd"),
                "estimated_transcription_cost_rub": estimated_cost.get("estimated_cost_rub"),
                "source_audio_path": str(audio_path),
                "workspace_audio_path": str(transcribed_audio_path),
            },
        )
        print("Транскрибация сохранена:")
        print(saved["md_path"])
        if purge_manifest_audio_after_transcription(
            entity_type=entity_type,
            entity_id=entity_id,
            activity_id=args.activity_id,
            audio_path=transcribed_audio_path,
            transcript_paths=saved,
        ):
            print("Исходное аудио удалено после успешного сохранения транскрипта.")
    else:
        txt_path = save_transcription(text, str(transcribed_audio_path))
        print(f"Транскрипция сохранена в файл:\n{txt_path}")

    print("Готово.")


if __name__ == "__main__":
    main()
