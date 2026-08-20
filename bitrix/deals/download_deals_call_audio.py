r"""
Download Bitrix CRM call audio for deals without duplicating existing files.

This script is read-only for Bitrix24. It reads local raw deal context, downloads
recordings referenced by CRM activity FILES through disk.file.get, and writes
local audio plus a manifest.

Default mode is missing-only: successful downloads already present in the
manifest and still existing on disk are not downloaded again.

```powershell
.\venv\Scripts\python.exe .\bitrix\deals\download_deals_call_audio.py --deal-ids 18507
```
"""

from __future__ import annotations

import argparse
import mimetypes
import re
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import requests
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bitrix.client import BitrixReadOnlyClient, get_env_required, load_json, save_json
from bitrix.customer_history import parse_bitrix_datetime
from openai_api.audio.short_call import enrich_download_with_duration, enrich_manifest_calls
from reliability.retry import DEFAULT_TRANSPORT_RETRY, run_with_retry
from progress_events import emit_progress, retry_progress_callback
from setup import BASE_DIR, MSK_TZ, get_logger


DEFAULT_DEAL_IDS = ["18507", "18493"]
DEFAULT_RAW_DIR = BASE_DIR / "reports" / "bitrix_customer_path" / "raw"
DEFAULT_AUDIO_DIR = BASE_DIR / "reports" / "bitrix_customer_path" / "audio"
AUDIO_FILE_DISCOVERY_WINDOW = timedelta(days=5)
EVENING_RECHECK_START = time(17, 30)
FIRST_MORNING_RECHECK_END = time(8, 30)
RECORDING_DURATION_RATIO = 0.80
RECORDING_DURATION_TOLERANCE_SECONDS = 5.0

