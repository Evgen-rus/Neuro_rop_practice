"""CRM-only Bitrix load diagnostic: two sequential cycles, no OpenAI.

Reuses production ``refresh_deal_control`` and ``collect_manager_trajectory``.
Does not start analysis jobs, transcription, or audio byte downloads.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from api.candidates import make_client
from api.deal_control import refresh_deal_control
from api.manager_trajectory import collect_manager_trajectory
from bitrix.client import BitrixReadOnlyClient, load_json
from bitrix.usage_trace import bitrix_trace_context
from bitrix.workspace import DEFAULT_AUDIO_MANIFEST_DIR, DEFAULT_RAW_DIR
from scripts.bitrix_usage_report import build_report, iter_events
from setup import LOGS_DIR, MSK_TZ, get_logger
from storage.rop_db import DEFAULT_DB_PATH, get_deal_control_scope, list_deal_control_deals


logger = get_logger(__file__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Два CRM-only Bitrix-прогона без OpenAI и без скачивания аудио",
    )
    parser.add_argument("--db-path", default=str(DEFAULT_DB_PATH))
    parser.add_argument("--sample-size", type=int, default=3)
    parser.add_argument("--skip-sample", action="store_true")
    parser.add_argument("--raw-dir", default=str(DEFAULT_RAW_DIR))
    parser.add_argument("--audio-dir", default=str(DEFAULT_AUDIO_MANIFEST_DIR))
    parser.add_argument("--usage-dir", default="")
    return parser.parse_args()


def _load_deal_fetch_module() -> Any:
    path = PROJECT_ROOT / "bitrix" / "deals" / "1_fetch_deals_context.py"
    spec = importlib.util.spec_from_file_location("bitrix_diag_deal_fetch", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Не удалось загрузить 1_fetch_deals_context.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _audio_helpers() -> Any:
    """Ленивый импорт: модуль аудио тянет локальные duration-helpers, не вызываем транскрибацию."""
    from bitrix.deals import download_deals_call_audio as audio

    return audio


def _iso(value: datetime) -> str:
    current = value if value.tzinfo is not None else value.replace(tzinfo=MSK_TZ)
    return current.astimezone(MSK_TZ).isoformat(timespec="seconds")


def _usage_report(usage_dir: Path, run_id: str) -> dict[str, Any]:
    today = date.today()
    return build_report(iter_events(usage_dir, today, today), start=today, end=today, run_id=run_id)


def _portfolio_snapshot(db_path: str | Path) -> dict[str, str]:
    return {
        str(item.get("deal_id") or ""): str(item.get("modified_at_crm") or "")
        for item in list_deal_control_deals(db_path, active_only=False)
        if str(item.get("deal_id") or "").strip()
    }


def _portfolio_delta(before: dict[str, str], after: dict[str, str]) -> dict[str, int]:
    before_ids = set(before)
    after_ids = set(after)
    changed = sum(
        1
        for deal_id in before_ids & after_ids
        if before.get(deal_id) != after.get(deal_id)
    )
    return {
        "checked": len(after_ids),
        "unchanged": len(before_ids & after_ids) - changed,
        "modified": changed,
        "appeared": len(after_ids - before_ids),
        "disappeared": len(before_ids - after_ids),
    }


def run_crm_only_cycle(
    client: BitrixReadOnlyClient,
    *,
    db_path: str | Path,
    run_id: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """One production-like CRM tick: deal-control then manager trajectory. No analysis."""
    started = datetime.now(MSK_TZ)
    current = now or started
    logger.info("CRM-only цикл %s: deal-control, затем manager trajectory.", run_id[-8:])
    before = _portfolio_snapshot(db_path)
    with bitrix_trace_context(run_id=run_id):
        client.trace_run_id = run_id
        sync_payload = refresh_deal_control(db_path=db_path, client=client, now=current)
        trajectory_payload = collect_manager_trajectory(client, db_path=db_path)
    after = _portfolio_snapshot(db_path)
    finished = datetime.now(MSK_TZ)
    logger.info(
        "CRM-only цикл %s завершён за %.1f с.",
        run_id[-8:],
        (finished - started).total_seconds(),
    )
    sync = sync_payload if isinstance(sync_payload, dict) else {}
    trajectory = trajectory_payload if isinstance(trajectory_payload, dict) else {}
    return {
        "run_id": run_id,
        "started_at": _iso(started),
        "finished_at": _iso(finished),
        "elapsed_ms": round((finished - started).total_seconds() * 1000, 3),
        "deal_control": {
            "sync_message": sync.get("sync_message"),
            "sync_error_count": len(sync.get("sync_errors") or []),
            "active_deals": len(sync.get("deals") or []),
            "portfolio": _portfolio_delta(before, after),
        },
        "trajectory": {
            "status": trajectory.get("status"),
            "period": trajectory.get("period") or {},
            "counts": trajectory.get("counts") or {},
            "error_count": len(trajectory.get("errors") or {}),
        },
    }


def select_sample_deal_ids(
    db_path: str | Path,
    *,
    raw_dir: Path,
    sample_size: int,
) -> list[str]:
    """Pick a small active-deal sample, preferring local call FILES / recheck window."""
    size = max(0, int(sample_size))
    if size == 0:
        return []
    audio = _audio_helpers()
    scored: list[tuple[int, str]] = []
    for item in list_deal_control_deals(db_path, active_only=True):
        deal_id = str(item.get("deal_id") or "").strip()
        if not deal_id:
            continue
        score = 1
        raw_path = raw_dir / f"deal_{deal_id}_context.json"
        if raw_path.exists():
            score += 2
            try:
                calls = audio.call_activities(load_json(raw_path))
            except (OSError, TypeError, ValueError):
                calls = []
            if calls:
                score += 2
            if any(item.get("FILES") for item in calls if isinstance(item, dict)):
                score += 3
            if any(audio.should_recheck_recording(item) for item in calls if isinstance(item, dict)):
                score += 4
        scored.append((score, deal_id))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [deal_id for _score, deal_id in scored[:size]]


def fetch_sample_contexts(
    client: BitrixReadOnlyClient,
    deal_ids: list[str],
    *,
    raw_dir: Path,
    run_id: str,
) -> dict[str, Any]:
    """Bitrix CRM fetch for the sample only; does not write reports or call LLM."""
    fetch = _load_deal_fetch_module()
    pipeline_map = PROJECT_ROOT / "crm_pipeline_map.json"
    stage_lookup = fetch.build_stage_lookup(pipeline_map)
    counts = {
        "requested": len(deal_ids),
        "with_previous_snapshot": 0,
        "incremental": 0,
        "full": 0,
        "errors": 0,
    }
    logger.info("Sample CRM fetch: %s сделок, без записи JSON и без LLM.", len(deal_ids))
    with bitrix_trace_context(run_id=run_id, component="other"):
        client.trace_run_id = run_id
        for deal_id in deal_ids:
            output_path = raw_dir / f"deal_{deal_id}_context.json"
            previous = None
            if output_path.exists():
                try:
                    loaded = load_json(output_path)
                except ValueError:
                    loaded = None
                if isinstance(loaded, dict):
                    previous = loaded
                    counts["with_previous_snapshot"] += 1
            try:
                bundle = fetch.fetch_deal_bundle(
                    client,
                    str(deal_id),
                    stage_lookup,
                    previous_bundle=previous,
                )
            except Exception:  # noqa: BLE001 - sample must not stop the diagnostic
                counts["errors"] += 1
                continue
            mode = str(((bundle or {}).get("sync") or {}).get("mode") or "")
            if mode == "incremental":
                counts["incremental"] += 1
            else:
                counts["full"] += 1
    return counts


def inspect_audio_metadata(
    client: BitrixReadOnlyClient,
    deal_ids: list[str],
    *,
    raw_dir: Path,
    audio_dir: Path,
    run_id: str,
) -> dict[str, Any]:
    """disk.file.get readiness/recheck only: no DOWNLOAD_URL GET and no transcription."""
    counts = {
        "deals": len(deal_ids),
        "missing_context": 0,
        "call_activities": 0,
        "empty_files": 0,
        "discovery_expired": 0,
        "recheck_candidates": 0,
        "skipped_stable": 0,
        "disk_file_get": 0,
        "size_grew": 0,
    }
    logger.info("Sample audio metadata/readiness: только disk.file.get, без скачивания.")
    audio = _audio_helpers()
    with bitrix_trace_context(run_id=run_id):
        client.trace_run_id = run_id
        for deal_id in deal_ids:
            raw_path = raw_dir / f"deal_{deal_id}_context.json"
            if not raw_path.exists():
                counts["missing_context"] += 1
                continue
            try:
                bundle = load_json(raw_path)
            except (OSError, TypeError, ValueError):
                counts["missing_context"] += 1
                continue
            manifest = audio.load_existing_manifest(audio_dir / f"deal_{deal_id}_call_audio_manifest.json")
            existing_downloads = audio.existing_downloads_by_activity(manifest)
            existing_transcriptions = audio.existing_transcriptions_by_activity(manifest)
            with bitrix_trace_context(component="audio_discovery"):
                calls = audio.call_activities(bundle)
            counts["call_activities"] += len(calls)
            for activity in calls:
                activity_id = str(activity.get("ID") or "")
                files = [item for item in (activity.get("FILES") or []) if isinstance(item, dict)]
                if not files:
                    if audio.audio_file_discovery_expired(activity):
                        counts["discovery_expired"] += 1
                    else:
                        counts["empty_files"] += 1
                    continue
                previous_downloads = existing_downloads.get(activity_id) or []
                previous_transcription = existing_transcriptions.get(activity_id)
                needs_recheck = audio.should_recheck_recording(activity) or (
                    not previous_downloads and not previous_transcription
                )
                if not needs_recheck:
                    counts["skipped_stable"] += 1
                    continue
                counts["recheck_candidates"] += 1
                file_ids = [str(item.get("id") or item.get("ID") or "") for item in files]
                if previous_transcription and not any(file_ids):
                    file_ids = [str(previous_transcription.get("source_file_id") or "")]
                with bitrix_trace_context(component="audio_readiness"):
                    for file_id in file_ids:
                        if not file_id:
                            continue
                        _download_url, disk_response = audio.file_download_url(client, file_id)
                        remote_size = audio.disk_file_size(disk_response)
                        counts["disk_file_get"] += 1
                        previous = next(
                            (
                                item
                                for item in previous_downloads
                                if str(item.get("file_id") or "") == file_id
                            ),
                            previous_downloads[0] if len(previous_downloads) == 1 else None,
                        )
                        previous_size = None
                        if previous is not None:
                            previous_size = previous.get("remote_size_bytes") or previous.get("size_bytes")
                        elif previous_transcription:
                            previous_size = previous_transcription.get("source_remote_size_bytes") or (
                                previous_transcription.get("source_size_bytes")
                            )
                        grew = False
                        try:
                            grew = remote_size is not None and previous_size is not None and int(remote_size) > int(previous_size)
                        except (TypeError, ValueError):
                            grew = False
                        if grew:
                            counts["size_grew"] += 1
                        if previous is not None:
                            audio.recording_readiness(
                                dict(previous),
                                activity,
                                previous=previous,
                                remote_size_bytes=remote_size,
                                size_changed=grew,
                            )
    return counts


def _pct_change(previous: float, current: float) -> float | None:
    if previous == 0:
        return None if current == 0 else None
    return round((current - previous) * 100.0 / previous, 1)


def compare_runs(run1: dict[str, Any], run2: dict[str, Any]) -> dict[str, Any]:
    usage1 = run1.get("usage") or {}
    usage2 = run2.get("usage") or {}
    keys = ("physical_http", "logical_commands", "batch_http", "batch_cmds", "duration_ms", "item_count")
    delta = {}
    for key in keys:
        left = float(usage1.get(key) or 0)
        right = float(usage2.get(key) or 0)
        delta[key] = {
            "run1": left,
            "run2": right,
            "diff": right - left,
            "pct": _pct_change(left, right),
        }
    avoidable = max(0.0, float(usage2.get("physical_http") or 0))
    changed = int(((run2.get("cycle") or {}).get("deal_control") or {}).get("portfolio", {}).get("modified") or 0)
    changed += int(((run2.get("cycle") or {}).get("trajectory") or {}).get("counts", {}).get("activities") or 0)
    return {
        "metrics": delta,
        "run2_changed_signals": changed,
        "run2_physical_http": avoidable,
        "note": (
            "RUN 2 шёл сразу после RUN 1. Почти все его HTTP при отсутствии CRM-изменений "
            "— кандидаты на сокращение, кроме overlap и обязательного audio recheck."
        ),
    }


def run_diagnostic(
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
    client: BitrixReadOnlyClient | None = None,
    usage_dir: Path | None = None,
    sample_size: int = 3,
    skip_sample: bool = False,
    raw_dir: Path | None = None,
    audio_dir: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    session_id = datetime.now(MSK_TZ).strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    isolated = usage_dir or (LOGS_DIR / "bitrix_diag" / session_id)
    isolated.mkdir(parents=True, exist_ok=True)
    previous_usage_dir = os.environ.get("BITRIX_USAGE_DAILY_DIR")
    os.environ["BITRIX_USAGE_DAILY_DIR"] = str(isolated)
    try:
        crm = client or make_client()
        raw_path = Path(raw_dir or DEFAULT_RAW_DIR)
        audio_path = Path(audio_dir or DEFAULT_AUDIO_MANIFEST_DIR)
        scope = get_deal_control_scope(db_path)
        pool_size = len(list_deal_control_deals(db_path, active_only=True))
        run1_id = f"diag_{session_id}_run1"
        run2_id = f"diag_{session_id}_run2"
        sample_id = f"diag_{session_id}_sample"
        cycle1 = run_crm_only_cycle(crm, db_path=db_path, run_id=run1_id, now=now)
        cycle2 = run_crm_only_cycle(crm, db_path=db_path, run_id=run2_id, now=now)
        sample_deal_ids: list[str] = []
        sample_fetch: dict[str, Any] | None = None
        sample_audio: dict[str, Any] | None = None
        if not skip_sample and sample_size > 0:
            sample_deal_ids = select_sample_deal_ids(db_path, raw_dir=raw_path, sample_size=sample_size)
            sample_fetch = fetch_sample_contexts(crm, sample_deal_ids, raw_dir=raw_path, run_id=sample_id)
            sample_audio = inspect_audio_metadata(
                crm,
                sample_deal_ids,
                raw_dir=raw_path,
                audio_dir=audio_path,
                run_id=sample_id,
            )
        run1 = {"cycle": cycle1, "usage": _usage_report(isolated, run1_id)}
        run2 = {"cycle": cycle2, "usage": _usage_report(isolated, run2_id)}
        sample = None
        if sample_fetch is not None:
            sample = {
                "sample_size": len(sample_deal_ids),
                "pool_size": pool_size,
                "fetch": sample_fetch,
                "audio": sample_audio,
                "usage": _usage_report(isolated, sample_id),
            }
            if pool_size and sample["sample_size"]:
                factor = pool_size / sample["sample_size"]
                sample["extrapolated_pool"] = {
                    "physical_http": round(float((sample["usage"] or {}).get("physical_http") or 0) * factor, 1),
                    "logical_commands": round(float((sample["usage"] or {}).get("logical_commands") or 0) * factor, 1),
                    "disk_file_get": round(float((sample_audio or {}).get("disk_file_get") or 0) * factor, 1),
                }
        summary = {
            "session_id": session_id,
            "usage_dir": str(isolated),
            "configured": bool(scope.get("configured")),
            "active_deal_count": pool_size,
            "manager_count": len(scope.get("manager_ids") or []),
            "run1": run1,
            "run2": run2,
            "comparison": compare_runs(run1, run2),
            "sample": sample,
        }
        summary_path = isolated / "summary.json"
        summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        summary["summary_path"] = str(summary_path)
        return summary
    finally:
        if previous_usage_dir is None:
            os.environ.pop("BITRIX_USAGE_DAILY_DIR", None)
        else:
            os.environ["BITRIX_USAGE_DAILY_DIR"] = previous_usage_dir


def render_summary(summary: dict[str, Any]) -> str:
    run1 = (summary.get("run1") or {}).get("usage") or {}
    run2 = (summary.get("run2") or {}).get("usage") or {}
    comparison = summary.get("comparison") or {}
    lines = [
        f"Bitrix CRM-only diagnostic {summary.get('session_id')}",
        (
            f"RUN 1: HTTP {run1.get('physical_http')}; logical {run1.get('logical_commands')}; "
            f"batch {run1.get('batch_http')}/{run1.get('batch_cmds')}; "
            f"{float(run1.get('duration_ms') or 0) / 1000:.1f} с"
        ),
        (
            f"RUN 2: HTTP {run2.get('physical_http')}; logical {run2.get('logical_commands')}; "
            f"batch {run2.get('batch_http')}/{run2.get('batch_cmds')}; "
            f"{float(run2.get('duration_ms') or 0) / 1000:.1f} с"
        ),
    ]
    metrics = comparison.get("metrics") or {}
    http = metrics.get("physical_http") or {}
    if http.get("pct") is not None:
        lines.append(f"Разница HTTP RUN2 vs RUN1: {http.get('diff')} ({http.get('pct')}%)")
    sample = summary.get("sample")
    if sample:
        extra = sample.get("extrapolated_pool") or {}
        lines.append(
            f"Sample {sample.get('sample_size')} из пула {sample.get('pool_size')}: "
            f"HTTP {((sample.get('usage') or {}).get('physical_http'))}; "
            f"disk.file.get {(sample.get('audio') or {}).get('disk_file_get')}; "
            f"экстраполяция HTTP {extra.get('physical_http')}"
        )
    lines.append(f"Сводка: {summary.get('summary_path') or summary.get('usage_dir')}")
    return "\n".join(lines)


def main() -> None:
    from dotenv import load_dotenv

    load_dotenv()
    args = parse_args()
    summary = run_diagnostic(
        db_path=args.db_path,
        usage_dir=Path(args.usage_dir) if args.usage_dir else None,
        sample_size=args.sample_size,
        skip_sample=args.skip_sample,
        raw_dir=Path(args.raw_dir),
        audio_dir=Path(args.audio_dir),
    )
    print(render_summary(summary))


if __name__ == "__main__":
    main()
