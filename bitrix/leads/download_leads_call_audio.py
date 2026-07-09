r"""
Download Bitrix CRM call audio for leads without duplicating existing files.

This is an isolated lead-side audio downloader. It is not wired into the lead
pipeline yet, so it can be tested separately first.

```powershell
.\venv\Scripts\python.exe .\bitrix\leads\download_leads_call_audio.py --lead-ids 227661
```
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bitrix.client import BitrixReadOnlyClient, get_env_required, load_json, save_json
from bitrix.deals.download_deals_call_audio import (
    call_activities_from_bundle,
    existing_downloads_by_activity,
    load_existing_manifest,
    process_call,
    summarize_manifest,
)
from openai_api.audio.short_call import enrich_manifest_calls
from setup import BASE_DIR, MSK_TZ, get_logger


DEFAULT_RAW_DIR = BASE_DIR / "reports" / "bitrix_lead_path" / "raw"
DEFAULT_AUDIO_DIR = BASE_DIR / "reports" / "bitrix_lead_path" / "audio"

logger = get_logger(__file__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Download Bitrix call audio from lead CRM activity FILES")
    parser.add_argument("--lead-ids", nargs="+", required=True, help="Lead IDs to process")
    parser.add_argument("--raw-dir", default=str(DEFAULT_RAW_DIR), help="Raw JSON dir")
    parser.add_argument("--audio-dir", default=str(DEFAULT_AUDIO_DIR), help="Local audio output dir")
    parser.add_argument(
        "--redownload",
        action="store_true",
        help="Download even if manifest already has an existing successful local file.",
    )
    return parser.parse_args()


def owner_type_id(entity_type: str) -> str:
    return {"lead": "1", "deal": "2", "contact": "3"}.get(entity_type, "")


def entity_from_key(entity_key: str) -> tuple[str, str]:
    if ":" not in entity_key:
        return "", ""
    entity_type, entity_id = entity_key.split(":", 1)
    return entity_type.strip().lower(), entity_id.strip()


def call_activities_from_customer_history(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for entity_key, history in (bundle.get("activities_by_entity") or {}).items():
        if not isinstance(history, dict):
            continue
        entity_type, entity_id = entity_from_key(str(entity_key))
        if not entity_type or not entity_id:
            continue
        rows.extend(
            call_activities_from_bundle(
                history,
                source=entity_key,
                owner_type_id=owner_type_id(entity_type),
                owner_id=entity_id,
            )
        )
    return sorted(rows, key=lambda item: (item.get("START_TIME") or item.get("CREATED") or "", int(item.get("ID") or 0)))


def call_activities(bundle: dict[str, Any], lead_id: str) -> list[dict[str, Any]]:
    if bundle.get("bundle_type") == "customer_history_bundle":
        return call_activities_from_customer_history(bundle)
    return call_activities_from_bundle(
        bundle,
        source="lead",
        owner_type_id="1",
        owner_id=str(lead_id),
    )


def timeline_items_from_customer_history(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for attempts in (bundle.get("timeline_comments_by_entity") or {}).values():
        for attempt in attempts or []:
            if isinstance(attempt, dict):
                rows.extend(item for item in attempt.get("items", []) if isinstance(item, dict))
    return rows


def timeline_items(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    if bundle.get("bundle_type") == "customer_history_bundle":
        return timeline_items_from_customer_history(bundle)
    rows = []
    for attempt in bundle.get("timeline_comments", []):
        if isinstance(attempt, dict):
            rows.extend(item for item in attempt.get("items", []) if isinstance(item, dict))
    return rows


def context_path(raw_dir: Path, lead_id: str) -> Path:
    full = raw_dir / f"lead_{lead_id}_customer_history_bundle.json"
    if full.exists():
        return full
    return raw_dir / f"lead_{lead_id}_context.json"


def build_manifest(
    *,
    client: BitrixReadOnlyClient,
    lead_id: str,
    raw_path: Path,
    lead_audio_dir: Path,
    existing_manifest: dict[str, Any],
    missing_only: bool,
) -> dict[str, Any]:
    bundle = load_json(raw_path)
    calls = call_activities(bundle, lead_id)
    timeline = timeline_items(bundle)
    existing_by_activity = existing_downloads_by_activity(existing_manifest)
    return {
        "lead_id": str(lead_id),
        "generated_at": datetime.now(MSK_TZ).isoformat(timespec="seconds"),
        "raw_path": str(raw_path),
        "audio_dir": str(lead_audio_dir),
        "missing_only": bool(missing_only),
        "calls": [
            process_call(
                client,
                lead_audio_dir,
                activity,
                timeline,
                existing_downloads=existing_by_activity.get(str(activity.get("ID") or "")),
                missing_only=missing_only,
            )
            for activity in calls
        ],
    }


def main() -> None:
    args = parse_args()
    load_dotenv()

    client = BitrixReadOnlyClient(get_env_required("BITRIX_WEBHOOK_URL"))
    raw_dir = Path(args.raw_dir)
    audio_dir = Path(args.audio_dir)
    audio_dir.mkdir(parents=True, exist_ok=True)
    missing_only = not args.redownload

    manifests = []
    for lead_id in args.lead_ids:
        raw_path = context_path(raw_dir, str(lead_id))
        if not raw_path.exists():
            logger.warning("Raw bundle not found: %s", raw_path)
            continue

        manifest_path = audio_dir / f"lead_{lead_id}_call_audio_manifest.json"
        existing_manifest = load_existing_manifest(manifest_path)
        manifest = enrich_manifest_calls(
            build_manifest(
                client=client,
                lead_id=str(lead_id),
                raw_path=raw_path,
                lead_audio_dir=audio_dir / f"lead_{lead_id}",
                existing_manifest=existing_manifest,
                missing_only=missing_only,
            )
        )
        save_json(manifest_path, manifest)
        manifest["manifest_path"] = str(manifest_path)
        manifests.append(manifest)
        logger.info("Saved lead call audio manifest: %s", manifest_path)
        print(f"Lead {lead_id} audio summary: {summarize_manifest(manifest)}")

    save_json(audio_dir / "index.json", {"generated_at": datetime.now(MSK_TZ).isoformat(timespec="seconds"), "items": manifests})
    logger.info("Saved lead call audio index: %s", audio_dir / "index.json")


if __name__ == "__main__":
    main()