logger = get_logger(__file__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Download Bitrix call audio from CRM activity FILES")
    parser.add_argument("--deal-ids", nargs="+", default=DEFAULT_DEAL_IDS, help="Deal IDs to process")
    parser.add_argument("--raw-dir", default=str(DEFAULT_RAW_DIR), help="Raw JSON dir")
    parser.add_argument("--audio-dir", default=str(DEFAULT_AUDIO_DIR), help="Local audio output dir")
    parser.add_argument(
        "--redownload",
        action="store_true",
        help="Download even if manifest already has an existing successful local file.",
    )
    return parser.parse_args()


def result_item(call_container: dict[str, Any] | None) -> dict[str, Any]:
    if not call_container or not call_container.get("ok"):
        return {}
    result = call_container.get("response", {}).get("result")
    return result if isinstance(result, dict) else {}


def is_call_activity(activity: dict[str, Any]) -> bool:
    provider = " ".join(str(activity.get(key) or "") for key in ("PROVIDER_ID", "PROVIDER_TYPE_ID", "SUBJECT")).upper()
    return str(activity.get("TYPE_ID") or "") == "2" or "CALL" in provider or "ИСХОДЯЩ" in provider


def detail_for(bundle: dict[str, Any], activity_id: str) -> dict[str, Any]:
    detail_container = bundle.get("activity_details", {}).get(activity_id, {})
    return result_item(detail_container) if isinstance(detail_container, dict) else {}


def call_activities_from_bundle(
    bundle: dict[str, Any],
    *,
    source: str,
    owner_type_id: str,
    owner_id: str,
) -> list[dict[str, Any]]:
    rows = []
    for activity in bundle.get("activities", {}).get("items", []):
        if not is_call_activity(activity):
            continue
        activity_id = str(activity.get("ID") or "")
        detail = detail_for(bundle, activity_id)
        row = {**activity, **detail} if detail else dict(activity)
        row["_source"] = source
        row["_owner_type_id"] = owner_type_id
        row["_owner_id"] = owner_id
        rows.append(row)
    return rows


def call_activities(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    rows = call_activities_from_bundle(
        bundle,
        source="deal",
        owner_type_id="2",
        owner_id=str(bundle.get("deal_id") or ""),
    )
    source_lead = bundle.get("source_lead") or {}
    source_lead_id = str(source_lead.get("lead_id") or "")
    if source_lead:
        rows.extend(
            call_activities_from_bundle(
                source_lead,
                source="source_lead",
                owner_type_id="1",
                owner_id=source_lead_id,
            )
        )
    return sorted(rows, key=lambda item: (item.get("START_TIME") or item.get("CREATED") or "", int(item.get("ID") or 0)))


def safe_filename(value: str) -> str:
    cleaned = re.sub(r"[^\wа-яА-ЯёЁ.-]+", "_", value, flags=re.U).strip("._")
    return cleaned or "call_audio"


def filename_from_response(response: requests.Response, fallback: str) -> str:
    disposition = response.headers.get("content-disposition", "")
    match = re.search(r"filename\*=UTF-8''([^;]+)", disposition, flags=re.I)
    if match:
        return safe_filename(unquote(match.group(1)))

    match = re.search(r'filename="?([^";]+)"?', disposition, flags=re.I)
    if match:
        return safe_filename(unquote(match.group(1)))

    path_name = Path(urlparse(response.url).path).name
    if path_name and "." in path_name:
        return safe_filename(unquote(path_name))

    content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
    extension = mimetypes.guess_extension(content_type) or ".bin"
    return safe_filename(f"{fallback}{extension}")


def file_download_url(client: BitrixReadOnlyClient, file_id: str) -> tuple[str | None, dict[str, Any] | None]:
    response = client.safe_call("disk.file.get", {"id": file_id})
    if not response.get("ok"):
        return None, response

    result = response.get("response", {}).get("result") or {}
    if not isinstance(result, dict):
        return None, response

    for key in ("DOWNLOAD_URL", "downloadUrl", "DOWNLOAD_LINK", "downloadLink"):
        value = result.get(key)
        if value:
            return str(value), response
    return None, response


def disk_file_size(response: dict[str, Any] | None) -> int | None:
    if not isinstance(response, dict) or not response.get("ok"):
        return None
    result = response.get("response", {}).get("result") or {}
    if not isinstance(result, dict):
        return None
    for key in ("SIZE", "FILE_SIZE", "size", "fileSize"):
        value = result.get(key)
        if value in (None, ""):
            continue
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            continue
        return parsed if parsed >= 0 else None
    return None


def activity_duration_seconds(activity: dict[str, Any]) -> float | None:
    started = parse_bitrix_datetime(activity.get("START_TIME") or activity.get("CREATED"))
    ended = parse_bitrix_datetime(activity.get("END_TIME"))
    if started is None or ended is None:
        return None
    duration = (ended - started).total_seconds()
    return round(duration, 1) if duration >= 0 else None


def previous_workday(day: date) -> date:
    candidate = day - timedelta(days=1)
    while candidate.weekday() > 4:
        candidate -= timedelta(days=1)
    return candidate


def should_recheck_recording(activity: dict[str, Any], *, now: datetime | None = None) -> bool:
    started = parse_bitrix_datetime(activity.get("START_TIME") or activity.get("CREATED"))
    if started is None:
        return False
    current = now or datetime.now(MSK_TZ)
    if current.tzinfo is None:
        current = current.replace(tzinfo=MSK_TZ)
    current = current.astimezone(MSK_TZ)
    started = started.astimezone(MSK_TZ)
    if started.date() == current.date():
        return True
    return (
        started.date() == previous_workday(current.date())
        and started.time() >= EVENING_RECHECK_START
        and current.time() <= FIRST_MORNING_RECHECK_END
    )


def recording_readiness(
    download: dict[str, Any],
    activity: dict[str, Any],
    *,
    previous: dict[str, Any] | None = None,
    remote_size_bytes: int | None = None,
    size_changed: bool = False,
) -> dict[str, Any]:
    row = dict(download)
    local_duration = row.get("duration_seconds")
    expected_duration = activity_duration_seconds(activity)
    previous_stable = int((previous or {}).get("recording_stable_observations") or 0)
    previous_remote = (previous or {}).get("remote_size_bytes")
    try:
        previous_remote_size = int(previous_remote) if previous_remote not in (None, "") else None
    except (TypeError, ValueError):
        previous_remote_size = None
    if remote_size_bytes is None:
        stable_observations = previous_stable
    elif size_changed or previous_remote_size != remote_size_bytes:
        stable_observations = 1
    else:
        stable_observations = previous_stable + 1
    row["remote_size_bytes"] = remote_size_bytes
    row["expected_call_duration_seconds"] = expected_duration
    row["recording_stable_observations"] = stable_observations

    materially_incomplete = False
    if local_duration is not None and expected_duration is not None and expected_duration > 0:
        tolerance = max(RECORDING_DURATION_TOLERANCE_SECONDS, expected_duration * (1.0 - RECORDING_DURATION_RATIO))
        materially_incomplete = float(local_duration) + tolerance < expected_duration

    if materially_incomplete:
        row["recording_ready_for_transcription"] = False
        row["recording_stability_status"] = "duration_incomplete"
    elif expected_duration is not None and local_duration is not None:
        row["recording_ready_for_transcription"] = True
        row["recording_stability_status"] = "duration_matches_activity"
    elif stable_observations >= 2:
        row["recording_ready_for_transcription"] = True
        row["recording_stability_status"] = "size_stable_twice"
    else:
        row["recording_ready_for_transcription"] = False
        row["recording_stability_status"] = "awaiting_second_size_observation"
    return row


def deterministic_output_path(output_dir: Path, response: requests.Response, fallback_name: str) -> Path:
    filename = filename_from_response(response, fallback_name)
    path = output_dir / filename
    if path.suffix:
        return path
    content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
    extension = mimetypes.guess_extension(content_type) or ".bin"
    return path.with_suffix(extension)


def try_download_url(
    url: str,
    output_dir: Path,
    fallback_name: str,
    retry_callback: Any = None,
    *,
    replace_existing: bool = False,
) -> dict[str, Any]:
    def download_once() -> dict[str, Any]:
        response = requests.get(url, stream=True, timeout=60, allow_redirects=True)
        try:
            content_type = response.headers.get("content-type", "")
            if response.status_code in {408, 409, 429} or response.status_code >= 500:
                response.raise_for_status()
            if response.status_code != 200:
                return {
                    "ok": False,
                    "status": "download_http_error",
                    "http_status": response.status_code,
                    "content_type": content_type,
                    "url": url,
                }

            chunks = response.iter_content(1024 * 256)
            first_chunk = next(chunks, b"")
            if b"<html" in first_chunk[:256].lower() or "text/html" in content_type.lower():
                return {
                    "ok": False,
                    "status": "download_returned_html_auth_required",
                    "http_status": response.status_code,
                    "content_type": content_type,
                    "url": url,
                }

            output_dir.mkdir(parents=True, exist_ok=True)
            output_path = deterministic_output_path(output_dir, response, fallback_name)
            if output_path.exists() and not replace_existing:
                return enrich_download_with_duration(
                    {
                        "ok": True,
                        "status": "already_downloaded",
                        "http_status": response.status_code,
                        "content_type": content_type,
                        "url": url,
                        "local_path": str(output_path),
                        "size_bytes": output_path.stat().st_size,
                    }
                )

            temporary_path = output_path.with_name(f"{output_path.name}.part")
            if temporary_path.exists():
                temporary_path.unlink()
            try:
                with temporary_path.open("wb") as file:
                    if first_chunk:
                        file.write(first_chunk)
                    for chunk in chunks:
                        if chunk:
                            file.write(chunk)
                if not temporary_path.exists() or temporary_path.stat().st_size == 0:
                    raise OSError("Downloaded audio file is empty")
                temporary_path.replace(output_path)
            except BaseException:
                if temporary_path.exists():
                    temporary_path.unlink()
                raise

            return enrich_download_with_duration(
                {
                    "ok": True,
                    "status": "redownloaded_grown_file" if replace_existing else "downloaded",
                    "http_status": response.status_code,
                    "content_type": content_type,
                    "url": url,
                    "local_path": str(output_path),
                    "size_bytes": output_path.stat().st_size,
                }
            )
        finally:
            response.close()

    return run_with_retry(
        download_once,
        operation_name="bitrix:audio_download",
        policy=DEFAULT_TRANSPORT_RETRY,
        on_event=retry_callback,
    )


def load_existing_manifest(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = load_json(path)
    except ValueError:
        logger.warning("Could not parse existing audio manifest: %s", path)
        return {}
    return value if isinstance(value, dict) else {}


def existing_downloads_by_activity(manifest: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    rows: dict[str, list[dict[str, Any]]] = {}
    for call in manifest.get("calls") or []:
        if not isinstance(call, dict):
            continue
        activity_id = str(call.get("activity_id") or "")
        if not activity_id:
            continue
        valid_downloads = []
        for item in call.get("downloads") or []:
            if not isinstance(item, dict) or not item.get("ok") or not item.get("local_path"):
                continue
            if Path(str(item["local_path"])).exists():
                valid_downloads.append(item)
        if valid_downloads:
            rows[activity_id] = valid_downloads
    return rows


def existing_transcriptions_by_activity(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Return successfully saved transcript bundles for purged source audio."""
    rows: dict[str, dict[str, Any]] = {}
    for call in manifest.get("calls") or []:
        if not isinstance(call, dict):
            continue
        activity_id = str(call.get("activity_id") or "")
        transcription = call.get("transcription")
        if not activity_id or not isinstance(transcription, dict):
            continue
        if transcription.get("status") != "transcribed_and_purged":
            continue
        transcript_path = transcription.get("transcript_json_path")
        if transcript_path and Path(str(transcript_path)).exists():
            rows[activity_id] = dict(transcription)
    return rows


def record_transcribed_and_purged(
    manifest_path: Path,
    audio_path: Path,
    activity_id: str,
    transcript_paths: dict[str, str],
) -> bool:
    """Persist that a manifest-managed audio file was deleted after transcription.

    The caller must delete ``audio_path`` first.  A transcript JSON bundle is
    required later to keep missing-only download from fetching the recording again.
    """
    manifest = load_existing_manifest(manifest_path)
    normalized_audio_path = audio_path.resolve()
    for call in manifest.get("calls") or []:
        if not isinstance(call, dict) or str(call.get("activity_id") or "") != str(activity_id):
            continue
        for download in call.get("downloads") or []:
            if not isinstance(download, dict) or not download.get("local_path"):
                continue
            try:
                is_source = Path(str(download["local_path"])).resolve() == normalized_audio_path
            except OSError:
                is_source = False
            if not is_source:
                continue
            download["status"] = "transcribed_and_purged"
            download["audio_purged"] = True
            call["status"] = "transcribed_and_purged"
            call["transcription"] = {
                "status": "transcribed_and_purged",
                "transcribed_at": datetime.now(MSK_TZ).isoformat(timespec="seconds"),
                "source_audio_path": str(audio_path),
                "source_file_id": str(download.get("file_id") or ""),
                "source_size_bytes": download.get("size_bytes"),
                "source_remote_size_bytes": download.get("remote_size_bytes") or download.get("size_bytes"),
                "source_duration_seconds": download.get("duration_seconds"),
                "transcript_txt_path": transcript_paths["txt_path"],
                "transcript_md_path": transcript_paths["md_path"],
                "transcript_json_path": transcript_paths["json_path"],
            }
            save_json(manifest_path, manifest)
            return True
    return False


def mark_existing_downloads(downloads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for item in downloads:
        row = dict(item)
        row["status"] = "already_downloaded"
        rows.append(row)
    return rows


def _existing_download_for_file(downloads: list[dict[str, Any]], file_id: str) -> dict[str, Any] | None:
    for item in downloads:
        if str(item.get("file_id") or "") == str(file_id):
            return item
    return downloads[0] if len(downloads) == 1 else None


def _transcribed_source_growth(
    client: BitrixReadOnlyClient,
    activity: dict[str, Any],
    transcription: dict[str, Any],
) -> tuple[bool, str | None, str | None, dict[str, Any] | None, int | None]:
    if not should_recheck_recording(activity):
        return False, None, None, None, None
    source_size = transcription.get("source_remote_size_bytes") or transcription.get("source_size_bytes")
    source_file_id = str(transcription.get("source_file_id") or "")
    if source_size in (None, "") or not source_file_id:
        return False, None, None, None, None
    try:
        previous_size = int(source_size)
    except (TypeError, ValueError):
        return False, None, None, None, None
    download_url, disk_response = file_download_url(client, source_file_id)
    remote_size = disk_file_size(disk_response)
    grew = remote_size is not None and remote_size > previous_size
    return grew, source_file_id, download_url, disk_response, remote_size


def _refresh_existing_downloads(
    client: BitrixReadOnlyClient,
    deal_audio_dir: Path,
    activity: dict[str, Any],
    existing_downloads: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], bool]:
    if not should_recheck_recording(activity):
        return mark_existing_downloads(existing_downloads), False

    refreshed: list[dict[str, Any]] = []
    any_growth = False
    files = [item for item in (activity.get("FILES") or []) if isinstance(item, dict)]
    file_ids = [str(item.get("id") or item.get("ID") or "") for item in files]
    if not any(file_ids):
        file_ids = [str(item.get("file_id") or "") for item in existing_downloads]

    for file_id in file_ids:
        if not file_id:
            continue
        previous = _existing_download_for_file(existing_downloads, file_id)
        if previous is None:
            continue
        download_url, disk_response = file_download_url(client, file_id)
        remote_size = disk_file_size(disk_response)
        local_path = Path(str(previous.get("local_path") or ""))
        local_size = local_path.stat().st_size if local_path.exists() else 0
        grew = remote_size is not None and remote_size > local_size
        if grew and download_url:
            try:
                result = try_download_url(
                    str(download_url),
                    deal_audio_dir,
                    f"activity_{activity.get('ID') or ''}_file_{file_id}",
                    client.retry_callback,
                    replace_existing=True,
                )
            except requests.RequestException as error:
                result = dict(previous)
                result["status"] = "refresh_download_request_error"
                result["refresh_error_type"] = type(error).__name__
            any_growth = any_growth or bool(
                result.get("ok") and result.get("status") == "redownloaded_grown_file"
            )
        else:
            result = dict(previous)
            result["status"] = "already_downloaded"
            result["size_bytes"] = local_size
        result["file_id"] = file_id
        result["source"] = "disk.file.get"
        result = recording_readiness(
            result,
            activity,
            previous=previous,
            remote_size_bytes=remote_size,
            size_changed=grew,
        )
        refreshed.append(result)

    return (refreshed or mark_existing_downloads(existing_downloads)), any_growth


def audio_file_discovery_expired(activity: dict[str, Any], *, now: datetime | None = None) -> bool:
    started_at = parse_bitrix_datetime(activity.get("START_TIME") or activity.get("CREATED"))
    if started_at is None:
        return False
    current = now or datetime.now(MSK_TZ)
    if current.tzinfo is None:
        current = current.replace(tzinfo=MSK_TZ)
    return current.astimezone(MSK_TZ) - started_at.astimezone(MSK_TZ) > AUDIO_FILE_DISCOVERY_WINDOW


def process_call(
    client: BitrixReadOnlyClient,
    deal_audio_dir: Path,
    activity: dict[str, Any],
    *,
    existing_downloads: list[dict[str, Any]] | None = None,
    existing_transcription: dict[str, Any] | None = None,
    missing_only: bool = True,
) -> dict[str, Any]:
    activity_id = str(activity.get("ID") or "")
    files = activity.get("FILES") or []
    row: dict[str, Any] = {
        "activity_id": activity_id,
        "source": activity.get("_source") or "deal",
        "source_label": (
            f"{activity.get('_source') or 'deal'}:{activity.get('_owner_id')}"
            if activity.get("_owner_id")
            else activity.get("_source") or "deal"
        ),
        "owner_type_id": activity.get("_owner_type_id"),
        "owner_id": activity.get("_owner_id"),
        "subject": activity.get("SUBJECT"),
        "start_time": activity.get("START_TIME") or activity.get("CREATED"),
        "end_time": activity.get("END_TIME"),
        "origin_id": activity.get("ORIGIN_ID"),
        "files": files,
        "downloads": [],
    }

    stale_transcription: dict[str, Any] | None = None
    forced_file: tuple[str, str, dict[str, Any] | None, int | None] | None = None
    if missing_only and existing_transcription:
        grew, file_id, download_url, disk_response, remote_size = _transcribed_source_growth(
            client,
            activity,
            existing_transcription,
        )
        if not grew or not file_id or not download_url:
            row["transcription"] = existing_transcription
            row["status"] = "transcribed_and_purged"
            return row
        stale_transcription = dict(existing_transcription)
        stale_transcription["status"] = "stale_source_grew"
        stale_transcription["replacement_remote_size_bytes"] = remote_size
        row["transcription"] = stale_transcription
        forced_file = (file_id, download_url, disk_response, remote_size)

    if missing_only and existing_downloads:
        row["downloads"], grew = _refresh_existing_downloads(client, deal_audio_dir, activity, existing_downloads)
        row["status"] = "redownloaded_grown_file" if grew else "already_downloaded"
        return row

    if not files and forced_file is None:
        if audio_file_discovery_expired(activity):
            row["status"] = "no_files_check_expired"
            row["audio_file_discovery_window_days"] = AUDIO_FILE_DISCOVERY_WINDOW.days
        else:
            row["status"] = "no_files_in_crm_activity"
        return row

    any_downloaded = False
    files_to_process = files
    if forced_file is not None:
        files_to_process = [{"id": forced_file[0]}]

    for file_info in files_to_process:
        if not isinstance(file_info, dict):
            row["downloads"].append({"ok": False, "status": "invalid_crm_activity_file"})
            continue
        file_id = str(file_info.get("id") or file_info.get("ID") or "")
        fallback_name = f"activity_{activity_id}_file_{file_id or 'unknown'}"

        if not file_id:
            row["downloads"].append(
                {
                    "ok": False,
                    "status": "missing_crm_activity_file_id",
                }
            )
            continue

        if forced_file is not None and file_id == forced_file[0]:
            download_url, disk_response, remote_size = forced_file[1], forced_file[2], forced_file[3]
        else:
            download_url, disk_response = file_download_url(client, file_id)
            remote_size = disk_file_size(disk_response)
        if not download_url:
            row["downloads"].append(
                {
                    "file_id": file_id,
                    "ok": False,
                    "status": "no_download_url",
                    "disk_file_get": disk_response,
                }
            )
            continue

        try:
            result = try_download_url(str(download_url), deal_audio_dir, fallback_name, client.retry_callback)
        except requests.RequestException as error:
            result = {"ok": False, "status": "download_request_error", "error": str(error), "url": download_url}

        result["file_id"] = file_id
        result["source"] = "disk.file.get"
        if result.get("ok"):
            result = recording_readiness(
                result,
                activity,
                remote_size_bytes=remote_size,
                size_changed=True,
            )

        any_downloaded = any_downloaded or bool(result.get("ok"))
        row["downloads"].append(result)

    if any_downloaded and all(item.get("status") == "already_downloaded" for item in row["downloads"] if item.get("ok")):
        row["status"] = "already_downloaded"
    elif any_downloaded:
        row["status"] = "recording_refreshed_transcript_stale" if stale_transcription else "downloaded"
    else:
        row["status"] = "not_downloaded"
    return row


def build_manifest(
    *,
    client: BitrixReadOnlyClient,
    deal_id: str,
    raw_path: Path,
    deal_audio_dir: Path,
    existing_manifest: dict[str, Any],
    missing_only: bool,
) -> dict[str, Any]:
    bundle = load_json(raw_path)
    calls = call_activities(bundle)
    existing_by_activity = existing_downloads_by_activity(existing_manifest)
    existing_transcriptions = existing_transcriptions_by_activity(existing_manifest)
    processed_calls = []
    for index, activity in enumerate(calls, start=1):
        emit_progress(
            "deal",
            deal_id,
            "audio_download",
            detail=f"Проверяет звонок {index} из {len(calls)}",
            current=index,
            total=len(calls),
        )
        processed_calls.append(
            process_call(
                client,
                deal_audio_dir,
                activity,
                existing_downloads=existing_by_activity.get(str(activity.get("ID") or "")),
                existing_transcription=existing_transcriptions.get(str(activity.get("ID") or "")),
                missing_only=missing_only,
            )
        )
    return {
        "deal_id": str(deal_id),
        "generated_at": datetime.now(MSK_TZ).isoformat(timespec="seconds"),
        "raw_path": str(raw_path),
        "audio_dir": str(deal_audio_dir),
        "missing_only": bool(missing_only),
        "calls": processed_calls,
    }


def summarize_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    statuses: dict[str, int] = {}
    download_statuses: dict[str, int] = {}
    for call in manifest.get("calls") or []:
        status = str(call.get("status") or "unknown")
        statuses[status] = statuses.get(status, 0) + 1
        for item in call.get("downloads") or []:
            item_status = str(item.get("status") or "unknown")
            download_statuses[item_status] = download_statuses.get(item_status, 0) + 1
    return {
        "calls": len(manifest.get("calls") or []),
        "statuses": statuses,
        "download_statuses": download_statuses,
    }


def main() -> None:
    args = parse_args()
    load_dotenv()

    webhook_url = get_env_required("BITRIX_WEBHOOK_URL")
    client = BitrixReadOnlyClient(webhook_url)
    raw_dir = Path(args.raw_dir)
    audio_dir = Path(args.audio_dir)
    audio_dir.mkdir(parents=True, exist_ok=True)
    missing_only = not args.redownload

    manifests = []
    for deal_id in args.deal_ids:
        client.retry_callback = retry_progress_callback(
            "deal", str(deal_id), "audio_download", detail="Запрос аудио к Bitrix"
        )
        raw_path = raw_dir / f"deal_{deal_id}_context.json"
        if not raw_path.exists():
            logger.warning("Raw bundle not found: %s", raw_path)
            continue

        manifest_path = audio_dir / f"deal_{deal_id}_call_audio_manifest.json"
        existing_manifest = load_existing_manifest(manifest_path)
        manifest = enrich_manifest_calls(
            build_manifest(
                client=client,
                deal_id=str(deal_id),
                raw_path=raw_path,
                deal_audio_dir=audio_dir / f"deal_{deal_id}",
                existing_manifest=existing_manifest,
                missing_only=missing_only,
            )
        )
        save_json(manifest_path, manifest)
        manifest["manifest_path"] = str(manifest_path)
        manifests.append(manifest)
        logger.info("Saved call audio manifest: %s", manifest_path)
        print(f"Deal {deal_id} audio summary: {summarize_manifest(manifest)}")

    save_json(audio_dir / "index.json", {"generated_at": datetime.now(MSK_TZ).isoformat(timespec="seconds"), "items": manifests})
    logger.info("Saved call audio index: %s", audio_dir / "index.json")


if __name__ == "__main__":
    main()
