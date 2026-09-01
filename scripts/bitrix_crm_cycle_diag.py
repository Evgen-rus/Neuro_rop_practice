"""CRM-only Bitrix load diagnostic: two sequential cycles, no OpenAI.

Reuses production ``refresh_deal_control`` and ``collect_manager_trajectory``.
Does not start analysis jobs, transcription, or audio byte downloads.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import uuid
from collections import defaultdict
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Iterator

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
        description="Bitrix diagnostic: CRM-only по умолчанию, --full-cycle для полного automatic path",
    )
    parser.add_argument("--db-path", default=str(DEFAULT_DB_PATH))
    parser.add_argument("--sample-size", type=int, default=3)
    parser.add_argument("--skip-sample", action="store_true")
    parser.add_argument("--raw-dir", default=str(DEFAULT_RAW_DIR))
    parser.add_argument("--audio-dir", default=str(DEFAULT_AUDIO_MANIFEST_DIR))
    parser.add_argument("--usage-dir", default="")
    parser.add_argument(
        "--full-cycle",
        action="store_true",
        help="Два полных production daytime cycle с audio/OpenAI; ждать все analysis jobs",
    )
    parser.add_argument(
        "--single-cycle",
        action="store_true",
        help="С --full-cycle: один cycle (RUN 2), не запускать повторный RUN 1",
    )
    parser.add_argument(
        "--second-pass-done-only",
        action="store_true",
        help="С --full-cycle: jobs только по сделкам, уже done в последнем automatic run",
    )
    parser.add_argument(
        "--one-automatic-cycle",
        action="store_true",
        help="Один штатный automatic cycle рядом с работающим API; сохранить изолированный summary.json",
    )
    parser.add_argument("--skip-preflight", action="store_true", help="Не проверять API/git/ffmpeg (только для тестов)")
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


FULL_CYCLE_ENV_KEYS = (
    "BITRIX_USAGE_DAILY_DIR",
    "BITRIX_TRACE_RUN_ID",
    "BITRIX_TRACE_COMPONENT",
    "BITRIX_TRACE_ALLOW_ENTITY_ID",
    "BITRIX_TRACE_ENTITY_ID",
    "BITRIX_DENY_WRITE_METHODS",
    "OPENAI_USAGE_DAILY_DIR",
    "OPENAI_USAGE_TRACE_PATH",
    "SPEND_DIARY_DIR",
)
ANALYSIS_COMPONENTS = frozenset(
    {
        "per_deal_context",
        "timeline",
        "stage_history",
        "task_history",
        "invoice",
        "entity_get",
        "product_rows",
        "audio_discovery",
        "audio_readiness",
        "disk_file_get",
    }
)
WRITE_METHOD_RE = re.compile(
    r"""(?:\.call|\.safe_call|\.safe_list_all|\.list_all)\(\s*['"]([^'"]+)['"]""",
)
WRITE_PARTS = frozenset(
    {"add", "update", "delete", "move", "complete", "start", "pause", "renew", "share", "upload", "copy", "setstatus"}
)
_TRUTHY = frozenset({"1", "true", "yes", "on"})


@contextmanager
def _temporary_env(updates: dict[str, str]) -> Iterator[None]:
    previous = {key: os.environ.get(key) for key in updates}
    os.environ.update(updates)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _progress(message: str) -> None:
    print(message, flush=True)
    logger.info("%s", message)


def previous_done_deal_ids(db_path: str | Path) -> list[str]:
    """Entity IDs that already reached terminal done in the latest automatic run."""
    from storage.rop_db import get_latest_automatic_analysis_run, list_automatic_analysis_items

    latest = get_latest_automatic_analysis_run(db_path)
    if not latest:
        return []
    items = list_automatic_analysis_items(db_path, int(latest["id"]))
    done: list[str] = []
    seen: set[str] = set()
    for item in items:
        entity_id = str(item.get("entity_id") or "").strip()
        if not entity_id or entity_id in seen:
            continue
        if str(item.get("processing_status") or "") != "done":
            continue
        seen.add(entity_id)
        done.append(entity_id)
    return done


def cycle_fn_for_second_pass(allowed_ids: list[str]) -> Callable[..., dict[str, Any]]:
    """Keep deal-control/trajectory on the full pool; enqueue jobs only for allowed deals."""
    allowed = {str(entity_id).strip() for entity_id in allowed_ids if str(entity_id).strip()}

    def cycle_fn(*, db_path: str | Path, trigger: str, **kwargs: Any) -> dict[str, Any]:
        from api.daytime_cycle import _analyze_work_pool, run_daytime_cycle

        del kwargs

        def analyze_fn(**analyze_kwargs: Any) -> dict[str, Any]:
            pool = [str(entity_id) for entity_id in (analyze_kwargs.get("deal_ids") or [])]
            filtered = [entity_id for entity_id in pool if entity_id in allowed]
            _progress(
                "Second-pass jobs: %s из %s активных сделок (остальные не завершили RUN 1)."
                % (len(filtered), len(pool))
            )
            return _analyze_work_pool(
                db_path=analyze_kwargs["db_path"],
                deal_ids=filtered,
                started_at=analyze_kwargs["started_at"],
                trigger=str(analyze_kwargs.get("trigger") or trigger),
            )

        return run_daytime_cycle(db_path=db_path, trigger=trigger, analyze_fn=analyze_fn)

    return cycle_fn


def _load_previous_summary(isolated: Path) -> dict[str, Any] | None:
    path = isolated / "summary.json"
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _number_stats(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"count": 0, "mean": None, "median": None, "min": None, "max": None}
    ordered = sorted(values)
    count = len(ordered)
    mid = count // 2
    median = ordered[mid] if count % 2 else (ordered[mid - 1] + ordered[mid]) / 2
    return {
        "count": count,
        "mean": round(sum(ordered) / count, 2),
        "median": round(float(median), 2),
        "min": round(float(ordered[0]), 2),
        "max": round(float(ordered[-1]), 2),
    }


def _http_distribution(values: list[int]) -> dict[str, int]:
    buckets = {"0-5": 0, "6-10": 0, "11-15": 0, "16-20": 0, "21-30": 0, "31+": 0}
    for value in values:
        if value <= 5:
            buckets["0-5"] += 1
        elif value <= 10:
            buckets["6-10"] += 1
        elif value <= 15:
            buckets["11-15"] += 1
        elif value <= 20:
            buckets["16-20"] += 1
        elif value <= 30:
            buckets["21-30"] += 1
        else:
            buckets["31+"] += 1
    return buckets


def scan_production_bitrix_write_calls(root: Path | None = None) -> list[dict[str, str]]:
    """Static scan of production call sites; tests/ and scripts/ are ignored."""
    base = root or PROJECT_ROOT
    hits: list[dict[str, str]] = []
    skip_parts = {".venv", "venv", "tests", "scripts", "node_modules", "__pycache__"}
    for path in sorted(base.rglob("*.py")):
        if any(part in skip_parts for part in path.parts):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for match in WRITE_METHOD_RE.finditer(text):
            method = match.group(1).strip()
            parts = {item.lower() for item in method.replace("-", ".").split(".") if item}
            if parts & WRITE_PARTS:
                hits.append({"path": str(path.relative_to(base)).replace("\\", "/"), "method": method})
    return hits


def _api_health(host: str = "127.0.0.1", port: int = 8000) -> dict[str, Any] | None:
    try:
        with socket.create_connection((host, port), timeout=1):
            pass
    except OSError:
        return None
    try:
        import urllib.request

        with urllib.request.urlopen(f"http://{host}:{port}/api/health", timeout=2) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 - closed port is success; opaque failure still blocks
        return {"ok": True, "service": "unknown-listener"}
    return payload if isinstance(payload, dict) else {"ok": True, "service": "unknown-listener"}


def preflight_full_cycle(
    *,
    db_path: str | Path,
    skip_git: bool = False,
    allow_running_api: bool = False,
) -> dict[str, Any]:
    problems: list[str] = []
    if not skip_git:
        try:
            branch = subprocess.check_output(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=str(PROJECT_ROOT),
                text=True,
            ).strip()
        except (OSError, subprocess.CalledProcessError):
            branch = ""
            problems.append("Не удалось определить git-ветку.")
        if branch and branch != "main":
            problems.append(f"Ожидалась ветка main, сейчас {branch}.")
    health = _api_health()
    if health is not None and not allow_running_api:
        service = str(health.get("service") or "")
        if service == "rop-assistant-api" or health.get("ok"):
            problems.append(
                "Локальный API на 127.0.0.1:8000 отвечает. Остановите uvicorn до полного diagnostic cycle."
            )
    writes = scan_production_bitrix_write_calls()
    if writes:
        problems.append(
            "Статический скан нашёл Bitrix write-методы: "
            + "; ".join(f"{item['path']}:{item['method']}" for item in writes[:8])
        )
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        problems.append("В PATH нет ffmpeg/ffprobe — транскрибация текущего production path сломается.")
    if not os.getenv("BITRIX_WEBHOOK_URL", "").strip():
        problems.append("BITRIX_WEBHOOK_URL пуст.")
    if not os.getenv("OPENAI_API_KEY", "").strip():
        problems.append("OPENAI_API_KEY пуст.")
    scope = get_deal_control_scope(db_path)
    if not scope.get("configured"):
        problems.append("Deal-control scope не настроен.")
    return {"ok": not problems, "problems": problems, "write_hits": writes}


def wait_until_automatic_run_done(
    db_path: str | Path,
    automatic_run_id: int,
    *,
    wait_job_fn: Callable[..., Any] | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    poll_seconds: float = 2.0,
    max_wait_seconds: float | None = None,
) -> dict[str, Any]:
    from api.jobs import wait_for_job
    from storage.rop_db import get_latest_automatic_analysis_run, list_automatic_analysis_items

    waiter = wait_job_fn or wait_for_job
    started = time.monotonic()
    seen_job_ids: set[str] = set()
    while True:
        items = list_automatic_analysis_items(db_path, automatic_run_id)
        for item in items:
            job_id = str(item.get("job_id") or "").strip()
            if not job_id or job_id in seen_job_ids:
                continue
            _progress(f"Ожидаю analysis job {job_id} (сделка {item.get('entity_id')}).")
            try:
                waiter(job_id, timeout_seconds=None)
            except Exception as error:  # noqa: BLE001 - SQLite remains the source of terminal status
                logger.warning("wait_for_job(%s): %s", job_id, type(error).__name__)
            seen_job_ids.add(job_id)
        items = list_automatic_analysis_items(db_path, automatic_run_id)
        unfinished = [
            item
            for item in items
            if str(item.get("processing_status") or "") not in {"done", "error"}
        ]
        if not unfinished:
            latest = get_latest_automatic_analysis_run(db_path) or {}
            run_status = str(latest.get("status") or "") if int(latest.get("id") or 0) == int(automatic_run_id) else "done"
            return {"items": items, "job_ids": sorted(seen_job_ids), "run_status": run_status or "done"}
        if max_wait_seconds is not None and time.monotonic() - started >= max_wait_seconds:
            raise TimeoutError(f"Automatic run {automatic_run_id} не завершился за {max_wait_seconds:.0f} с")
        sleep_fn(max(0.05, float(poll_seconds)))


def _iter_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as stream:
        for line in stream:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                rows.append(value)
    return rows


def _collect_bitrix_events(usage_dir: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    if not usage_dir.exists():
        return events
    for path in sorted(usage_dir.glob("*.jsonl")):
        events.extend(_iter_jsonl(path))
    return events


def _per_deal_bitrix(events: list[dict[str, Any]]) -> dict[str, Any]:
    by_deal: dict[str, dict[str, Any]] = {}
    analysis_events = [
        event
        for event in events
        if str(event.get("component") or "") in ANALYSIS_COMPONENTS or event.get("entity_id")
    ]
    for event in analysis_events:
        deal_id = str(event.get("entity_id") or "").strip()
        if not deal_id:
            continue
        row = by_deal.setdefault(
            deal_id,
            {"physical_http": 0, "duration_ms": 0.0, "empty": 0, "methods": defaultdict(int), "components": defaultdict(int)},
        )
        row["physical_http"] += 1
        row["duration_ms"] += float(event.get("duration_ms") or 0)
        row["empty"] += int(bool(event.get("empty")))
        row["methods"][str(event.get("method") or "unknown")] += 1
        row["components"][str(event.get("component") or "other")] += 1
    http_values = [int(item["physical_http"]) for item in by_deal.values()]
    ranked = sorted(
        (
            {
                "deal_id": deal_id,
                "physical_http": item["physical_http"],
                "duration_ms": round(float(item["duration_ms"]), 3),
                "empty": item["empty"],
                "methods": dict(item["methods"]),
                "components": dict(item["components"]),
            }
            for deal_id, item in by_deal.items()
        ),
        key=lambda item: (-int(item["physical_http"]), item["deal_id"]),
    )
    return {
        "deal_count": len(by_deal),
        "http_stats": _number_stats([float(value) for value in http_values]),
        "distribution": _http_distribution(http_values),
        "top_http": ranked[:10],
        "all": ranked,
    }


def _method_empty_cost(events: list[dict[str, Any]], method: str) -> dict[str, Any]:
    matched = [event for event in events if str(event.get("method") or "") == method]
    empty = [event for event in matched if event.get("empty")]
    return {
        "method": method,
        "http": len(matched),
        "empty_http": len(empty),
        "item_count": sum(int(event.get("item_count") or 0) for event in matched),
        "duration_ms": round(sum(float(event.get("duration_ms") or 0) for event in matched), 3),
    }


def collect_audio_metrics(deal_ids: list[str], *, raw_dir: Path, audio_dir: Path) -> dict[str, Any]:
    audio = _audio_helpers()
    counts = {
        "deals": len(deal_ids),
        "call_activities": 0,
        "empty_files": 0,
        "discovery_active": 0,
        "discovery_expired": 0,
        "recheck_candidates": 0,
        "downloaded": 0,
        "already_existed": 0,
        "grew": 0,
        "transcribed_current": 0,
        "had_current_transcript": 0,
        "retranscribe_growth": 0,
        "downloaded_bytes": 0,
        "no_files_in_crm_activity": 0,
        "no_files_check_expired": 0,
        "statuses": {},
        "download_statuses": {},
    }
    for deal_id in deal_ids:
        manifest_path = audio_dir / f"deal_{deal_id}_call_audio_manifest.json"
        workspace_manifest = (
            PROJECT_ROOT / "reports" / "rop_assistant" / "deals" / f"deal_{deal_id}" / "audio"
            / f"deal_{deal_id}_call_audio_manifest.json"
        )
        path = manifest_path if manifest_path.exists() else workspace_manifest
        manifest = audio.load_existing_manifest(path) if path.exists() else {"calls": []}
        summary = audio.summarize_manifest(manifest)
        for key, value in (summary.get("statuses") or {}).items():
            counts["statuses"][key] = counts["statuses"].get(key, 0) + int(value)
        for key, value in (summary.get("download_statuses") or {}).items():
            counts["download_statuses"][key] = counts["download_statuses"].get(key, 0) + int(value)
        raw_path = raw_dir / f"deal_{deal_id}_context.json"
        calls: list[dict[str, Any]] = []
        if raw_path.exists():
            try:
                calls = audio.call_activities(load_json(raw_path))
            except (OSError, TypeError, ValueError):
                calls = []
        counts["call_activities"] += len(calls)
        for activity in calls:
            files = [item for item in (activity.get("FILES") or []) if isinstance(item, dict)]
            if not files:
                counts["empty_files"] += 1
                if audio.audio_file_discovery_expired(activity):
                    counts["discovery_expired"] += 1
                else:
                    counts["discovery_active"] += 1
            if audio.should_recheck_recording(activity):
                counts["recheck_candidates"] += 1
        for call in manifest.get("calls") or []:
            status = str(call.get("status") or "")
            if status == "no_files_in_crm_activity":
                counts["no_files_in_crm_activity"] += 1
            if status == "no_files_check_expired":
                counts["no_files_check_expired"] += 1
            transcription = call.get("transcription") if isinstance(call.get("transcription"), dict) else {}
            if transcription.get("status") == "transcribed_and_purged":
                counts["had_current_transcript"] += 1
            if transcription.get("status") == "stale_source_grew":
                counts["retranscribe_growth"] += 1
            for item in call.get("downloads") or []:
                item_status = str(item.get("status") or "")
                size = int(item.get("size_bytes") or 0) if str(item.get("size_bytes") or "").isdigit() or isinstance(item.get("size_bytes"), int) else 0
                try:
                    size = int(item.get("size_bytes") or 0)
                except (TypeError, ValueError):
                    size = 0
                if item_status in {"downloaded", "redownloaded_grown_file"}:
                    counts["downloaded"] += 1
                    counts["downloaded_bytes"] += max(0, size)
                if item_status == "already_downloaded":
                    counts["already_existed"] += 1
                if item_status == "redownloaded_grown_file":
                    counts["grew"] += 1
    return counts


def collect_openai_metrics(usage_path: Path, events_dir: Path) -> dict[str, Any]:
    events = _iter_jsonl(usage_path)
    if not events:
        daily = events_dir / date.today().isoformat()
        # also read spend diary events
    spend_events: list[dict[str, Any]] = []
    for path in sorted(events_dir.glob("*.events.jsonl")):
        spend_events.extend(_iter_jsonl(path))
    for path in sorted((events_dir / "batches").glob("*.jsonl")) if (events_dir / "batches").exists() else []:
        spend_events.extend(_iter_jsonl(path))
    by_call: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "requests": 0,
            "errors": 0,
            "retries": 0,
            "input_tokens": 0,
            "cached_input_tokens": 0,
            "output_tokens": 0,
            "estimated_cost_rub": 0.0,
            "elapsed_seconds": 0.0,
            "models": defaultdict(int),
        }
    )
    totals = {
        "requests": 0,
        "errors": 0,
        "retries": 0,
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "output_tokens": 0,
        "estimated_cost_rub": 0.0,
        "elapsed_seconds": 0.0,
        "by_entity": defaultdict(lambda: {"requests": 0, "estimated_cost_rub": 0.0, "input_tokens": 0}),
    }
    source_events = events or spend_events
    for event in source_events:
        call_type = str(event.get("call_type") or event.get("kind") or "other")
        bucket = by_call[call_type]
        bucket["requests"] += 1
        totals["requests"] += 1
        status = str(event.get("status") or "success")
        if status not in {"success", "ok"}:
            bucket["errors"] += 1
            totals["errors"] += 1
        retries = int(event.get("transport_retry_count") or 0)
        bucket["retries"] += retries
        totals["retries"] += retries
        input_tokens = int(event.get("input_tokens") or 0)
        cached = int(event.get("cached_input_tokens") or 0)
        output_tokens = int(event.get("output_tokens") or 0)
        cost = float(event.get("estimated_cost_rub") or 0)
        elapsed = float(event.get("latency_seconds") or 0)
        bucket["input_tokens"] += input_tokens
        bucket["cached_input_tokens"] += cached
        bucket["output_tokens"] += output_tokens
        bucket["estimated_cost_rub"] += cost
        bucket["elapsed_seconds"] += elapsed
        totals["input_tokens"] += input_tokens
        totals["cached_input_tokens"] += cached
        totals["output_tokens"] += output_tokens
        totals["estimated_cost_rub"] += cost
        totals["elapsed_seconds"] += elapsed
        model = str(event.get("model") or "-")
        bucket["models"][model] += 1
        entity_id = str(event.get("entity_id") or "").strip()
        if entity_id:
            entity = totals["by_entity"][entity_id]
            entity["requests"] += 1
            entity["estimated_cost_rub"] += cost
            entity["input_tokens"] += input_tokens
    operations = {
        key: {
            **value,
            "models": dict(value["models"]),
            "estimated_cost_rub": round(float(value["estimated_cost_rub"]), 4),
            "elapsed_seconds": round(float(value["elapsed_seconds"]), 3),
        }
        for key, value in by_call.items()
    }
    top_cost = sorted(
        (
            {
                "deal_id": deal_id,
                "requests": item["requests"],
                "estimated_cost_rub": round(float(item["estimated_cost_rub"]), 4),
                "input_tokens": item["input_tokens"],
            }
            for deal_id, item in totals["by_entity"].items()
        ),
        key=lambda item: (-float(item["estimated_cost_rub"]), item["deal_id"]),
    )[:10]
    return {
        "requests": totals["requests"],
        "errors": totals["errors"],
        "retries": totals["retries"],
        "input_tokens": totals["input_tokens"],
        "cached_input_tokens": totals["cached_input_tokens"],
        "output_tokens": totals["output_tokens"],
        "estimated_cost_rub": round(float(totals["estimated_cost_rub"]), 4),
        "elapsed_seconds": round(float(totals["elapsed_seconds"]), 3),
        "operations": operations,
        "top_cost": top_cost,
        "transcriptions": int((operations.get("transcription") or {}).get("requests") or 0)
        + int((operations.get("transcription_voice") or {}).get("requests") or 0),
    }


def collect_analysis_metrics(
    items: list[dict[str, Any]],
    *,
    per_deal_bitrix: dict[str, Any],
) -> dict[str, Any]:
    counts = {"checked": len(items), "full": 0, "mini": 0, "skip": 0, "error": 0, "jobs": 0}
    fetch_and_skip: list[str] = []
    fetch_ids = {str(item.get("deal_id") or "") for item in per_deal_bitrix.get("all") or []}
    durations: list[float] = []
    by_decision: dict[str, list[str]] = {"full": [], "mini": [], "skip": [], "error": []}
    plan_modes: dict[str, int] = defaultdict(int)
    plan_reasons: dict[str, int] = defaultdict(int)
    item_metrics: list[dict[str, Any]] = []
    per_deal_rows = {
        str(row.get("deal_id") or ""): row
        for row in (per_deal_bitrix.get("all") or [])
        if str(row.get("deal_id") or "").strip()
    }
    for item in items:
        decision = str(item.get("decision_status") or "")
        entity_id = str(item.get("entity_id") or "")
        if item.get("job_id"):
            counts["jobs"] += 1
        if decision in by_decision:
            counts[decision] += 1
            by_decision[decision].append(entity_id)
        elif str(item.get("processing_status") or "") == "error":
            counts["error"] += 1
            by_decision["error"].append(entity_id)
        if decision == "skip" and entity_id in fetch_ids:
            fetch_and_skip.append(entity_id)
        sync_mode = str(item.get("sync_mode") or "unknown")
        sync_reasons = [str(reason) for reason in (item.get("sync_reasons") or []) if str(reason)]
        plan_modes[sync_mode] += 1
        for reason in sync_reasons:
            plan_reasons[reason] += 1
        started = item.get("started_at")
        finished = item.get("finished_at")
        duration_seconds: float | None = None
        if started and finished:
            try:
                start_dt = datetime.fromisoformat(str(started).replace("Z", "+00:00"))
                end_dt = datetime.fromisoformat(str(finished).replace("Z", "+00:00"))
                duration_seconds = max(0.0, (end_dt - start_dt).total_seconds())
                durations.append(duration_seconds)
            except ValueError:
                pass
        bitrix = per_deal_rows.get(entity_id) or {}
        item_metrics.append(
            {
                "deal_id": entity_id,
                "sync_mode": sync_mode,
                "sync_reasons": sync_reasons,
                "decision": decision or None,
                "processing_status": item.get("processing_status"),
                "duration_seconds": round(duration_seconds, 3) if duration_seconds is not None else None,
                "bitrix_physical_http": int(bitrix.get("physical_http") or 0),
                "bitrix_duration_ms": round(float(bitrix.get("duration_ms") or 0), 3),
            }
        )
    openai_deals = counts["full"]  # compact FULL includes incremental LLM
    return {
        "active_deals": counts["checked"],
        "jobs_enqueued": counts["jobs"],
        "crm_context_fetch_deals": len(fetch_ids),
        "full": counts["full"],
        "mini": counts["mini"],
        "skip": counts["skip"],
        "error": counts["error"],
        "no_meaningful_change": counts["skip"],
        "heavy_fetch_despite_no_change": len(fetch_and_skip),
        "reached_openai_full": openai_deals,
        "fetch_before_decision": {
            "enqueued": counts["jobs"],
            "fetched": len(fetch_ids),
            "skipped_after_fetch": len(fetch_and_skip),
            "answer": "B" if counts["jobs"] and len(fetch_ids) >= max(1, counts["jobs"] - counts["error"]) else "unknown",
        },
        "duration_seconds": _number_stats(durations),
        "sync_plan": {
            "modes": dict(sorted(plan_modes.items())),
            "reasons": dict(sorted(plan_reasons.items())),
        },
        "items": item_metrics,
        "ids": {key: value for key, value in by_decision.items() if value},
    }


def _method_http(usage: dict[str, Any], method: str) -> int:
    for row in usage.get("methods") or []:
        if row.get("method") == method:
            return int(row.get("physical_http") or 0)
    return 0


def compare_full_runs(run1: dict[str, Any], run2: dict[str, Any]) -> dict[str, Any]:
    usage1 = run1.get("bitrix") or {}
    usage2 = run2.get("bitrix") or {}
    openai1 = run1.get("openai") or {}
    openai2 = run2.get("openai") or {}
    audio1 = run1.get("audio") or {}
    audio2 = run2.get("audio") or {}
    analysis1 = run1.get("analysis") or {}
    analysis2 = run2.get("analysis") or {}

    def row(name: str, left: Any, right: Any) -> dict[str, Any]:
        left_n = float(left or 0)
        right_n = float(right or 0)
        return {
            "metric": name,
            "run1": round(left_n, 3),
            "run2": round(right_n, 3),
            "diff": round(right_n - left_n, 3),
        }

    return {
        "table": [
            row("Bitrix physical HTTP", usage1.get("physical_http"), usage2.get("physical_http")),
            row("Bitrix logical commands", usage1.get("logical_commands"), usage2.get("logical_commands")),
            row("Batch commands", usage1.get("batch_cmds"), usage2.get("batch_cmds")),
            row("Bitrix REST seconds", float(usage1.get("duration_ms") or 0) / 1000, float(usage2.get("duration_ms") or 0) / 1000),
            row("Audio downloads", audio1.get("downloaded"), audio2.get("downloaded")),
            row("Downloaded MB", float(audio1.get("downloaded_bytes") or 0) / 1_000_000, float(audio2.get("downloaded_bytes") or 0) / 1_000_000),
            row("disk.file.get", _method_http(usage1, "disk.file.get"), _method_http(usage2, "disk.file.get")),
            row("Transcriptions", openai1.get("transcriptions"), openai2.get("transcriptions")),
            row("OpenAI requests", openai1.get("requests"), openai2.get("requests")),
            row("Input tokens", openai1.get("input_tokens"), openai2.get("input_tokens")),
            row("Output tokens", openai1.get("output_tokens"), openai2.get("output_tokens")),
            row("OpenAI ₽", openai1.get("estimated_cost_rub"), openai2.get("estimated_cost_rub")),
            row("FULL", analysis1.get("full"), analysis2.get("full")),
            row("MINI", analysis1.get("mini"), analysis2.get("mini")),
            row("SKIP", analysis1.get("skip"), analysis2.get("skip")),
            row("Total wall time", run1.get("elapsed_seconds"), run2.get("elapsed_seconds")),
        ]
    }


def _top_waste(events: list[dict[str, Any]], usage: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in usage.get("by_component_method") or usage.get("components") or []:
        rows.append(
            {
                "operation": f"{item.get('component') or 'other'}:{item.get('method') or item.get('component')}",
                "times": int(item.get("physical_http") or 0),
                "http": int(item.get("physical_http") or 0),
                "seconds": round(float(item.get("duration_ms") or 0) / 1000, 2),
                "rows": int(item.get("item_count") or 0),
                "empty": int(item.get("empty") or 0),
            }
        )
    if not rows:
        by_pair: dict[tuple[str, str], dict[str, Any]] = defaultdict(
            lambda: {"times": 0, "http": 0, "seconds": 0.0, "rows": 0, "empty": 0}
        )
        for event in events:
            key = (str(event.get("component") or "other"), str(event.get("method") or "unknown"))
            bucket = by_pair[key]
            bucket["times"] += 1
            bucket["http"] += 1
            bucket["seconds"] += float(event.get("duration_ms") or 0) / 1000
            bucket["rows"] += int(event.get("item_count") or 0)
            bucket["empty"] += int(bool(event.get("empty")))
        rows = [
            {"operation": f"{component}:{method}", **{**values, "seconds": round(float(values["seconds"]), 2)}}
            for (component, method), values in by_pair.items()
        ]
    rows.sort(key=lambda item: (-int(item["http"]), -float(item["seconds"])))
    notes = {
        "invoice:crm.invoice.list": (
            "часто пустой; нужен, пока счета остаются частью deal evidence",
            "не отключать без проверки invoice_attempts в анализе",
        ),
        "entity_get:crm.company.get": (
            "компания редко меняется каждый цикл",
            "не потерять COMPANY_ID и реквизиты в FULL-контексте",
        ),
        "timeline:crm.timeline.comment.list": (
            "created_after сейчас отбрасывается, история всегда полная",
            "не сломать внутренние комментарии и Max voice discovery",
        ),
        "stage_history:crm.stagehistory.list": (
            "полная история стадий каждый job",
            "семантика стадий и change detection зависят от полного списка",
        ),
        "product_rows:crm.deal.productrows.get": (
            "строки товаров читаются каждый job",
            "состав сделки нужен FULL-анализу",
        ),
        "deal_control:crm.deal.list": (
            "полный rescan рабочего пула без DATE_MODIFY",
            "не потерять появление/исчезновение активных сделок",
        ),
        "deal_control:crm.activity.list": (
            "три bulk-выборки активностей в каждом tick",
            "open tasks и communications_today зависят от этого",
        ),
        "per_deal_context:crm.activity.list": (
            "incremental, но всё равно на каждую сделку до decision",
            "FILES/COMMUNICATIONS нужны audio discovery",
        ),
        "disk_file_get:disk.file.get": (
            "size check growing recordings",
            "не упрощать 5-day discovery и evening→morning recheck",
        ),
        "manager_presence:user.get": (
            "presence каждый trajectory tick, в UI дня не показывается",
            "не удалять, пока admin trajectory не подтвердит ненужность",
        ),
    }
    result = []
    for item in rows[:10]:
        why, keep = notes.get(item["operation"], ("повтор без новых данных на RUN 2", "проверить контракт потребителя перед сокращением"))
        result.append({**item, "why_maybe_extra": why, "must_not_break": keep})
    return result


def render_full_cycle_markdown(summary: dict[str, Any]) -> str:
    run1 = summary.get("run1") or {}
    run2 = summary.get("run2") or {}
    comparison = summary.get("comparison") or {}
    analysis1 = run1.get("analysis") or {}
    fetch = analysis1.get("fetch_before_decision") or {}
    lines = [
        "# Полный automatic cycle — factual report",
        "",
        f"Сессия: `{summary.get('session_id')}`",
        f"Артефакты: `{summary.get('usage_dir')}`",
        "",
        "## A. Executive summary",
        "",
    ]
    for item in summary.get("executive_summary") or []:
        lines.append(f"- {item}")
    lines.extend(
        [
            "",
            "## B. Что реально делает один automatic cycle сейчас",
            "",
            "1. `run_daytime_cycle` → `refresh_deal_control` (полный `crm.deal.list` рабочего среза).",
            "2. `collect_manager_trajectory` (incremental core + supplementary full-history для затронутых + presence).",
            "3. `_analyze_work_pool` ставит **каждую** активную сделку в automatic job (`force_llm=False`, audio+transcribe on).",
            "4. Job subprocess `run_rop_assistant.py`: pipeline fetch+audio → `transcribe_missing_audio` → `analyze_deal_if_changed`.",
            "5. Decision engine выбирает FULL / MINI / skip **после** тяжёлого fetch.",
            "",
            "## C. RUN 1 vs RUN 2",
            "",
            "| Метрика | RUN 1 | RUN 2 | изменение |",
            "| --- | ---: | ---: | ---: |",
        ]
    )
    for row in comparison.get("table") or []:
        lines.append(
            f"| {row.get('metric')} | {row.get('run1')} | {row.get('run2')} | {row.get('diff')} |"
        )
    bitrix1 = run1.get("bitrix") or {}
    openai1 = run1.get("openai") or {}
    audio1 = run1.get("audio") or {}
    lines.extend(
        [
            "",
            "## D. Где реально возникает основная Bitrix нагрузка",
            "",
            f"Физический HTTP RUN 1: {bitrix1.get('physical_http')}; logical: {bitrix1.get('logical_commands')}; REST: {float(bitrix1.get('duration_ms') or 0)/1000:.1f} с.",
            "",
        ]
    )
    for item in (bitrix1.get("components") or [])[:8]:
        lines.append(
            f"- `{item.get('component')}`: HTTP {item.get('physical_http')}, "
            f"{float(item.get('duration_ms') or 0)/1000:.1f} с, rows {item.get('item_count')}"
        )
    per_deal = run1.get("per_deal") or {}
    stats = per_deal.get("http_stats") or {}
    lines.extend(
        [
            "",
            f"Analysis/per-deal path: сделок {per_deal.get('deal_count')}, "
            f"mean {stats.get('mean')} HTTP/deal, median {stats.get('median')}, "
            f"min {stats.get('min')}, max {stats.get('max')}.",
            f"Распределение: {json.dumps(per_deal.get('distribution') or {}, ensure_ascii=False)}",
            "",
            "Дорогие пустые методы:",
            "",
        ]
    )
    for item in run1.get("empty_expensive") or []:
        lines.append(
            f"- `{item.get('method')}`: HTTP {item.get('http')}, empty {item.get('empty_http')}, "
            f"{float(item.get('duration_ms') or 0)/1000:.1f} с, rows {item.get('item_count')}"
        )
    lines.extend(
        [
            "",
            "## E. Где реально возникает основная OpenAI нагрузка",
            "",
            f"Запросов: {openai1.get('requests')}; input {openai1.get('input_tokens')}; "
            f"cached {openai1.get('cached_input_tokens')}; output {openai1.get('output_tokens')}; "
            f"оценка {openai1.get('estimated_cost_rub')} ₽.",
            "",
        ]
    )
    for name, payload in (openai1.get("operations") or {}).items():
        lines.append(
            f"- `{name}`: req {payload.get('requests')}, in {payload.get('input_tokens')}, "
            f"out {payload.get('output_tokens')}, {payload.get('estimated_cost_rub')} ₽, "
            f"models {payload.get('models')}"
        )
    lines.extend(
        [
            "",
            "## F. Что происходит с audio",
            "",
            json.dumps(audio1, ensure_ascii=False, indent=2),
            "",
            "## G. Главный ответ: fetch до change detection?",
            "",
            f"По call chain это **B**: pipeline fetch+audio выполняется до `analyze_deal_if_changed`.",
            f"Метрики RUN 1: jobs={fetch.get('enqueued')}, fetched={fetch.get('fetched')}, "
            f"skip после fetch={fetch.get('skipped_after_fetch')}, вердикт={fetch.get('answer')}.",
            f"RUN 2 skip={((run2.get('analysis') or {}).get('skip'))}, "
            f"heavy_fetch_despite_no_change={((run2.get('analysis') or {}).get('heavy_fetch_despite_no_change'))}.",
            "",
            "## H. TOP-10 потенциально лишних операций (без изменений кода)",
            "",
        ]
    )
    for item in summary.get("top_waste") or []:
        lines.extend(
            [
                f"### `{item.get('operation')}`",
                f"- раз: {item.get('times')}; HTTP: {item.get('http')}; секунд: {item.get('seconds')}; записей: {item.get('rows')}; empty: {item.get('empty')}",
                f"- почему может быть лишней: {item.get('why_maybe_extra')}",
                f"- что нельзя сломать: {item.get('must_not_break')}",
                "",
            ]
        )
    lines.extend(
        [
            "## I. Предварительная архитектура Этапа 2 (рекомендация)",
            "",
            "CURRENT: каждые 30 мин A полный deal-control + B trajectory core + C supplementary full-for-affected + D presence + enqueue всех сделок (fetch+audio+LLM).",
            "PROPOSED: 30 мин A+B; C только при change + редкий recon; D реже; E audio по своему окну; F AI/CRM-context fetch только при сигнале change detection, а не до него.",
            "",
            "## J. Точные файлы/функции Этапа 2",
            "",
            "- `api/daytime_cycle.py` — `_analyze_work_pool`: не ставить job на каждую сделку без сигнала.",
            "- `run_rop_assistant.py` — порядок pipeline vs `analyze_*_if_changed`.",
            "- `bitrix/deals/1_fetch_deals_context.py` — `fetch_deal_bundle`, `fetch_timeline_comments` (`del created_after`), invoice/product_rows/stage_history.",
            "- `bitrix/deals/download_deals_call_audio.py` — discovery/recheck, не упрощать семантику.",
            "- `api/deal_control.py` — полный `crm.deal.list` без DATE_MODIFY.",
            "- `api/manager_trajectory.py` / `api/manager_trajectory_sources.py` — supplementary full-history и presence.",
            "- `openai_api/change_detection/decision_engine.py` + `analyze_deal_if_changed.py` — решение должно стоять до тяжёлого fetch.",
            "- `bitrix/customer_history.py` — incremental activities overlap.",
            "",
            "## K. Diagnostic artifacts",
            "",
            f"- каталог: `{summary.get('usage_dir')}`",
            f"- JSON: `{summary.get('summary_path')}`",
            f"- Markdown: `{summary.get('report_path')}`",
            "- `run1/` и `run2/`: Bitrix JSONL, OpenAI JSONL, spend diary.",
            "",
        ]
    )
    return "\n".join(lines) + "\n"


def run_one_full_cycle(
    *,
    db_path: str | Path,
    run_dir: Path,
    run_id: str,
    trigger: str,
    raw_dir: Path,
    audio_dir: Path,
    wait_job_fn: Callable[..., Any] | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    cycle_fn: Callable[..., dict[str, Any]] | None = None,
    poll_seconds: float = 2.0,
    max_wait_seconds: float | None = None,
) -> dict[str, Any]:
    from api.daytime_cycle import run_daytime_cycle
    from storage.rop_db import get_latest_automatic_analysis_run, list_automatic_analysis_items, list_deal_control_deals

    run_dir.mkdir(parents=True, exist_ok=True)
    openai_path = run_dir / "openai_usage.jsonl"
    openai_daily = run_dir / "openai_usage_daily"
    spend_dir = run_dir / "daily_spend"
    bitrix_dir = run_dir / "bitrix"
    bitrix_dir.mkdir(parents=True, exist_ok=True)
    openai_daily.mkdir(parents=True, exist_ok=True)
    spend_dir.mkdir(parents=True, exist_ok=True)
    env = {
        "BITRIX_USAGE_DAILY_DIR": str(bitrix_dir),
        "BITRIX_TRACE_RUN_ID": run_id,
        "BITRIX_TRACE_ALLOW_ENTITY_ID": "1",
        "BITRIX_DENY_WRITE_METHODS": "1",
        "OPENAI_USAGE_DAILY_DIR": str(openai_daily),
        "OPENAI_USAGE_TRACE_PATH": str(openai_path),
        "SPEND_DIARY_DIR": str(spend_dir),
    }
    started = datetime.now(MSK_TZ)
    _progress(f"{run_id}: старт полного automatic cycle.")
    with _temporary_env(env), bitrix_trace_context(run_id=run_id):
        runner = cycle_fn or run_daytime_cycle
        previous_run = get_latest_automatic_analysis_run(db_path) or {}
        previous_run_id = int(previous_run.get("id") or 0)
        cycle_payload = runner(db_path=db_path, trigger=trigger)
        automatic_run_id = int((cycle_payload or {}).get("automatic_analysis_run_id") or 0)
        if not automatic_run_id:
            latest = get_latest_automatic_analysis_run(db_path) or {}
            latest_id = int(latest.get("id") or 0)
            if latest_id > previous_run_id and str(latest.get("trigger") or "") == trigger:
                automatic_run_id = latest_id
        if not automatic_run_id:
            raise RuntimeError(
                f"Diagnostic cycle не создал свой automatic run (status={(cycle_payload or {}).get('status')}). "
                "Вероятно, другой цикл уже выполняется; повторите команду позже."
            )
        _progress(
            f"{run_id}: tick завершён status={cycle_payload.get('status')}, "
            f"automatic_run_id={automatic_run_id}, жду analysis jobs."
        )
        wait_payload = {"items": [], "job_ids": [], "run_status": cycle_payload.get("status")}
        if automatic_run_id:
            wait_payload = wait_until_automatic_run_done(
                db_path,
                automatic_run_id,
                wait_job_fn=wait_job_fn,
                sleep_fn=sleep_fn,
                poll_seconds=poll_seconds,
                max_wait_seconds=max_wait_seconds,
            )
        items = wait_payload.get("items") or list_automatic_analysis_items(db_path, automatic_run_id)
        deal_ids = [
            str(item.get("deal_id") or "").strip()
            for item in list_deal_control_deals(db_path, active_only=True)
            if str(item.get("deal_id") or "").strip()
        ]
        events = _collect_bitrix_events(bitrix_dir)
        usage = _usage_report(bitrix_dir, run_id)
        per_deal = _per_deal_bitrix(events)
        analysis = collect_analysis_metrics(items, per_deal_bitrix=per_deal)
        audio = collect_audio_metrics(deal_ids, raw_dir=raw_dir, audio_dir=audio_dir)
        openai = collect_openai_metrics(openai_path, spend_dir)
        empty_expensive = [
            _method_empty_cost(events, method)
            for method in (
                "crm.invoice.list",
                "crm.company.get",
                "crm.timeline.comment.list",
                "crm.stagehistory.list",
                "crm.deal.productrows.get",
                "crm.activity.list",
                "disk.file.get",
                "crm.item.list",
                "tasks.task.get",
            )
        ]
    finished = datetime.now(MSK_TZ)
    elapsed = (finished - started).total_seconds()
    _progress(f"{run_id}: полный цикл завершён за {elapsed:.1f} с.")
    return {
        "run_id": run_id,
        "automatic_run_id": automatic_run_id,
        "started_at": _iso(started),
        "finished_at": _iso(finished),
        "elapsed_seconds": round(elapsed, 3),
        "cycle": {
            "status": (cycle_payload or {}).get("status"),
            "trigger": trigger,
            "deal_count": len(deal_ids),
            "phase_seconds": (cycle_payload or {}).get("phase_seconds") or {},
        },
        "wait": {"job_ids": wait_payload.get("job_ids") or [], "run_status": wait_payload.get("run_status")},
        "bitrix": usage,
        "per_deal": per_deal,
        "analysis": analysis,
        "audio": audio,
        "openai": openai,
        "empty_expensive": empty_expensive,
        "top_waste": _top_waste(events, usage),
    }


def run_full_cycle_diagnostic(
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
    usage_dir: Path | None = None,
    raw_dir: Path | None = None,
    audio_dir: Path | None = None,
    skip_preflight: bool = False,
    wait_job_fn: Callable[..., Any] | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    cycle_fn: Callable[..., dict[str, Any]] | None = None,
    poll_seconds: float = 2.0,
    max_wait_seconds: float | None = None,
    single_cycle: bool = False,
    second_pass_done_only: bool = False,
    allow_running_api: bool = False,
) -> dict[str, Any]:
    isolated = Path(usage_dir) if usage_dir is not None else None
    previous = _load_previous_summary(isolated) if isolated is not None else None
    session_id = str((previous or {}).get("session_id") or "") or (
        datetime.now(MSK_TZ).strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    )
    if isolated is None:
        isolated = LOGS_DIR / "bitrix_diag" / session_id
    isolated.mkdir(parents=True, exist_ok=True)
    raw_path = Path(raw_dir or DEFAULT_RAW_DIR)
    audio_path = Path(audio_dir or DEFAULT_AUDIO_MANIFEST_DIR)
    if not skip_preflight:
        preflight = preflight_full_cycle(
            db_path=db_path,
            skip_git=allow_running_api,
            allow_running_api=allow_running_api,
        )
        if not preflight["ok"]:
            raise RuntimeError("Preflight не пройден: " + " ".join(preflight["problems"]))
    else:
        preflight = {"ok": True, "problems": [], "write_hits": []}
    pool_size = len(list_deal_control_deals(db_path, active_only=True))
    job_filter: dict[str, Any] = {
        "second_pass_done_only": bool(second_pass_done_only),
        "previous_done": 0,
        "jobs": pool_size,
    }
    effective_cycle = cycle_fn
    if second_pass_done_only:
        done_ids = previous_done_deal_ids(db_path)
        job_filter["previous_done"] = len(done_ids)
        job_filter["jobs"] = len(done_ids)
        if not done_ids:
            raise RuntimeError("Нет done-сделок в последнем automatic run для второго прохода.")
        if effective_cycle is None:
            effective_cycle = cycle_fn_for_second_pass(done_ids)
        _progress(
            f"Second-pass filter: jobs={len(done_ids)}, активный пул={pool_size}."
        )
    _progress(f"Full-cycle diagnostic {session_id}: активных сделок {pool_size}.")
    run_kwargs = {
        "db_path": db_path,
        "raw_dir": raw_path,
        "audio_dir": audio_path,
        "wait_job_fn": wait_job_fn,
        "sleep_fn": sleep_fn,
        "cycle_fn": effective_cycle,
        "poll_seconds": poll_seconds,
        "max_wait_seconds": max_wait_seconds,
    }
    run1: dict[str, Any]
    if single_cycle:
        run1 = (previous or {}).get("run1") or {}
        _progress("Один cycle: пропускаю RUN 1, запускаю RUN 2.")
    else:
        run1 = run_one_full_cycle(
            run_dir=isolated / "run1",
            run_id=f"full_{session_id}_run1",
            trigger="diagnostic_full_run1",
            **run_kwargs,
        )
        _progress("RUN 1 полностью завершён. Запускаю RUN 2 на свежем локальном состоянии.")
    run2 = run_one_full_cycle(
        run_dir=isolated / "run2",
        run_id=f"full_{session_id}_run2",
        trigger="diagnostic_full_run2",
        **run_kwargs,
    )
    comparison = compare_full_runs(run1, run2) if run1 else {}
    fetch1 = (run1.get("analysis") or {}).get("fetch_before_decision") or {}
    fetch2 = (run2.get("analysis") or {}).get("fetch_before_decision") or {}
    mode = "full-cycle-run2-only" if single_cycle else "full-cycle"
    executive = [
        f"Пул: {pool_size} активных сделок. "
        + (
            f"Только RUN 2, jobs={job_filter.get('jobs')} (done с RUN 1)."
            if single_cycle
            else "Два полных automatic cycle подряд."
        ),
        f"RUN 1 HTTP {((run1.get('bitrix') or {}).get('physical_http'))}, "
        f"OpenAI req {((run1.get('openai') or {}).get('requests'))}, "
        f"FULL {((run1.get('analysis') or {}).get('full'))}, "
        f"MINI {((run1.get('analysis') or {}).get('mini'))}, "
        f"SKIP {((run1.get('analysis') or {}).get('skip'))}, "
        f"wall {run1.get('elapsed_seconds')} с.",
        f"RUN 2 HTTP {((run2.get('bitrix') or {}).get('physical_http'))}, "
        f"OpenAI req {((run2.get('openai') or {}).get('requests'))}, "
        f"FULL {((run2.get('analysis') or {}).get('full'))}, "
        f"MINI {((run2.get('analysis') or {}).get('mini'))}, "
        f"SKIP {((run2.get('analysis') or {}).get('skip'))}, "
        f"wall {run2.get('elapsed_seconds')} с.",
        f"Change detection vs fetch: вердикт {fetch1.get('answer') or fetch2.get('answer')} "
        f"(jobs {fetch2.get('enqueued') or fetch1.get('enqueued')}, "
        f"fetch {fetch2.get('fetched') or fetch1.get('fetched')}, "
        f"skip после fetch {fetch2.get('skipped_after_fetch')}).",
        f"Per-deal HTTP RUN 2: mean {((run2.get('per_deal') or {}).get('http_stats') or {}).get('mean')}, "
        f"median {((run2.get('per_deal') or {}).get('http_stats') or {}).get('median')}.",
        "Этап 2 не внедрялся.",
    ]
    summary = {
        "mode": mode,
        "session_id": session_id,
        "usage_dir": str(isolated),
        "active_deal_count": pool_size,
        "job_filter": job_filter,
        "preflight": preflight or (previous or {}).get("preflight") or {"ok": True, "problems": [], "write_hits": []},
        "run1": run1,
        "run2": run2,
        "comparison": comparison,
        "top_waste": run2.get("top_waste") or run1.get("top_waste") or [],
        "executive_summary": executive,
        "call_chain_answer": fetch1.get("answer") or fetch2.get("answer") or "B",
    }
    summary_path = isolated / "summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    report_path = isolated / "summary.md"
    report_path.write_text(render_full_cycle_markdown({**summary, "summary_path": str(summary_path), "report_path": str(report_path)}), encoding="utf-8")
    summary["summary_path"] = str(summary_path)
    summary["report_path"] = str(report_path)
    return summary


def main() -> None:
    from dotenv import load_dotenv

    load_dotenv()
    args = parse_args()
    if args.one_automatic_cycle:
        if args.full_cycle or args.single_cycle or args.second_pass_done_only:
            raise SystemExit("--one-automatic-cycle используется отдельно, без full-cycle флагов.")
        summary = run_full_cycle_diagnostic(
            db_path=args.db_path,
            usage_dir=Path(args.usage_dir) if args.usage_dir else None,
            raw_dir=Path(args.raw_dir),
            audio_dir=Path(args.audio_dir),
            single_cycle=True,
            allow_running_api=True,
        )
        print(f"Диагностика завершена. Передайте файл: {summary.get('summary_path')}")
        return
    if args.single_cycle or args.second_pass_done_only:
        if not args.full_cycle:
            raise SystemExit("--single-cycle / --second-pass-done-only работают только вместе с --full-cycle.")
    if args.second_pass_done_only and not args.single_cycle:
        raise SystemExit("--second-pass-done-only нужен вместе с --single-cycle, иначе RUN 1 тоже сузится.")
    if args.full_cycle:
        summary = run_full_cycle_diagnostic(
            db_path=args.db_path,
            usage_dir=Path(args.usage_dir) if args.usage_dir else None,
            raw_dir=Path(args.raw_dir),
            audio_dir=Path(args.audio_dir),
            skip_preflight=args.skip_preflight,
            single_cycle=args.single_cycle,
            second_pass_done_only=args.second_pass_done_only,
        )
        print(f"Full-cycle diagnostic {summary.get('session_id')}")
        print("\n".join(summary.get("executive_summary") or []))
        print(f"Отчёт: {summary.get('report_path')}")
        return
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
