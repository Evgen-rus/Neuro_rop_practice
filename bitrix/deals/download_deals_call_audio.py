r"""
Download Bitrix CRM call audio for deals without duplicating existing files.

This script is read-only for Bitrix24. It reads local raw deal context, downloads
recordings from CRM activity FILES / disk.file.get / voximplant.statistic.get,
and writes local audio plus a manifest.

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
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import requests
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bitrix.client import BitrixReadOnlyClient, get_env_required, load_json, save_json
from setup import BASE_DIR, MSK_TZ, get_logger


DEFAULT_DEAL_IDS = ["18507", "18493"]
DEFAULT_RAW_DIR = BASE_DIR / "reports" / "bitrix_customer_path" / "raw"
DEFAULT_AUDIO_DIR = BASE_DIR / "reports" / "bitrix_customer_path" / "audio"

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


def timeline_items(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for current in [bundle, bundle.get("source_lead") or {}]:
        for attempt in current.get("timeline_comments", []):
            if isinstance(attempt, dict):
                rows.extend(item for item in attempt.get("items", []) if isinstance(item, dict))
    return rows


def add_candidate(candidates: list[str], value: Any) -> None:
    if value in (None, "", [], {}):
        return
    text = str(value).strip()
    if text and text not in candidates:
        candidates.append(text)


def collect_values_by_key(value: Any, key_pattern: re.Pattern[str]) -> list[Any]:
    matches = []
    if isinstance(value, dict):
        for key, child in value.items():
            if key_pattern.search(str(key)):
                matches.append(child)
            matches.extend(collect_values_by_key(child, key_pattern))
    elif isinstance(value, list):
        for child in value:
            matches.extend(collect_values_by_key(child, key_pattern))
    return matches


def call_id_candidates(activity: dict[str, Any], timeline: list[dict[str, Any]]) -> list[str]:
    candidates: list[str] = []
    key_pattern = re.compile(r"(CALL|VOX|ORIGIN)", flags=re.I)

    for key in ("CALL_ID", "EXTERNAL_CALL_ID", "ORIGIN_ID", "ASSOCIATED_ENTITY_ID", "ID"):
        add_candidate(candidates, activity.get(key))

    origin_id = str(activity.get("ORIGIN_ID") or "")
    if origin_id.startswith("VI_externalCall."):
        stripped = origin_id.removeprefix("VI_externalCall.")
        add_candidate(candidates, stripped)
        for part in stripped.split("."):
            add_candidate(candidates, part)

    for item in timeline:
        for found in collect_values_by_key(item, key_pattern):
            add_candidate(candidates, found)
        text = " ".join(str(item.get(key) or "") for key in ("COMMENT", "TEXT", "DESCRIPTION"))
        for match in re.findall(r"(VI_[\w.-]+|VI_externalCall\.[\w.-]+|[a-f0-9]{24,}\.\d{8,})", text, flags=re.I):
            add_candidate(candidates, match)

    return candidates


def first_url_from_stat_row(row: dict[str, Any]) -> str | None:
    for key in ("RECORD_FILE_URL", "CALL_RECORD_URL", "RECORD_URL", "DOWNLOAD_URL", "CALL_RECORD", "RECORD_FILE"):
        value = row.get(key)
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            return value

    for value in row.values():
        if isinstance(value, dict):
            nested = first_url_from_stat_row(value)
            if nested:
                return nested
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    nested = first_url_from_stat_row(item)
                    if nested:
                        return nested
                elif isinstance(item, str) and item.startswith(("http://", "https://")):
                    return item
    return None


def voximplant_record_urls(
    client: BitrixReadOnlyClient,
    candidates: list[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    attempts = []
    urls = []
    seen_urls: set[str] = set()

    for candidate in candidates:
        response = client.safe_call("voximplant.statistic.get", {"FILTER": {"CALL_ID": candidate}})
        result = response.get("response", {}).get("result") if response.get("ok") else None
        rows = result if isinstance(result, list) else []
        attempts.append(
            {
                "filter": {"CALL_ID": candidate},
                "ok": response.get("ok"),
                "error": response.get("error"),
                "count": len(rows),
            }
        )

        for row in rows:
            if not isinstance(row, dict):
                continue
            record_url = first_url_from_stat_row(row)
            if record_url and record_url not in seen_urls:
                urls.append({"url": record_url, "call_id": candidate, "stat_row": row})
                seen_urls.add(record_url)

    return urls, attempts


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


def deterministic_output_path(output_dir: Path, response: requests.Response, fallback_name: str) -> Path:
    filename = filename_from_response(response, fallback_name)
    path = output_dir / filename
    if path.suffix:
        return path
    content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
    extension = mimetypes.guess_extension(content_type) or ".bin"
    return path.with_suffix(extension)


def try_download_url(url: str, output_dir: Path, fallback_name: str) -> dict[str, Any]:
    response = requests.get(url, stream=True, timeout=60, allow_redirects=True)
    content_type = response.headers.get("content-type", "")

    if response.status_code != 200:
        return {
            "ok": False,
            "status": "download_http_error",
            "http_status": response.status_code,
            "content_type": content_type,
            "url": url,
        }

    first_chunk = next(response.iter_content(8192), b"")
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

    if output_path.exists():
        return {
            "ok": True,
            "status": "already_downloaded",
            "http_status": response.status_code,
            "content_type": content_type,
            "url": url,
            "local_path": str(output_path),
            "size_bytes": output_path.stat().st_size,
        }

    with output_path.open("wb") as file:
        if first_chunk:
            file.write(first_chunk)
        for chunk in response.iter_content(1024 * 256):
            if chunk:
                file.write(chunk)

    return {
        "ok": True,
        "status": "downloaded",
        "http_status": response.status_code,
        "content_type": content_type,
        "url": url,
        "local_path": str(output_path),
        "size_bytes": output_path.stat().st_size,
    }


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


def mark_existing_downloads(downloads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for item in downloads:
        row = dict(item)
        row["status"] = "already_downloaded"
        rows.append(row)
    return rows


def process_call(
    client: BitrixReadOnlyClient,
    deal_audio_dir: Path,
    activity: dict[str, Any],
    timeline: list[dict[str, Any]],
    *,
    existing_downloads: list[dict[str, Any]] | None = None,
    missing_only: bool = True,
) -> dict[str, Any]:
    activity_id = str(activity.get("ID") or "")
    files = activity.get("FILES") or []
    candidates = call_id_candidates(activity, timeline)
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
        "origin_id": activity.get("ORIGIN_ID"),
        "call_id_candidates": candidates,
        "files": files,
        "downloads": [],
        "voximplant_attempts": [],
    }

    if missing_only and existing_downloads:
        row["downloads"] = mark_existing_downloads(existing_downloads)
        row["status"] = "already_downloaded"
        return row

    any_downloaded = False
    for file_info in files:
        file_id = str(file_info.get("id") or file_info.get("ID") or "")
        direct_url = file_info.get("url")
        fallback_name = f"activity_{activity_id}_file_{file_id or 'unknown'}"

        disk_url = None
        disk_response = None
        if file_id:
            disk_url, disk_response = file_download_url(client, file_id)

        download_source = "disk.file.get" if disk_url else "crm_activity_file_url"
        download_url = disk_url or direct_url
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
            result = try_download_url(str(download_url), deal_audio_dir, fallback_name)
        except requests.RequestException as error:
            result = {"ok": False, "status": "download_request_error", "error": str(error), "url": download_url}

        result["file_id"] = file_id
        result["source"] = download_source
        if disk_response is not None and not disk_response.get("ok"):
            result["disk_file_get_error"] = disk_response.get("error")

        any_downloaded = any_downloaded or bool(result.get("ok"))
        row["downloads"].append(result)

    record_urls, attempts = voximplant_record_urls(client, candidates)
    row["voximplant_attempts"] = attempts
    for index, record in enumerate(record_urls, start=1):
        fallback_name = f"activity_{activity_id}_voximplant_{index}"
        try:
            result = try_download_url(record["url"], deal_audio_dir, fallback_name)
        except requests.RequestException as error:
            result = {"ok": False, "status": "download_request_error", "error": str(error), "url": record["url"]}

        result["source"] = "voximplant.statistic.get"
        result["call_id"] = record.get("call_id")
        any_downloaded = any_downloaded or bool(result.get("ok"))
        row["downloads"].append(result)

    if any_downloaded and all(item.get("status") == "already_downloaded" for item in row["downloads"] if item.get("ok")):
        row["status"] = "already_downloaded"
    elif any_downloaded:
        row["status"] = "downloaded"
    elif not files and not record_urls:
        row["status"] = "no_files_in_crm_activity"
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
    timeline = timeline_items(bundle)
    existing_by_activity = existing_downloads_by_activity(existing_manifest)
    return {
        "deal_id": str(deal_id),
        "generated_at": datetime.now(MSK_TZ).isoformat(timespec="seconds"),
        "raw_path": str(raw_path),
        "audio_dir": str(deal_audio_dir),
        "missing_only": bool(missing_only),
        "calls": [
            process_call(
                client,
                deal_audio_dir,
                activity,
                timeline,
                existing_downloads=existing_by_activity.get(str(activity.get("ID") or "")),
                missing_only=missing_only,
            )
            for activity in calls
        ],
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
        raw_path = raw_dir / f"deal_{deal_id}_context.json"
        if not raw_path.exists():
            logger.warning("Raw bundle not found: %s", raw_path)
            continue

        manifest_path = audio_dir / f"deal_{deal_id}_call_audio_manifest.json"
        existing_manifest = load_existing_manifest(manifest_path)
        manifest = build_manifest(
            client=client,
            deal_id=str(deal_id),
            raw_path=raw_path,
            deal_audio_dir=audio_dir / f"deal_{deal_id}",
            existing_manifest=existing_manifest,
            missing_only=missing_only,
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
